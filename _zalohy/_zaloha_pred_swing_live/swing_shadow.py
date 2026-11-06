"""Swing F0: vícedenní držení US akcií JEN VE STÍNU. Nikdy neposílá příkazy, nečte ani neprodává pozice na T212.

Denní rytmus (bot volá tick() každých pár minut):
- ráno po ranním přehledu (SWING_MORNING_PRAGUE, nejpozději do 09:45 New York): sken (swing_scanner), pak tři stopy
  se stejnou matematikou vytvoří čekající vstupy na dnešek:
    A = kandidáti -> Gemini výběr (max 5) -> 3 hlasy analytika u top 3 -> BUY při >= 2/3
    B = kandidáti podle skóre (bez Gemini), C = kandidáti v náhodném pořadí (seed = datum, zapsaný v logu)
- večer 30 min po zavření US: vstupy za cenu 15 min po otevření (5M svíčka 09:45 New York, jinak open),
  čekající výstupy za dnešní open, stop/polovina/BE/chandelier/časový stop nad denní svíčkou,
  Gemini kontrola pozic stopy A (jedno volání), ocenění účtů, srovnání se SPY, souhrn do logu a Discordu.
Zmeškané večery se doženou postupně (denní svíčky zůstávají), zmeškané ráno se přeskočí.

Záznamy: swing_shadow.jsonl (append-only), stav: swing_shadow_state.json (přežije restart).
"""

import asyncio
import json
import logging
import random
import threading
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

import cost_model
import market_hours
import swing_analyst
import swing_rules as R
import swing_scanner
from config import (
    BASE_DIR, SWING_SHADOW_ENABLED, SWING_LIVE_ENABLED, SWING_MAX_ANALYZE, SWING_MAX_NEW_PER_DAY,
    SWING_EARNINGS_BLACKOUT_DAYS, SWING_LOSS_STREAK, SWING_LOSS_PAUSE_DAYS, SWING_MAX_DD_PCT,
    SWING_REVIEW_EVERY_DAYS, SWING_START_EQUITY_FALLBACK_EUR, SWING_MORNING_PRAGUE,
    SWING_EVENING_AFTER_CLOSE_MIN, SWING_GEMINI_MAX_CALLS_PER_DAY,
)

logger = logging.getLogger("TradingBot.Swing")

LOG_FILE = BASE_DIR / "swing_shadow.jsonl"
STATE_FILE = BASE_DIR / "swing_shadow_state.json"
TRACKS = {"A": "A Gemini", "B": "B skóre", "C": "C náhoda"}
ACCOUNT_CCY = "EUR"              # virtuální účty stop jsou v EUR
PICK_PROMPT_MAX = 15             # Gemini výběr dostane nejvýš 15 nejlepších kandidátů
MAX_CATCHUP_EVENINGS = 5
ENTRY_DELAY_MIN = 15
_lock = threading.Lock()


class LiveDisabled(RuntimeError):
    pass


def assert_shadow_only() -> None:
    """Živá cesta swingu neexistuje. SWING_LIVE_ENABLED = True stín zastaví, žádný příkaz se neodešle."""
    if SWING_LIVE_ENABLED:
        raise LiveDisabled("SWING_LIVE_ENABLED = True, ale živé obchodování swingu není postavené (F0 = jen stín)")


# ---------------------------------------------------------------- soubory

def log_event(ev: Dict[str, Any]) -> None:
    rec = {"ts": datetime.now(market_hours.PRAGUE_TZ).isoformat(timespec="seconds"), **ev}
    try:
        with _lock, open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        logger.warning(f"Swing: zápis do {LOG_FILE.name} selhal: {e}")


def load_state() -> Dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.error(f"Swing: stav {STATE_FILE.name} nejde přečíst: {e}")
        return {}


def save_state(st: Dict[str, Any]) -> None:
    with _lock:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        tmp.replace(STATE_FILE)


# ---------------------------------------------------------------- obchodní dny

def is_trading_day(d: date) -> bool:
    return market_hours.session_bounds("US", datetime.combine(d, dtime(12, 0), market_hours.NY_TZ)) is not None


def add_trading_days(d: date, n: int) -> date:
    while n > 0:
        d += timedelta(days=1)
        if is_trading_day(d):
            n -= 1
    return d


def prev_trading_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


# ---------------------------------------------------------------- data (synchronní, volá se přes to_thread)

