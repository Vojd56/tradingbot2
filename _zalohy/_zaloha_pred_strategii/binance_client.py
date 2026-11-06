"""Modul pro integraci s Binance Futures Testnet a aplikaci strategie Jiřího Přibyla.

Obsahuje:
- Autentizaci přes HMAC-SHA256 s BINANCE_API_KEY a BINANCE_SECRET
- Získání zůstatku a otevřených pozic (GET /fapi/v2/account, /fapi/v2/positionRisk)
- Stahování svíčkových dat (OHLCV)
- Detekci SFP (Swing Failure Pattern) dle výzkumného materiálu Jiřího Přibyla (sekce 6.5)
- Odesílání a uzavírání příkazů (POST /fapi/v1/order)
"""

import os
import time
import hmac
import hashlib
import logging
import asyncio
from typing import Dict, Any, List, Optional, Tuple, Union
from urllib.parse import urlencode
import requests

from price_utils import round_price

from config import (
    BINANCE_API_KEY,
    BINANCE_SECRET,
    BINANCE_FUTURES_URL,
    ENABLE_BINANCE,
)

logger = logging.getLogger("TradingBot.Binance")


def is_configured() -> bool:
    """Ověří, zda jsou nastaveny platné klíče pro Binance API."""
    return ENABLE_BINANCE


