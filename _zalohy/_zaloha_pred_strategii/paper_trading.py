"""Modul pro správu paper tradingu a ukládání obchodů do paper_trades.json.
"""

from price_utils import round_price
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any

from config import (
    PAPER_TRADES_FILE,
    MAX_OPEN_POSITIONS,
    MAX_OPEN_POSITIONS_STOCKS,
    MAX_OPEN_POSITIONS_CRYPTO,
    CRYPTO_WATCHLIST
)

logger = logging.getLogger("TradingBot.PaperTrading")


def _ensure_file_exists() -> None:
    """Zajistí existenci souboru paper_trades.json."""
    if not PAPER_TRADES_FILE.exists():
        try:
            with open(PAPER_TRADES_FILE, "w", encoding="utf-8") as f:
                json.dump([], f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Chyba při vytváření {PAPER_TRADES_FILE}: {e}")


def load_trades() -> List[Dict[str, Any]]:
    """Načte všechny paper trady ze souboru."""
    _ensure_file_exists()
    try:
        with open(PAPER_TRADES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            return []
    except Exception as e:
        logger.error(f"Chyba při čtení {PAPER_TRADES_FILE}: {e}")
        return []


def save_trades(trades: List[Dict[str, Any]]) -> bool:
    """Uloží seznam obchodů do paper_trades.json."""
    _ensure_file_exists()
    try:
        temp_file = PAPER_TRADES_FILE.with_suffix(".tmp")
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(trades, f, indent=2, ensure_ascii=False)
        temp_file.replace(PAPER_TRADES_FILE)
        return True
    except Exception as e:
        logger.error(f"Chyba při ukládání {PAPER_TRADES_FILE}: {e}")
        return False


def get_open_positions() -> List[Dict[str, Any]]:
    """Vrátí všechny aktuálně otevřené pozice."""
    trades = load_trades()
    return [t for t in trades if t.get("status") == "OPEN"]


def get_open_positions_count() -> int:
    """Vrátí celkový počet aktuálně otevřených pozic."""
    return len(get_open_positions())


def get_open_positions_by_market(market_type: str = "STOCK") -> List[Dict[str, Any]]:
    """Vrátí otevřené pozice pro daný trh ('STOCK' pro Trading 212 nebo 'CRYPTO' pro Binance)."""
    positions = get_open_positions()
    if market_type == "CRYPTO":
        return [p for p in positions if p.get("market_type") == "CRYPTO" or p.get("ticker", "").endswith("USDT")]
    else:
        return [p for p in positions if p.get("market_type") != "CRYPTO" and not p.get("ticker", "").endswith("USDT")]


def is_ticker_already_open(ticker: str) -> bool:
    """Zkontroluje, zda již pro daný ticker existuje otevřená pozice."""
    t_clean = ticker.upper().strip()
    return any(p.get("ticker", "").upper() == t_clean for p in get_open_positions())


def get_positions_limit_info(ticker: str) -> Dict[str, Any]:
    """Vrátí počet otevřených pozic a limit pro trh daného tickeru (5 pro T212, 5 pro Binance)."""
    t_clean = ticker.upper().strip()
    is_crypto = t_clean.endswith("USDT") or t_clean in ["BTC", "ETH", "SOL", "BNB"] or t_clean in CRYPTO_WATCHLIST
    if is_crypto:
        count = len(get_open_positions_by_market("CRYPTO"))
        limit = MAX_OPEN_POSITIONS_CRYPTO
        platform = "Binance Futures"
        m_type = "CRYPTO"
    else:
        count = len(get_open_positions_by_market("STOCK"))
        limit = MAX_OPEN_POSITIONS_STOCKS
        platform = "Trading 212"
        m_type = "STOCK"

    already_open = is_ticker_already_open(ticker)
    can_open = (count < limit) and not already_open

    return {
        "count": count,
        "limit": limit,
        "can_open": can_open,
        "already_open": already_open,
        "platform": platform,
        "market_type": m_type
    }


def can_open_new_position_for(ticker: str) -> bool:
    """Zkontroluje, zda lze otevřít pozici pro daný ticker (max 5 na T212 a max 5 na Binance, nesmí již být otevřen)."""
    return get_positions_limit_info(ticker)["can_open"]


def can_open_new_position() -> bool:
    """Celková kontrola otevřených pozic (max 10 celkem)."""
    return get_open_positions_count() < MAX_OPEN_POSITIONS


def record_new_trade(
    ticker: str,
    action: str,  # "APPROVED" nebo "REJECTED"
    status: str,  # "OPEN" pokud schváleno, "REJECTED" pokud zamítnuto
    entry: float,
    stop_loss: float,
    take_profit: float,
    rrr: str,
    allocated_usd: float,
    shares: float,
    news_summary: str,
    skeptik_reasons: List[str],
    user_name: str,
    user_id: int,
    trade_id: Optional[str] = None,
    t212_order_id: Optional[str] = None,
    t212_status: Optional[str] = None,
    binance_order_id: Optional[str] = None,
    binance_status: Optional[str] = None,
    market_type: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Zaznamená nový paper trade nebo rozhodnutí o zamítnutí.

    extra: doplňková pole uložená k obchodu (např. bot_qty, sl_order, tp_order, opened_by_bot).
    """
    trades = load_trades()
    now_iso = datetime.now(timezone.utc).isoformat()

    if not trade_id:
        trade_id = f"TRADE-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{ticker}"

    # Zkontrolujeme, zda trade_id již neexistuje (aktualizace)
    existing_index = None
    for idx, t in enumerate(trades):
        if t.get("trade_id") == trade_id:
            existing_index = idx
            break

    trade_record = {
        "trade_id": trade_id,
        "ticker": ticker.upper(),
        "market_type": market_type or ("CRYPTO" if ticker.upper().endswith("USDT") else "STOCK"),
        "action": action,
        "status": status,
        "entry": round_price(entry),
        "stop_loss": round_price(stop_loss),
        "take_profit": round_price(take_profit),
        "rrr": rrr,
        "allocated_usd": round(allocated_usd, 2),
        "shares": round(shares, 6) if isinstance(shares, (int, float)) else shares,
        "news_summary": news_summary,
        "skeptik_reasons": skeptik_reasons,
        "decided_by": user_name,
        "decided_by_id": user_id,
        "decided_at": now_iso,
        "t212_order_id": t212_order_id,
        "t212_status": t212_status,
        "binance_order_id": binance_order_id,
        "binance_status": binance_status,
        "closed_at": None,
        "exit_price": None,
        "pnl_usd": None,
        "pnl_pct": None,
    }
    if extra:
        trade_record.update(extra)

    if existing_index is not None:
        trades[existing_index] = trade_record
    else:
        trades.append(trade_record)

    save_trades(trades)
    return trade_record


def update_trade(trade_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Aktualizuje vybraná pole obchodu podle trade_id."""
    trades = load_trades()
    for t in trades:
        if t.get("trade_id") == trade_id:
            t.update(updates)
            save_trades(trades)
            return t
    return None


def close_position(
    ticker: str,
    exit_price: float,
    user_name: str,
    trade_id: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """Uzavře otevřenou pozici (podle trade_id, jinak podle tickeru) a spočítá zisk/ztrátu.

    Volat AŽ PO potvrzení prodeje na burze – viz position_manager.close_bot_trade.
    """
    trades = load_trades()
    target_idx = None

    for idx, t in enumerate(trades):
        if t.get("status") != "OPEN":
            continue
        if (trade_id and t.get("trade_id") == trade_id) or (not trade_id and t.get("ticker") == ticker.upper()):
            target_idx = idx
            break

    if target_idx is None:
        return None

    trade = trades[target_idx]
    entry = trade.get("entry", 0.0)
    shares = trade.get("bot_qty", trade.get("shares", 0))
    try:
        shares = float(shares)
    except (TypeError, ValueError):
        shares = 0.0

    pnl_usd = (exit_price - entry) * shares
    pnl_pct = ((exit_price - entry) / entry) * 100 if entry > 0 else 0.0

    trade["status"] = "CLOSED"
    trade["closed_at"] = datetime.now(timezone.utc).isoformat()
    trade["exit_price"] = round_price(exit_price)
    trade["pnl_usd"] = round(pnl_usd, 2)
    trade["pnl_pct"] = round(pnl_pct, 2)
    trade["closed_by"] = user_name
    if extra:
        trade.update(extra)

    save_trades(trades)
    return trade