def _account_info() -> Dict[str, Any]:
    """Měna skutečného účtu T212 (pro náklady) a volná hotovost v EUR (start virtuálních účtů). Jen čtení."""
    out = {"real_ccy": "EUR", "start_eur": SWING_START_EQUITY_FALLBACK_EUR, "source": "výchozí 1000 EUR"}
    try:
        import trading212
        s = trading212.get_account_summary_sync() or {}
        ccy = s.get("currency")
        free = float((s.get("cash") or {}).get("availableToTrade") or 0.0)
        if ccy:
            out["real_ccy"] = ccy
        if ccy and free > 0:
            fx = trading212.get_fx_rate_sync(ccy, ACCOUNT_CCY)
            if fx:
                out.update(start_eur=round(free * fx, 2), source=f"volná hotovost T212 {free:.2f} {ccy}")
    except Exception as e:
        logger.warning(f"Swing: účet T212 nejde načíst ({e}), start 1000 EUR")
    return out


def _usd_eur() -> Optional[float]:
    try:
        import trading212
        return trading212.get_fx_rate_sync("USD", ACCOUNT_CCY)
    except Exception:
        return None


def fetch_daily(tickers: List[str]) -> Dict[str, pd.DataFrame]:
    return swing_scanner.download_daily(sorted(set(tickers)), period="6mo")


def fetch_entry_prices(tickers: List[str], day: date) -> Dict[str, Dict[str, float]]:
    """Cena 15 min po otevření (open 5M svíčky 09:45 New York) a high/low od té chvíle do zavření."""
    import yfinance as yf
    out = {}
    for t in tickers:
        try:
            df = yf.Ticker(t).history(period="5d", interval="5m")
            if df is None or df.empty:
                continue
            idx = df.index.tz_convert(market_hours.NY_TZ) if df.index.tz is not None else df.index.tz_localize("UTC").tz_convert(market_hours.NY_TZ)
            df = df.set_axis(idx)
            start = datetime.combine(day, dtime(9, 30 + ENTRY_DELAY_MIN), market_hours.NY_TZ)
            after = df[(df.index >= start) & (df.index.date == day)]
            if after.empty:
                continue
            out[t] = {"fill": float(after["Open"].iloc[0]), "high": float(after["High"].max()),
                      "low": float(after["Low"].min()), "src": "5M 09:45"}
        except Exception as e:
            logger.debug(f"Swing 5M {t}: {e}")
    return out


def fetch_news(ticker: str) -> List[str]:
    import news_briefing
    return news_briefing.fetch_fresh_news(ticker, news_briefing.company_name(ticker) or None)


def _row(df: Optional[pd.DataFrame], day: date) -> Optional[Dict[str, float]]:
    if df is None or df.empty:
        return None
    idx = pd.DatetimeIndex(df.index)
    dates = idx.tz_localize(None).date if idx.tz is not None else idx.date
    m = [d == day for d in dates]
    if not any(m):
        return None
    r = df[m].iloc[-1]
    return {"open": float(r["Open"]), "high": float(r["High"]), "low": float(r["Low"]), "close": float(r["Close"])}


def _upto(df: pd.DataFrame, day: date) -> pd.DataFrame:
    idx = pd.DatetimeIndex(df.index)
    dates = idx.tz_localize(None).date if idx.tz is not None else idx.date
    return df[[d <= day for d in dates]]


def _trail_inputs(df: Optional[pd.DataFrame], day: date) -> Dict[str, Optional[float]]:
    if df is None or len(df) < 30:
        return {"hh22": None, "atr22": None, "atr14": None}
    d = _upto(df, day)
    return {"hh22": float(d["High"].tail(22).max()), "atr22": float(R.atr(d, 22).iloc[-1]),
            "atr14": float(R.atr(d, 14).iloc[-1])}


# ---------------------------------------------------------------- účty stop

def _new_track(start_eur: float, spy_close: float) -> Dict[str, Any]:
    return {"equity_start": start_eur, "cash": start_eur, "equity": start_eur, "peak": start_eur,
            "positions": [], "closed_n": 0, "wins": 0, "sum_r": 0.0, "streak": 0, "pause_until": None,
            "stopped": False, "bench_spy_start": spy_close, "bench_equity": start_eur, "last_close": {}}


def _ensure_tracks(st: Dict[str, Any], acct: Dict[str, Any], spy_close: float, day: date) -> None:
    if st.get("tracks"):
        return
    st.update(version=1, created=datetime.now(market_hours.PRAGUE_TZ).isoformat(timespec="seconds"),
              first_day=day.isoformat(), real_ccy=acct["real_ccy"], start_source=acct["source"],
              tracks={k: _new_track(acct["start_eur"], spy_close) for k in TRACKS})
    log_event({"type": "init", "start_eur": acct["start_eur"], "source": acct["source"], "real_ccy": acct["real_ccy"],
               "spy_start": spy_close, "tracks": list(TRACKS)})


def _side(st: Dict[str, Any], ticker: str) -> float:
    c = cost_model.stock_cost(ticker, "USD", st.get("real_ccy") or ACCOUNT_CCY) or cost_model.stock_cost(ticker, "USD", "EUR")
    return c.side_rate()


