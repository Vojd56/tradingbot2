"""Strategie ORB15 pro US akcie (M4, zatím jen stínový režim, žádné příkazy).

Pravidla jsou zmrazená podle protokolu z rešerše (opening range breakout, 5M svíčky, čas New York):
1. Pásmo = první 15 minut seance (svíčky 09:30, 09:35, 09:40). Den se vynechá, když
   - RVOL15 (objem pásma proti stejnému oknu posledních až 14 dní) < 1,5,
   - pásmo je širší než 1,5 x ATR(14) z denních svíček,
   - prvních 15 minut není růstových (close 09:40 <= open 09:30).
2. Vstup long: uzavřená 5M svíčka zavře nad ORB high i nad VWAP, svíčka končí 09:50-14:30
   (u zkrácené seance o 3,5 h dřív než zavření). Plnění = open další svíčky. Max. 1 obchod na ticker a den.
3. Stop = max(ORB low, entry - 0,25 x ATR, VWAP při signálu). Náklady > 15 % R se jen poznačí (cost_ok),
   ať stín ukáže, kolik obchodů by brána nákladů vzala.
4. Správa (varianta A): +1R -> stop na vstup + náklady, +2R -> půlka pryč, zbytek do zavření 5M svíčky
   pod VWAP nebo do 15:50. Varianta B (jako původní studie): jen počáteční stop a výstup v 15:50.
V jedné svíčce se nejdřív počítá se stopem (pesimisticky), pak s cíli.

Modul je čistý (bez sítě): data dodává orb_shadow.py, testy syntetické svíčky.
"""

from dataclasses import dataclass, asdict
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd

from cost_model import VenueCost
from config import MAX_COST_SHARE_OF_R

ORB_BARS = 3                    # 3 x 5 min = 15 min
RVOL_MIN = 1.5
RVOL_MIN_DAYS = 5               # méně historie = RVOL nespolehlivý, den se vynechá
RVOL_LOOKBACK_DAYS = 14
MAX_RANGE_ATR = 1.5
ATR_PERIOD = 14
STOP_ATR_FRAC = 0.25
ENTRY_FROM = dtime(9, 50)       # nejdřív svíčka, která končí 09:50
ENTRY_TO = dtime(14, 30)
FLAT_AT = dtime(15, 50)
BAR = timedelta(minutes=5)


@dataclass
class OrbSetup:
    ticker: str
    day: str
    orb_high: float = 0.0
    orb_low: float = 0.0
    atr14: float = 0.0
    rvol15: float = 0.0
    first15_bullish: bool = False
    skip_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _day_bars(bars: pd.DataFrame, day: date) -> pd.DataFrame:
    return bars[[ts.date() == day for ts in bars.index]]


def atr(daily: pd.DataFrame, period: int = ATR_PERIOD) -> Optional[float]:
    """Průměr true range za posledních `period` dní (jen dny před dneškem dodá volající)."""
    if daily is None or len(daily) < period + 1:
        return None
    h, l, c = daily["High"].astype(float), daily["Low"].astype(float), daily["Close"].astype(float)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1).iloc[1:]
    return float(tr.iloc[-period:].mean())


def build_setup(ticker: str, bars_5m: pd.DataFrame, daily: pd.DataFrame, day: date) -> OrbSetup:
    """Ranní pásmo dne `day`. bars_5m = 5M svíčky v čase New York za ~3 týdny vč. dneška, daily = denní svíčky."""
    s = OrbSetup(ticker=ticker.upper(), day=day.isoformat())
    today = _day_bars(bars_5m, day)
    orb = today[[dtime(9, 30) <= ts.time() < dtime(9, 45) for ts in today.index]]
    if len(orb) < ORB_BARS:
        s.skip_reason = "chybí svíčky pásma 09:30-09:45"
        return s
    s.orb_high = float(orb["High"].max())
    s.orb_low = float(orb["Low"].min())
    s.first15_bullish = float(orb["Close"].iloc[-1]) > float(orb["Open"].iloc[0])

    prev_daily = daily[[ts.date() < day for ts in daily.index]]
    a = atr(prev_daily)
    if not a:
        s.skip_reason = "málo denních svíček pro ATR"
        return s
    s.atr14 = a

    vols = []
    for d in sorted({ts.date() for ts in bars_5m.index if ts.date() < day})[-RVOL_LOOKBACK_DAYS:]:
        b = _day_bars(bars_5m, d)
        w = b[[dtime(9, 30) <= ts.time() < dtime(9, 45) for ts in b.index]]
        if len(w) == ORB_BARS:
            vols.append(float(w["Volume"].sum()))
    if len(vols) < RVOL_MIN_DAYS or sum(vols) <= 0:
        s.skip_reason = "málo historie pro RVOL15"
        return s
    s.rvol15 = float(orb["Volume"].sum()) / (sum(vols) / len(vols))

    if s.rvol15 < RVOL_MIN:
        s.skip_reason = f"RVOL15 {s.rvol15:.2f} < {RVOL_MIN}"
    elif s.orb_high - s.orb_low > MAX_RANGE_ATR * a:
        s.skip_reason = f"pásmo {(s.orb_high - s.orb_low) / a:.2f} x ATR > {MAX_RANGE_ATR}"
    elif not s.first15_bullish:
        s.skip_reason = "prvních 15 min není růstových"
    return s