def _sign_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """Přidá timestamp a vygeneruje HMAC-SHA256 podpis pro Binance API."""
    signed_params = dict(params)
    signed_params["timestamp"] = int(time.time() * 1000)
    query_str = urlencode(signed_params)
    signature = hmac.new(
        BINANCE_SECRET.encode("utf-8"),
        query_str.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    signed_params["signature"] = signature
    return signed_params


def _get_headers() -> Dict[str, str]:
    return {
        "X-MBX-APIKEY": BINANCE_API_KEY,
        "Content-Type": "application/x-www-form-urlencoded"
    }


# Specifikace přesnosti množství (stepSize a quantityPrecision) pro Binance Futures
BINANCE_LOT_SPECS: Dict[str, Dict[str, Any]] = {
    "BTCUSDT": {"precision": 3, "step_size": 0.001, "min_qty": 0.001},
    "ETHUSDT": {"precision": 3, "step_size": 0.001, "min_qty": 0.001},
    "SOLUSDT": {"precision": 2, "step_size": 0.01, "min_qty": 0.01},
    "BNBUSDT": {"precision": 2, "step_size": 0.01, "min_qty": 0.01},
    "LINKUSDT": {"precision": 2, "step_size": 0.01, "min_qty": 0.01},
    "XRPUSDT": {"precision": 1, "step_size": 0.1, "min_qty": 0.1},
    "SUIUSDT": {"precision": 1, "step_size": 0.1, "min_qty": 0.1},
    "DOGEUSDT": {"precision": 0, "step_size": 1.0, "min_qty": 1.0},
    "NEARUSDT": {"precision": 0, "step_size": 1.0, "min_qty": 1.0},
    "AVAXUSDT": {"precision": 0, "step_size": 1.0, "min_qty": 1.0},
}


def format_quantity(symbol: str, raw_qty: float) -> Union[int, float]:
    """Zformátuje množství na přesný počet desetinných míst a stepSize povolený burzou Binance Futures."""
    sym = symbol.upper().strip()
    spec = BINANCE_LOT_SPECS.get(sym)
    if spec:
        prec = spec["precision"]
        min_qty = spec["min_qty"]
        if prec == 0:
            return max(int(min_qty), int(round(raw_qty)))
        else:
            return max(min_qty, round(raw_qty, prec))
    # Fallback pro ostatní coiny
    return max(0.01, round(raw_qty, 2))


_TICK_SIZE_CACHE: Dict[str, float] = {}
_TICK_SIZE_LOADED_AT: float = 0.0


def get_tick_size(symbol: str) -> Optional[float]:
    """Vrátí tickSize (PRICE_FILTER) daného symbolu z /fapi/v1/exchangeInfo. Cache na 6 hodin."""
    global _TICK_SIZE_LOADED_AT
    sym = symbol.upper().strip()
    if not _TICK_SIZE_CACHE or time.time() - _TICK_SIZE_LOADED_AT > 6 * 3600:
        try:
            r = requests.get(f"{BINANCE_FUTURES_URL}/fapi/v1/exchangeInfo", timeout=10)
            if r.status_code == 200:
                for s in r.json().get("symbols", []):
                    for f in s.get("filters", []):
                        if f.get("filterType") == "PRICE_FILTER":
                            _TICK_SIZE_CACHE[s["symbol"]] = float(f["tickSize"])
                _TICK_SIZE_LOADED_AT = time.time()
        except Exception as e:
            logger.error(f"Nepodařilo se načíst exchangeInfo: {e}")
    return _TICK_SIZE_CACHE.get(sym)


def format_price(symbol: str, price: float) -> float:
    """Zaokrouhlí cenu DOLŮ na násobek tickSize burzy (jinak Binance příkaz odmítne chybou -1111/-4014)."""
    from decimal import Decimal, ROUND_DOWN
    tick = get_tick_size(symbol)
    if not tick:
        # Fallback: 6 platných číslic, aby i levné coiny (DOGE ~0.1) měly dost desetinných míst
        return float(f"{price:.6g}")
    d_tick = Decimal(str(tick))
    return float((Decimal(str(price)) / d_tick).to_integral_value(rounding=ROUND_DOWN) * d_tick)


def get_account_info_sync() -> Optional[Dict[str, Any]]:
    """Synchronní načtení informací o účtu na Binance Futures."""
    if not is_configured():
        return None

    url = f"{BINANCE_FUTURES_URL}/fapi/v2/account"
    params = _sign_params({})

    try:
        r = requests.get(url, params=params, headers=_get_headers(), timeout=8)
        if r.status_code == 200:
            return r.json()
        logger.error(f"Binance /fapi/v2/account vrátil {r.status_code}: {r.text}")
        return None
    except Exception as e:
        logger.error(f"Chyba při volání Binance /fapi/v2/account: {e}")
        return None


async def get_account_info() -> Optional[Dict[str, Any]]:
    """Asynchronní obal pro informace o účtu."""
    return await asyncio.to_thread(get_account_info_sync)


def get_open_positions_sync() -> List[Dict[str, Any]]:
    """Vrátí všechny otevřené pozice s nenulovým množstvím."""
    acc = get_account_info_sync()
    if not acc:
        return []
    positions = acc.get("positions", [])
    return [p for p in positions if float(p.get("positionAmt", 0)) != 0]


async def get_open_positions() -> List[Dict[str, Any]]:
    """Asynchronní obal pro otevřené pozice."""
    return await asyncio.to_thread(get_open_positions_sync)


def get_klines_sync(symbol: str, interval: str = "1h", limit: int = 50) -> List[List[Any]]:
    """Stáhne svíčková data (OHLCV) pro daný symbol z Binance Futures."""
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {
        "symbol": symbol.upper(),
        "interval": interval,
        "limit": limit
    }
    try:
        r = requests.get(url, params=params, timeout=8)
        if r.status_code == 200:
            return r.json()
        return []
    except Exception as e:
        logger.error(f"Chyba při stahování klines pro {symbol}: {e}")
        return []


async def get_klines(symbol: str, interval: str = "1h", limit: int = 50) -> List[List[Any]]:
    """Asynchronní obal pro stažení klines."""
    return await asyncio.to_thread(get_klines_sync, symbol, interval, limit)


# =====================================================================
# IMPLEMENTACE ALGORITMU JIŘÍHO PŘIBYLA: SFP (SWING FAILURE PATTERN) - 15M DNO
# =====================================================================

def detect_sfp_pattern_sync(symbol: str = "BTCUSDT") -> Dict[str, Any]:
    """Implementace strategie SFP Bullish Reversal dle výzkumu Jiřího Přibyla na 15M ČASOVÉM RÁMCI (15min dno).

    Pravidla pro 15M daytrading:
    1. Identifikace klíčového 15M Swing Low (15minutové dno s potvrzením pivotu).
    2. Sledování 15M svíčky: Low prorazí pod 15M dno knotem (sweep likvidity pod dnem).
    3. Svíčka však uzavře tělem zpět NAD 15M dnem (rejection / absorpce).
    4. Objem (Volume) je vyšší než 20periodový průměr na 15M svíčkách (nákupní absorpce).
    """
    symbol = symbol.upper()

    # 1. Stažení 15M klines pro nalezení 15min dna (Swing Low) a ověření sweepu
    klines_15m = get_klines_sync(symbol, interval="15m", limit=60)

    if len(klines_15m) < 25:
        return {"pattern_detected": False, "reason": "Nedostatek 15M svíčkových dat"}

    # Nalezení potvrzených 15M Swing Lows (pivot svíčka má nižší minimum než 2 svíčky vlevo a 2 vpravo)
    # Vyloučíme poslední 2 svíčky, které ještě nemusí být plně potvrzeny jako hotový swing
    swing_lows = []
    swing_highs = []

    for i in range(2, len(klines_15m) - 3):
        cur_low = float(klines_15m[i][3])
        prev_low1 = float(klines_15m[i-1][3])
        prev_low2 = float(klines_15m[i-2][3])
        next_low1 = float(klines_15m[i+1][3])
        next_low2 = float(klines_15m[i+2][3])

        if cur_low < prev_low1 and cur_low < prev_low2 and cur_low < next_low1 and cur_low < next_low2:
            swing_lows.append(cur_low)

        cur_high = float(klines_15m[i][2])
        prev_high1 = float(klines_15m[i-1][2])
        prev_high2 = float(klines_15m[i-2][2])
        next_high1 = float(klines_15m[i+1][2])
        next_high2 = float(klines_15m[i+2][2])

        if cur_high > prev_high1 and cur_high > prev_high2 and cur_high > next_high1 and cur_high > next_high2:
            swing_highs.append(cur_high)

    if not swing_lows:
        # Fallback na minimum z předchozích 20 svíček před aktuálními
        swing_low_15m = min(float(k[3]) for k in klines_15m[-25:-2])
    else:
        # Poslední potvrzené 15minutové dno
        swing_low_15m = swing_lows[-1]

    # Analýza aktuální 15M svíčky a předchozí svíčky
    current_15m = klines_15m[-1]
    prev_15m = klines_15m[-2]

    c_open = float(current_15m[1])
    c_high = float(current_15m[2])
    c_low = float(current_15m[3])
    c_close = float(current_15m[4])
    c_volume = float(current_15m[5])

    # 20periodový průměrný objem na 15M
    volumes_20 = [float(k[5]) for k in klines_15m[-21:-1]]
    avg_vol_20 = sum(volumes_20) / len(volumes_20) if volumes_20 else 1.0
    vol_ratio = c_volume / avg_vol_20 if avg_vol_20 > 0 else 1.0

    # Podmínky 15M SFP Bullish Reversal:
    # 1. Cena prorazila pod 15M dno (Low < Swing_Low)
    pierced = c_low < swing_low_15m or float(prev_15m[3]) < swing_low_15m
    # 2. Svíčka uzavřela zpět NAD 15M dnem (Close > Swing_Low)
    closed_above = c_close > swing_low_15m
    # 3. Zvýšený objem (absorpce)
    volume_confirmed = vol_ratio >= 1.15

    sfp_valid = pierced and closed_above and volume_confirmed

    # Stop Loss pod sweep knot + 0.05% buffer
    lowest_sweep = min(c_low, float(prev_15m[3])) if pierced else c_low
    buffer = lowest_sweep * 0.0005
    stop_loss = round_price(lowest_sweep - buffer)
    entry_price = round_price(c_close)

    risk_per_unit = max(entry_price - stop_loss, entry_price * 0.005)
    take_profit_1 = round_price(entry_price + (risk_per_unit * 2.0))  # RRR 1:2

    # Take-Profit 2 na nejbližším 15M Swing High (vrcholu)
    recent_high_15m = swing_highs[-1] if swing_highs else max(float(k[2]) for k in klines_15m[-15:])
    take_profit_2 = round_price(max(recent_high_15m, take_profit_1))

    return {
        "symbol": symbol,
        "pattern_detected": sfp_valid,
        "pattern_name": "SFP (Swing Failure Pattern) 15M Bullish Reversal",
        "timeframe": "15m",
        "entry": entry_price,
        "stop_loss": stop_loss,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "swing_low_15m": round_price(swing_low_15m),
        "swing_low_4h": round_price(swing_low_15m),  # alias pro kompatibilitu
        "candle_low": round_price(c_low),
        "vol_ratio": round(vol_ratio, 2),
        "rrr": f"1:{((take_profit_1 - entry_price) / risk_per_unit):.1f}",
        "methodology": "Jiří Přibyl (15M Intraday SFP Swing Low Sweep)"
    }


async def detect_sfp_pattern(symbol: str = "BTCUSDT") -> Dict[str, Any]:
    """Asynchronní obal pro SFP detekci."""
    return await asyncio.to_thread(detect_sfp_pattern_sync, symbol)


def _signed_request(method: str, path: str, params: Dict[str, Any], timeout: int = 10) -> Tuple[bool, Any]:
    """Podepsaný požadavek na Binance Futures. Vrací (ok, data nebo text chyby)."""
    url = f"{BINANCE_FUTURES_URL}{path}"
    signed_params = _sign_params(params)
    try:
        r = requests.request(method, url, params=signed_params, headers=_get_headers(), timeout=timeout)
        if r.status_code == 200:
            return True, r.json()
        return False, f"HTTP {r.status_code}: {r.text}"
    except Exception as e:
        return False, str(e)


def get_position_amt_sync(symbol: str) -> Optional[float]:
    """Vrátí aktuální velikost pozice na burze (kladná = long). None = nepodařilo se zjistit.

    Rozlišuje chybu (None) od nulové pozice (0.0), aby bot při výpadku API nepovažoval pozici za uzavřenou.
    """
    if not is_configured():
        return None
    ok, data = _signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol.upper()}, timeout=8)
    if not ok or not isinstance(data, list):
        logger.error(f"Binance positionRisk {symbol} selhal: {data}")
        return None
    return sum(float(p.get("positionAmt", 0) or 0) for p in data if p.get("symbol") == symbol.upper())