def _entry_gate(tr: Dict[str, Any], day: date) -> Optional[str]:
    if tr.get("stopped"):
        return f"stopa zastavena (propad {SWING_MAX_DD_PCT:.0f} % od vrcholu)"
    if tr.get("pause_until") and day < date.fromisoformat(tr["pause_until"]):
        return f"pauza po {SWING_LOSS_STREAK} ztrátách do {tr['pause_until']}"
    return None


def _gemini_budget(st: Dict[str, Any], day: date) -> swing_analyst.Budget:
    g = st.get("gemini") or {}
    used = g.get("used", 0) if g.get("day") == day.isoformat() else 0
    return swing_analyst.Budget(used, SWING_GEMINI_MAX_CALLS_PER_DAY)


def _save_budget(st: Dict[str, Any], day: date, b: swing_analyst.Budget) -> None:
    st["gemini"] = {"day": day.isoformat(), "used": b.used}


def _add_pending(st: Dict[str, Any], key: str, cands: List[Dict[str, Any]], day: date, fx: float,
                 limit: int, note: Dict[str, Any]) -> List[str]:
    """Čekající vstupy na dnešek; limity portfolia a velikost z plánovaného vstupu (včerejší close)."""
    tr = st["tracks"][key]
    added = []
    for c in cands:
        if len(added) >= limit:
            break
        p, side = c["plan"], _side(st, c["ticker"])
        reserved = sum(x.get("planned_notional_acct", 0.0) for x in tr["positions"] if x["status"] == "PENDING")
        size = R.size_position(tr["equity"], tr["cash"] - reserved, p["entry_ref"], p["stop"], side, fx)
        why = size["reason"] or R.portfolio_block(tr["positions"], c.get("sector", "Unknown"), size["risk_acct"],
                                                  tr["equity"], fx, c["ticker"])
        rec = {"type": "risk", "track": key, "day": day.isoformat(), "ticker": c["ticker"], "score": c["score"],
               "plan": p, "size": size, "decision": "REJECT" if why else "PENDING", "reason": why or ""}
        log_event(rec)
        if why:
            continue
        tr["positions"].append({
            "id": f"{key}-{day.isoformat()}-{c['ticker']}", "track": key, "ticker": c["ticker"],
            "sector": c.get("sector", "Unknown"), "status": "PENDING", "entry_day": day.isoformat(),
            "plan_stop": p["stop"], "target": p["target"], "entry_ref": p["entry_ref"], "score": c["score"],
            "atr14": c["ind"]["atr14"], "cost_side": side, "planned_risk_acct": size["risk_acct"],
            "planned_notional_acct": size["notional_acct"], "note": note.get(c["ticker"], {})})
        added.append(c["ticker"])
    return added


# ---------------------------------------------------------------- ráno

