"""Vícedenní držení akcií (swing F0): čistá matematika bez sítě a bez Gemini.

Indikátory z denních svíček, filtr vesmíru, skóre kandidáta, plán obchodu (SL, cíl, RRR po nákladech),
velikost pozice, limity portfolia a denní správa pozice (break-even, polovina na +2R, chandelier, časový stop).
Všechno počítá kód; Gemini v swing_analyst.py jen vybírá a hlasuje.
"""

import math
from typing import Any, Dict, List, Optional

import pandas as pd

from config import (
    SWING_MIN_PRICE, SWING_MIN_DOLLAR_VOLUME, SWING_ATR_STOP_MULT, SWING_MIN_NET_RR,
    SWING_RISK_PCT, SWING_MAX_ALLOC_PCT, SWING_MAX_POSITIONS, SWING_MAX_PER_SECTOR, SWING_MAX_OPEN_RISK_PCT,
    SWING_SECTOR_LIMIT_SCOPE,
    SWING_BE_AT_R, SWING_PARTIAL_AT_R, SWING_CHANDELIER_DAYS, SWING_CHANDELIER_ATR,
    SWING_TIME_STOP_DAYS, SWING_TIME_STOP_MIN_R, SWING_TIGHTEN_ATR,
)

WEIGHTS = {"trend": 0.30, "momentum": 0.20, "volume": 0.20, "news": 0.15, "proximity": 0.15}
PIVOT_K = 3            # pivot = nejnižší/nejvyšší svíčka v okně ±3 dny
LEVEL_LOOKBACK = 120   # supporty a rezistence z posledních 120 svíček
MIN_BARS = 210         # SMA200 + rezerva

POSITIVE_WORDS = ("beat", "beats", "raises", "raised", "upgrade", "upgraded", "record", "wins", "won", "approval",
                  "approved", "buyback", "surge", "soars", "jumps", "outperform", "contract", "partnership", "tops")
NEGATIVE_WORDS = ("miss", "misses", "cuts", "cut", "downgrade", "downgraded", "lawsuit", "probe", "recall",
                  "plunge", "plunges", "offering", "falls", "slumps", "investigation", "warning", "delay", "halt")


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return lo
    return max(lo, min(hi, x))


def wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    prev = df["Close"].shift(1)
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - prev).abs(), (df["Low"] - prev).abs()], axis=1).max(axis=1)
    return wilder(tr, n)


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up, dn = wilder(d.clip(lower=0), n), wilder((-d).clip(lower=0), n)
    return 100 - 100 / (1 + up / dn.replace(0, float("nan")))


def pivot_levels(df: pd.DataFrame, k: int = PIVOT_K, lookback: int = LEVEL_LOOKBACK) -> Dict[str, List[float]]:
    """Pivotní minima (supporty) a maxima (rezistence) z uzavřených svíček; poslední k svíček pivot být nemůže."""
    d = df.tail(lookback)
    lows, highs = d["Low"].values, d["High"].values
    sup, res = [], []
    for i in range(k, len(d) - k):
        if lows[i] == min(lows[i - k:i + k + 1]):
            sup.append(float(lows[i]))
        if highs[i] == max(highs[i - k:i + k + 1]):
            res.append(float(highs[i]))
    return {"supports": sup, "resistances": res}


def nearest_levels(price: float, levels: Dict[str, List[float]]) -> Dict[str, Optional[float]]:
    below = [x for x in levels["supports"] if x < price]
    above = [x for x in levels["resistances"] if x > price]
    return {"support": max(below) if below else None, "resistance": min(above) if above else None}


