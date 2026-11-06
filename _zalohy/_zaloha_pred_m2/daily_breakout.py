"""Strategie HIGH20: denní průraz 20denního maxima na kryptu (Binance Futures).

Zdroj: Padysak & Vojtko, Seasonality, Trend-following, and Mean Reversion in Bitcoin (Quantpedia).
Backtest 28. 10. 2026 (10 párů bota, 2 roky, poplatek 0,05 % + skluz 0,02 % na stranu):
rok 1 +5,1 %, rok 2 (out-of-sample, trh −30 %) +17,1 %, Sharpe 1,07, max. propad 6,9 %;
zisk drží pro lookback 10 až 50 dní, se stopem 1,5x ATR OOS +14,3 %.

Pravidla (bez look-ahead, jen uzavřené denní svíčky UTC):
1. Po zavření denní svíčky (00:00 UTC): close >= nejvyšší close posledních HIGH20_LOOKBACK dní (vč. dneška).
2. Vstup long hned po open nového dne (bot to dělá v okně HIGH20_ENTRY_WINDOW_HOURS po půlnoci UTC).
3. Ochranný Stop-Loss = entry − HIGH20_STOP_ATR x ATR(14) z denních svíček, na burze.
4. Výstup na konci dne vstupu (časový stop v 00:00 UTC následujícího dne).
5. Take-Profit = entry + HIGH20_TP_ATR x ATR jen jako pojistka proti extrémnímu knotu; v backtestu žádný nebyl.
Velikost pozice a pojistky řeší bot.open_and_record_trade (risk sizing, portfolio heat, denní limit).
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import (
    BASE_DIR,
    HIGH20_LOOKBACK,
    HIGH20_STOP_ATR,
    HIGH20_TP_ATR,
    HIGH20_ENTRY_WINDOW_HOURS,
)
from price_utils import round_price
from strategy_rules import STRATEGY_HIGH20
ATR_PERIOD = 14
STATE_FILE = BASE_DIR / "high20_state.json"


def _closed(klines: List[List[Any]], now_ms: int) -> List[List[Any]]:
    """Jen uzavřené svíčky (closeTime < now). Rozjetá dnešní svíčka se zahodí."""
    return [k for k in klines if int(k[6]) < now_ms]


def atr(klines: List[List[Any]], period: int = ATR_PERIOD) -> Optional[float]:
    """ATR (Wilder) poslední svíčky. None, když je svíček málo."""
    if len(klines) < period + 1:
        return None
    trs = []
    for i in range(1, len(klines)):
        h, l, pc = float(klines[i][2]), float(klines[i][3]), float(klines[i - 1][4])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


def signal(klines_1d: List[List[Any]], now_ms: int, lookback: int = HIGH20_LOOKBACK) -> Optional[Dict[str, Any]]:
    """Poslední uzavřená denní svíčka zavřela na lookback-denním maximu closů. Vrací {"close", "level", "atr", "day_close_ms"} nebo None."""
    bars = _closed(klines_1d, now_ms)
    if len(bars) < max(lookback, ATR_PERIOD + 1):
        return None
    last = bars[-1]
    close = float(last[4])
    level = max(float(k[4]) for k in bars[-lookback:])
    if close < level:
        return None
    a = atr(bars)
    if not a or a <= 0:
        return None
    return {"close": close, "level": level, "atr": a, "day_close_ms": int(last[6])}


def levels(entry: float, atr_value: float) -> Dict[str, float]:
    """Stop-Loss a pojistný Take-Profit od skutečné vstupní ceny."""
    return {"stop_loss": round_price(entry - HIGH20_STOP_ATR * atr_value),
            "take_profit": round_price(entry + HIGH20_TP_ATR * atr_value)}


def exit_at(entry_time: datetime) -> datetime:
    """Konec dne vstupu = následující půlnoc UTC."""
    day = entry_time.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return day + timedelta(days=1)


def expired(trade: Dict[str, Any], now: datetime) -> bool:
    """Otevřená HIGH20 pozice, jejíž den už skončil (čeká na zavření Scoutem)."""
    if trade.get("strategy") != STRATEGY_HIGH20 or not trade.get("exit_at"):
        return False
    try:
        until = datetime.fromisoformat(str(trade["exit_at"]))
    except ValueError:
        return False
    return now >= until


def in_entry_window(now: datetime) -> bool:
    """Vstupy jen krátce po zavření denní svíčky, aby cena odpovídala backtestu (vstup za open)."""
    now = now.astimezone(timezone.utc)
    return now.hour < HIGH20_ENTRY_WINDOW_HOURS


def build_analysis(symbol: str, price: float, sig: Dict[str, Any]) -> Dict[str, Any]:
    """Analýza ve tvaru, který čeká bot.open_and_record_trade (bez Gemini)."""
    lv = levels(price, sig["atr"])
    risk = price - lv["stop_loss"]
    return {
        "ticker": symbol,
        "price": price,
        "chart": {"entry": price, "stop_loss": lv["stop_loss"], "take_profit": lv["take_profit"],
                  "rrr": f"1:{(lv['take_profit'] - price) / risk:.1f}" if risk > 0 else "-",
                  "source": "HIGH20", "atr": sig["atr"], "level": sig["level"]},
        "sizer": {},
        "news": f"HIGH20: denní close {sig['close']:.6g} na {HIGH20_LOOKBACK}denním maximu, ATR {sig['atr']:.6g}",
    }


# ---------- stav: každý den se vyhodnocuje jen jednou ----------

def load_state(path: Path = None) -> Dict[str, Any]:
    path = path or STATE_FILE
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: Dict[str, Any], path: Path = None) -> None:
    path = path or STATE_FILE
    Path(path).write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def already_done(state: Dict[str, Any], now: datetime) -> bool:
    return state.get("last_day") == now.astimezone(timezone.utc).strftime("%Y-%m-%d")


def done_state(now: datetime, results: Dict[str, str]) -> Dict[str, Any]:
    return {"last_day": now.astimezone(timezone.utc).strftime("%Y-%m-%d"), "results": results}
