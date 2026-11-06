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
    extended_hours: bool = False
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
    extended_hours: bool = False
) -> Dict[str, Any]:
    """Asynchronní obal pro odeslání tržního nákupu."""
    return await asyncio.to_thread(place_market_order_sync, symbol, quantity, extended_hours)


def _request(method: str, path: str, json_body: Optional[Dict[str, Any]] = None, timeout: int = 10):
    """Požadavek na T212 API. Vrací (status_code, data_nebo_text); status 0 = výjimka/síť."""
    url = f"{TRADING_212_BASE_URL}{path}"
    try:
        r = requests.request(
            method, url,
            auth=(TRADING_212_API_KEY, TRADING_212_SECRET_KEY),
            json=json_body,
            timeout=timeout
        )
        try:
            data = r.json()
        except Exception:
            data = r.text
        return r.status_code, data
    except Exception as e:
        return 0, str(e)


def get_portfolio_or_none_sync() -> Optional[List[Dict[str, Any]]]:
    """Jako get_portfolio_sync, ale při chybě vrací None (ne prázdný seznam), aby šlo chybu rozlišit od prázdného portfolia."""
    if not is_configured():
        return None
    status, data = _request("GET", "/equity/portfolio", timeout=8)
    if status == 200 and isinstance(data, list):
        return data
    logger.error(f"Trading 212 /portfolio selhal: {status} {data}")
    return None


def get_pending_orders_sync() -> Optional[List[Dict[str, Any]]]:
    """Vrátí všechny čekající příkazy (GET /equity/orders). None = chyba API."""
    if not is_configured():
        return None
    status, data = _request("GET", "/equity/orders", timeout=8)
    if status == 200 and isinstance(data, list):
        return data
    logger.error(f"Trading 212 /orders selhal: {status} {data}")
    return None


def find_portfolio_qty(portfolio: List[Dict[str, Any]], symbol: str) -> float:
    """Vrátí počet kusů daného tickeru v portfoliu (0, pokud tam není)."""
    t212_ticker = to_t212_ticker(symbol)
    for p in portfolio:
        if p.get("ticker") == t212_ticker:
            return float(p.get("quantity", 0) or 0)
    return 0.0


def find_portfolio_price(portfolio: List[Dict[str, Any]], symbol: str) -> Optional[float]:
    """Aktuální cena tickeru podle portfolia T212 (None, pokud tam není)."""
    t212_ticker = to_t212_ticker(symbol)
    for p in portfolio:
        if p.get("ticker") == t212_ticker:
            try:
                price = float(p.get("currentPrice") or 0)
            except (TypeError, ValueError):
                return None
            return price if price > 0 else None
    return None


def place_stop_order_sync(symbol: str, quantity: float, stop_price: float) -> Dict[str, Any]:
    """Založí na T212 prodejní STOP příkaz (Stop-Loss) jen na množství bota.

    Pozn.: T212 API na reálném (live) účtu podporuje jen tržní příkazy; na demo účtu stop příkaz funguje.
    """
    if not is_configured():
        return {"success": False, "error": "Trading 212 API klíče nejsou nakonfigurovány"}
    payload = {
        "ticker": to_t212_ticker(symbol),
        "quantity": -round(abs(quantity), 4),
        "stopPrice": round(stop_price, 2),
        "timeValidity": "GOOD_TILL_CANCEL",
    }
    logger.info(f"Odesílám Stop-Loss příkaz do Trading 212: {payload}")
    status, data = _request("POST", "/equity/orders/stop", payload)
    if status in (200, 201) and isinstance(data, dict) and data.get("id"):
        return {"success": True, "ref": {"api": "t212_stop", "id": data.get("id"), "trigger": payload["stopPrice"], "qty": abs(payload["quantity"])}}
    return {"success": False, "error": f"HTTP {status}: {data}"}