def get_all_position_amts_sync() -> Optional[Dict[str, float]]:
    """Vrátí {symbol: positionAmt} pro všechny nenulové pozice. None = chyba API."""
    acc = get_account_info_sync()
    if not acc:
        return None
    result: Dict[str, float] = {}
    for p in acc.get("positions", []):
        amt = float(p.get("positionAmt", 0) or 0)
        if amt != 0:
            result[p.get("symbol")] = result.get(p.get("symbol"), 0.0) + amt
    return result


def place_futures_order_sync(
    symbol: str,
    side: str,  # "BUY" nebo "SELL"
    quantity: float,
    order_type: str = "MARKET",
    reduce_only: bool = False
) -> Dict[str, Any]:
    """Odeslání tržní objednávky na Binance Futures a vrácení skutečně vyplněného množství a ceny."""
    if not is_configured():
        return {"success": False, "error": "Binance API klíče nejsou nakonfigurovány"}

    symbol = symbol.upper()

    # Automatická sanitizace množství dle pravidel Binance Futures (precision & stepSize)
    sanitized_qty = format_quantity(symbol, quantity)

    params = {
        "symbol": symbol,
        "side": side.upper(),
        "type": order_type.upper(),
        "quantity": sanitized_qty,
        # RESULT = burza vrátí rovnou stav FILLED, executedQty a avgPrice
        "newOrderRespType": "RESULT",
    }
    if reduce_only:
        params["reduceOnly"] = "true"

    logger.info(f"Odesílám Binance Futures příkaz: {symbol} {side} {sanitized_qty} (reduceOnly={reduce_only})")
    ok, data = _signed_request("POST", "/fapi/v1/order", params)
    if not ok:
        logger.warning(f"⚠️ Binance Futures odmítl příkaz: {data}")
        return {"success": False, "error": data}

    executed_qty = float(data.get("executedQty", 0) or 0)
    avg_price = float(data.get("avgPrice", 0) or 0)
    status = data.get("status", "NEW")
    if executed_qty <= 0:
        # Příkaz přijat, ale nic se nevyplnilo -> nepovažujeme za úspěch
        return {"success": False, "error": f"Příkaz nebyl vyplněn (status {status})", "order_id": data.get("orderId"), "data": data}

    logger.info(f"✅ Binance Futures Order vyplněn! ID: {data.get('orderId')} | {executed_qty} @ {avg_price}")
    return {
        "success": True,
        "order_id": data.get("orderId"),
        "status": status,
        "symbol": symbol,
        "side": side,
        "quantity": executed_qty,
        "avg_price": avg_price,
        "data": data
    }