def vwap_series(today: pd.DataFrame) -> pd.Series:
    """VWAP ke konci každé svíčky dne (typická cena x objem)."""
    tp = (today["High"].astype(float) + today["Low"].astype(float) + today["Close"].astype(float)) / 3
    vol = today["Volume"].astype(float)
    cum_v = vol.cumsum().replace(0, float("nan"))
    return ((tp * vol).cumsum() / cum_v).ffill().fillna(tp)


def entry_signal(s: OrbSetup, bar_start: datetime, close: float, vwap: float, entry_to: dtime = ENTRY_TO) -> bool:
    """Uzavřená svíčka zavřela nad ORB high i nad VWAP v povoleném okně. Knot nad pásmem nestačí."""
    if s.skip_reason:
        return False
    end = (bar_start + BAR).time()
    return ENTRY_FROM <= end <= entry_to and close > s.orb_high and close > vwap


def cost_share_of_r(entry: float, stop: float, cost: VenueCost) -> float:
    risk = entry - stop
    if risk <= 0:
        return float("inf")
    return (entry * cost.side_rate() * 2 + entry * cost.buy_tax_pct / 100.0) / risk


def initial_stop(s: OrbSetup, entry: float, vwap: float) -> Optional[float]:
    """max(ORB low, entry - 0,25 ATR, VWAP); None, když by stop nebyl pod vstupem."""
    stop = max(s.orb_low, entry - STOP_ATR_FRAC * s.atr14, vwap)
    return stop if stop < entry else None


def entry_deadline(close_ny: Optional[dtime]) -> dtime:
    """Konec vstupů: 14:30, u zkrácené seance (zavření 13:00) o stejný odstup od zavření dřív."""
    if close_ny and close_ny < dtime(16, 0):
        d = datetime.combine(date.today(), close_ny) - timedelta(hours=1, minutes=30)
        return min(ENTRY_TO, d.time())
    return ENTRY_TO