def cancel_order_sync(order_id: Any) -> Dict[str, Any]:
    """Zruší čekající příkaz. Vrací {'success': bool, 'not_found': bool}.

    not_found=True znamená, že příkaz už nečeká (mohl být VYPLNĚN) – volající pak nesmí naslepo prodávat.
    """
    status, data = _request("DELETE", f"/equity/orders/{order_id}")
    if status in (200, 204):
        return {"success": True, "not_found": False}
    if status == 404:
        return {"success": False, "not_found": True}
    return {"success": False, "not_found": False, "error": f"HTTP {status}: {data}"}


def wait_for_fill_sync(order_id: Any, attempts: int = 5, delay_s: float = 2.0) -> bool:
    """Počká, až tržní příkaz zmizí z čekajících. Vrací True, pokud už nečeká.

    Pozor: True NEZNAMENÁ vyplněno. T212 vrací 404 i pro zamítnutý nebo zrušený příkaz,
    proto volající musí vyplnění ověřit v portfoliu (position_manager._open_stock_sync).
    """
    import time as _time
    for _ in range(attempts):
        status, data = _request("GET", f"/equity/orders/{order_id}", timeout=8)
        if status == 404:
            return True
        if status == 200 and isinstance(data, dict) and data.get("status") in ("FILLED",):
            return True
        _time.sleep(delay_s)
    return False


def close_bot_position_sync(symbol: str, bot_qty: float, qty_before: Optional[float] = None) -> Dict[str, Any]:
    """Prodá POUZE množství, které koupil bot. Ruční akcie uživatele ve stejném tickeru zůstanou nedotčené.

    qty_before = kolik kusů bylo v portfoliu před nákupem bota (ruční akcie). Bot nikdy neprodá víc než
    held - qty_before, takže ani při chybné evidenci nesáhne na ruční kusy.
    """
    if not is_configured():
        return {"success": False, "error": "Trading 212 API klíče nejsou nakonfigurovány"}

    portfolio = get_portfolio_or_none_sync()
    if portfolio is None:
        return {"success": False, "error": "Nepodařilo se načíst portfolio Trading 212"}

    held = find_portfolio_qty(portfolio, symbol)
    bot_held = held - float(qty_before or 0.0)
    if held <= 0 or bot_held <= 1e-9:
        return {"success": False, "already_flat": True, "error": f"Kusy bota {symbol} už v portfoliu Trading 212 nejsou"}

    sell_quantity = min(float(bot_qty), bot_held)
    if sell_quantity <= 0:
        return {"success": False, "error": f"Neplatné množství k prodeji: {sell_quantity}"}

    # Prodejní příkaz vyžaduje ZÁPORNOU hodnotu quantity
    payload = {
        "ticker": to_t212_ticker(symbol),
        "quantity": -round(sell_quantity, 4),
        # Jen v řádné seanci: v after-hours je nízká likvidita a široký spread
        "extendedHours": False
    }
    logger.info(f"Odesílám Market Sell (jen množství bota) do Trading 212: {payload} | v portfoliu celkem {held}")
    status, data = _request("POST", "/equity/orders/market", payload)
    if status in (200, 201) and isinstance(data, dict):
        order_id = data.get("id")
        logger.info(f"✅ Prodejní příkaz přijat Trading 212! Order ID: {order_id}")
        fill_price = data.get("fillPrice") or data.get("filledValue") and data.get("filledQuantity") and \
            abs(float(data["filledValue"]) / float(data["filledQuantity"]))
        return {
            "success": True,
            "order_id": order_id,
            "status": data.get("status", "NEW"),
            "ticker": payload["ticker"],
            "quantity": sell_quantity,
            "exit_price": float(fill_price) if fill_price else None,
            "data": data
        }
    err_msg = f"HTTP {status}: {data}"
    logger.error(f"❌ Trading 212 odmítl prodejní příkaz: {err_msg}")
    return {"success": False, "error": err_msg}
