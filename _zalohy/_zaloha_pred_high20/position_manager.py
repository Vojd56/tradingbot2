"""Správa pozic bota na burzách: otevření s ochranným Stop-Lossem, ověřené uzavření a synchronizace.

Zásady (ochrana kapitálu):
1. Pozice se v evidenci (paper_trades.json) označí jako CLOSED až PO potvrzení prodeje na burze.
2. Hned po nákupu se na burze založí Stop-Loss (Binance: STOP_MARKET reduceOnly, T212: STOP příkaz).
   Když se na Binance Stop-Loss založit nepodaří, bot pozici okamžitě zavře – nikdy nenechá pozici bez ochrany.
3. Bot prodává VÝHRADNĚ množství, které sám koupil (bot_qty). Ruční pozice uživatele nikdy nezavírá:
   - T212: prodá max. bot_qty, ruční akcie ve stejném tickeru zůstanou.
   - Binance: neotevře obchod na symbolu, kde už existuje pozice, kterou bot neotevřel (ruční).
   - Obchod bez order ID z burzy (nebyl na burze proveden) se uzavře jen v evidenci, nic se neprodává.
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import binance_client
import trading212
from config import ENABLE_BINANCE, ENABLE_TRADING_212, PARTIAL_TAKE_FRACTION
from paper_trading import close_position, update_trade
from price_utils import round_price
import strategy_rules

logger = logging.getLogger("TradingBot.PositionManager")

EPS = 1e-9
T212_REQUEST_GAP_S = 2.1  # T212 omezuje zadávání příkazů zhruba na 1 požadavek za 2 s


def _is_crypto_trade(trade: Dict[str, Any]) -> bool:
    return trade.get("market_type") == "CRYPTO" or str(trade.get("ticker", "")).upper().endswith("USDT")


def _bot_qty(trade: Dict[str, Any]) -> float:
    try:
        return float(trade.get("bot_qty", trade.get("shares", 0)) or 0)
    except (TypeError, ValueError):
        return 0.0


def _executed_on_exchange(trade: Dict[str, Any]) -> bool:
    """True, pokud byl obchod skutečně proveden na burze (má order ID)."""
    if _is_crypto_trade(trade):
        return bool(trade.get("binance_order_id"))
    return bool(trade.get("t212_order_id"))


# =====================================================================
# OTEVŘENÍ POZICE
# =====================================================================

def _open_crypto_sync(ticker: str, qty: float, sl: float, tp: float) -> Dict[str, Any]:
    warnings: List[str] = []

    amt = binance_client.get_position_amt_sync(ticker)
    if amt is None:
        return {"success": False, "error": "Nepodařilo se ověřit stav pozice na Binance – nákup zrušen"}
    if abs(amt) > EPS:
        return {"success": False, "error": f"Na {ticker} už je na Binance pozice ({amt}), kterou neotevřel bot – nesahám na ni"}

    buy = binance_client.place_futures_order_sync(symbol=ticker, side="BUY", quantity=qty)
    if not buy.get("success"):
        return {"success": False, "error": buy.get("error", "Nákup selhal"), "exchange": buy}

    fill_qty = float(buy["quantity"])
    fill_price = float(buy.get("avg_price") or 0)

    if fill_price > 0 and sl >= fill_price:
        # Vyplněno pod stopem = setup je zneplatněný. Posunout SL na −2 % by tiše zvětšilo riziko
        # (velikost pozice byla spočítaná na původní stop), proto se pozice hned zavře.
        close_res = binance_client.close_bot_position_sync(ticker, fill_qty)
        return {
            "success": False,
            "error": f"Vyplněno za {fill_price}, tedy pod Stop-Lossem {sl} – pozice "
                     f"{'okamžitě uzavřena' if close_res.get('success') else 'NEUZAVŘENA – ZKONTROLUJ BURZU RUČNĚ'}",
            "emergency_close": close_res,
            "exchange": buy,
        }
    if fill_price > 0 and tp <= fill_price:
        new_tp = fill_price + 2 * (fill_price - sl)
        warnings.append(f"TP {tp} nebyl nad vstupní cenou, upraven na {new_tp:.6g} (RRR 1:2)")
        tp = new_tp

    sl_res = binance_client.place_protective_order_sync(ticker, "SL", fill_qty, sl)
    if not sl_res.get("success"):
        # Pozice bez Stop-Lossu na burze je nepřijatelná -> okamžitě zavřít
        close_res = binance_client.close_bot_position_sync(ticker, fill_qty)
        return {
            "success": False,
            "error": f"Stop-Loss se nepodařilo založit ({sl_res.get('error')}). "
                     f"Pozice byla {'okamžitě uzavřena' if close_res.get('success') else 'NEUZAVŘENA – ZKONTROLUJ BURZU RUČNĚ'}",
            "emergency_close": close_res,
            "exchange": buy,
        }

    tp_res = binance_client.place_protective_order_sync(ticker, "TP", fill_qty, tp)
    if not tp_res.get("success"):
        warnings.append("Take-Profit se na burze nepodařilo založit, bot ho hlídá softwarově")

    return {
        "success": True,
        "exchange": buy,
        "order_id": str(buy.get("order_id", "")),
        "status": buy.get("status"),
        "fill_qty": fill_qty,
        "fill_price": fill_price or None,
        "stop_loss": sl,
        "take_profit": tp,
        "warnings": warnings,
        "extra": {
            "opened_by_bot": True,
            "bot_qty": fill_qty,
            "sl_order": sl_res.get("ref"),
            "tp_order": tp_res.get("ref") if tp_res.get("success") else None,
        },
    }


def _check_stock_levels(price: float, sl: float, tp: float, warnings: List[str]) -> tuple:
    """SL musí ležet pod aktuální cenou a TP nad ní, jinak by se stop na T212 spustil hned po nákupu."""
    if price and price > 0 and sl >= price * 0.999:
        new_sl = round_price(price * 0.98)
        warnings.append(f"SL {sl} nebyl pod aktuální cenou {price}, upraven na {new_sl} (−2 %)")
        sl = new_sl
    if price and price > 0 and tp <= price:
        new_tp = round_price(price + 2 * (price - sl))
        warnings.append(f"TP {tp} nebyl nad aktuální cenou, upraven na {new_tp} (RRR 1:2)")
        tp = new_tp
    return sl, tp


def _open_stock_sync(ticker: str, qty: float, sl: float, tp: float) -> Dict[str, Any]:
    warnings: List[str] = []

    portfolio = trading212.get_portfolio_or_none_sync()
    if portfolio is None:
        return {"success": False, "error": "Nepodařilo se načíst portfolio Trading 212 – nákup zrušen"}
    qty_before = trading212.find_portfolio_qty(portfolio, ticker)

    buy = trading212.place_market_order_sync(symbol=ticker, quantity=qty)
    if not buy.get("success"):
        return {"success": False, "error": buy.get("error", "Nákup selhal"), "exchange": buy}

    order_id = buy.get("order_id")
    not_pending = trading212.wait_for_fill_sync(order_id)

    fill_qty = qty
    ref_price = None
    filled = False
    if not_pending:
        # Příkaz už nečeká, ale mohl být i ZAMÍTNUT (T212 vrací 404 v obou případech).
        # Za vyplněný ho bereme jen tehdy, když v portfoliu opravdu přibyly kusy.
        time.sleep(T212_REQUEST_GAP_S)
        after = trading212.get_portfolio_or_none_sync()
        if after is None:
            warnings.append("Portfolio po nákupu se nepodařilo načíst, vyplnění ověří další průchod smyčky")
            not_pending = False
        else:
            added = trading212.find_portfolio_qty(after, ticker) - qty_before
            if added <= EPS:
                return {"success": False, "error": "T212 nákup nebyl vyplněn (příkaz zamítnut nebo zrušen) – nic se nezapisuje",
                        "exchange": buy}
            fill_qty = min(qty, added)
            if fill_qty < qty - EPS:
                warnings.append(f"T212 vyplnil jen {fill_qty} z {qty} ks")
            ref_price = trading212.find_portfolio_price(after, ticker)
            filled = True

    if ref_price:
        sl, tp = _check_stock_levels(ref_price, sl, tp, warnings)

    sl_ref = None
    if filled:
        time.sleep(T212_REQUEST_GAP_S)
        sl_res = trading212.place_stop_order_sync(ticker, fill_qty, sl)
        if sl_res.get("success"):
            sl_ref = sl_res["ref"]
        else:
            warnings.append(f"Stop-Loss na T212 se nepodařilo založit ({sl_res.get('error')}), bot ho hlídá softwarově a zkusí znovu")
    else:
        warnings.append("Nákup na T212 zatím čeká na vyplnění, Stop-Loss se založí po vyplnění")

    return {
        "success": True,
        "exchange": buy,
        "order_id": str(order_id or ""),
        "status": buy.get("status"),
        "fill_qty": fill_qty,
        "fill_price": ref_price,
        "stop_loss": sl,
        "take_profit": tp,
        "warnings": warnings,
        "extra": {
            "opened_by_bot": True,
            "bot_qty": fill_qty,
            "t212_qty_before": qty_before,
            "t212_buy_pending": not filled,
            "sl_order": sl_ref,
            "tp_order": None,  # T212 nepodporuje OCO; TP hlídá bot softwarově
        },
    }


async def open_bot_position(ticker: str, is_crypto: bool, qty: float, sl: float, tp: float) -> Dict[str, Any]:
    """Nakoupí na burze a hned založí ochranný Stop-Loss. Vrací success=False, pokud se nemá nic zapsat jako OPEN."""
    if is_crypto:
        return await asyncio.to_thread(_open_crypto_sync, ticker, qty, sl, tp)
    return await asyncio.to_thread(_open_stock_sync, ticker, qty, sl, tp)


# =====================================================================
# UZAVŘENÍ POZICE (jen množství bota, s ověřením výsledku)
# =====================================================================

def _close_trade_sync(trade: Dict[str, Any], current_price: float, reason: str) -> Dict[str, Any]:
    ticker = trade["ticker"]
    trade_id = trade["trade_id"]
    is_crypto = _is_crypto_trade(trade)
    enabled = ENABLE_BINANCE if is_crypto else ENABLE_TRADING_212

    # Obchod, který nikdy nebyl na burze proveden -> jen evidence, nic neprodávat
    if not enabled or not _executed_on_exchange(trade):
        closed = close_position(ticker, current_price, reason, trade_id=trade_id,
                                extra={"exit_reason": reason, "exchange_close": "jen evidence (obchod nebyl na burze)"})
        return {"success": True, "trade": closed, "paper_only": True}

    qty = _bot_qty(trade)

    if is_crypto:
        res = binance_client.close_bot_position_sync(ticker, qty)
        if res.get("success") or res.get("already_flat"):
            binance_client.cancel_protective_order_sync(ticker, trade.get("sl_order"))
            binance_client.cancel_protective_order_sync(ticker, trade.get("tp_order"))
            exit_price = res.get("exit_price") or current_price
            note = f"Binance order #{res.get('order_id')}" if res.get("success") else "na burze už pozice nebyla"
            closed = close_position(ticker, exit_price, reason, trade_id=trade_id,
                                    extra={"exit_reason": reason, "exchange_close": note,
                                           "exit_price_approx": not res.get("exit_price")})
            return {"success": True, "trade": closed, "exchange": res}
        update_trade(trade_id, {"close_error": res.get("error"), "close_attempts": int(trade.get("close_attempts", 0)) + 1})
        return {"success": False, "error": res.get("error"), "exchange": res}

    # --- Trading 212 ---
    sl_ref = trade.get("sl_order")
    stop_cancelled = False
    if sl_ref and sl_ref.get("id"):
        c = trading212.cancel_order_sync(sl_ref["id"])
        if c.get("success"):
            stop_cancelled = True
            time.sleep(T212_REQUEST_GAP_S)
        elif c.get("not_found"):
            # Stop už nečeká – mohl se vyplnit. Ověříme v portfoliu, ať neprodáme ruční akcie.
            if _t212_stop_was_filled(trade):
                closed = close_position(ticker, float(sl_ref.get("trigger") or current_price), "Stop-Loss na T212", trade_id=trade_id,
                                        extra={"exit_reason": "STOP-LOSS (příkaz na burze)", "exit_price_approx": True})
                return {"success": True, "trade": closed, "closed_by_exchange": True}
        else:
            update_trade(trade_id, {"close_error": c.get("error")})
            return {"success": False, "error": f"Nepodařilo se zrušit Stop-Loss příkaz: {c.get('error')}"}

    res = trading212.close_bot_position_sync(ticker, qty, trade.get("t212_qty_before"))
    if res.get("success") or res.get("already_flat"):
        exit_price = res.get("exit_price") or current_price
        note = f"T212 order #{res.get('order_id')}" if res.get("success") else "v portfoliu už nebyla"
        closed = close_position(ticker, exit_price, reason, trade_id=trade_id,
                                extra={"exit_reason": reason, "exchange_close": note,
                                       "exit_price_approx": not res.get("exit_price")})
        return {"success": True, "trade": closed, "exchange": res}

    # Prodej selhal -> vrátit Stop-Loss zpět, ať pozice nezůstane bez ochrany
    updates: Dict[str, Any] = {"close_error": res.get("error"), "close_attempts": int(trade.get("close_attempts", 0)) + 1}
    if stop_cancelled:
        time.sleep(T212_REQUEST_GAP_S)
        sl_res = trading212.place_stop_order_sync(ticker, qty, float(trade.get("stop_loss", 0)))
        updates["sl_order"] = sl_res.get("ref") if sl_res.get("success") else None
    update_trade(trade_id, updates)
    return {"success": False, "error": res.get("error"), "exchange": res}


async def close_bot_trade(trade: Dict[str, Any], current_price: float, reason: str) -> Dict[str, Any]:
    """Uzavře pozici bota na burze a teprve po potvrzení ji označí v evidenci jako CLOSED."""
    return await asyncio.to_thread(_close_trade_sync, trade, current_price, reason)


def _t212_stop_was_filled(trade: Dict[str, Any], portfolio: Optional[List[Dict[str, Any]]] = None) -> bool:
    """Odhadne, zda Stop-Loss bota na T212 proběhl: kusy bota z portfolia zmizely."""
    if portfolio is None:
        portfolio = trading212.get_portfolio_or_none_sync()
    if portfolio is None:
        return False
    held = trading212.find_portfolio_qty(portfolio, trade["ticker"])
    before = trade.get("t212_qty_before")
    if before is None:
        return held <= EPS
    return held <= float(before) + EPS


# =====================================================================
# SYNCHRONIZACE S BURZOU (volá se v každém průchodu smyčky)
# =====================================================================

def fetch_snapshot_sync(open_trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Jedním dotazem na burzu načte stav všech pozic (šetří rate limity)."""
    snap: Dict[str, Any] = {"binance": None, "t212_portfolio": None, "t212_pending": None}
    if ENABLE_BINANCE and any(_is_crypto_trade(t) and _executed_on_exchange(t) for t in open_trades):
        snap["binance"] = binance_client.get_all_position_amts_sync()
    if ENABLE_TRADING_212 and any(not _is_crypto_trade(t) and _executed_on_exchange(t) for t in open_trades):
        snap["t212_portfolio"] = trading212.get_portfolio_or_none_sync()
        snap["t212_pending"] = trading212.get_pending_orders_sync()
    return snap