def indicators(df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """Čísla pro filtr, skóre, plán i prompt. None = málo dat."""
    if df is None or len(df) < MIN_BARS:
        return None
    c, v = df["Close"], df["Volume"]
    close = float(c.iloc[-1])
    a14, a22 = atr(df, 14), atr(df, SWING_CHANDELIER_DAYS)
    sma50 = c.rolling(50).mean()
    lv = nearest_levels(close, pivot_levels(df))
    avg20_vol = float(v.iloc[-21:-1].mean())
    ind = {
        "close": close,
        "open": float(df["Open"].iloc[-1]),
        "high": float(df["High"].iloc[-1]),
        "low": float(df["Low"].iloc[-1]),
        "ema20": float(c.ewm(span=20, adjust=False).mean().iloc[-1]),
        "ema50": float(c.ewm(span=50, adjust=False).mean().iloc[-1]),
        "ema200": float(c.ewm(span=200, adjust=False).mean().iloc[-1]),
        "sma50": float(sma50.iloc[-1]),
        "sma50_prev10": float(sma50.iloc[-11]),
        "sma200": float(c.rolling(200).mean().iloc[-1]),
        "rsi14": float(rsi(c, 14).iloc[-1]),
        "atr14": float(a14.iloc[-1]),
        "atr22": float(a22.iloc[-1]),
        "hh22": float(df["High"].iloc[-SWING_CHANDELIER_DAYS:].max()),
        "ret20": close / float(c.iloc[-21]) - 1.0,
        "rel_vol": float(v.iloc[-1]) / avg20_vol if avg20_vol > 0 else 0.0,
        "rel_vol5": float(v.iloc[-5:].mean()) / avg20_vol if avg20_vol > 0 else 0.0,
        "adv_usd": float((c * v).iloc[-20:].mean()),
        "high20_prev": float(c.iloc[-21:-1].max()),
        "support": lv["support"],
        "resistance": lv["resistance"],
        "last_date": pd.Timestamp(df.index[-1]).date().isoformat(),
    }
    return {k: (round(x, 4) if isinstance(x, float) else x) for k, x in ind.items()}


def universe_reject(ind: Dict[str, Any], min_adv: float = SWING_MIN_DOLLAR_VOLUME, adv_fx: float = 1.0,
                    adv_ccy: str = "USD", px_ccy: str = "USD") -> Optional[str]:
    """Důvod vyřazení z vesmíru, nebo None. adv_fx převádí obrat v měně akcie na měnu limitu (EU: EUR)."""
    if ind["close"] < SWING_MIN_PRICE:
        return f"cena < 5 {px_ccy}"
    if ind["adv_usd"] * adv_fx < min_adv:
        return f"obrat < {min_adv / 1e6:g} mil. {adv_ccy}"
    if not (ind["close"] > ind["sma50"] and ind["close"] > ind["sma200"]):
        return "pod SMA50/SMA200"
    return None


def regime_ok(spy_close: float, spy_sma200: float) -> bool:
    """Nové vstupy jen když SPY zavřel nad SMA200."""
    return spy_close >= spy_sma200


def news_score(headlines: List[str], briefing_bonus: bool = False) -> float:
    """0..1 z počtu a tónu titulků (slova, bez Gemini); ticker z ranního přehledu / skeneru +0,25."""
    if not headlines:
        return 0.5 if briefing_bonus else 0.0
    pos = neg = 0
    for h in headlines:
        words = set(str(h).lower().replace(",", " ").replace(".", " ").split())
        pos += any(w in words for w in POSITIVE_WORDS)
        neg += any(w in words for w in NEGATIVE_WORDS)
    s = 0.5 + 0.5 * (pos - neg) / (pos + neg) if pos + neg else 0.5
    return round(_clip(s + (0.25 if briefing_bonus else 0.0)), 4)


def score_parts(ind: Dict[str, Any], news: float = 0.0) -> Dict[str, float]:
    trend = sum([ind["close"] > ind["ema20"], ind["ema20"] > ind["ema50"], ind["ema50"] > ind["ema200"],
                 ind["sma50"] > ind["sma50_prev10"]]) / 4.0
    r = ind["rsi14"]
    m_rsi = 1.0 if 50 <= r <= 70 else (_clip((r - 40) / 10) if r < 50 else _clip((80 - r) / 10))
    momentum = 0.5 * _clip(ind["ret20"] / 0.15) + 0.5 * m_rsi
    volume = _clip((max(ind["rel_vol"], ind["rel_vol5"]) - 0.8) / 0.7)
    prox = 0.0
    if ind.get("support") and ind["atr14"] > 0:
        d = (ind["close"] - ind["support"]) / ind["atr14"]
        prox = _clip(1 - (d - 1) / 2)
    if ind["close"] >= ind["high20_prev"]:
        prox = 1.0
    return {"trend": round(trend, 4), "momentum": round(momentum, 4), "volume": round(volume, 4),
            "news": round(_clip(news), 4), "proximity": round(prox, 4)}


def total_score(parts: Dict[str, float]) -> float:
    return round(sum(WEIGHTS[k] * parts[k] for k in WEIGHTS), 4)


def target_2r(entry: float, stop: float, side_rate: float) -> float:
    """Cíl, u kterého je zisk po nákladech přesně 2× riziko vč. nákladů (+2R čistě)."""
    risk_ps = (entry - stop) + entry * side_rate + stop * side_rate
    return (entry * (1 + side_rate) + SWING_MIN_NET_RR * risk_ps) / (1 - side_rate)


def plan_trade(entry: float, atr14: float, support: Optional[float], resistance: Optional[float],
               side_rate: float) -> Dict[str, Any]:
    """SL = nižší z (nejbližší support, entry − 2×ATR14), první cíl = +2R po nákladech (rozhodnutí uživatele 2. 11.).

    Rezistence už není cíl ani podmínka vstupu, jen informace do logu a pro Gemini.
    """
    if not entry or entry <= 0 or not atr14 or atr14 <= 0:
        return {"ok": False, "reason": "chybí cena nebo ATR"}
    stop = entry - SWING_ATR_STOP_MULT * atr14
    if support is not None and support < stop:
        stop = support
    if stop <= 0:
        return {"ok": False, "reason": "stop <= 0"}
    risk_ps = (entry - stop) + entry * side_rate + stop * side_rate
    target = target_2r(entry, stop, side_rate)
    reward_ps = (target - entry) - entry * side_rate - target * side_rate
    rr = reward_ps / risk_ps if risk_ps > 0 else 0.0
    out = {"stop": round(stop, 4), "target": round(target, 4), "risk_ps": round(risk_ps, 6),
           "rr_net": round(rr, 3), "entry_ref": round(entry, 4), "resistance": resistance}
    if rr < SWING_MIN_NET_RR - 1e-6:
        return dict(out, ok=False, reason=f"RRR po nákladech {rr:.2f} < {SWING_MIN_NET_RR}")
    return dict(out, ok=True, reason="")


def size_position(equity: float, cash: float, entry: float, stop: float, side_rate: float, fx: float) -> Dict[str, Any]:
    """Kusy tak, aby ztráta na stopu vč. nákladů = 0,5 % účtu; strop 20 % účtu a volná hotovost. Částky v měně účtu."""
    risk_ps_acct = ((entry - stop) + entry * side_rate + stop * side_rate) * fx
    unit_cost = entry * fx * (1 + side_rate)
    if risk_ps_acct <= 0 or unit_cost <= 0 or equity <= 0:
        return {"qty": 0.0, "reason": "neplatné vstupy"}
    by_risk = equity * SWING_RISK_PCT / 100.0 / risk_ps_acct
    by_alloc = equity * SWING_MAX_ALLOC_PCT / 100.0 / unit_cost
    by_cash = max(0.0, cash) / unit_cost
    qty = math.floor(min(by_risk, by_alloc, by_cash) * 10000) / 10000
    limit = min((by_risk, "risk"), (by_alloc, "20 % účtu"), (by_cash, "hotovost"))[1]
    return {"qty": qty, "risk_acct": round(qty * risk_ps_acct, 4), "notional_acct": round(qty * unit_cost, 4),
            "limited_by": limit, "reason": "" if qty > 0 else "nulová velikost"}


def position_open_risk(pos: Dict[str, Any], fx: float) -> float:
    """Kolik by pozice ještě ztratila (měna účtu), kdyby dojela na stop. Stop nad nákupem = 0."""
    c = pos["cost_side"]
    loss_ps = pos["entry"] * (1 + c) - pos["stop"] * (1 - c)
    return max(0.0, pos["qty_open"] * loss_ps * fx)


def portfolio_block(positions: List[Dict[str, Any]], sector: str, new_risk_acct: float, equity: float,
                    fx: float, ticker: str = "", check_heat: bool = True, market: Optional[str] = None) -> Optional[str]:
    """Max 5 pozic (vč. čekajících), max 1 na sektor, celkové otevřené riziko <= 3 % účtu."""
    if len(positions) >= SWING_MAX_POSITIONS:
        return f"plno ({SWING_MAX_POSITIONS} pozic)"
    if ticker and any(p["ticker"] == ticker for p in positions):
        return "ticker už v portfoliu"
    same = [p for p in positions if p.get("sector") == sector]
    if market and SWING_SECTOR_LIMIT_SCOPE == "per_market":
        same = [p for p in same if p.get("market", "US") == market]   # limit sektoru zvlášť pro US a EU
    if len(same) >= SWING_MAX_PER_SECTOR:
        return f"sektor {sector} už obsazen{f' ({market})' if market and SWING_SECTOR_LIMIT_SCOPE == 'per_market' else ''}"
    if not check_heat:
        return None
    heat = sum(p.get("planned_risk_acct", 0.0) if p.get("status") == "PENDING" else position_open_risk(p, fx)
               for p in positions)
    if heat + new_risk_acct > equity * SWING_MAX_OPEN_RISK_PCT / 100.0 + 1e-9:
        return f"otevřené riziko {heat + new_risk_acct:.2f} > {SWING_MAX_OPEN_RISK_PCT} % účtu"
    return None


def r_now(pos: Dict[str, Any], price: float) -> float:
    return (price - pos["entry"]) / pos["r_ps"] if pos.get("r_ps") else 0.0


def process_bar(pos: Dict[str, Any], o: float, h: float, l: float, entry_day: bool) -> List[Dict[str, Any]]:
    """Výstupy během dne podle stopu platného od rána. Pořadí uvnitř svíčky neznáme -> nejdřív stop (konzervativně).

    Den vstupu: jen Stop-Loss (o, h, l jsou pak ceny až od vstupu). Vrací [{"qty", "price", "reason"}].
    """
    stop = pos["stop"]
    if l <= stop:
        px = o if (o < stop and not entry_day) else stop
        return [{"qty": pos["qty_open"], "price": px, "reason": "GAP_STOP" if px < stop else "STOP"}]
    if entry_day:
        return []
    t2 = pos.get("target") or pos["entry"] + SWING_PARTIAL_AT_R * pos["r_ps"]   # +2R po nákladech
    if not pos.get("partial_done") and h >= t2:
        px = max(o, t2)
        return [{"qty": round(pos["qty_open"] / 2, 4), "price": px, "reason": "PARTIAL_2R"}]
    return []


def update_excursions(pos: Dict[str, Any], h: float, l: float) -> None:
    pos["max_high"] = max(pos.get("max_high", pos["entry"]), h)
    pos["min_low"] = min(pos.get("min_low", pos["entry"]), l)
    pos["mfe_r"] = round(r_now(pos, pos["max_high"]), 3)
    pos["mae_r"] = round(r_now(pos, pos["min_low"]), 3)


def next_stop(pos: Dict[str, Any], hh22: float, atr22: float) -> Dict[str, Any]:
    """Stop na další den: break-even (+1R), chandelier (HH22 − 3×ATR22). Stop se jen zvedá."""
    old = pos["stop"]
    new, why = old, []
    if pos.get("max_high", 0) >= pos["entry"] + SWING_BE_AT_R * pos["r_ps"]:
        be = round(pos["entry"] * (1 + 2 * pos["cost_side"]), 4)
        if be > new:
            new, why = be, why + ["BE"]
    if hh22 and atr22:
        ch = round(hh22 - SWING_CHANDELIER_ATR * atr22, 4)
        if ch > new:
            new, why = ch, why + ["CHANDELIER"]
    return {"stop": new, "moved": new > old, "why": why}


def tighten_stop(stop: float, close: float, atr14: float) -> float:
    """TIGHTEN od Gemini: stop o krok výš na close − 2×ATR14, nikdy níž."""
    return round(max(stop, close - SWING_TIGHTEN_ATR * atr14), 4)


def time_stop_due(pos: Dict[str, Any]) -> bool:
    """Po 10 obchodních dnech bez dosažení +0,5R -> výstup."""
    return pos.get("days_held", 0) >= SWING_TIME_STOP_DAYS and pos.get("mfe_r", 0.0) < SWING_TIME_STOP_MIN_R