def simulate_day(s: OrbSetup, bars_5m: pd.DataFrame, cost: VenueCost, now: Optional[datetime] = None,
                 close_ny: Optional[dtime] = None) -> Dict[str, Any]:
    """Přehraje den nad UZAVŘENÝMI 5M svíčkami a vrátí stav stínového obchodu.

    Výsledek: {"status": NO_SETUP|WAITING|PENDING|OPEN|CLOSED|NO_TRADE, ...}. Je idempotentní: každý průchod
    smyčky přehraje den znovu ze stejných dat, takže nevzniká stav, který by se mohl rozejít s trhem.
    """
    day = date.fromisoformat(s.day)
    today = _day_bars(bars_5m, day)
    if now is not None:
        today = today[[ts + BAR <= now for ts in today.index]]
    out: Dict[str, Any] = {"ticker": s.ticker, "day": s.day, "setup": s.to_dict()}
    if s.skip_reason:
        return dict(out, status="NO_SETUP", reason=s.skip_reason)
    if today.empty:
        return dict(out, status="WAITING")
    vw = vwap_series(today)
    out["two_sided"] = bool((today["High"] > s.orb_high).any() and (today["Low"] < s.orb_low).any())
    entry_to = entry_deadline(close_ny)
    flat_at = min(FLAT_AT, (datetime.combine(day, close_ny) - timedelta(minutes=10)).time()) if close_ny else FLAT_AT

    idx = list(today.index)
    sig_i = next((i for i, ts in enumerate(idx)
                  if entry_signal(s, ts, float(today["Close"].iloc[i]), float(vw.iloc[i]), entry_to)), None)
    if sig_i is None:
        last_end = (idx[-1] + BAR).time()
        return dict(out, status="NO_TRADE" if last_end >= entry_to else "WAITING")
    if sig_i + 1 >= len(idx):
        return dict(out, status="PENDING", signal_at=idx[sig_i].isoformat())

    entry_ts = idx[sig_i + 1]
    entry = float(today["Open"].iloc[sig_i + 1])
    stop0 = initial_stop(s, entry, float(vw.iloc[sig_i]))
    out.update(signal_at=idx[sig_i].isoformat(), entry_at=entry_ts.isoformat(), entry=entry)
    if stop0 is None:
        return dict(out, status="NO_TRADE", reason="stop by nebyl pod vstupem (otevření pod VWAP / ORB low)")
    risk = entry - stop0
    share = cost_share_of_r(entry, stop0, cost)
    out.update(stop=stop0, risk=risk, cost_share_r=round(share, 3), cost_ok=share <= MAX_COST_SHARE_OF_R,
               cost_pct_per_side=cost.pct_per_side)

    rate = cost.side_rate()
    be_stop = entry * (1 + 2 * rate + cost.buy_tax_pct / 100.0)
    a = {"stop": stop0, "qty": 1.0, "fills": [], "be": False, "half": False}
    b = {"stop": stop0, "qty": 1.0, "fills": []}
    for j in range(sig_i + 1, len(idx)):
        ts = idx[j]
        o, h, l, c = (float(today[k].iloc[j]) for k in ("Open", "High", "Low", "Close"))
        end_t = (ts + BAR).time()
        for v in (a, b):
            if v["qty"] > 0 and l <= v["stop"]:
                px = min(o, v["stop"])     # gap pod stop = plnění za open
                v["fills"].append((v["qty"], px, ts.isoformat(), "stop"))
                v["qty"] = 0.0
        if a["qty"] > 0:
            if not a["be"] and h >= entry + risk:
                a["be"], a["stop"] = True, max(a["stop"], be_stop)
            if not a["half"] and h >= entry + 2 * risk:
                a["half"] = True
                a["fills"].append((0.5, entry + 2 * risk, ts.isoformat(), "+2R půlka"))
                a["qty"] = 0.5
            if a["qty"] > 0 and c < float(vw.iloc[j]):
                a["fills"].append((a["qty"], c, ts.isoformat(), "close pod VWAP"))
                a["qty"] = 0.0
        if end_t >= flat_at:
            for v in (a, b):
                if v["qty"] > 0:
                    v["fills"].append((v["qty"], c, ts.isoformat(), "konec dne"))
                    v["qty"] = 0.0
        if a["qty"] == 0 and b["qty"] == 0:
            break

    def result(v):
        exit_value = sum(q * px for q, px, _, _ in v["fills"])
        closed_qty = sum(q for q, _, _, _ in v["fills"])
        costs = entry * closed_qty * (rate + cost.buy_tax_pct / 100.0) + exit_value * rate
        pnl = exit_value - entry * closed_qty - costs
        return {"open_qty": v["qty"], "fills": v["fills"], "r_net": round(pnl / risk, 3) if v["qty"] == 0 else None}

    out["variant_a"], out["variant_b"] = result(a), result(b)
    out["status"] = "CLOSED" if a["qty"] == 0 and b["qty"] == 0 else "OPEN"
    return out


def summarize(trades: List[Dict[str, Any]], variant: str = "variant_a", only_cost_ok: bool = False,
              only_catalyst: bool = False) -> Dict[str, Any]:
    """Souhrn uzavřených stínových obchodů pro bránu M4: počet, čistý výsledek v R, Profit Factor, max. propad (R)."""
    rs = []
    for t in sorted(trades, key=lambda x: (x.get("day", ""), x.get("entry_at", ""))):
        if t.get("status") != "CLOSED":
            continue
        if only_cost_ok and not t.get("cost_ok"):
            continue
        if only_catalyst and (t.get("catalyst") or {}).get("direction") != "YES":
            continue
        r = (t.get(variant) or {}).get("r_net")
        if r is not None:
            rs.append(r)
    wins, losses = sum(r for r in rs if r > 0), -sum(r for r in rs if r < 0)
    peak = eq = dd = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    days = {t.get("day") for t in trades if t.get("status") in ("CLOSED", "OPEN", "NO_TRADE")}
    two = {t.get("day") for t in trades if t.get("two_sided")}
    return {"n": len(rs), "net_r": round(sum(rs), 2), "pf": round(wins / losses, 2) if losses else None,
            "win_rate": round(sum(1 for r in rs if r > 0) / len(rs), 2) if rs else None,
            "max_dd_r": round(dd, 2), "two_sided_share": round(len(two & days) / len(days), 2) if days else None}
