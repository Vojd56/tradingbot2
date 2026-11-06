"""Strategie TURTLE 4h (Donchian 20/10, jen long) – POUZE DEMO / PAPER TRADING.

Backtest (%TEMP%\\trend_replay, 180 dní, 10 krypto): jediná průrazová varianta v plusu v obou polovinách,
ale v kontrolních 60 dnech jen +2,6R a táhl ji jeden symbol. Proto běží jen na papíře, aby se změřila naživo.

Pravidla:
- Vstup: poslední UZAVŘENÁ 4h svíčka zavře nad nejvyšším high předchozích TURTLE_ENTRY_BARS svíček.
  Nákup za aktuální cenu v okamžiku vyhodnocení (krátce po zavření svíčky).
- Počáteční Stop-Loss = entry - TURTLE_ATR_MULT x ATR(14) z 4h svíček.
- Výstup: uzavřená 4h svíčka zavře pod nejnižším low předchozích TURTLE_EXIT_BARS svíček,
  nebo cena kdykoli klesne na Stop-Loss.
- Jen long, 1 pozice na symbol, max TURTLE_MAX_POSITIONS najednou, 1R ~ TURTLE_RISK_USD.

Žádné příkazy na Binance (ani testnet), žádné Gemini. Vlastní evidence TURTLE_TRADES_FILE,
takže se nepočítá do limitů SFP/akcií, denního limitu ztrát ani risk_guard.
"""

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

from config import (
    CRYPTO_WATCHLIST,
    KILL_SWITCH_FILE,
    TURTLE_ENABLED,
    TURTLE_ENTRY_BARS,
    TURTLE_EXIT_BARS,
    TURTLE_ATR_MULT,
    TURTLE_MAX_POSITIONS,
    TURTLE_RISK_USD,
    TURTLE_FEE_PCT,
    TURTLE_SLIPPAGE_PCT,
    TURTLE_TRADES_FILE,
)

logger = logging.getLogger("TradingBot.Turtle")

STRATEGY_TURTLE = "TURTLE4H"
ATR_PERIOD = 14
BAR_MS = 4 * 3600 * 1000
MAX_SIGNAL_AGE_MS = 60 * 60 * 1000  # Signál starší než 1 h po zavření svíčky se nebere (např. po restartu)
KLINES_LIMIT = 60


# =====================================================================
# ČISTÉ FUNKCE (bez API, kvůli testům)
# =====================================================================

def closed_bars(klines: List[List[Any]], now_ms: int) -> List[List[Any]]:
    """Jen uzavřené svíčky (closeTime < now). Rozjetá svíčka se zahodí."""
    return [k for k in klines if int(k[6]) < now_ms]


