"""Modul pro integraci s Trading 212 API (Equity Invest/Demo).

Zajišťuje:
- Autentizaci přes HTTP Basic Auth (API Key + Secret Key)
- Získání informací o účtu a zůstatku (GET /equity/account/summary)
- Získání otevřených pozic (GET /equity/portfolio)
- Odesílání tržních nákupních příkazů (POST /equity/orders/market)
- Odesílání prodejních příkazů pro uzavření pozice (POST /equity/orders/market se zápornou quantity)
"""

import logging
import asyncio
from typing import Optional, Dict, Any, List
import requests

from config import (
    TRADING_212_API_KEY,
    TRADING_212_SECRET_KEY,
    TRADING_212_BASE_URL,
    TRADING_212_ENV,
    ENABLE_TRADING_212,
)

logger = logging.getLogger("TradingBot.Trading212")


def is_configured() -> bool:
    """Ověří, zda jsou nastaveny platné klíče pro Trading 212."""
    return ENABLE_TRADING_212


def to_t212_ticker(symbol: str) -> str:
    """Převede obecný burzovní symbol (např. NVDA) na formát Trading 212 (NVDA_US_EQ)."""
    clean_sym = symbol.upper().strip()
    if clean_sym.endswith("_US_EQ") or clean_sym.endswith("_EQ"):
        return clean_sym
    return f"{clean_sym}_US_EQ"


def get_account_summary_sync() -> Optional[Dict[str, Any]]:
    """Synchronní načtení souhrnu účtu (hotovost, portfolio, měna)."""
    if not is_configured():
        return None

    url = f"{TRADING_212_BASE_URL}/equity/account/summary"
    try:
        r = requests.get(
            url,
            auth=(TRADING_212_API_KEY, TRADING_212_SECRET_KEY),
            timeout=8
        )
        if r.status_code == 200:
            return r.json()
        logger.error(f"Trading 212 /account/summary vrátil kód {r.status_code}: {r.text}")
        return None
    except Exception as e:
        logger.error(f"Chyba při volání Trading 212 /account/summary: {e}")
        return None


async def get_account_summary() -> Optional[Dict[str, Any]]:
    """Asynchronní obal pro načtení souhrnu účtu."""
    return await asyncio.to_thread(get_account_summary_sync)


def get_portfolio_sync() -> List[Dict[str, Any]]:
    """Synchronní načtení otevřených pozic z Trading 212."""
    if not is_configured():
        return []

    url = f"{TRADING_212_BASE_URL}/equity/portfolio"
    try:
        r = requests.get(
            url,
            auth=(TRADING_212_API_KEY, TRADING_212_SECRET_KEY),
            timeout=8
        )
        if r.status_code == 200:
            return r.json()
        logger.error(f"Trading 212 /portfolio vrátil kód {r.status_code}: {r.text}")
        return []
    except Exception as e:
        logger.error(f"Chyba při volání Trading 212 /portfolio: {e}")
        return []


async def get_portfolio() -> List[Dict[str, Any]]:
    """Asynchronní obal pro načtení otevřených pozic."""
    return await asyncio.to_thread(get_portfolio_sync)


def place_market_order_sync(
    symbol: str,
    quantity: float,
    extended_hours: bool = True
) -> Dict[str, Any]:
    """Odeslání nákupního tržního příkazu do Trading 212."""
    if not is_configured():
        return {"success": False, "error": "Trading 212 API klíče nejsou nakonfigurovány"}

    t212_ticker = to_t212_ticker(symbol)
    url = f"{TRADING_212_BASE_URL}/equity/orders/market"

    payload = {
        "ticker": t212_ticker,
        "quantity": round(quantity, 4),
        "extendedHours": extended_hours
    }

    try:
        logger.info(f"Odesílám Market Buy příkaz do Trading 212 ({TRADING_212_ENV}): {payload}")
        r = requests.post(
            url,
            auth=(TRADING_212_API_KEY, TRADING_212_SECRET_KEY),
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10
        )

        if r.status_code in [200, 201]:
            resp_data = r.json()
            order_id = resp_data.get("id")
            logger.info(f"✅ Příkaz úspěšně zadán do Trading 212! Order ID: {order_id}")
            return {
                "success": True,
                "order_id": order_id,
                "status": resp_data.get("status", "NEW"),
                "ticker": t212_ticker,
                "quantity": quantity,
                "data": resp_data
            }
        else:
            err_msg = f"HTTP {r.status_code}: {r.text}"
            logger.error(f"❌ Trading 212 odmítl příkaz: {err_msg}")
            return {"success": False, "error": err_msg}

    except Exception as e:
        logger.error(f"Výjimka při odesílání příkazu do Trading 212: {e}")
        return {"success": False, "error": str(e)}


async def place_market_order(
    symbol: str,
    quantity: float,
    extended_hours: bool = True
) -> Dict[str, Any]:
    """Asynchronní obal pro odeslání tržního nákupu."""
    return await asyncio.to_thread(place_market_order_sync, symbol, quantity, extended_hours)


def close_position_sync(symbol: str, quantity: Optional[float] = None) -> Dict[str, Any]:
    """Uzavření pozice pro daný ticker odesláním záporné quantity."""
    if not is_configured():
        return {"success": False, "error": "Trading 212 API klíče nejsou nakonfigurovány"}

    t212_ticker = to_t212_ticker(symbol)
    portfolio = get_portfolio_sync()

    target_pos = None
    for p in portfolio:
        if p.get("ticker") == t212_ticker or p.get("ticker", "").startswith(f"{symbol.upper()}_"):
            target_pos = p
            break

    if not target_pos:
        return {"success": False, "error": f"Ticker {t212_ticker} nebyl nalezen v otevřených pozicích Trading 212"}

    pos_quantity = float(target_pos.get("quantity", 0))
    sell_quantity = quantity if quantity is not None else pos_quantity

    if sell_quantity <= 0:
        return {"success": False, "error": f"Neplatné množství k prodeji: {sell_quantity}"}

    # Prodejní příkaz vyžaduje ZÁPORNOU hodnotu quantity
    url = f"{TRADING_212_BASE_URL}/equity/orders/market"
    payload = {
        "ticker": target_pos.get("ticker", t212_ticker),
        "quantity": -round(sell_quantity, 4),
        "extendedHours": True
    }

    try:
        logger.info(f"Odesílám Market Sell (Close) do Trading 212: {payload}")
        r = requests.post(
            url,
            auth=(TRADING_212_API_KEY, TRADING_212_SECRET_KEY),
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10
        )

        if r.status_code in [200, 201]:
            resp_data = r.json()
            order_id = resp_data.get("id")
            logger.info(f"✅ Prodejní příkaz úspěšně zadán do Trading 212! Order ID: {order_id}")
            return {
                "success": True,
                "order_id": order_id,
                "status": resp_data.get("status", "NEW"),
                "ticker": t212_ticker,
                "quantity": sell_quantity,
                "data": resp_data
            }
        else:
            err_msg = f"HTTP {r.status_code}: {r.text}"
            logger.error(f"❌ Trading 212 odmítl prodejní příkaz: {err_msg}")
            return {"success": False, "error": err_msg}

    except Exception as e:
        logger.error(f"Výjimka při prodeji v Trading 212: {e}")
        return {"success": False, "error": str(e)}


async def close_position_t212(symbol: str, quantity: Optional[float] = None) -> Dict[str, Any]:
    """Asynchronní obal pro uzavření pozice."""
    return await asyncio.to_thread(close_position_sync, symbol, quantity)