async def run_morning(day: date, st: Dict[str, Any]) -> Dict[str, Any]:
    import momentum_scanner
    import news_briefing
    acct = await asyncio.to_thread(_account_info) if not st.get("tracks") else {}
    briefing = news_briefing.get_extra_tickers()
    dyn = momentum_scanner.get_dynamic_tickers("US")
    last_bo = add_trading_days(day, SWING_EARNINGS_BLACKOUT_DAYS)
    res = await asyncio.to_thread(swing_scanner.scan, day, last_bo, dyn, briefing, st.get("real_ccy") or acct.get("real_ccy", "EUR"),
                                  fetch_news)
    log_event({"type": "scan", "day": day.isoformat(), "ok": res.get("ok"), "reason": res.get("reason"),
               "regime_ok": res.get("regime_ok"), "spy_close": res.get("spy_close"), "spy_sma200": res.get("spy_sma200"),
               "stats": res.get("stats"), "rejected": res.get("rejected"),
               "candidates": [{"ticker": c["ticker"], "score": c["score"], "parts": c["parts"], "sector": c.get("sector"),
                               "plan": c["plan"], "ind": c["ind"], "headlines": c.get("headlines"),
                               "earnings_date": c.get("earnings_date")} for c in res.get("candidates", [])]})
    if not res.get("ok"):
        return {"text": f"🌱 Swing stín {day}: sken selhal ({res.get('reason')})"}
    if not st.get("tracks"):
        _ensure_tracks(st, acct, res["spy_close"], day)
    fx = await asyncio.to_thread(_usd_eur) or st.get("fx_usd_eur") or 1.0
    st["fx_usd_eur"] = fx
    cands = res["candidates"]
    out: Dict[str, List[str]] = {k: [] for k in TRACKS}
    notes: Dict[str, str] = {}

    if not res["regime_ok"]:
        log_event({"type": "regime_block", "day": day.isoformat(), "spy_close": res["spy_close"], "spy_sma200": res["spy_sma200"]})
        notes = {k: "SPY pod SMA200, žádné vstupy" for k in TRACKS}
    else:
        gates = {k: _entry_gate(st["tracks"][k], day) for k in TRACKS}
        for k, g in gates.items():
            if g:
                notes[k] = g
                log_event({"type": "gate", "track": k, "day": day.isoformat(), "reason": g})
        # A: Gemini výběr + 3 hlasy
        if not gates["A"] and cands:
            budget = _gemini_budget(st, day)
            try:
                pk = await swing_analyst.pick(cands[:PICK_PROMPT_MAX], budget)
                log_event({"type": "gemini_pick", "day": day.isoformat(), **pk})
                picked = {p["ticker"]: p for p in pk["picks"]}
                top = sorted((c for c in cands if c["ticker"] in picked), key=lambda c: c["score"], reverse=True)[:SWING_MAX_ANALYZE]
                approved, info = [], {}
                for c in top:
                    v = await swing_analyst.vote(c, budget, picked[c["ticker"]])
                    log_event({"type": "gemini_votes", "day": day.isoformat(), **v})
                    if v["buy"]:
                        approved.append(c)
                        info[c["ticker"]] = {"pick": picked[c["ticker"]], "buy_votes": v["buy_votes"], "model": v["votes"][0]["model"]}
                out["A"] = _add_pending(st, "A", approved, day, fx, SWING_MAX_ANALYZE, info)
                if not approved:
                    notes["A"] = f"Gemini vybral {len(pk['picks'])}, BUY 0"
            except swing_analyst.BudgetExceeded as e:
                notes["A"] = f"stopa A dnes vynechána: {e}"
                log_event({"type": "track_skip", "track": "A", "day": day.isoformat(), "reason": str(e)})
                logger.warning(f"🌱 Swing: {notes['A']}")
            finally:
                _save_budget(st, day, budget)
        # B: podle skóre
        if not gates["B"]:
            out["B"] = _add_pending(st, "B", cands, day, fx, SWING_MAX_NEW_PER_DAY, {})
        # C: náhodné pořadí, seed zapsaný v logu
        if not gates["C"]:
            seed = int(day.strftime("%Y%m%d"))
            pool = list(cands)
            random.Random(seed).shuffle(pool)
            log_event({"type": "random_pick", "track": "C", "day": day.isoformat(), "seed": seed,
                       "order": [c["ticker"] for c in pool]})
            out["C"] = _add_pending(st, "C", pool, day, fx, SWING_MAX_NEW_PER_DAY, {})
    s = res["stats"]
    parts = [f"{TRACKS[k]}: {', '.join(out[k]) or notes.get(k, 'nic')}" for k in TRACKS]
    text = (f"🌱 **Swing stín {day}** (jen papír): vesmír {s.get('tradable')}, prošlo filtrem {s.get('passed_universe')}, "
            f"skóre >= 0,6: {s.get('score_ok')}, kandidáti {s.get('candidates')}, SPY {'nad' if res['regime_ok'] else 'POD'} SMA200.\n"
            f"Čekající vstupy (15 min po otevření): " + " | ".join(parts))
    return {"text": text}


# ---------------------------------------------------------------- večer

def _sell(st: Dict[str, Any], key: str, pos: Dict[str, Any], qty: float, px: float, reason: str, day: date, fx: float) -> None:
    tr = st["tracks"][key]
    c = pos["cost_side"]
    qty = min(qty, pos["qty_open"])
    proceeds = qty * px * fx * (1 - c)
    basis = qty * pos["entry"] * pos["fx_entry"] * (1 + c)
    tr["cash"] += proceeds
    pos["realized_acct"] = pos.get("realized_acct", 0.0) + proceeds - basis
    pos["qty_open"] = round(pos["qty_open"] - qty, 6)
    if reason == "PARTIAL_2R":
        pos["partial_done"] = True
    log_event({"type": "exit", "track": key, "day": day.isoformat(), "id": pos["id"], "ticker": pos["ticker"],
               "qty": qty, "price": round(px, 4), "reason": reason, "r_at_price": round(R.r_now(pos, px), 3),
               "pnl_acct": round(proceeds - basis, 4)})
    if pos["qty_open"] <= 1e-6:
        r = pos["realized_acct"] / pos["initial_risk_acct"] if pos.get("initial_risk_acct") else 0.0
        pos.update(status="CLOSED", exit_day=day.isoformat(), r_net=round(r, 3))
        tr["closed_n"] += 1
        tr["sum_r"] = round(tr["sum_r"] + r, 3)
        if pos["realized_acct"] > 0:
            tr["wins"] += 1
            tr["streak"] = 0
        else:
            tr["streak"] += 1
            if tr["streak"] >= SWING_LOSS_STREAK:
                tr["pause_until"] = add_trading_days(day, SWING_LOSS_PAUSE_DAYS).isoformat()
                tr["streak"] = 0
                log_event({"type": "pause", "track": key, "day": day.isoformat(), "until": tr["pause_until"]})
        log_event({"type": "close", "track": key, "day": day.isoformat(), "id": pos["id"], "ticker": pos["ticker"],
                   "entry_day": pos["entry_day"], "entry": pos["entry"], "r_net": round(r, 3),
                   "pnl_acct": round(pos["realized_acct"], 4), "mfe_r": pos.get("mfe_r"), "mae_r": pos.get("mae_r"),
                   "days_held": pos.get("days_held"), "last_reason": reason})