async def fetch_snapshot(open_trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    return await asyncio.to_thread(fetch_snapshot_sync, open_trades)


def _sync_crypto(trade: Dict[str, Any], price: float, snap: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ticker = trade["ticker"]
    amts = snap.get("binance")
    if amts is None:
        return None  # stav burzy neznámý -> nic nedělat
    amt = float(amts.get(ticker, 0.0))
    entry = float(trade.get("entry", price))

    if amt <= EPS:
        # Pozici zavřel Stop-Loss / Take-Profit příkaz na burze (nebo likvidace / ruční zásah)
        binance_client.cancel_protective_order_sync(ticker, trade.get("sl_order"))
        binance_client.cancel_protective_order_sync(ticker, trade.get("tp_order"))
        sl = float(trade.get("stop_loss") or 0.0)
        tp = float(trade.get("take_profit") or 0.0)
        # Po částečném zisku leží SL nad entry, proto rozhoduje, ke které úrovni má cena blíž
        hit_sl = abs(price - sl) <= abs(price - tp) if sl > 0 and tp > 0 else price < entry
        ref = trade.get("sl_order") if hit_sl else trade.get("tp_order")
        exit_price = float(ref.get("trigger")) if ref and ref.get("trigger") else price
        reason = "STOP-LOSS (příkaz na burze)" if hit_sl else "TAKE-PROFIT (příkaz na burze)"
        closed = close_position(ticker, exit_price, reason, trade_id=trade["trade_id"],
                                extra={"exit_reason": reason, "exit_price_approx": True})
        return {"type": "closed", "reason": reason, "trade": closed, "hit_sl": hit_sl}

    qty = min(_bot_qty(trade), amt)
    if not trade.get("sl_order"):
        sl_res = binance_client.place_protective_order_sync(ticker, "SL", qty, float(trade.get("stop_loss", 0)))
        if sl_res.get("success"):
            update_trade(trade["trade_id"], {"sl_order": sl_res["ref"]})
            trade["sl_order"] = sl_res["ref"]
    return None


def _sync_stock(trade: Dict[str, Any], price: float, snap: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ticker = trade["ticker"]
    portfolio = snap.get("t212_portfolio")
    pending = snap.get("t212_pending")
    if portfolio is None or pending is None:
        return None
    pending_ids = {str(o.get("id")) for o in pending}
    updates: Dict[str, Any] = {}

    held = trading212.find_portfolio_qty(portfolio, ticker)

    if trade.get("t212_buy_pending"):
        if str(trade.get("t212_order_id")) in pending_ids:
            return None  # nákup ještě není vyplněn
        before = trade.get("t212_qty_before")
        added = held - float(before or 0.0)
        if before is not None and added <= EPS:
            # Příkaz už nečeká, ale kusy nepřibyly -> T212 nákup zamítl/zrušil. Nic neprodávat, jen uzavřít evidenci.
            entry = float(trade.get("entry") or price)
            closed = close_position(ticker, entry, "Nákup na T212 nebyl vyplněn", trade_id=trade["trade_id"],
                                    extra={"exit_reason": "NÁKUP NEVYPLNĚN (T212)", "t212_buy_pending": False,
                                           "exchange_close": "jen evidence (nákup nebyl vyplněn)"})
            return {"type": "closed", "reason": "NÁKUP NA T212 NEBYL VYPLNĚN", "trade": closed, "hit_sl": False}
        updates["t212_buy_pending"] = False
        trade["t212_buy_pending"] = False
        if before is not None and added < _bot_qty(trade) - EPS:
            updates["bot_qty"] = added
            updates["shares"] = added
            trade["bot_qty"] = added
            trade["shares"] = added

    if trade.get("t212_qty_before") is None:
        # Starší obchod bez snímku: předpokládáme, že kusy bota jsou stále v portfoliu
        trade["t212_qty_before"] = max(0.0, held - _bot_qty(trade))
        updates["t212_qty_before"] = trade["t212_qty_before"]

    sl_ref = trade.get("sl_order")
    if sl_ref and str(sl_ref.get("id")) not in pending_ids:
        if _t212_stop_was_filled(trade, portfolio):
            closed = close_position(ticker, float(sl_ref.get("trigger") or price), "Stop-Loss na T212", trade_id=trade["trade_id"],
                                    extra={"exit_reason": "STOP-LOSS (příkaz na burze)", "exit_price_approx": True, **updates})
            return {"type": "closed", "reason": "STOP-LOSS (příkaz na burze)", "trade": closed, "hit_sl": True}
        # Stop zmizel, ale kusy bota tu jsou (zrušen/expirován) -> založit znovu
        sl_ref = None
        trade["sl_order"] = None
        updates["sl_order"] = None

    if not sl_ref and held > float(trade.get("t212_qty_before") or 0) + EPS:
        qty = min(_bot_qty(trade), held - float(trade.get("t212_qty_before") or 0))
        time.sleep(T212_REQUEST_GAP_S)
        sl_res = trading212.place_stop_order_sync(ticker, qty, float(trade.get("stop_loss", 0)))
        if sl_res.get("success"):
            updates["sl_order"] = sl_res["ref"]
            trade["sl_order"] = sl_res["ref"]

    if updates:
        update_trade(trade["trade_id"], updates)
    return None


def sync_trade_sync(trade: Dict[str, Any], price: float, snap: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Zjistí, zda burza pozici už nezavřela, a doplní chybějící Stop-Loss. Vrací událost nebo None."""
    if not _executed_on_exchange(trade):
        return None
    if _is_crypto_trade(trade):
        return _sync_crypto(trade, price, snap) if ENABLE_BINANCE else None
    return _sync_stock(trade, price, snap) if ENABLE_TRADING_212 else None


async def sync_trade(trade: Dict[str, Any], price: float, snap: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return await asyncio.to_thread(sync_trade_sync, trade, price, snap)


def needs_software_exit(trade: Dict[str, Any], price: float) -> Optional[str]:
    """Softwarová záloha: vrátí důvod výstupu, pokud cena zasáhla SL/TP a odpovídající příkaz na burze neleží."""
    sl = float(trade.get("stop_loss", 0.0) or 0.0)
    tp = float(trade.get("take_profit", 0.0) or 0.0)
    if sl > 0 and price <= sl and not trade.get("sl_order"):
        return "STOP-LOSS ZASAŽEN"
    if tp > 0 and price >= tp and not trade.get("tp_order"):
        return "TAKE-PROFIT DOSAŽEN"
    return None


# =====================================================================
# SPRÁVA OTEVŘENÉ SFP POZICE: polovina při +1R, stop zbytku na nulu, časový stop
# =====================================================================

def _take_partial_sync(trade: Dict[str, Any], price: float) -> Dict[str, Any]:
    """Prodá část pozice při +1R a Stop-Loss zbytku posune na entry + poplatky.

    Pozice nikdy nezůstane bez Stop-Lossu: když se nový SL na burze nepodaří založit, zbytek se hned zavře.
    """
    ticker = trade["ticker"]
    trade_id = trade["trade_id"]
    entry = float(trade.get("entry") or price)
    qty = _bot_qty(trade)
    tp = float(trade.get("take_profit") or 0.0)
    new_sl = strategy_rules.breakeven_price(entry)
    if new_sl >= price:
        return {"success": False, "error": "Cena je pod úrovní stopu na nule, posun odložen"}

    base = {"partial_taken": True, "initial_stop_loss": trade.get("initial_stop_loss") or trade.get("stop_loss"),
            "stop_loss": new_sl}

    on_exchange = _is_crypto_trade(trade) and ENABLE_BINANCE and _executed_on_exchange(trade)
    if not on_exchange:
        # Jen evidence (paper): spočítat částečný zisk bez burzy
        part = qty * PARTIAL_TAKE_FRACTION
        updates = {**base, "partial_qty": part, "partial_exit_price": round_price(price),
                   "partial_pnl_usd": round((price - entry) * part, 2), "bot_qty": qty - part, "shares": qty - part}
        update_trade(trade_id, updates)
        return {"success": True, "updates": updates, "paper_only": True}

    part = float(binance_client.format_quantity(ticker, qty * PARTIAL_TAKE_FRACTION))
    if part <= 0 or part >= qty:
        part = 0.0  # pozici nejde rozdělit (minimální množství) -> jen posun stopu

    exit_price = price
    if part > 0:
        res = binance_client.close_bot_position_sync(ticker, part)
        if not res.get("success"):
            return {"success": False, "error": res.get("error", "Částečný prodej selhal")}
        part = float(res.get("quantity") or part)
        exit_price = float(res.get("exit_price") or price)

    remaining = max(qty - part, 0.0)
    binance_client.cancel_protective_order_sync(ticker, trade.get("sl_order"))
    binance_client.cancel_protective_order_sync(ticker, trade.get("tp_order"))

    sl_res = binance_client.place_protective_order_sync(ticker, "SL", remaining, new_sl)
    if not sl_res.get("success"):
        close_res = binance_client.close_bot_position_sync(ticker, remaining)
        update_trade(trade_id, {**base, "partial_qty": part, "partial_exit_price": round_price(exit_price),
                                "partial_pnl_usd": round((exit_price - entry) * part, 2),
                                "bot_qty": remaining, "shares": remaining, "sl_order": None, "tp_order": None})
        return {"success": False, "emergency_close": close_res,
                "error": f"Nový Stop-Loss se nepodařilo založit ({sl_res.get('error')}), zbytek "
                         f"{'zavřen' if close_res.get('success') else 'NEZAVŘEN – ZKONTROLUJ BURZU'}"}
    tp_res = binance_client.place_protective_order_sync(ticker, "TP", remaining, tp) if tp > 0 else {}

    updates = {**base, "partial_qty": part, "partial_exit_price": round_price(exit_price),
               "partial_pnl_usd": round((exit_price - entry) * part, 2),
               "bot_qty": remaining, "shares": remaining,
               "sl_order": sl_res.get("ref"), "tp_order": tp_res.get("ref") if tp_res.get("success") else None}
    update_trade(trade_id, updates)
    trade.update(updates)
    return {"success": True, "updates": updates}


def manage_open_trade_sync(trade: Dict[str, Any], price: float) -> Optional[Dict[str, Any]]:
    """Správa SFP pozice v každém průchodu smyčky. Vrací událost pro Discord nebo None.

    {"type": "exit", "reason": ...}  -> volající pozici zavře přes close_bot_trade
    {"type": "partial", ...}         -> polovina prodána, stop zbytku na nule
    """
    action = strategy_rules.management_action(trade, price)
    if action == "TIME_STOP":
        return {"type": "exit", "reason": "ČASOVÝ STOP (SFP do 6 h nedošel na cíl)"}
    if action == "PARTIAL":
        res = _take_partial_sync(trade, price)
        if res.get("success"):
            return {"type": "partial", "result": res}
        logger.warning(f"⚠️ {trade.get('ticker')}: částečný zisk neproveden: {res.get('error')}")
        return {"type": "partial_failed", "result": res}
    return None


async def manage_open_trade(trade: Dict[str, Any], price: float) -> Optional[Dict[str, Any]]:
    return await asyncio.to_thread(manage_open_trade_sync, trade, price)
