"""Swing stopa A naživo na Trading 212 DEMO (2. 11. 2026). Stopy B a C a virtuální A zůstávají ve stínu (swing_shadow).

Vlastnictví: swing pozice jsou jen v swing_live_state.json (id příkazů T212 + množství). Prodává se jen vlastní
množství (nikdy víc než drženo − intradenní kusy bota), ruční pozice uživatele ani intradenní obchody se nikdy neprodají.
Intradenní modul swing tickery nekupuje a swing pozice nezavírá (nejsou v paper_trades.json).

Průběh:
- ráno (swing_shadow.run_morning): schválení Gemini stopy A -> plan_entries (PLANNED na dnešek)
- 09:45-15:30 New York: limitní nákup DAY (cena + 0,3 %), velikost z reálného účtu (0,5 % riziko vč. nákladů,
  strop 20 %, 5 pozic, 1 na sektor, riziko 3 %), globální limity bota (počet akcií, denní ztráta, vypínač)
- po vyplnění hned Stop-Loss na burze; v seanci polovina na +2R (ne v den vstupu); naplánované výstupy
  (časový stop, Gemini SELL) další obchodní den po otevření; večer stop jen nahoru (BE, chandelier, TIGHTEN)
- každý průchod: sesouhlasení stavu s portfoliem a příkazy T212; nesoulad = log + Discord + žádné nové nákupy

Všechno synchronní (síť), swing_shadow volá přes asyncio.to_thread. Broker jde v testech podstrčit.
"""

import json
import logging
import math
import threading
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import cost_model
import market_hours
import swing_rules as R
from config import (BASE_DIR, SWING_LIVE_ENABLED, SWING_LIVE_LIMIT_SLIPPAGE_PCT, TRADING_212_ENV, TRADING_212_BASE_URL,
                    MAX_OPEN_POSITIONS_STOCKS, SWING_REVIEW_EVERY_DAYS, SWING_MAX_OPEN_RISK_PCT)

logger = logging.getLogger("TradingBot.SwingLive")

STATE_FILE = BASE_DIR / "swing_live_state.json"
EPS = 1e-6
QTY_STEP = 0.01
ENTRY_FROM_MIN = 15          # nákup od 09:45 New York
ENTRY_UNTIL_BEFORE_CLOSE_MIN = 30
EXIT_FROM_MIN = 5            # naplánované výstupy od 09:35 New York
LIVE_STATUSES = ("PLANNED", "ORDERED", "OPEN")
_lock = threading.Lock()


class NotDemo(RuntimeError):
    pass


def demo_guard(env: str = None, base_url: str = None) -> Optional[str]:
    """Důvod, proč živé swing obchody nejsou dovolené, nebo None. Jen T212 DEMO."""
    env = TRADING_212_ENV if env is None else env
    base_url = TRADING_212_BASE_URL if base_url is None else base_url
    if not SWING_LIVE_ENABLED:
        return "SWING_LIVE_ENABLED = False"
    if env != "demo" or "://demo." not in base_url:
        return f"T212 není demo ({env}, {base_url}), swing naživo odmítnut"
    return None


def live_active() -> bool:
    return demo_guard() is None


# ---------------------------------------------------------------- stav