def _fill_entry(st, key, pos, day, ip, bar, fx) -> None:
    tr = st["tracks"][key]
    src = ip or ({"fill": bar["open"], "high": bar["high"], "low": bar["low"], "src": "denní open"} if bar else None)
    if not src:
        pos.update(status="CANCELLED", cancel_reason="chybí cena vstupu")
    else:
        fill, stop, side = src["fill"], pos["plan_stop"], pos["cost_side"]
        if fill > stop:
            pos["target"] = round(R.target_2r(fill, stop, side), 4)   # +2R po nákladech od skutečného vstupu
        reserved = sum(x.get("planned_notional_acct", 0.0) for x in tr["positions"] if x["status"] == "PENDING" and x is not pos)
        size = R.size_position(tr["equity"], tr["cash"] - reserved, fill, stop, side, fx)
        if fill <= stop:
            pos.update(status="CANCELLED", cancel_reason=f"open {fill:.2f} pod stopem {stop}")
        elif fill >= pos["target"]:
            pos.update(status="CANCELLED", cancel_reason=f"open {fill:.2f} nad cílem {pos['target']}")
        elif size["qty"] <= 0:
            pos.update(status="CANCELLED", cancel_reason=size["reason"])
        else:
            q = size["qty"]
            tr["cash"] -= q * fill * fx * (1 + side)
            pos.update(status="OPEN", entry=round(fill, 4), stop=stop, initial_stop=stop, r_ps=round(fill - stop, 6),
                       qty=q, qty_open=q, fx_entry=fx, initial_risk_acct=size["risk_acct"], fill_src=src["src"],
                       days_held=0, max_high=fill, min_low=fill, realized_acct=0.0, partial_done=False,
                       next_review=SWING_REVIEW_EVERY_DAYS, last_news_hash=None)
            log_event({"type": "fill", "track": key, "day": day.isoformat(), "id": pos["id"], "ticker": pos["ticker"],
                       "price": round(fill, 4), "src": src["src"], "qty": q, "stop": stop, "target": pos["target"],
                       "risk_acct": size["risk_acct"], "notional_acct": size["notional_acct"], "fx": fx})
            R.update_excursions(pos, src["high"], src["low"])
            for ex in R.process_bar(pos, fill, src["high"], src["low"], entry_day=True):
                _sell(st, key, pos, ex["qty"], ex["price"], ex["reason"], day, fx)
            return
    log_event({"type": "cancel", "track": key, "day": day.isoformat(), "id": pos["id"], "ticker": pos["ticker"],
               "reason": pos.get("cancel_reason")})


