"""Stín vstupu po poklesu (6. 11. 2026): po zavření trhu přehraje dnešní signály Scouta nad 5M svíčkami. NIC nenakupuje.

Rozbor 56 obchodů ukázal, že nákup těsně pod denním maximem prodělává, ale jestli vydělá nákup po poklesu, je nejisté
(první půlka období +0,51 R, druhá -0,09 R). Pravidlo bylo vybrané podle starých dat, proto se počítá až od FORWARD_FROM.

Bere jen signály, které Scout už má (žádné volání Gemini navíc): KÚPIŤ nebo POČKAŤ s katalyzátorem, první na ticker a den.
Varianty se stejným výstupem (stop podle ATR, cíl 2R, jinak prodej na konci seance, bez posunu stopu na vstupní cenu):
  HIGH    nákup hned po signálu KÚPIŤ, srovnání s dnešním chováním bota
  PB      nákup po poklesu 2 až 5,5 % od denního maxima
  PB_TIME totéž, ale jen 30 až 180 minut po otevření
Výsledky jdou do paper_pullback.json (oddělené od ostatních evidencí).
"""

import json
import logging
import threading
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd

import bars
import market_hours
from config import BASE_DIR

logger = logging.getLogger("TradingBot.Pullback")

SIGNALS_FILE = BASE_DIR / "signals.jsonl"
SHADOW_FILE = BASE_DIR / "paper_pullback.json"
FORWARD_FROM = "2026-11-09"     # první obchodní den po zavedení; starší signály pravidlo pomáhaly vybrat
BAND_MIN_PCT = 2.0
BAND_MAX_PCT = 5.5
WINDOW_MIN = (30, 180)
STOP_MIN_PCT = 2.2
STOP_ATR_SHARE = 0.7            # stop = větší z (2,2 %, 0,7 x ATR14 v %)
TARGET_R = 2.0
ROUND_TRIP_COST_PCT = 0.4       # 0,2 % na stranu jako v nákladovém modelu bota
AFTER_CLOSE_MIN = 20
GIVE_UP_DAYS = 3
BAR_MIN = 5
VARIANTS = ("HIGH", "PB", "PB_TIME")
_file_lock = threading.Lock()