async def place_futures_order(
    symbol: str,
    side: str,
    quantity: float,
    order_type: str = "MARKET",
    reduce_only: bool = False
) -> Dict[str, Any]:
    """Asynchronní obal pro odeslání objednávky na Binance Futures."""
    return await asyncio.to_thread(place_futures_order_sync, symbol, side, quantity, order_type, reduce_only)


# =====================================================================
# OCHRANNÉ PŘÍKAZY NA BURZE (STOP-LOSS / TAKE-PROFIT)
# =====================================================================

def place_protective_order_sync(symbol: str, kind: str, quantity: float, trigger_price: float) -> Dict[str, Any]:
    """Založí na burze ochranný SELL příkaz pro long pozici bota.

    kind: "SL" (STOP_MARKET) nebo "TP" (TAKE_PROFIT_MARKET).
    Používá reduceOnly s množstvím bota (ne closePosition), takže nikdy nezavře víc, než bot koupil.
    Nejdřív zkusí Algo Order API (Binance od 12/2025 přesouvá podmíněné příkazy tam),
    při nedostupnosti se vrátí ke klasickému /fapi/v1/order.
    """
    if not is_configured():
        return {"success": False, "error": "Binance API není nakonfigurováno"}

    symbol = symbol.upper()
    order_type = "STOP_MARKET" if kind == "SL" else "TAKE_PROFIT_MARKET"
    qty = format_quantity(symbol, quantity)
    price = format_price(symbol, trigger_price)

    algo_params = {
        "algoType": "CONDITIONAL",
        "symbol": symbol,
        "side": "SELL",
        "type": order_type,
        "quantity": qty,
        "triggerPrice": price,
        "reduceOnly": "true",
        "workingType": "MARK_PRICE",
        "priceProtect": "true",
    }
    ok, data = _signed_request("POST", "/fapi/v1/algoOrder", algo_params)
    if ok and data.get("algoId"):
        logger.info(f"🛡️ Binance {kind} založen (algo): {symbol} {qty} @ {price} | algoId {data.get('algoId')}")
        return {"success": True, "ref": {"api": "algo", "id": data.get("algoId"), "trigger": price, "qty": qty}}
    algo_err = data

    legacy_params = {
        "symbol": symbol,
        "side": "SELL",
        "type": order_type,
        "quantity": qty,
        "stopPrice": price,
        "reduceOnly": "true",
        "workingType": "MARK_PRICE",
        "priceProtect": "true",
    }
    ok, data = _signed_request("POST", "/fapi/v1/order", legacy_params)
    if ok and data.get("orderId"):
        logger.info(f"🛡️ Binance {kind} založen (order): {symbol} {qty} @ {price} | orderId {data.get('orderId')}")
        return {"success": True, "ref": {"api": "order", "id": data.get("orderId"), "trigger": price, "qty": qty}}

    err = f"algo: {algo_err} | order: {data}"
    logger.error(f"❌ Binance {kind} pro {symbol} se nepodařilo založit: {err}")
    return {"success": False, "error": err}