async def run_evening(day: date, st: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Večerní zpracování dne `day`. None = data dne ještě nejsou (zkusí se znovu)."""
    tracks = st["tracks"]
    live = [p for tr in tracks.values() for p in tr["positions"] if p["status"] in ("OPEN", "PENDING")]
    tickers = sorted({p["ticker"] for p in live})
    daily = await asyncio.to_thread(fetch_daily, tickers + ["SPY"])
    spy_bar = _row(daily.get("SPY"), day)
    if not spy_bar:
        return None
    pend = sorted({p["ticker"] for p in live if p["status"] == "PENDING" and p["entry_day"] == day.isoformat()})
    intraday = await asyncio.to_thread(fetch_entry_prices, pend, day) if pend else {}
    fx = await asyncio.to_thread(_usd_eur) or st.get("fx_usd_eur") or 1.0
    st["fx_usd_eur"] = fx
    trail = {t: _trail_inputs(daily.get(t), day) for t in tickers}

    for key, tr in tracks.items():
        for pos in list(tr["positions"]):
            t = pos["ticker"]
            bar = _row(daily.get(t), day)
            if pos["status"] == "PENDING":
                if pos["entry_day"] == day.isoformat():
                    _fill_entry(st, key, pos, day, intraday.get(t), bar, fx)
                elif pos["entry_day"] < day.isoformat():
                    pos.update(status="CANCELLED", cancel_reason="zmeškaný den vstupu")
                    log_event({"type": "cancel", "track": key, "day": day.isoformat(), "id": pos["id"], "ticker": t,
                               "reason": pos["cancel_reason"]})
                continue
            if pos["status"] != "OPEN" or pos["entry_day"] >= day.isoformat():
                continue
            if not bar:
                log_event({"type": "no_data", "track": key, "day": day.isoformat(), "ticker": t})
                continue
            if pos.get("pending_exit"):
                _sell(st, key, pos, pos["qty_open"], bar["open"], pos.pop("pending_exit"), day, fx)
                continue
            pos["days_held"] = pos.get("days_held", 0) + 1
            R.update_excursions(pos, bar["high"], bar["low"])
            for ex in R.process_bar(pos, bar["open"], bar["high"], bar["low"], entry_day=False):
                _sell(st, key, pos, ex["qty"], ex["price"], ex["reason"], day, fx)

        # Stop na zítřek, časový stop, ocenění
        for pos in tr["positions"]:
            if pos["status"] != "OPEN":
                continue
            t, bar = pos["ticker"], _row(daily.get(pos["ticker"]), day)
            ti = trail.get(t) or {}
            if ti.get("hh22"):
                ns = R.next_stop(pos, ti["hh22"], ti["atr22"])
                if ns["moved"]:
                    log_event({"type": "stop_update", "track": key, "day": day.isoformat(), "id": pos["id"], "ticker": t,
                               "old": pos["stop"], "new": ns["stop"], "why": ns["why"]})
                    pos["stop"] = ns["stop"]
            if ti.get("atr14"):
                pos["atr14"] = round(ti["atr14"], 4)
            if bar:
                tr["last_close"][t] = bar["close"]
            if R.time_stop_due(pos) and not pos.get("pending_exit"):
                pos["pending_exit"] = "TIME_STOP"
                log_event({"type": "exit_scheduled", "track": key, "day": day.isoformat(), "id": pos["id"], "ticker": t,
                           "reason": "TIME_STOP", "mfe_r": pos.get("mfe_r"), "days_held": pos.get("days_held")})

    # Gemini kontrola pozic stopy A (jedno volání, jen nové zprávy nebo revize po 20 dnech)
    await _gemini_check(st, day)

    lines = []
    for key, tr in tracks.items():
        mv = sum(p["qty_open"] * tr["last_close"].get(p["ticker"], p["entry"]) * fx
                 for p in tr["positions"] if p["status"] == "OPEN")
        tr["equity"] = round(tr["cash"] + mv, 4)
        tr["peak"] = max(tr["peak"], tr["equity"])
        dd = 1 - tr["equity"] / tr["peak"] if tr["peak"] else 0.0
        if dd * 100 >= SWING_MAX_DD_PCT and not tr["stopped"]:
            tr["stopped"] = True
            log_event({"type": "track_stopped", "track": key, "day": day.isoformat(), "dd_pct": round(dd * 100, 2)})
        tr["bench_equity"] = round(tr["equity_start"] * spy_bar["close"] / tr["bench_spy_start"], 4) if tr.get("bench_spy_start") else None
        closed = [p for p in tr["positions"] if p["status"] in ("CLOSED", "CANCELLED")]
        tr["positions"] = [p for p in tr["positions"] if p["status"] in ("OPEN", "PENDING")]
        tr.setdefault("history", []).extend({k: p.get(k) for k in ("id", "status", "r_net", "cancel_reason")} for p in closed)
        tr["history"] = tr["history"][-200:]
        lines.append(track_line(key, tr))
    log_event({"type": "day_summary", "day": day.isoformat(), "spy_close": spy_bar["close"], "fx_usd_eur": fx,
               "tracks": {k: {x: tr.get(x) for x in ("equity", "cash", "peak", "bench_equity", "closed_n", "wins", "sum_r",
                                                       "streak", "pause_until", "stopped")} |
                          {"open": [p["ticker"] for p in tr["positions"] if p["status"] == "OPEN"]}
                          for k, tr in tracks.items()}})
    return {"text": f"🌙 **Swing stín {day}** (jen papír)\n" + "\n".join(lines)}


async def _gemini_check(st: Dict[str, Any], day: date) -> None:
    tr = st["tracks"]["A"]
    items, by_id = [], {}
    for pos in tr["positions"]:
        if pos["status"] != "OPEN" or pos.get("pending_exit") or pos["entry_day"] >= day.isoformat():
            continue
        review = pos.get("days_held", 0) >= pos.get("next_review", SWING_REVIEW_EVERY_DAYS)
        try:
            heads = await asyncio.to_thread(fetch_news, pos["ticker"])
        except Exception:
            heads = []
        h = swing_analyst.prompt_hash("\n".join(sorted(heads))) if heads else None
        new_news = bool(heads) and h != pos.get("last_news_hash")
        if not (new_news or review):
            continue
        pid = f"P{len(items) + 1}"
        close = tr["last_close"].get(pos["ticker"], pos["entry"])
        items.append({"id": pid, "ticker": pos["ticker"], "entry": pos["entry"], "stop": pos["stop"], "close": close,
                      "r_now": R.r_now(pos, close), "days_held": pos.get("days_held", 0), "review": review,
                      "headlines": heads if new_news else []})
        by_id[pid] = (pos, h, review, close)
    if not items:
        return
    budget = _gemini_budget(st, day)
    try:
        res = await swing_analyst.evening_check(items, budget)
    except swing_analyst.BudgetExceeded as e:
        log_event({"type": "check_skip", "track": "A", "day": day.isoformat(), "reason": str(e)})
        return
    finally:
        _save_budget(st, day, budget)
    log_event({"type": "gemini_check", "track": "A", "day": day.isoformat(), "model": res["model"],
               "prompt_hash": res["prompt_hash"], "results": res["results"],
               "items": [{k: it[k] for k in ("id", "ticker", "review")} for it in items]})
    for pid, (pos, h, review, close) in by_id.items():
        pos["last_news_hash"] = h or pos.get("last_news_hash")
        if review:
            pos["next_review"] = pos.get("days_held", 0) + SWING_REVIEW_EVERY_DAYS
        r = res["results"].get(pid)
        if not r:
            continue
        if r["ignored"]:
            log_event({"type": "gemini_ignored", "track": "A", "day": day.isoformat(), "id": pos["id"], "why": r["ignored"]})
        if r["action"] == "TIGHTEN" and pos.get("atr14"):
            new = R.tighten_stop(pos["stop"], close, pos["atr14"])
            if new > pos["stop"]:
                log_event({"type": "stop_update", "track": "A", "day": day.isoformat(), "id": pos["id"],
                           "ticker": pos["ticker"], "old": pos["stop"], "new": new, "why": ["GEMINI_TIGHTEN"]})
                pos["stop"] = new
        elif r["action"] == "SELL":
            pos["pending_exit"] = "GEMINI_SELL"
            log_event({"type": "exit_scheduled", "track": "A", "day": day.isoformat(), "id": pos["id"],
                       "ticker": pos["ticker"], "reason": "GEMINI_SELL", "cited": r["cited_ids"], "why": r["reason_cs"]})


# ---------------------------------------------------------------- texty

def track_line(key: str, tr: Dict[str, Any]) -> str:
    eq, start = tr.get("equity", 0.0), tr.get("equity_start", 0.0) or 1.0
    bench = tr.get("bench_equity")
    opens = [p for p in tr["positions"] if p["status"] == "OPEN"]
    pend = [p["ticker"] for p in tr["positions"] if p["status"] == "PENDING"]
    op = ", ".join(f"{p['ticker']} {R.r_now(p, tr['last_close'].get(p['ticker'], p['entry'])):+.1f}R" for p in opens) or "žádné"
    flag = " ⛔ zastavena" if tr.get("stopped") else (f" ⏸ pauza do {tr['pause_until']}" if tr.get("pause_until") else "")
    return (f"**{TRACKS[key]}**: {eq:.2f} EUR ({(eq / start - 1) * 100:+.2f} %)"
            f"{f', SPY {(bench / start - 1) * 100:+.2f} %' if bench else ''} | otevřeno: {op}"
            f"{' | čeká: ' + ', '.join(pend) if pend else ''} | uzavřeno {tr.get('closed_n', 0)} "
            f"(v zisku {tr.get('wins', 0)}), {tr.get('sum_r', 0.0):+.2f} R{flag}")


def status_text() -> str:
    st = load_state()
    if not st.get("tracks"):
        return "🌱 Swing stín zatím neběžel (první sken ráno po ranním přehledu v obchodní den USA)."
    g = st.get("gemini") or {}
    head = (f"🌱 **Swing stín** (jen papír, start {st.get('first_day')}, {st.get('start_source')}) | poslední ráno "
            f"{st.get('last_morning') or '-'}, večer {st.get('last_evening') or '-'} | Gemini dnes {g.get('used', 0)}/"
            f"{SWING_GEMINI_MAX_CALLS_PER_DAY}")
    lines = [head] + [track_line(k, st["tracks"][k]) for k in TRACKS if k in st["tracks"]]
    for k in TRACKS:
        for p in st["tracks"].get(k, {}).get("positions", []):
            if p["status"] == "OPEN":
                lines.append(f"• {k} {p['ticker']}: nákup {p['entry']} ({p['entry_day']}), stop {p['stop']}, cíl {p['target']}, "
                             f"drženo {p.get('days_held', 0)} d{', ' + p['pending_exit'] + ' zítra' if p.get('pending_exit') else ''}")
    return "\n".join(lines)


# ---------------------------------------------------------------- plánovač

def _morning_time() -> dtime:
    h, m = SWING_MORNING_PRAGUE.split(":")
    return dtime(int(h), int(m))


MAX_ATTEMPTS = 3


def _attempt(st: Dict[str, Any], key: str) -> int:
    """Počítadlo pokusů (uložené hned), ať chyba neopakuje drahý sken každých 5 minut donekonečna."""
    att = {k: v for k, v in (st.get("attempts") or {}).items() if k.split(" ")[1] >= (date.today() - timedelta(days=10)).isoformat()}
    att[key] = att.get(key, 0) + 1
    st["attempts"] = att
    fresh = load_state()
    fresh["attempts"] = att
    save_state(fresh)
    return att[key]


async def tick(now: Optional[datetime] = None) -> List[str]:
    """Volá se z bota každých pár minut. Vrací texty pro Discord. Výjimky propadnou do smyčky bota (ta je zaloguje)."""
    if not SWING_SHADOW_ENABLED:
        return []
    assert_shadow_only()
    now = now or datetime.now(market_hours.NY_TZ)
    now_ny, now_pr = now.astimezone(market_hours.NY_TZ), now.astimezone(market_hours.PRAGUE_TZ)
    st = load_state()
    msgs: List[str] = []

    # 1. Večery (i zmeškané), nejdřív, ať ráno vidí aktuální pozice
    if st.get("tracks"):
        today = now_ny.date()
        last_day = today if is_trading_day(today) else prev_trading_day(today)
        b = market_hours.session_bounds("US", datetime.combine(last_day, dtime(12, 0), market_hours.NY_TZ))
        if b and now_ny < b[1] + timedelta(minutes=SWING_EVENING_AFTER_CLOSE_MIN):
            last_day = prev_trading_day(last_day)
        start = (date.fromisoformat(st["last_evening"]) + timedelta(days=1)) if st.get("last_evening") \
            else date.fromisoformat(st["first_day"])
        d, done = start, 0
        while d <= last_day and done < MAX_CATCHUP_EVENINGS:
            if is_trading_day(d):
                try:
                    res = await run_evening(d, st)
                except Exception as e:
                    if _attempt(st, f"evening {d}") < MAX_ATTEMPTS:
                        raise
                    fresh = load_state()
                    st.clear()
                    st.update(fresh)    # rozpracované změny z neúspěšného běhu zahodit
                    logger.error(f"🌙 Swing: večer {d} selhal {MAX_ATTEMPTS}x ({e}), přeskakuji", exc_info=True)
                    log_event({"type": "evening_failed", "day": d.isoformat(), "error": str(e)})
                    res = {"text": f"🌙 Swing stín {d}: večerní zpracování selhalo, den přeskočen"}
                if res is None:
                    logger.info(f"🌙 Swing: denní data za {d} ještě nejsou, zkusím později")
                    break
                st["last_evening"] = d.isoformat()
                save_state(st)
                logger.info(res["text"].replace("**", "").replace("\n", " || "))
                msgs.append(res["text"])
                done += 1
            d += timedelta(days=1)

    # 2. Ráno
    today = now_ny.date()
    b = market_hours.session_bounds("US", now_ny)
    if b and st.get("last_morning") != today.isoformat():
        evening_ok = not st.get("tracks") or st.get("last_evening", "") >= prev_trading_day(today).isoformat() \
            or st.get("first_day", "") >= today.isoformat()
        if now_ny >= b[0] + timedelta(minutes=ENTRY_DELAY_MIN):
            if st.get("skipped_morning") != today.isoformat():
                st["skipped_morning"] = today.isoformat()
                save_state(st)
                log_event({"type": "morning_missed", "day": today.isoformat()})
                logger.info(f"🌱 Swing: ranní sken {today} zmeškán (bot neběžel před 09:45 New York), dnes bez vstupů")
        elif now_pr.time() >= _morning_time() and evening_ok:
            try:
                res = await run_morning(today, st)
            except Exception as e:
                if _attempt(st, f"morning {today}") < MAX_ATTEMPTS:
                    raise
                fresh = load_state()
                st.clear()
                st.update(fresh)    # rozpracované změny z neúspěšného běhu zahodit
                logger.error(f"🌱 Swing: ranní sken {today} selhal {MAX_ATTEMPTS}x ({e}), dnes bez vstupů", exc_info=True)
                log_event({"type": "morning_failed", "day": today.isoformat(), "error": str(e)})
                res = {"text": f"🌱 Swing stín {today}: ranní sken selhal, dnes bez vstupů"}
            st["last_morning"] = today.isoformat()
            save_state(st)
            logger.info(res["text"].replace("**", "").replace("\n", " || "))
            msgs.append(res["text"])
    return msgs