def atr(klines: List[List[Any]], period: int = ATR_PERIOD) -> Optional[float]:
    """ATR (Wilder) poslední svíčky. None, když je svíček málo."""
    if len(klines) < period + 1:
        return None
    trs = []
    for prev, k in zip(klines, klines[1:]):
        h, l, pc = float(k[2]), float(k[3]), float(prev[4])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    value = sum(trs[:period]) / period
    for tr in trs[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def entry_signal(klines: List[List[Any]], now_ms: int, entry_bars: int = TURTLE_ENTRY_BARS,
                 atr_mult: float = TURTLE_ATR_MULT) -> Optional[Dict[str, Any]]:
    """Průraz: poslední uzavřená svíčka zavřela nad high předchozích entry_bars svíček.

    Vrací {"bar_open", "close", "level", "atr", "stop_distance"} nebo None.
    """
    bars = closed_bars(klines, now_ms)
    if len(bars) < max(entry_bars, ATR_PERIOD) + 1:
        return None
    last = bars[-1]
    if now_ms - int(last[6]) > MAX_SIGNAL_AGE_MS:
        return None
    level = max(float(k[2]) for k in bars[-entry_bars - 1:-1])
    close = float(last[4])
    if close <= level:
        return None
    a = atr(bars)
    if not a or a <= 0:
        return None
    return {"bar_open": int(last[0]), "close": close, "level": level, "atr": a, "stop_distance": atr_mult * a}


def exit_signal(klines: List[List[Any]], now_ms: int, opened_ms: int,
                exit_bars: int = TURTLE_EXIT_BARS) -> Optional[Dict[str, Any]]:
    """Výstup: poslední uzavřená svíčka (zavřená po otevření pozice) zavřela pod low předchozích exit_bars svíček."""
    bars = closed_bars(klines, now_ms)
    if len(bars) < exit_bars + 1:
        return None
    last = bars[-1]
    if int(last[6]) <= opened_ms:
        return None
    level = min(float(k[3]) for k in bars[-exit_bars - 1:-1])
    close = float(last[4])
    if close >= level:
        return None
    return {"bar_open": int(last[0]), "close": close, "level": level}


def position_qty(entry: float, stop_loss: float, risk_usd: float = TURTLE_RISK_USD) -> float:
    """Množství tak, aby ztráta při Stop-Lossu byla risk_usd (1R)."""
    risk = entry - stop_loss
    return risk_usd / risk if risk > 0 else 0.0


def trade_pnl(entry: float, exit_price: float, qty: float, stop_loss: float,
              fee_pct: float = TURTLE_FEE_PCT, slip_pct: float = TURTLE_SLIPPAGE_PCT) -> Dict[str, float]:
    """P&L long obchodu po poplatcích a skluzu (obojí za stranu). R se počítá z původního rizika entry - SL."""
    fee, slip = fee_pct / 100.0, slip_pct / 100.0
    buy_fill = entry * (1 + slip)
    sell_fill = exit_price * (1 - slip)
    fees = qty * (buy_fill + sell_fill) * fee
    pnl = qty * (sell_fill - buy_fill) - fees
    risk_usd = qty * (entry - stop_loss)
    return {"buy_fill": buy_fill, "sell_fill": sell_fill, "fees_usd": round(fees, 4),
            "pnl_usd": round(pnl, 4), "r": round(pnl / risk_usd, 3) if risk_usd > 0 else 0.0}


def open_positions(trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [t for t in trades if t.get("status") == "OPEN"]


def can_open(trades: List[Dict[str, Any]], symbol: str, bar_open: int,
             max_positions: int = TURTLE_MAX_POSITIONS) -> Optional[str]:
    """None = smí otevřít, jinak důvod proč ne."""
    opened = open_positions(trades)
    if any(t["ticker"] == symbol for t in opened):
        return "pozice na symbolu už je otevřená"
    if len(opened) >= max_positions:
        return f"limit {max_positions} turtle pozic"
    if any(t["ticker"] == symbol and t.get("signal_bar_open") == bar_open for t in trades):
        return "signál z této svíčky už byl použit"
    return None


def new_trade(symbol: str, price: float, signal: Dict[str, Any], now: datetime,
              risk_usd: float = TURTLE_RISK_USD) -> Optional[Dict[str, Any]]:
    stop_loss = price - signal["stop_distance"]
    if stop_loss <= 0 or stop_loss >= price:
        return None
    qty = position_qty(price, stop_loss, risk_usd)
    return {
        "trade_id": f"TURTLE-{uuid.uuid4().hex[:8]}",
        "strategy": STRATEGY_TURTLE,
        "ticker": symbol,
        "status": "OPEN",
        "paper_only": True,
        "entry": price,
        "stop_loss": stop_loss,
        "atr": signal["atr"],
        "breakout_level": signal["level"],
        "signal_close": signal["close"],
        "signal_bar_open": signal["bar_open"],
        "qty": qty,
        "risk_usd": round(qty * (price - stop_loss), 4),
        "opened_at": now.isoformat(),
        "opened_ms": int(now.timestamp() * 1000),
    }


def close_trade(trade: Dict[str, Any], price: float, reason: str, now: datetime) -> Dict[str, Any]:
    res = trade_pnl(trade["entry"], price, trade["qty"], trade["stop_loss"])
    trade.update({"status": "CLOSED", "exit_price": price, "exit_reason": reason,
                  "closed_at": now.isoformat(), **res})
    return trade


def stop_hit(trade: Dict[str, Any], price: float) -> bool:
    return trade.get("status") == "OPEN" and price <= float(trade["stop_loss"])


def summary(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    closed = [t for t in trades if t.get("status") == "CLOSED"]
    wins = sum(1 for t in closed if t.get("pnl_usd", 0) > 0)
    return {"open": len(open_positions(trades)), "closed": len(closed), "wins": wins,
            "total_r": round(sum(t.get("r", 0) for t in closed), 2),
            "total_usd": round(sum(t.get("pnl_usd", 0) for t in closed), 2)}


# =====================================================================
# EVIDENCE (vlastní soubor, NE paper_trades.json)
# =====================================================================

def load_ledger(path: Path = None) -> List[Dict[str, Any]]:
    path = Path(path or TURTLE_TRADES_FILE)
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.error(f"Chyba při čtení {path.name}: {e}")
        return []


def save_ledger(trades: List[Dict[str, Any]], path: Path = None) -> None:
    path = Path(path or TURTLE_TRADES_FILE)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(trades, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


# =====================================================================
# DATA Z BURZY (veřejné endpointy, žádné API klíče)
# =====================================================================

def fetch_klines_4h(symbol: str, limit: int = KLINES_LIMIT) -> List[List[Any]]:
    r = requests.get("https://fapi.binance.com/fapi/v1/klines",
                     params={"symbol": symbol, "interval": "4h", "limit": limit}, timeout=10)
    r.raise_for_status()
    return r.json()


def fetch_price(symbol: str) -> float:
    r = requests.get("https://fapi.binance.com/fapi/v1/ticker/price", params={"symbol": symbol}, timeout=10)
    r.raise_for_status()
    return float(r.json()["price"])


# =====================================================================
# VYHODNOCENÍ (volá bot.py přes asyncio.to_thread)
# =====================================================================

def run_4h_evaluation(symbols: List[str] = None, now: datetime = None,
                      get_klines: Callable[[str], List[List[Any]]] = fetch_klines_4h,
                      get_price: Callable[[str], float] = fetch_price,
                      path: Path = None, kill_switch: Path = KILL_SWITCH_FILE) -> List[Dict[str, Any]]:
    """Po zavření 4h svíčky: nejdřív výstupy otevřených pozic, pak nové vstupy. Vrací události pro Discord."""
    if not TURTLE_ENABLED:
        return []
    symbols = symbols or CRYPTO_WATCHLIST
    now = now or datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    trades = load_ledger(path)
    events: List[Dict[str, Any]] = []
    klines_cache: Dict[str, List[List[Any]]] = {}

    def klines_for(sym: str) -> List[List[Any]]:
        if sym not in klines_cache:
            klines_cache[sym] = get_klines(sym)
        return klines_cache[sym]

    for t in open_positions(trades):
        try:
            ex = exit_signal(klines_for(t["ticker"]), now_ms, int(t.get("opened_ms", 0)))
            if ex:
                price = get_price(t["ticker"])
                close_trade(t, price, f"zavření pod {TURTLE_EXIT_BARS}-svíčkovým minimem {ex['level']:.6g}", now)
                events.append({"type": "close", "trade": t})
        except Exception as e:
            logger.error(f"Turtle: chyba při výstupu {t.get('ticker')}: {e}")

    entries_blocked = kill_switch is not None and Path(kill_switch).exists()
    if entries_blocked:
        logger.info("Turtle: vypínač STOP existuje, nové vstupy se neotevírají.")
    else:
        for sym in symbols:
            try:
                sig = entry_signal(klines_for(sym), now_ms)
                if not sig:
                    continue
                reason = can_open(trades, sym, sig["bar_open"])
                if reason:
                    logger.info(f"Turtle: {sym} průraz nad {sig['level']:.6g}, ale nevstupuji ({reason}).")
                    continue
                trade = new_trade(sym, get_price(sym), sig, now)
                if trade:
                    trades.append(trade)
                    events.append({"type": "open", "trade": trade})
            except Exception as e:
                logger.error(f"Turtle: chyba při vstupu {sym}: {e}")

    if events:
        save_ledger(trades, path)
    logger.info(f"🐢 TURTLE vyhodnocení 4h: otevřeno {sum(e['type'] == 'open' for e in events)}, "
                f"zavřeno {sum(e['type'] == 'close' for e in events)}, otevřených {len(open_positions(trades))}")
    return events


def check_stops(get_price: Callable[[str], float] = fetch_price, path: Path = None,
                now: datetime = None) -> List[Dict[str, Any]]:
    """Průběžná kontrola Stop-Lossu otevřených turtle pozic podle aktuální ceny."""
    trades = load_ledger(path)
    opened = open_positions(trades)
    if not opened:
        return []
    now = now or datetime.now(timezone.utc)
    events = []
    for t in opened:
        try:
            price = get_price(t["ticker"])
            if stop_hit(t, price):
                close_trade(t, price, "Stop-Loss", now)
                events.append({"type": "close", "trade": t})
        except Exception as e:
            logger.error(f"Turtle: chyba při kontrole SL {t.get('ticker')}: {e}")
    if events:
        save_ledger(trades, path)
    return events


def format_event(ev: Dict[str, Any]) -> str:
    """Krátká česká zpráva do Discordu."""
    t = ev["trade"]
    sym = t["ticker"]
    if ev["type"] == "open":
        return (f"🐢 **[TURTLE DEMO] Nákup {sym}** za ${t['entry']:.6g} | SL ${t['stop_loss']:.6g} "
                f"(2x ATR) | průraz nad ${t['breakout_level']:.6g} | 1R = ${t['risk_usd']:.2f} | jen papír, žádný příkaz na burzu")
    emoji = "🟢" if t.get("pnl_usd", 0) > 0 else "🔴"
    return (f"{emoji} **[TURTLE DEMO] Prodej {sym}** za ${t['exit_price']:.6g} ({t['exit_reason']}) | "
            f"vstup ${t['entry']:.6g} | {t['r']:+.2f}R, ${t['pnl_usd']:+.2f} po poplatcích")