def cancel_protective_order_sync(symbol: str, ref: Optional[Dict[str, Any]]) -> bool:
    """Zruší ochranný příkaz bota. Vrací True i v případě, že už na burze neexistuje."""
    if not ref or not ref.get("id"):
        return True
    if ref.get("api") == "algo":
        ok, data = _signed_request("DELETE", "/fapi/v1/algoOrder", {"algoId": ref["id"]})
    else:
        ok, data = _signed_request("DELETE", "/fapi/v1/order", {"symbol": symbol.upper(), "orderId": ref["id"]})
    if ok:
        return True
    text = str(data)
    # -2011 = Unknown order (už vyplněn nebo zrušen) -> nic dalšího rušit nejde
    if "-2011" in text or "Unknown order" in text or "not exist" in text.lower():
        return True
    logger.warning(f"Zrušení ochranného příkazu {ref} pro {symbol} selhalo: {text}")
    return False


def close_bot_position_sync(symbol: str, bot_qty: float) -> Dict[str, Any]:
    """Uzavře POUZE množství, které otevřel bot (reduceOnly MARKET SELL).

    Nikdy neprodá víc než bot_qty ani víc, než je aktuálně v long pozici, a nikdy neotočí pozici do shortu.
    Vrací success jen tehdy, když burza příkaz skutečně vyplnila.
    """
    if not is_configured():
        return {"success": False, "error": "Binance API není nakonfigurováno"}

    symbol = symbol.upper()
    amt = get_position_amt_sync(symbol)
    if amt is None:
        return {"success": False, "error": "Nepodařilo se ověřit pozici na burze"}
    if amt <= 0:
        return {"success": False, "already_flat": True, "error": f"Na burze už není long pozice {symbol}"}

    sell_qty = min(float(bot_qty), amt)
    res = place_futures_order_sync(symbol=symbol, side="SELL", quantity=sell_qty, reduce_only=True)
    if not res.get("success"):
        return res
    res["exit_price"] = res.get("avg_price")
    return res


async def close_bot_position(symbol: str, bot_qty: float) -> Dict[str, Any]:
    """Asynchronní obal pro uzavření pozice bota na Binance Futures."""
    return await asyncio.to_thread(close_bot_position_sync, symbol, bot_qty)