def load_state(path=None) -> Dict[str, Any]:
    try:
        return json.loads((path or STATE_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"positions": [], "blocked": None}
    except Exception as e:
        logger.error(f"Swing live: stav nejde přečíst: {e}")
        return {"positions": [], "blocked": "stav swing_live_state.json nejde přečíst"}


def save_state(st: Dict[str, Any], path=None) -> None:
    p = path or STATE_FILE
    with _lock:
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        tmp.replace(p)


def live_positions(st: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    st = st if st is not None else load_state()
    return [p for p in st.get("positions", []) if p.get("status") in ("ORDERED", "OPEN")]


def live_count() -> int:
    """Počet swing pozic na T212 (otevřené + čekající nákup) pro globální limit akcií."""
    return len(live_positions())


def live_tickers() -> set:
    return {p["ticker"].upper() for p in load_state().get("positions", []) if p.get("status") in LIVE_STATUSES}


# ---------------------------------------------------------------- broker (T212 + cena z yfinance)

class Broker:
    def summary(self):
        import trading212
        return trading212.get_account_summary_sync()

    def portfolio(self):
        import trading212
        return trading212.get_portfolio_or_none_sync()

    def pending(self):
        import trading212
        return trading212.get_pending_orders_sync()

    def qty(self, portfolio, sym):
        import trading212
        return trading212.find_portfolio_qty(portfolio, sym)

    def avg_price(self, portfolio, sym):
        import trading212
        return trading212.find_portfolio_avg_price(portfolio, sym)

    def cur_price(self, portfolio, sym):
        import trading212
        return trading212.find_portfolio_price(portfolio, sym)

    def t212_ticker(self, sym):
        import trading212
        return trading212.to_t212_ticker(sym)

    def fx(self, a, b):
        import trading212
        return trading212.get_fx_rate_sync(a, b)

    def limit_buy(self, sym, qty, px):
        import trading212
        return trading212.place_limit_buy_sync(sym, qty, px)

    def stop_sell(self, sym, qty, stop):
        import trading212
        return trading212.place_stop_order_sync(sym, qty, stop)

    def cancel(self, oid):
        import trading212
        return trading212.cancel_order_sync(oid)

    def sell_own(self, sym, qty, others):
        """Prodá jen vlastní kusy: nejvýš drženo − others (others = kusy, které nejsou swing)."""
        import trading212
        return trading212.close_bot_position_sync(sym, qty, others)

    def last_price(self, sym):
        import yfinance as yf
        df = yf.Ticker(sym).history(period="1d", interval="1m")
        return float(df["Close"].dropna().iloc[-1]) if df is not None and not df.empty else None


def intraday_qty(ticker: str) -> float:
    """Kusy intradenních obchodů bota na T212 v tickeru (ty swing nikdy neprodá)."""
    try:
        import paper_trading
        return sum(float(t.get("bot_qty") or t.get("shares") or 0) for t in paper_trading.get_open_positions()
                   if t.get("ticker", "").upper() == ticker.upper() and t.get("market_type") != "CRYPTO")
    except Exception:
        return 0.0


def global_block(ticker: str) -> Optional[str]:
    """Globální limity bota: vypínač / denní ztráta / série ztrát, počet akciových pozic."""
    try:
        import risk_guard
        import paper_trading
        b = risk_guard.entries_blocked()
        if b:
            return b
        n = len(paper_trading.get_open_positions_by_market("STOCK")) + live_count()
        if n >= MAX_OPEN_POSITIONS_STOCKS:
            return f"globální limit akciových pozic {n}/{MAX_OPEN_POSITIONS_STOCKS}"
    except Exception as e:
        return f"globální limity nejde ověřit: {e}"
    return None


# ---------------------------------------------------------------- ráno: plán

def plan_entries(approved: List[Dict[str, Any]], day: date, path=None, market: str = "US") -> List[str]:
    """Kandidáti schválení Gemini (stopa A) -> PLANNED na dnešek. Nákup od 15 min po otevření trhu (US 09:45 NY, EU 09:15)."""
    if not live_active():
        return []
    st = load_state(path)
    added = []
    for c in approved:
        if any(p["ticker"] == c["ticker"] and p["status"] in LIVE_STATUSES for p in st["positions"]):
            continue
        st["positions"].append({"id": f"L-{day.isoformat()}-{c['ticker']}", "ticker": c["ticker"], "market": market,
                                "ccy": c.get("ccy", "USD"),
                                "sector": c.get("sector", "Unknown"), "status": "PLANNED", "entry_day": day.isoformat(),
                                "plan_stop": c["plan"]["stop"], "score": c["score"], "atr14": c["ind"]["atr14"]})
        added.append(c["ticker"])
    save_state(st, path)
    return added


# ---------------------------------------------------------------- jeden průchod (každých 5 min)

def _log(ev):
    try:
        import swing_shadow
        swing_shadow.log_event({"live": True, **ev})
    except Exception:
        pass


def _side(ticker: str, acct_ccy: str, instr_ccy: str = "USD") -> float:
    c = cost_model.stock_cost(ticker, instr_ccy, acct_ccy) or cost_model.stock_cost(ticker, instr_ccy, "EUR")
    return c.side_rate()


def _mkt(pos: Dict[str, Any]) -> str:
    return pos.get("market", "US")


def _floor(q: float) -> float:
    return math.floor(q / QTY_STEP + 1e-9) * QTY_STEP


def _place_stop(b: Broker, pos: Dict[str, Any], msgs: List[str]) -> None:
    res = b.stop_sell(pos["ticker"], pos["qty_open"], pos["stop"])
    if res.get("success"):
        pos["stop_order_id"] = str(res["ref"]["id"])
        _log({"type": "live_stop_placed", "id": pos["id"], "ticker": pos["ticker"], "stop": pos["stop"], "qty": pos["qty_open"]})
    else:
        pos["stop_order_id"] = None
        msgs.append(f"⚠️ Swing A {pos['ticker']}: Stop-Loss na T212 se nepodařilo založit ({res.get('error')}), zkusím znovu")


def reconcile(st: Dict[str, Any], b: Broker, portfolio, pending, msgs: List[str]) -> Optional[str]:
    """Sesouhlasí swing stav s T212. Uzavře pozice zavřené stopem na burze, doplní chybějící stop.

    Vrací popis nesouladu (pak žádné nové nákupy), nebo None."""
    pend_ids = {str(o.get("id")) for o in pending}
    problems = []
    for pos in st["positions"]:
        if pos["status"] != "OPEN":
            continue
        t = pos["ticker"]
        cur = b.qty(portfolio, t) - intraday_qty(t)          # kusy mimo intradenní obchody bota
        seen = pos.get("seen_held", pos["qty_open"])         # totéž při minulém průchodu
        if pos.get("stop_order_id") and str(pos["stop_order_id"]) in pend_ids:
            if cur < pos["qty_open"] - EPS:
                problems.append(f"{t}: na T212 {cur:g} ks, swing eviduje {pos['qty_open']:g} (stop čeká)")
            else:
                pos["seen_held"] = cur
            continue
        if cur <= seen - pos["qty_open"] + EPS:
            # Kusy ubyly přesně o swing množství -> proběhl stop na burze (ruční kusy, pokud jsou, zůstaly)
            px = pos["stop"]
            pos["seen_held"] = cur
            _close(st, pos, pos["qty_open"], px, "STOP_T212")
            msgs.append(f"🛑 Swing A {t}: Stop-Loss na T212 proběhl (~{px}), pozice uzavřena")
        elif abs(cur - seen) <= EPS and cur >= pos["qty_open"] - EPS:
            _place_stop(b, pos, msgs)                           # stop zmizel (zrušen/expirován), kusy jsou
        else:
            problems.append(f"{t}: stop nečeká a kusy se změnily z {seen:g} na {cur:g} (swing {pos['qty_open']:g})")
    return "; ".join(problems) or None


def _close(st, pos, qty, px, reason) -> None:
    c = pos["cost_side"]
    fx = pos.get("fx_entry") or 1.0
    pnl = qty * (px * (1 - c) - pos["entry"] * (1 + c)) * fx
    pos["realized_acct"] = round(pos.get("realized_acct", 0.0) + pnl, 4)
    pos["qty_open"] = round(pos["qty_open"] - qty, 6)
    if reason == "PARTIAL_2R":
        pos["partial_done"] = True
    _log({"type": "live_exit", "id": pos["id"], "ticker": pos["ticker"], "qty": qty, "price": px, "reason": reason,
          "r_at_price": round(R.r_now(pos, px), 3), "pnl_acct_est": round(pnl, 4)})
    if pos["qty_open"] <= EPS:
        r = pos["realized_acct"] / pos["initial_risk_acct"] if pos.get("initial_risk_acct") else 0.0
        pos.update(status="CLOSED", exit_day=datetime.now(market_hours.NY_TZ).date().isoformat(), r_net=round(r, 3))
        _log({"type": "live_close", "id": pos["id"], "ticker": pos["ticker"], "r_net": round(r, 3),
              "pnl_acct_est": pos["realized_acct"], "mfe_r": pos.get("mfe_r"), "mae_r": pos.get("mae_r"),
              "days_held": pos.get("days_held"), "last_reason": reason})


def _sell(st, b: Broker, pos, qty, reason, portfolio, msgs, today: date) -> bool:
    """Prodej vlastních kusů: nejdřív zrušit stop (nevyplněný = kusy jsou naše), pak tržní prodej, zbytek znovu chránit."""
    if pos["entry_day"] >= today.isoformat():
        return False   # v den vstupu jen stop na burze
    oid = pos.get("stop_order_id")
    if oid:
        c = b.cancel(oid)
        if c.get("not_found"):
            msgs.append(f"⚠️ Swing A {pos['ticker']}: stop už nečeká (možná vyplněn), prodej odložen na sesouhlasení")
            pos["stop_order_id"] = None
            return False
        if not c.get("success"):
            msgs.append(f"⚠️ Swing A {pos['ticker']}: stop nejde zrušit ({c.get('error')}), prodej odložen")
            return False
        pos["stop_order_id"] = None
    others = max(0.0, b.qty(portfolio, pos["ticker"]) - pos["qty_open"])
    res = b.sell_own(pos["ticker"], qty, others)
    if res.get("success"):
        px = res.get("exit_price") or b.cur_price(portfolio, pos["ticker"]) or pos["entry"]
        sold = min(qty, res.get("quantity") or qty)
        pos["seen_held"] = pos.get("seen_held", pos["qty_open"]) - sold
        _close(st, pos, sold, float(px), reason)
        msgs.append(f"📤 Swing A {pos['ticker']}: prodáno {qty:g} ks ({reason}) ~{float(px):.2f}")
    else:
        msgs.append(f"⚠️ Swing A {pos['ticker']}: prodej selhal ({res.get('error')})")
    if pos["status"] == "OPEN" and pos["qty_open"] > EPS:
        _place_stop(b, pos, msgs)
    return bool(res.get("success"))


def _order_entry(st, b: Broker, pos, portfolio, pending, summary, msgs) -> None:
    t = pos["ticker"]
    why = None
    ccy = (summary or {}).get("currency") or "EUR"
    total = float((summary or {}).get("totalValue") or 0)
    free = float(((summary or {}).get("cash") or {}).get("availableToTrade") or 0)
    t212 = b.t212_ticker(t)
    if st.get("blocked"):
        why = f"nesoulad se T212: {st['blocked']}"
    elif not summary or total <= 0:
        why = "účet T212 nejde načíst"
    elif b.qty(portfolio, t) > EPS or any(o.get("ticker") == t212 for o in pending):
        why = "ticker už je v portfoliu T212 nebo má čekající příkaz (ruční / intradenní)"
    else:
        why = global_block(t)
    if not why:
        icc = pos.get("ccy", "USD")
        price, fx = b.last_price(t), b.fx(icc, ccy)
        side = _side(t, ccy, icc)
        stop = pos["plan_stop"]
        if not price or not fx:
            why = "chybí cena nebo kurz"
        elif price <= stop:
            why = f"cena {price:.2f} pod stopem {stop}"
        else:
            limit = round(price * (1 + SWING_LIVE_LIMIT_SLIPPAGE_PCT / 100), 2)
            reserved = sum(p.get("notional_acct", 0.0) for p in st["positions"] if p["status"] == "ORDERED")
            size = R.size_position(total, free - reserved, limit, stop, side, fx)
            qty = _floor(size["qty"])
            others = [p for p in st["positions"] if p["status"] in ("ORDERED", "OPEN") and p is not pos]
            risk = qty * ((limit - stop) + limit * side + stop * side) * fx
            # otevřené riziko US + EU v měně účtu: každá pozice svým kurzem z nákupu
            heat = sum(p.get("risk_acct", 0.0) if p["status"] == "ORDERED" else R.position_open_risk(p, p.get("fx_entry") or 1.0)
                       for p in others)
            why = ("velikost pod 0,01 ks" if qty <= 0 else
                   R.portfolio_block(others, pos["sector"], risk, total, fx, t, check_heat=False))
            if not why and heat + risk > total * SWING_MAX_OPEN_RISK_PCT / 100 + 1e-9:
                why = f"otevřené riziko {heat + risk:.2f} > {SWING_MAX_OPEN_RISK_PCT} % účtu"
            if not why:
                res = b.limit_buy(t, qty, limit)
                if not res.get("success"):
                    why = f"T212 odmítl nákup: {res.get('error')}"
                else:
                    pos.update(status="ORDERED", order_id=str(res["order_id"]), limit_price=limit, qty_ordered=qty,
                               cost_side=side, fx_entry=fx, acct_ccy=ccy, risk_acct=round(risk, 4),
                               notional_acct=round(qty * limit * fx * (1 + side), 4),
                               ordered_at=datetime.now(market_hours.NY_TZ).isoformat(timespec="seconds"))
                    _log({"type": "live_order", "id": pos["id"], "ticker": t, "qty": qty, "limit": limit, "stop": stop,
                          "risk_acct": round(risk, 4), "equity": total, "free": free, "ccy": ccy, "limited_by": size["limited_by"]})
                    msgs.append(f"🛒 Swing A (DEMO, {_mkt(pos)}) {t}: limitní nákup {qty:g} ks @ {limit} {icc} (stop {stop}), "
                                f"riziko ~{risk:.2f} {ccy}")
                    return
    pos.update(status="CANCELLED", cancel_reason=why)
    _log({"type": "live_cancel", "id": pos["id"], "ticker": t, "reason": why})
    msgs.append(f"⏭️ Swing A (DEMO) {t}: nákup nezadán ({why})")


def _check_fill(st, b: Broker, pos, portfolio, pending, msgs, session_over: bool) -> None:
    pend_ids = {str(o.get("id")) for o in pending}
    t = pos["ticker"]
    still = pos["order_id"] in pend_ids
    if still and not session_over:
        return
    if still:
        b.cancel(pos["order_id"])
    added = b.qty(portfolio, t) - intraday_qty(t)
    if added <= EPS:
        pos["fill_checks"] = pos.get("fill_checks", 0) + 1
        if not session_over and pos["fill_checks"] < 3:
            return      # příkaz už nečeká, portfolio se mohlo ještě neobnovit: zkontrolovat znovu
        pos.update(status="CANCELLED", cancel_reason="limitní nákup nevyplněn")
        _log({"type": "live_cancel", "id": pos["id"], "ticker": t, "reason": pos["cancel_reason"]})
        msgs.append(f"⏭️ Swing A (DEMO) {t}: limitní nákup se nevyplnil, zrušen")
        return
    qty = round(min(added, pos["qty_ordered"]), 6)
    entry = b.avg_price(portfolio, t) or pos["limit_price"]
    stop, side = pos["plan_stop"], pos["cost_side"]
    pos.update(status="OPEN", qty=qty, qty_open=qty, entry=round(entry, 4), stop=stop, initial_stop=stop,
               r_ps=round(entry - stop, 6), seen_held=round(added, 6), target=round(R.target_2r(entry, stop, side), 4), days_held=0,
               max_high=entry, min_low=entry, partial_done=False, realized_acct=0.0, next_review=SWING_REVIEW_EVERY_DAYS,
               initial_risk_acct=round(qty * ((entry - stop) + entry * side + stop * side) * pos["fx_entry"], 4))
    _log({"type": "live_fill", "id": pos["id"], "ticker": t, "qty": qty, "price": pos["entry"], "stop": stop,
          "target": pos["target"], "slippage_vs_limit": round(entry - pos["limit_price"], 4)})
    msgs.append(f"✅ Swing A (DEMO) {t}: nakoupeno {qty:g} ks @ {pos['entry']}, stop {stop}, polovina na {pos['target']}")
    _place_stop(b, pos, msgs)


def step(now: Optional[datetime] = None, broker: Optional[Broker] = None, path=None) -> List[str]:
    """Jeden průchod živé stopy A. Vrací zprávy pro Discord."""
    if demo_guard():
        return []
    b = broker or Broker()
    now = (now or datetime.now(market_hours.NY_TZ)).astimezone(market_hours.NY_TZ)
    today = now.date()
    st = load_state(path)
    msgs: List[str] = []
    active = [p for p in st["positions"] if p["status"] in LIVE_STATUSES]
    if not active and not st.get("blocked"):
        return []
    portfolio, pending = b.portfolio(), b.pending()
    if portfolio is None or pending is None:
        return []
    prev = st.get("blocked")
    st["blocked"] = reconcile(st, b, portfolio, pending, msgs)
    if st["blocked"] and st["blocked"] != prev:
        _log({"type": "live_mismatch", "detail": st["blocked"]})
        msgs.append(f"🚨 Swing A (DEMO): nesoulad evidence a T212 — {st['blocked']}. Nové swing nákupy zastaveny, dokud se to nevyřeší.")
    elif prev and not st["blocked"]:
        msgs.append("✅ Swing A (DEMO): evidence opět sedí s T212, nákupy povoleny")

    summary = None
    for market in ("US", "EU"):
        mine = [p for p in st["positions"] if _mkt(p) == market]
        if not mine:
            continue
        bounds = market_hours.session_bounds(market, now)
        if not bounds:
            continue
        open_dt, close_dt = bounds
        today = now.astimezone(market_hours.MARKETS[market]["tz"]).date()
        for pos in mine:
            if pos["status"] == "ORDERED":
                _check_fill(st, b, pos, portfolio, pending, msgs, session_over=now >= close_dt)
        if open_dt + timedelta(minutes=ENTRY_FROM_MIN) <= now < close_dt - timedelta(minutes=ENTRY_UNTIL_BEFORE_CLOSE_MIN):
            for pos in mine:
                if pos["status"] == "PLANNED" and pos["entry_day"] == today.isoformat():
                    summary = summary or b.summary()
                    _order_entry(st, b, pos, portfolio, pending, summary, msgs)
        if open_dt + timedelta(minutes=EXIT_FROM_MIN) <= now < close_dt - timedelta(minutes=5):
            for pos in mine:
                if pos["status"] != "OPEN" or pos["entry_day"] >= today.isoformat():
                    continue
                if pos.get("pending_exit"):
                    _sell(st, b, pos, pos["qty_open"], pos["pending_exit"], portfolio, msgs, today)
                    if pos["status"] == "CLOSED" or pos["qty_open"] <= EPS:
                        pos.pop("pending_exit", None)
                    continue
                px = b.cur_price(portfolio, pos["ticker"])
                if px and not pos.get("partial_done") and px >= pos["target"]:
                    _sell(st, b, pos, _floor(pos["qty_open"] / 2) or pos["qty_open"], "PARTIAL_2R", portfolio, msgs, today)
    for pos in st["positions"]:
        if pos["status"] == "PLANNED" and pos["entry_day"] < now.astimezone(market_hours.MARKETS[_mkt(pos)]["tz"]).date().isoformat():
            pos.update(status="CANCELLED", cancel_reason="den vstupu uplynul bez nákupu")
            _log({"type": "live_cancel", "id": pos["id"], "ticker": pos["ticker"], "reason": pos["cancel_reason"]})
    st["positions"] = [p for p in st["positions"] if p["status"] in LIVE_STATUSES] + \
        [p for p in st["positions"] if p["status"] not in LIVE_STATUSES][-100:]
    save_state(st, path)
    return msgs


# ---------------------------------------------------------------- večer: stop jen nahoru

def move_stop(b: Broker, pos: Dict[str, Any], new_stop: float, why: List[str], msgs: List[str]) -> bool:
    """Posune stop na T212 výš (zrušit + založit). Nikdy níž."""
    if new_stop <= pos["stop"] + EPS:
        return False
    old = pos["stop"]
    oid = pos.get("stop_order_id")
    if oid:
        c = b.cancel(oid)
        if c.get("not_found"):
            pos["stop_order_id"] = None
            return False   # stop mohl proběhnout, vyřeší sesouhlasení
        if not c.get("success"):
            msgs.append(f"⚠️ Swing A {pos['ticker']}: stop nejde zrušit pro posun ({c.get('error')})")
            return False
    pos["stop"] = round(new_stop, 4)
    pos["stop_order_id"] = None
    _place_stop(b, pos, msgs)
    _log({"type": "live_stop_update", "id": pos["id"], "ticker": pos["ticker"], "old": old, "new": pos["stop"], "why": why})
    return True


def evening(day: date, daily_rows: Dict[str, Dict[str, float]], trail: Dict[str, Dict[str, Any]],
            gemini: Dict[str, Dict[str, Any]], broker: Optional[Broker] = None, path=None, market: str = "US") -> List[str]:
    """Po zavření: dny držení, MFE/MAE, časový stop, BE/chandelier/TIGHTEN (stop jen nahoru), Gemini SELL na zítra."""
    if demo_guard():
        return []
    b = broker or Broker()
    st = load_state(path)
    msgs: List[str] = []
    for pos in st["positions"]:
        if pos["status"] != "OPEN" or _mkt(pos) != market:
            continue
        t, bar = pos["ticker"], daily_rows.get(pos["ticker"])
        if not bar:
            continue
        if pos["entry_day"] < day.isoformat():
            pos["days_held"] = pos.get("days_held", 0) + 1
        R.update_excursions(pos, bar["high"], bar["low"])
        ti = trail.get(t) or {}
        new, why = pos["stop"], []
        if ti.get("hh22"):
            ns = R.next_stop(pos, ti["hh22"], ti["atr22"])
            new, why = ns["stop"], ns["why"]
        g = gemini.get(t) or {}
        if g.get("action") == "TIGHTEN" and ti.get("atr14"):
            tg = R.tighten_stop(new, bar["close"], ti["atr14"])
            if tg > new:
                new, why = tg, why + ["GEMINI_TIGHTEN"]
        if new > pos["stop"] + EPS:
            if move_stop(b, pos, new, why, msgs):
                msgs.append(f"🔼 Swing A (DEMO) {t}: stop {'/'.join(why)} -> {pos['stop']}")
        if pos["entry_day"] < day.isoformat() and not pos.get("pending_exit"):
            if g.get("action") == "SELL":
                pos["pending_exit"] = "GEMINI_SELL"
            elif R.time_stop_due(pos):
                pos["pending_exit"] = "TIME_STOP"
            if pos.get("pending_exit"):
                _log({"type": "live_exit_scheduled", "id": pos["id"], "ticker": t, "reason": pos["pending_exit"]})
                msgs.append(f"📅 Swing A (DEMO) {t}: výstup {pos['pending_exit']} zítra po otevření")
    save_state(st, path)
    return msgs


def status_lines() -> List[str]:
    st = load_state()
    if demo_guard():
        return [f"Swing A naživo vypnuto ({demo_guard()})"]
    lines = [f"**Swing A naživo (T212 DEMO)**{' 🚨 ' + st['blocked'] if st.get('blocked') else ''}"]
    for p in st.get("positions", []):
        if p["status"] in LIVE_STATUSES:
            lines.append(f"• {_mkt(p)} {p['ticker']} {p['status']}" + (f": {p['qty_open']:g} ks @ {p['entry']}, stop {p['stop']}, "
                         f"cíl ½ {p['target']}{', ' + p['pending_exit'] + ' zítra' if p.get('pending_exit') else ''}"
                         if p["status"] == "OPEN" else f" ({p['entry_day']})"))
    closed = [p for p in st.get("positions", []) if p["status"] == "CLOSED"]
    if closed:
        lines.append(f"Uzavřeno {len(closed)}, celkem {sum(p.get('r_net') or 0 for p in closed):+.2f} R (odhad)")
    return lines