def load_results() -> Dict[str, Dict[str, Any]]:
    try:
        return json.loads(SHADOW_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.error(f"Chyba při čtení {SHADOW_FILE}: {e}")
        return {}


def save_results(data: Dict[str, Dict[str, Any]]) -> None:
    with _file_lock:
        tmp = SHADOW_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(SHADOW_FILE)


def has_catalyst(sig: Dict[str, Any]) -> bool:
    if sig.get("verdict") == "KÚPIŤ":
        return True
    return sig.get("verdict") == "POČKAŤ" and not str(sig.get("news") or "").startswith("Žádný jasný")


def load_signals(from_day: str = FORWARD_FROM) -> List[Dict[str, Any]]:
    """První signál s katalyzátorem na ticker a den. Den se bere v čase burzy, ne UTC."""
    out: Dict[str, Dict[str, Any]] = {}
    try:
        lines = SIGNALS_FILE.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    for line in lines:
        try:
            s = json.loads(line)
        except ValueError:
            continue
        if s.get("strategy") != "SCOUT" or not has_catalyst(s):
            continue
        market = "EU" if s.get("market") == "STOCK_EU" else "US"
        ts = datetime.fromisoformat(s["ts_utc"]).astimezone(market_hours.MARKETS[market]["tz"])
        day = ts.date().isoformat()
        if day < from_day:
            continue
        out.setdefault(f"{day}|{s['ticker']}", dict(s, day=day, session=market, ts=ts))
    return list(out.values())


def stop_pct(atr_pct: float) -> float:
    return max(STOP_MIN_PCT, STOP_ATR_SHARE * atr_pct)


def atr_pct_before(daily: pd.DataFrame, day: str) -> Optional[float]:
    """ATR14 v % poslední známé ceny, jen ze svíček před daným dnem."""
    d = daily[[i.date().isoformat() < day for i in daily.index]]
    if len(d) < 15:
        return None
    prev = d["Close"].shift()
    tr = pd.concat([d["High"] - d["Low"], (d["High"] - prev).abs(), (d["Low"] - prev).abs()], axis=1).max(axis=1)
    return float(tr.rolling(14).mean().iloc[-1] / d["Close"].iloc[-1] * 100)


def _outcome(after: pd.DataFrame, entry: float, st_pct: float, opened_at, minutes: float) -> Dict[str, Any]:
    """Stop má při sporu ve stejné svíčce přednost. Bez stopu a cíle se prodává za poslední cenu seance."""
    risk = entry * st_pct / 100
    stop, target = entry - risk, entry + TARGET_R * risk
    exit_px, reason = float(after["Close"].iloc[-1]), "KONEC SEANCE"
    for _, b in after.iterrows():
        if b["Low"] <= stop:
            exit_px, reason = stop, "STOP"
            break
        if b["High"] >= target:
            exit_px, reason = target, "CIL"
            break
    r = (exit_px - entry) / risk
    return {"status": "DONE", "entry": round(entry, 4), "entry_at": opened_at.isoformat(), "min_since_open": round(minutes),
            "stop_pct": round(st_pct, 2), "exit": round(exit_px, 4), "reason": reason, "r": round(r, 3),
            "r_net": round(r - ROUND_TRIP_COST_PCT / st_pct, 3)}


def replay(day_bars: pd.DataFrame, sig_ts: datetime, variant: str, st_pct: float) -> Dict[str, Any]:
    """Přehraje jednu variantu nad 5M svíčkami seance. Svíčka je označená svým začátkem.

    O vstupu rozhoduje závěr svíčky, nakupuje se na open další, takže se nedívá do budoucna.
    """
    open_at = day_bars.index[0]
    high = -1.0
    for i in range(len(day_bars) - 1):
        bar = day_bars.iloc[i]
        high = max(high, float(bar["High"]))
        bar_end = day_bars.index[i] + timedelta(minutes=BAR_MIN)
        if bar_end < sig_ts:
            continue
        if variant != "HIGH":
            drop = (1 - float(bar["Close"]) / high) * 100
            if not BAND_MIN_PCT <= drop <= BAND_MAX_PCT:
                continue
        at = day_bars.index[i + 1]
        minutes = (at - open_at).total_seconds() / 60
        if variant == "PB_TIME" and minutes > WINDOW_MIN[1]:
            break
        if variant == "PB_TIME" and minutes < WINDOW_MIN[0]:
            continue
        return _outcome(day_bars.iloc[i + 1:], float(day_bars.iloc[i + 1]["Open"]), st_pct, at, minutes)
    return {"status": "NO_ENTRY"}


def evaluate_signal(sig: Dict[str, Any], m5: pd.DataFrame, daily: pd.DataFrame) -> Dict[str, Any]:
    day = sig["day"]
    day_bars = m5[[i.date().isoformat() == day for i in m5.index]]
    atr = atr_pct_before(daily, day)
    rec: Dict[str, Any] = {"ticker": sig["ticker"], "day": day, "market": sig["session"], "verdict": sig.get("verdict"),
                           "signal_at": sig["ts"].isoformat(), "atr_pct": None if atr is None else round(atr, 2), "variants": {}}
    if day_bars.empty or atr is None:
        rec["status"] = "NO_DATA"
        return rec
    rec["status"] = "DONE"
    st = stop_pct(atr)
    for v in VARIANTS:
        if v == "HIGH" and sig.get("verdict") != "KÚPIŤ":
            continue
        rec["variants"][v] = replay(day_bars, sig["ts"], v, st)
    return rec


def _session_finished(sig: Dict[str, Any], now: datetime) -> bool:
    tz = market_hours.MARKETS[sig["session"]]["tz"]
    local = now.astimezone(tz)
    if sig["day"] < local.date().isoformat():
        return True
    bounds = market_hours.session_bounds(sig["session"], local)
    return bool(bounds) and local >= bounds[1] + timedelta(minutes=AFTER_CLOSE_MIN)


def run_pass(now: Optional[datetime] = None) -> List[str]:
    """Vyhodnotí signály z uzavřených seancí, které ještě nemají záznam. Vrací nově vyhodnocené klíče."""
    import yfinance as yf
    now = now or datetime.now(market_hours.MARKETS["US"]["tz"])
    data = load_results()
    new: List[str] = []
    for sig in load_signals():
        key = f"{sig['day']}|{sig['ticker']}"
        if key in data or not _session_finished(sig, now):
            continue
        tz = market_hours.MARKETS[sig["session"]]["tz"]
        try:
            t = yf.Ticker(sig["ticker"])
            m5 = bars.fetch_bars(sig["ticker"], "5m", "1mo", closed_only=True, yf_ticker=t, now=now).tz_convert(tz)
            daily = bars.fetch_bars(sig["ticker"], "1d", "3mo", closed_only=False, yf_ticker=t, now=now).tz_convert(tz)
            rec = evaluate_signal(sig, m5, daily)
        except Exception as e:
            logger.warning(f"Stín poklesu {key}: data nejdou stáhnout: {e}")
            age = (now.astimezone(tz).date() - date.fromisoformat(sig["day"])).days
            if age < GIVE_UP_DAYS:
                continue
            rec = {"ticker": sig["ticker"], "day": sig["day"], "market": sig["session"], "status": "NO_DATA", "variants": {}}
        data[key] = rec
        new.append(key)
    if new:
        save_results(data)
    return new


def summary_text(data: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
    data = load_results() if data is None else data
    lines = [f"🪜 **Stín vstupu po poklesu** (od {FORWARD_FROM}, jen papír, R po nákladech):"]
    names = {"HIGH": "Nákup hned po signálu", "PB": "Po poklesu 2 až 5,5 %", "PB_TIME": "Po poklesu, 30 až 180 min"}
    for v in VARIANTS:
        rows = [x["variants"][v] for x in data.values() if v in x.get("variants", {})]
        done = [x["r_net"] for x in rows if x.get("status") == "DONE"]
        if not done:
            lines.append(f"{names[v]}: zatím žádný obchod ({len(rows)} signálů)")
            continue
        wins = sum(1 for r in done if r > 0)
        lines.append(f"{names[v]}: {len(done)} obchodů z {len(rows)} signálů, průměr {sum(done) / len(done):+.2f} R, "
                     f"výher {wins / len(done) * 100:.0f} %, součet {sum(done):+.1f} R")
    return "\n".join(lines)


if __name__ == "__main__":
    print(summary_text().replace("**", ""))
