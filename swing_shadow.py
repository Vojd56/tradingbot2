"""Swing: vícedenní držení akcií US a EU. Stopy B, C a virtuální A jen ve stínu; stopa A naživo přes swing_live (T212 DEMO).

Denní rytmus pro každý trh zvlášť (bot volá tick() každých 5 minut):
- ráno (SWING_MARKETS[trh]["morning"] Praha, nejpozději 30 min před zavřením): sken (swing_scanner), pak tři stopy
  se stejnou matematikou vytvoří čekající vstupy na dnešek:
    A = kandidáti -> Gemini výběr (max 5) -> čerstvé zprávy -> 3 hlasy analytika u top 3 -> BUY při >= 2/3
    B = kandidáti podle skóre (bez Gemini), C = kandidáti v náhodném pořadí (seed = datum, zapsaný v logu)
- večer po zavření trhu: virtuální vstupy za cenu 15 min po otevření (nebo po pozdním skenu), čekající výstupy za open,
  stop/polovina/BE/chandelier/časový stop nad denní svíčkou, Gemini kontrola pozic stopy A, ocenění, souhrn.
Účty stop jsou společné pro US i EU (limity pozic, sektorů a rizika platí dohromady), v EUR.
Zmeškané večery se doženou postupně, ráno po zavření trhu se přeskočí.

Záznamy: swing_shadow.jsonl (append-only, každý záznam s market), stav: swing_shadow_state.json (přežije restart).
"""

import asyncio
import json
import logging
import random
import threading
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

import cost_model
import market_hours
import swing_analyst
import swing_rules as R
import swing_live
import swing_scanner
import swing_lite_team
import swing_insider
from config import (
    BASE_DIR, SWING_SHADOW_ENABLED, SWING_LIVE_ENABLED, SWING_MAX_ANALYZE, SWING_MAX_NEW_PER_DAY,
    SWING_EARNINGS_BLACKOUT_DAYS, SWING_LOSS_STREAK, SWING_LOSS_PAUSE_DAYS, SWING_MAX_DD_PCT,
    SWING_REVIEW_EVERY_DAYS, SWING_START_EQUITY_FALLBACK_EUR, SWING_MORNING_PRAGUE,
    SWING_EVENING_AFTER_CLOSE_MIN, SWING_GEMINI_MAX_CALLS_PER_DAY, SWING_MARKETS, SWING_MAX_OPEN_RISK_PCT,
    SWING_GEMINI_BUDGET,
)

logger = logging.getLogger("TradingBot.Swing")

LOG_FILE = BASE_DIR / "swing_shadow.jsonl"
STATE_FILE = BASE_DIR / "swing_shadow_state.json"
TRACKS = {"A": "A Gemini", "B": "B skóre", "C": "C náhoda", "D": "D Lite tým", "E": "E insideři"}
MARKET_NAMES = {"US": "🇺🇸 US", "EU": "🇪🇺 EU"}
ACCOUNT_CCY = "EUR"              # virtuální účty stop jsou v EUR
PICK_PROMPT_MAX = 15             # Gemini výběr dostane nejvýš 15 nejlepších kandidátů
MAX_CATCHUP_EVENINGS = 5
ENTRY_DELAY_MIN = 15
MORNING_CUTOFF_BEFORE_CLOSE_MIN = 30
FX_CCYS = ("USD", "CHF", "DKK", "SEK", "NOK")
_lock = threading.Lock()


class LiveDisabled(RuntimeError):
    pass


_guard_logged = False


def assert_shadow_only() -> None:
    """Stín (B, C, virtuální A) nikdy nic neposílá. Naživo jen stopa A přes swing_live a jen na T212 DEMO."""
    global _guard_logged
    why = swing_live.demo_guard()
    if SWING_LIVE_ENABLED and why and not _guard_logged:
        _guard_logged = True
        logger.error(f"🌱 Swing: {why}. Běží jen stín.")


def markets() -> List[str]:
    return [m for m, c in SWING_MARKETS.items() if c.get("enabled")]


def _tz(market: str):
    return market_hours.MARKETS[market]["tz"]


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
        st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.error(f"Swing: stav {STATE_FILE.name} nejde přečíst: {e}")
        return {}
    return _migrate(st)


def _migrate(st: Dict[str, Any]) -> Dict[str, Any]:
    """Stav z doby jen US (2. 11. dopoledne) -> stav po trzích."""
    if st and "markets" not in st:
        us = {k: st.pop(k) for k in ("last_morning", "last_evening", "skipped_morning") if k in st}
        if st.get("first_day"):
            us["first_day"] = st["first_day"]
        st["markets"] = {"US": us}
    for tr in (st.get("tracks") or {}).values():
        for p in tr.get("positions", []):
            p.setdefault("market", "US")
            p.setdefault("ccy", "USD")
    for k in ("D", "E"):
        if st.get("tracks") and k not in st["tracks"]:
            # stopa D přidána 2. 11. večer, E (insideři, V3) 3. 11. večer: vlastní účet se stejným startem jako ostatní
            start = next(iter(st["tracks"].values())).get("equity_start", SWING_START_EQUITY_FALLBACK_EUR)
            st["tracks"][k] = _new_track(start)
    return st


def save_state(st: Dict[str, Any]) -> None:
    with _lock:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        tmp.replace(STATE_FILE)


def _mst(st: Dict[str, Any], market: str) -> Dict[str, Any]:
    return st.setdefault("markets", {}).setdefault(market, {})


# ---------------------------------------------------------------- obchodní dny

def is_trading_day(d: date, market: str = "US") -> bool:
    return market_hours.session_bounds(market, datetime.combine(d, dtime(12, 0), _tz(market))) is not None


def add_trading_days(d: date, n: int, market: str = "US") -> date:
    while n > 0:
        d += timedelta(days=1)
        if is_trading_day(d, market):
            n -= 1
    return d


def prev_trading_day(d: date, market: str = "US") -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d, market):
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


def _fx_to_eur(ccy: str) -> Optional[float]:
    if ccy == ACCOUNT_CCY:
        return 1.0
    try:
        import trading212
        return trading212.get_fx_rate_sync(ccy, ACCOUNT_CCY)
    except Exception:
        return None


def _usd_eur() -> Optional[float]:
    return _fx_to_eur("USD")


def fx_map(st: Dict[str, Any]) -> Dict[str, float]:
    """Kurzy měn do EUR (poslední známý, když yfinance nejde)."""
    known = st.setdefault("fx", {})
    if st.get("fx_usd_eur") and "USD" not in known:
        known["USD"] = st["fx_usd_eur"]
    out = {"EUR": 1.0}
    for c in FX_CCYS:
        v = _fx_to_eur(c) or known.get(c)
        if v:
            out[c] = known[c] = v
    st["fx_usd_eur"] = out.get("USD", st.get("fx_usd_eur"))
    return out


def fetch_daily(tickers: List[str]) -> Dict[str, pd.DataFrame]:
    return swing_scanner.download_daily(sorted(set(tickers)), period="6mo")


def fetch_entry_prices(items: List[Tuple[str, datetime]], day: date) -> Dict[str, Dict[str, float]]:
    """Cena vstupu = open první 5M svíčky od `start` (15 min po otevření, nebo po pozdním skenu) a high/low do zavření."""
    import yfinance as yf
    out = {}
    for t, start in items:
        try:
            df = yf.Ticker(t).history(period="5d", interval="5m")
            if df is None or df.empty:
                continue
            idx = df.index if df.index.tz is not None else df.index.tz_localize("UTC")
            df = df.set_axis(idx.tz_convert(start.tzinfo))
            after = df[(df.index >= start) & (df.index.date == day)]
            if after.empty:
                continue
            out[t] = {"fill": float(after["Open"].iloc[0]), "high": float(after["High"].max()),
                      "low": float(after["Low"].min()), "src": f"5M {start.strftime('%H:%M')}"}
        except Exception as e:
            logger.debug(f"Swing 5M {t}: {e}")
    return out


def fetch_news(ticker: str, name: Optional[str] = None) -> List[str]:
    import news_briefing
    return news_briefing.fetch_fresh_news(ticker, news_briefing.company_name(ticker, name) or None)


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

def _new_track(start_eur: float) -> Dict[str, Any]:
    return {"equity_start": start_eur, "cash": start_eur, "equity": start_eur, "peak": start_eur,
            "positions": [], "closed_n": 0, "wins": 0, "sum_r": 0.0, "streak": 0, "pause_until": None,
            "stopped": False, "bench": {}, "last_close": {}}


def _ensure_tracks(st: Dict[str, Any], acct: Dict[str, Any], day: date) -> None:
    if st.get("tracks"):
        return
    st.update(version=2, created=datetime.now(market_hours.PRAGUE_TZ).isoformat(timespec="seconds"),
              first_day=day.isoformat(), real_ccy=acct["real_ccy"], start_source=acct["source"],
              tracks={k: _new_track(acct["start_eur"]) for k in TRACKS})
    log_event({"type": "init", "start_eur": acct["start_eur"], "source": acct["source"], "real_ccy": acct["real_ccy"],
               "tracks": list(TRACKS)})


def _bench_start(st: Dict[str, Any], market: str, regime_close: float) -> None:
    """Srovnávací index (SPY pro US, STOXX 600 pro EU) od prvního skenu trhu."""
    for tr in st["tracks"].values():
        b = tr.setdefault("bench", {})
        if "bench_spy_start" in tr and "US" not in b:
            b["US"] = {"start": tr.pop("bench_spy_start"), "equity": tr.pop("bench_equity", None)}
        b.setdefault(market, {"start": regime_close, "equity": tr["equity_start"]})


def _side(st: Dict[str, Any], ticker: str, ccy: str = "USD") -> float:
    c = cost_model.stock_cost(ticker, ccy, st.get("real_ccy") or ACCOUNT_CCY) or cost_model.stock_cost(ticker, ccy, "EUR")
    return c.side_rate()


def _entry_gate(tr: Dict[str, Any], day: date) -> Optional[str]:
    if tr.get("stopped"):
        return f"stopa zastavena (propad {SWING_MAX_DD_PCT:.0f} % od vrcholu)"
    if tr.get("pause_until") and day < date.fromisoformat(tr["pause_until"]):
        return f"pauza po {SWING_LOSS_STREAK} ztrátách do {tr['pause_until']}"
    return None


def _gemini_used(st: Dict[str, Any], day: date) -> Dict[str, int]:
    g = st.get("gemini") or {}
    if g.get("day") != day.isoformat():
        return {}
    used = g.get("used", {})
    return used if isinstance(used, dict) else {"swing_US": int(used or 0)}   # starý formát: jedno číslo


def _gemini_budget(st: Dict[str, Any], day: date, component: str = "swing_US") -> swing_analyst.Budget:
    """Vlastní denní podíl silných volání pro každou část (swing EU 10, US 10, večer 2)."""
    b = swing_analyst.Budget(_gemini_used(st, day).get(component, 0), SWING_GEMINI_BUDGET.get(component, 0))
    b.component = component
    return b


def _save_budget(st: Dict[str, Any], day: date, b: swing_analyst.Budget) -> None:
    used = dict(_gemini_used(st, day))
    used[getattr(b, "component", "swing_US")] = b.used
    st["gemini"] = {"day": day.isoformat(), "used": used}


def _pos_fx(pos: Dict[str, Any], fx: Dict[str, float]) -> float:
    return fx.get(pos.get("ccy", "USD")) or pos.get("fx_now") or pos.get("fx_entry") or 1.0


def _add_pending(st: Dict[str, Any], key: str, cands: List[Dict[str, Any]], day: date, fx, limit: int,
                 note: Dict[str, Any], market: str = "US", fill_after: Optional[str] = None) -> List[str]:
    """Čekající vstupy na dnešek; limity portfolia (US + EU dohromady) a velikost z plánovaného vstupu (včerejší close)."""
    tr = st["tracks"][key]
    fxm = fx if isinstance(fx, dict) else {"USD": fx, "EUR": 1.0}
    added = []
    for c in cands:
        if len(added) >= limit:
            break
        ccy = c.get("ccy", "USD")
        f = fxm.get(ccy) or 1.0
        p, side = c["plan"], _side(st, c["ticker"], ccy)
        reserved = sum(x.get("planned_notional_acct", 0.0) for x in tr["positions"] if x["status"] == "PENDING")
        size = R.size_position(tr["equity"], tr["cash"] - reserved, p["entry_ref"], p["stop"], side, f)
        why = size["reason"] or R.portfolio_block(tr["positions"], c.get("sector") or f"Unknown:{c['ticker']}",
                                                  size["risk_acct"], tr["equity"], 1.0, c["ticker"], check_heat=False,
                                                  market=market)
        if not why:
            # otevřené riziko v EUR (US + EU dohromady): každá pozice svým kurzem
            heat = sum(x.get("planned_risk_acct", 0.0) if x["status"] == "PENDING" else R.position_open_risk(x, _pos_fx(x, fxm))
                       for x in tr["positions"])
            if heat + size["risk_acct"] > tr["equity"] * SWING_MAX_OPEN_RISK_PCT / 100 + 1e-9:
                why = f"otevřené riziko {heat + size['risk_acct']:.2f} > {SWING_MAX_OPEN_RISK_PCT} % účtu"
        rec = {"type": "risk", "market": market, "track": key, "day": day.isoformat(), "ticker": c["ticker"],
               "score": c["score"], "plan": p, "size": size, "decision": "REJECT" if why else "PENDING", "reason": why or ""}
        log_event(rec)
        if why:
            continue
        tr["positions"].append({
            "id": f"{key}-{day.isoformat()}-{c['ticker']}", "track": key, "market": market, "ccy": ccy, "ticker": c["ticker"],
            "sector": c.get("sector", "Unknown"), "status": "PENDING", "entry_day": day.isoformat(), "fill_after": fill_after,
            "plan_stop": p["stop"], "target": p["target"], "entry_ref": p["entry_ref"], "score": c["score"],
            "atr14": c["ind"]["atr14"], "cost_side": side, "planned_risk_acct": size["risk_acct"],
            "planned_notional_acct": size["notional_acct"], "note": note.get(c["ticker"], {}),
            **{k: c[k] for k in ("time_stop_days", "max_hold_days") if c.get(k)}})
        added.append(c["ticker"])
    return added


# ---------------------------------------------------------------- ráno

async def run_morning(day: date, st: Dict[str, Any], market: str = "US", now: Optional[datetime] = None) -> Dict[str, Any]:
    import momentum_scanner
    import news_briefing
    acct = await asyncio.to_thread(_account_info) if not st.get("tracks") else {}
    fxm = await asyncio.to_thread(fx_map, st)
    if market == "US":
        briefing, dyn = news_briefing.get_extra_tickers(), momentum_scanner.get_dynamic_tickers("US")
    else:
        briefing, dyn = [], momentum_scanner.get_dynamic_tickers("EU")
    last_bo = add_trading_days(day, SWING_EARNINGS_BLACKOUT_DAYS, market)
    res = await asyncio.to_thread(swing_scanner.scan, day, last_bo, dyn, briefing,
                                  st.get("real_ccy") or acct.get("real_ccy", "EUR"), fetch_news,
                                  swing_scanner.earnings_date, None, None, market, fxm)
    log_event({"type": "scan", "market": market, "day": day.isoformat(), "ok": res.get("ok"), "reason": res.get("reason"),
               "regime_ok": res.get("regime_ok"), "regime_sym": res.get("regime_sym"), "regime_close": res.get("spy_close"),
               "regime_sma200": res.get("spy_sma200"), "stats": res.get("stats"), "rejected": res.get("rejected"),
               "candidates": [{"ticker": c["ticker"], "ccy": c.get("ccy"), "score": c["score"], "parts": c["parts"],
                               "sector": c.get("sector"), "plan": c["plan"], "ind": c["ind"], "headlines": c.get("headlines"),
                               "earnings_date": c.get("earnings_date")} for c in res.get("candidates", [])]})
    name = MARKET_NAMES[market]
    if not res.get("ok"):
        return {"text": f"🌱 Swing {name} {day}: sken selhal ({res.get('reason')})"}
    res.setdefault("regime_sym", SWING_MARKETS[market]["regime"])
    if not st.get("tracks"):
        _ensure_tracks(st, acct, day)
    _bench_start(st, market, res["spy_close"])
    tz = _tz(market)
    now_l = (now or datetime.now(tz)).astimezone(tz)
    open_dt = market_hours.session_bounds(market, now_l)[0]
    fill_after = max(open_dt + timedelta(minutes=ENTRY_DELAY_MIN), now_l + timedelta(minutes=5)).isoformat(timespec="minutes")
    cands = res["candidates"]
    out: Dict[str, List[str]] = {k: [] for k in TRACKS}
    notes: Dict[str, str] = {}

    if not res["regime_ok"]:
        log_event({"type": "regime_block", "market": market, "day": day.isoformat(), "close": res["spy_close"],
                   "sma200": res["spy_sma200"]})
        notes = {k: f"{res['regime_sym']} pod SMA200, žádné vstupy" for k in TRACKS}
    else:
        for k in TRACKS:
            st["tracks"].setdefault(k, _new_track(next(iter(st["tracks"].values()))["equity_start"]))
        gates = {k: _entry_gate(st["tracks"][k], day) for k in TRACKS}
        for k, g in gates.items():
            if g:
                notes[k] = g
                log_event({"type": "gate", "market": market, "track": k, "day": day.isoformat(), "reason": g})
        # A: Gemini výběr + čerstvé zprávy + 3 hlasy
        if not gates["A"] and cands:
            budget = _gemini_budget(st, day, f"swing_{market}")
            try:
                pk = await swing_analyst.pick(cands[:PICK_PROMPT_MAX], budget, market=market)
                log_event({"type": "gemini_pick", "market": market, "day": day.isoformat(), **pk})
                picked = {p["ticker"]: p for p in pk["picks"]}
                top = sorted((c for c in cands if c["ticker"] in picked), key=lambda c: c["score"], reverse=True)[:SWING_MAX_ANALYZE]
                approved, info = [], {}
                for c in top:
                    c = await refresh_news(c, market, day)
                    try:
                        v = await swing_analyst.vote(c, budget, picked[c["ticker"]], market=market)
                    except swing_analyst.BudgetExceeded as e:
                        # rozpočet došel uprostřed: schválené dřív platí, zbytek se dnes neposuzuje
                        notes["A_budget"] = f"rozpočet Gemini došel, neposouzeno: {c['ticker']} a další"
                        log_event({"type": "track_partial", "market": market, "track": "A", "day": day.isoformat(),
                                   "reason": str(e), "stopped_at": c["ticker"]})
                        break
                    log_event({"type": "gemini_votes", "market": market, "day": day.isoformat(), **v})
                    if v["buy"]:
                        approved.append(c)
                        info[c["ticker"]] = {"pick": picked[c["ticker"]], "buy_votes": v["buy_votes"], "model": v["votes"][0]["model"]}
                out["A"] = _add_pending(st, "A", approved, day, fxm, SWING_MAX_ANALYZE, info, market, fill_after)
                live = await asyncio.to_thread(swing_live.plan_entries, approved, day, None, market)
                if live:
                    notes["A_live"] = f"DEMO limitní nákup od {fill_after[11:16]} ({market}): {', '.join(live)}"
                if not approved:
                    notes["A"] = f"Gemini vybral {len(pk['picks'])}, BUY 0"
            except swing_analyst.BudgetExceeded as e:
                notes["A"] = f"stopa A dnes vynechána ({market}): {e}"
                log_event({"type": "track_skip", "market": market, "track": "A", "day": day.isoformat(), "reason": str(e)})
                logger.warning(f"🌱 Swing: {notes['A']}")
            finally:
                _save_budget(st, day, budget)
        if not gates["B"]:
            out["B"] = _add_pending(st, "B", cands, day, fxm, SWING_MAX_NEW_PER_DAY, {}, market, fill_after)
        # D: Lite tým (6 rolí na Flash Lite) nad stejnými top kandidáty podle skóre
        if not gates.get("D") and cands:
            budget_d = _gemini_budget(st, day, "swing_D")
            approved_d, info_d = [], {}
            try:
                for c in sorted(cands, key=lambda c: c["score"], reverse=True)[:SWING_MAX_ANALYZE]:
                    c = await refresh_news(c, market, day)
                    r = await swing_lite_team.team_decide(c, budget_d, market)
                    log_event({"type": "lite_team", "market": market, "track": "D", "day": day.isoformat(), **r})
                    if r["buy"]:
                        approved_d.append(c)
                        info_d[c["ticker"]] = {"buy_votes": r["buy_votes"], "skeptic": r["skeptic"]["verdict"]}
            except swing_analyst.BudgetExceeded as e:
                notes["D"] = f"stopa D dnes neúplná ({market}): {e}"
                log_event({"type": "track_skip", "market": market, "track": "D", "day": day.isoformat(), "reason": str(e)})
            finally:
                _save_budget(st, day, budget_d)
            out["D"] = _add_pending(st, "D", approved_d, day, fxm, SWING_MAX_ANALYZE, info_d, market, fill_after)
            if not approved_d and "D" not in notes:
                notes["D"] = "Lite tým: žádný BUY"
        if not gates["C"]:
            seed = int(day.strftime("%Y%m%d")) + (0 if market == "US" else 1)
            pool = list(cands)
            random.Random(seed).shuffle(pool)
            log_event({"type": "random_pick", "market": market, "track": "C", "day": day.isoformat(), "seed": seed,
                       "order": [c["ticker"] for c in pool]})
            out["C"] = _add_pending(st, "C", pool, day, fxm, SWING_MAX_NEW_PER_DAY, {}, market, fill_after)
        # E: oportunistické nákupy insiderů z SEC Form 4 (pravidlo V3), jen US, bez Gemini
        if "E" in TRACKS and not gates.get("E"):
            if market != "US":
                notes["E"] = "jen US"
            else:
                try:
                    ce = await asyncio.to_thread(_track_e_candidates, day, st, last_bo)
                    out["E"] = _add_pending(st, "E", ce, day, fxm, SWING_MAX_NEW_PER_DAY,
                                            {c["ticker"]: {"insider": c.get("insider")} for c in ce}, market, fill_after)
                    if not ce:
                        notes["E"] = "žádný nákup insiderů dle pravidla"
                except Exception as e:
                    notes["E"] = f"stopa E dnes vynechána: {e}"
                    log_event({"type": "track_skip", "market": market, "track": "E", "day": day.isoformat(), "reason": str(e)})
                    logger.warning(f"🌱 Swing: {notes['E']}")
    s = res["stats"]
    parts = [f"{TRACKS[k]}: {', '.join(out[k]) or notes.get(k, 'nic')}" for k in TRACKS]
    text = (f"🌱 **Swing {name} {day}**: vesmír {s.get('tradable')}, prošlo filtrem {s.get('passed_universe')}, "
            f"skóre >= 0,6: {s.get('score_ok')}, kandidáti {s.get('candidates')}, {res['regime_sym']} "
            f"{'nad' if res['regime_ok'] else 'POD'} SMA200.\n"
            f"Papírové vstupy (od {fill_after[11:16]} místního času): " + " | ".join(parts)
            + (f"\n🛒 Stopa A naživo: {notes['A_live']}" if notes.get("A_live") else "")
            + (f"\n⚠️ {notes['A_budget']}" if notes.get("A_budget") else ""))
    return {"text": text}


def _track_e_candidates(day: date, st: Dict[str, Any], last_bo: date) -> List[Dict[str, Any]]:
    """Stopa E: signál z podání Form 4 v předchozí obchodní den (t), vstup dnes. Výsledky do 5 dní = nekupovat."""
    t_day = prev_trading_day(day, "US")
    days = [t_day]
    while len(days) < swing_insider.SELL_WINDOW:
        days.append(prev_trading_day(days[-1], "US"))
    buy_days, sell_days = days[:swing_insider.BUY_WINDOW], days
    uni = swing_scanner.load_universe()
    res = swing_insider.candidates(day, t_day, buy_days, sell_days, uni, _side(st, "AAPL", "USD"),
                                   lambda ts: swing_scanner.download_daily(sorted(set(ts)), period="1y"), _upto)
    ok = []
    for c in res["candidates"]:
        why = swing_scanner.earnings_block(swing_scanner.earnings_date(c["ticker"]), day, last_bo)
        if why:
            res["rejected"].append({"ticker": c["ticker"], "reason": why})
        else:
            ok.append(c)
    log_event({"type": "insider_scan", "market": "US", "track": "E", "day": day.isoformat(), "signal_day": t_day.isoformat(),
               "update": res["update"], "flags": res["flags"], "rejected": res["rejected"][:20],
               "candidates": [{"ticker": c["ticker"], "score": c["score"], "plan": c["plan"], "insider": c["insider"]}
                              for c in ok]})
    return ok


async def refresh_news(c: Dict[str, Any], market: str, day: date) -> Dict[str, Any]:
    """Čerstvé zprávy těsně před hlasy analytika (1 HTTP dotaz, žádné volání Gemini navíc)."""
    try:
        heads = await asyncio.to_thread(fetch_news, c["ticker"], c.get("name"))
    except Exception as e:
        logger.debug(f"Swing zprávy {c['ticker']}: {e}")
        heads = []
    log_event({"type": "fresh_news", "market": market, "day": day.isoformat(), "ticker": c["ticker"],
               "n": len(heads), "headlines": heads[:5], "had": len(c.get("headlines") or [])})
    return dict(c, headlines=heads[:5]) if heads else c


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
    mk = pos.get("market", "US")
    log_event({"type": "exit", "market": mk, "track": key, "day": day.isoformat(), "id": pos["id"], "ticker": pos["ticker"],
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
                tr["pause_until"] = add_trading_days(day, SWING_LOSS_PAUSE_DAYS, mk).isoformat()
                tr["streak"] = 0
                log_event({"type": "pause", "track": key, "day": day.isoformat(), "until": tr["pause_until"]})
        log_event({"type": "close", "market": mk, "track": key, "day": day.isoformat(), "id": pos["id"], "ticker": pos["ticker"],
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
                       qty=q, qty_open=q, fx_entry=fx, fx_now=fx, last_px=fill, initial_risk_acct=size["risk_acct"],
                       fill_src=src["src"], days_held=0, max_high=fill, min_low=fill, realized_acct=0.0, partial_done=False,
                       next_review=SWING_REVIEW_EVERY_DAYS, last_news_hash=None)
            log_event({"type": "fill", "market": pos.get("market", "US"), "track": key, "day": day.isoformat(), "id": pos["id"],
                       "ticker": pos["ticker"], "price": round(fill, 4), "src": src["src"], "qty": q, "stop": stop,
                       "target": pos["target"], "risk_acct": size["risk_acct"], "notional_acct": size["notional_acct"], "fx": fx})
            R.update_excursions(pos, src["high"], src["low"])
            for ex in R.process_bar(pos, fill, src["high"], src["low"], entry_day=True):
                _sell(st, key, pos, ex["qty"], ex["price"], ex["reason"], day, fx)
            return
    log_event({"type": "cancel", "market": pos.get("market", "US"), "track": key, "day": day.isoformat(), "id": pos["id"],
               "ticker": pos["ticker"], "reason": pos.get("cancel_reason")})


def _fill_start(pos: Dict[str, Any], day: date) -> datetime:
    mk = pos.get("market", "US")
    tz = _tz(mk)
    if pos.get("fill_after"):
        try:
            return datetime.fromisoformat(pos["fill_after"]).astimezone(tz)
        except ValueError:
            pass
    open_t = market_hours.MARKETS[mk]["open"]
    return datetime.combine(day, open_t, tz) + timedelta(minutes=ENTRY_DELAY_MIN)


async def run_evening(day: date, st: Dict[str, Any], market: str = "US") -> Optional[Dict[str, Any]]:
    """Večerní zpracování dne `day` pro jeden trh. None = data dne ještě nejsou (zkusí se znovu)."""
    tracks = st["tracks"]
    mine = lambda p: p.get("market", "US") == market
    live = [p for tr in tracks.values() for p in tr["positions"] if p["status"] in ("OPEN", "PENDING") and mine(p)]
    real = [p["ticker"] for p in swing_live.live_positions() if p["status"] == "OPEN" and p.get("market", "US") == market]
    tickers = sorted({p["ticker"] for p in live} | set(real))
    regime = SWING_MARKETS[market]["regime"]
    daily = await asyncio.to_thread(fetch_daily, tickers + [regime])
    idx_bar = _row(daily.get(regime), day)
    if not idx_bar:
        return None
    pend = [p for p in live if p["status"] == "PENDING" and p["entry_day"] == day.isoformat()]
    items = list({p["ticker"]: (p["ticker"], _fill_start(p, day)) for p in pend}.values())
    intraday = await asyncio.to_thread(fetch_entry_prices, items, day) if items else {}
    fxm = await asyncio.to_thread(fx_map, st)
    trail = {t: _trail_inputs(daily.get(t), day) for t in tickers}

    for key, tr in tracks.items():
        for pos in list(tr["positions"]):
            if not mine(pos):
                continue
            t, fx = pos["ticker"], _pos_fx(pos, fxm)
            bar = _row(daily.get(t), day)
            if pos["status"] == "PENDING":
                if pos["entry_day"] == day.isoformat():
                    _fill_entry(st, key, pos, day, intraday.get(t), bar, fx)
                elif pos["entry_day"] < day.isoformat():
                    pos.update(status="CANCELLED", cancel_reason="zmeškaný den vstupu")
                    log_event({"type": "cancel", "market": market, "track": key, "day": day.isoformat(), "id": pos["id"],
                               "ticker": t, "reason": pos["cancel_reason"]})
                continue
            if pos["status"] != "OPEN" or pos["entry_day"] >= day.isoformat():
                continue
            if not bar:
                log_event({"type": "no_data", "market": market, "track": key, "day": day.isoformat(), "ticker": t})
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
            if pos["status"] != "OPEN" or not mine(pos):
                continue
            t, bar = pos["ticker"], _row(daily.get(pos["ticker"]), day)
            ti = trail.get(t) or {}
            if ti.get("hh22"):
                ns = R.next_stop(pos, ti["hh22"], ti["atr22"])
                if ns["moved"]:
                    log_event({"type": "stop_update", "market": market, "track": key, "day": day.isoformat(), "id": pos["id"],
                               "ticker": t, "old": pos["stop"], "new": ns["stop"], "why": ns["why"]})
                    pos["stop"] = ns["stop"]
            if ti.get("atr14"):
                pos["atr14"] = round(ti["atr14"], 4)
            if bar:
                tr["last_close"][t] = bar["close"]
                pos["last_px"], pos["fx_now"] = bar["close"], _pos_fx(pos, fxm)
            if R.time_stop_due(pos) and not pos.get("pending_exit"):
                pos["pending_exit"] = "TIME_STOP"
                log_event({"type": "exit_scheduled", "market": market, "track": key, "day": day.isoformat(), "id": pos["id"],
                           "ticker": t, "reason": "TIME_STOP", "mfe_r": pos.get("mfe_r"), "days_held": pos.get("days_held")})

    gem = await _gemini_check(st, day, market)
    real_msgs = await asyncio.to_thread(swing_live.evening, day, {t: _row(daily.get(t), day) for t in real}, trail, gem,
                                        None, None, market)

    lines = []
    for key, tr in tracks.items():
        mv = sum(p["qty_open"] * p.get("last_px", p["entry"]) * (p.get("fx_now") or p.get("fx_entry") or 1.0)
                 for p in tr["positions"] if p["status"] == "OPEN")
        tr["equity"] = round(tr["cash"] + mv, 4)
        tr["peak"] = max(tr["peak"], tr["equity"])
        dd = 1 - tr["equity"] / tr["peak"] if tr["peak"] else 0.0
        if dd * 100 >= SWING_MAX_DD_PCT and not tr["stopped"]:
            tr["stopped"] = True
            log_event({"type": "track_stopped", "track": key, "day": day.isoformat(), "dd_pct": round(dd * 100, 2)})
        b = tr.setdefault("bench", {}).get(market)
        if b and b.get("start"):
            b["equity"] = round(tr["equity_start"] * idx_bar["close"] / b["start"], 4)
        closed = [p for p in tr["positions"] if p["status"] in ("CLOSED", "CANCELLED")]
        tr["positions"] = [p for p in tr["positions"] if p["status"] in ("OPEN", "PENDING")]
        tr.setdefault("history", []).extend({k: p.get(k) for k in ("id", "market", "status", "r_net", "cancel_reason")} for p in closed)
        tr["history"] = tr["history"][-200:]
        lines.append(track_line(key, tr))
    log_event({"type": "day_summary", "market": market, "day": day.isoformat(), "regime_close": idx_bar["close"], "fx": fxm,
               "tracks": {k: {x: tr.get(x) for x in ("equity", "cash", "peak", "bench", "closed_n", "wins", "sum_r",
                                                       "streak", "pause_until", "stopped")} |
                          {"open": [f"{p['ticker']}" for p in tr["positions"] if p["status"] == "OPEN"]}
                          for k, tr in tracks.items()}})
    text = f"🌙 **Swing {MARKET_NAMES[market]} {day}** (papírové stopy, účty společné US+EU)\n" + "\n".join(lines)
    if real_msgs:
        text += "\n" + "\n".join(real_msgs)
    return {"text": text}


async def _gemini_check(st: Dict[str, Any], day: date, market: str = "US") -> Dict[str, Dict[str, Any]]:
    """Kontrola pozic stopy A jednoho trhu. Vrací ticker -> výsledek (stejné rozhodnutí platí i pro živou stopu A)."""
    tr = st["tracks"]["A"]
    items, by_id = [], {}
    for pos in tr["positions"]:
        if pos["status"] != "OPEN" or pos.get("pending_exit") or pos["entry_day"] >= day.isoformat() \
                or pos.get("market", "US") != market:
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
        return {}
    budget = _gemini_budget(st, day, "evening")
    try:
        res = await swing_analyst.evening_check(items, budget)
    except swing_analyst.BudgetExceeded as e:
        log_event({"type": "check_skip", "market": market, "track": "A", "day": day.isoformat(), "reason": str(e)})
        return {}
    finally:
        _save_budget(st, day, budget)
    log_event({"type": "gemini_check", "market": market, "track": "A", "day": day.isoformat(), "model": res["model"],
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
            log_event({"type": "gemini_ignored", "market": market, "track": "A", "day": day.isoformat(), "id": pos["id"],
                       "why": r["ignored"]})
        if r["action"] == "TIGHTEN" and pos.get("atr14"):
            new = R.tighten_stop(pos["stop"], close, pos["atr14"])
            if new > pos["stop"]:
                log_event({"type": "stop_update", "market": market, "track": "A", "day": day.isoformat(), "id": pos["id"],
                           "ticker": pos["ticker"], "old": pos["stop"], "new": new, "why": ["GEMINI_TIGHTEN"]})
                pos["stop"] = new
        elif r["action"] == "SELL":
            pos["pending_exit"] = "GEMINI_SELL"
            log_event({"type": "exit_scheduled", "market": market, "track": "A", "day": day.isoformat(), "id": pos["id"],
                       "ticker": pos["ticker"], "reason": "GEMINI_SELL", "cited": r["cited_ids"], "why": r["reason_cs"]})
    return {by_id[pid][0]["ticker"]: r for pid, r in res["results"].items() if pid in by_id}


# ---------------------------------------------------------------- texty

def _bench_txt(tr: Dict[str, Any]) -> str:
    start = tr.get("equity_start") or 1.0
    names = {"US": "SPY", "EU": "STOXX600"}
    out = []
    for m, b in (tr.get("bench") or {}).items():
        if b.get("equity"):
            out.append(f"{names.get(m, m)} {(b['equity'] / start - 1) * 100:+.2f} %")
    if not out and tr.get("bench_equity"):
        out.append(f"SPY {(tr['bench_equity'] / start - 1) * 100:+.2f} %")
    return (", " + ", ".join(out)) if out else ""


def track_line(key: str, tr: Dict[str, Any]) -> str:
    eq, start = tr.get("equity", 0.0), tr.get("equity_start", 0.0) or 1.0
    parts = []
    for m in ("US", "EU"):
        opens = [p for p in tr["positions"] if p["status"] == "OPEN" and p.get("market", "US") == m]
        pend = [p["ticker"] for p in tr["positions"] if p["status"] == "PENDING" and p.get("market", "US") == m]
        if opens or pend:
            op = ", ".join(f"{p['ticker']} {R.r_now(p, p.get('last_px', tr['last_close'].get(p['ticker'], p['entry']))):+.1f}R"
                           for p in opens) or "-"
            parts.append(f"{m}: {op}{' (čeká ' + ', '.join(pend) + ')' if pend else ''}")
    flag = " ⛔ zastavena" if tr.get("stopped") else (f" ⏸ pauza do {tr['pause_until']}" if tr.get("pause_until") else "")
    return (f"**{TRACKS[key]}**: {eq:.2f} EUR ({(eq / start - 1) * 100:+.2f} %){_bench_txt(tr)} | "
            f"{' | '.join(parts) or 'bez pozic'} | uzavřeno {tr.get('closed_n', 0)} "
            f"(v zisku {tr.get('wins', 0)}), {tr.get('sum_r', 0.0):+.2f} R{flag}")


def status_text() -> str:
    st = load_state()
    if not st.get("tracks"):
        return "🌱 Swing zatím neběžel (první sken ráno v obchodní den)."
    g = st.get("gemini") or {}
    ms = st.get("markets") or {}
    sched = " | ".join(f"{m}: ráno {ms.get(m, {}).get('last_morning') or '-'}, večer {ms.get(m, {}).get('last_evening') or '-'}"
                       for m in markets())
    used = g.get("used") if isinstance(g.get("used"), dict) else {}
    gem = ", ".join(f"{k} {used.get(k, 0)}/{v}" for k, v in SWING_GEMINI_BUDGET.items() if k != "reserve")
    head = (f"🌱 **Swing** (start {st.get('first_day')}, {st.get('start_source')}) | {sched} | Gemini dnes: {gem}")
    lines = [head] + [track_line(k, st["tracks"][k]) for k in TRACKS if k in st["tracks"]] + swing_live.status_lines()
    for m in ("US", "EU"):
        rows = [f"• {k} {p['ticker']}: nákup {p['entry']} ({p['entry_day']}), stop {p['stop']}, cíl ½ {p['target']}, "
                f"drženo {p.get('days_held', 0)} d{', ' + p['pending_exit'] + ' zítra' if p.get('pending_exit') else ''}"
                for k in TRACKS for p in st["tracks"].get(k, {}).get("positions", [])
                if p["status"] == "OPEN" and p.get("market", "US") == m]
        if rows:
            lines += [f"__{MARKET_NAMES[m]} papírové pozice__"] + rows
    return "\n".join(lines)


# ---------------------------------------------------------------- plánovač

def _hm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def _morning_time(market: str = "US") -> dtime:
    return _hm(SWING_MARKETS.get(market, {}).get("morning") or SWING_MORNING_PRAGUE)


MAX_ATTEMPTS = 3


def _attempt(st: Dict[str, Any], key: str) -> int:
    """Počítadlo pokusů (uložené hned), ať chyba neopakuje drahý sken každých 5 minut donekonečna."""
    cutoff = (date.today() - timedelta(days=10)).isoformat()
    att = {k: v for k, v in (st.get("attempts") or {}).items() if k.split(" ")[-1] >= cutoff}
    att[key] = att.get(key, 0) + 1
    st["attempts"] = att
    fresh = load_state()
    fresh["attempts"] = att
    save_state(fresh)
    return att[key]


async def _evenings(st: Dict[str, Any], market: str, now: datetime, msgs: List[str]) -> None:
    ms = _mst(st, market)
    if not st.get("tracks") or not ms.get("first_day"):
        return
    tz = _tz(market)
    now_l = now.astimezone(tz)
    today = now_l.date()
    last_day = today if is_trading_day(today, market) else prev_trading_day(today, market)
    b = market_hours.session_bounds(market, datetime.combine(last_day, dtime(12, 0), tz))
    after = SWING_MARKETS[market].get("evening_after_min", SWING_EVENING_AFTER_CLOSE_MIN)
    if b and now_l < b[1] + timedelta(minutes=after):
        last_day = prev_trading_day(last_day, market)
    start = (date.fromisoformat(ms["last_evening"]) + timedelta(days=1)) if ms.get("last_evening") \
        else date.fromisoformat(ms["first_day"])
    d, done = start, 0
    while d <= last_day and done < MAX_CATCHUP_EVENINGS:
        if is_trading_day(d, market):
            try:
                res = await run_evening(d, st, market)
            except Exception as e:
                if _attempt(st, f"evening {market} {d}") < MAX_ATTEMPTS:
                    raise
                fresh = load_state()
                st.clear()
                st.update(fresh)    # rozpracované změny z neúspěšného běhu zahodit
                logger.error(f"🌙 Swing {market}: večer {d} selhal {MAX_ATTEMPTS}x ({e}), přeskakuji", exc_info=True)
                log_event({"type": "evening_failed", "market": market, "day": d.isoformat(), "error": str(e)})
                res = {"text": f"🌙 Swing {market} {d}: večerní zpracování selhalo, den přeskočen"}
            if res is None:
                logger.info(f"🌙 Swing {market}: denní data za {d} ještě nejsou, zkusím později")
                break
            _mst(st, market)["last_evening"] = d.isoformat()
            save_state(st)
            logger.info(res["text"].replace("**", "").replace("\n", " || "))
            msgs.append(res["text"])
            done += 1
        d += timedelta(days=1)


async def _morning(st: Dict[str, Any], market: str, now: datetime, msgs: List[str]) -> None:
    tz = _tz(market)
    now_l, now_pr = now.astimezone(tz), now.astimezone(market_hours.PRAGUE_TZ)
    today = now_l.date()
    b = market_hours.session_bounds(market, now_l)
    ms = _mst(st, market)
    if not b or ms.get("last_morning") == today.isoformat():
        return
    evening_ok = not ms.get("first_day") or ms.get("last_evening", "") >= prev_trading_day(today, market).isoformat() \
        or ms.get("first_day", "") >= today.isoformat()
    if now_l >= b[1] - timedelta(minutes=MORNING_CUTOFF_BEFORE_CLOSE_MIN):
        if ms.get("skipped_morning") != today.isoformat():
            ms["skipped_morning"] = today.isoformat()
            save_state(st)
            log_event({"type": "morning_missed", "market": market, "day": today.isoformat()})
            logger.info(f"🌱 Swing {market}: ranní sken {today} zmeškán (bot neběžel před koncem seance), dnes bez vstupů")
        return
    if now_pr.time() < _morning_time(market) or not evening_ok:
        return
    try:
        res = await run_morning(today, st, market, now)
    except Exception as e:
        if _attempt(st, f"morning {market} {today}") < MAX_ATTEMPTS:
            raise
        fresh = load_state()
        st.clear()
        st.update(fresh)    # rozpracované změny z neúspěšného běhu zahodit
        logger.error(f"🌱 Swing {market}: ranní sken {today} selhal {MAX_ATTEMPTS}x ({e}), dnes bez vstupů", exc_info=True)
        log_event({"type": "morning_failed", "market": market, "day": today.isoformat(), "error": str(e)})
        res = {"text": f"🌱 Swing {market} {today}: ranní sken selhal, dnes bez vstupů"}
    ms = _mst(st, market)
    ms["last_morning"] = today.isoformat()
    ms.setdefault("first_day", today.isoformat())
    save_state(st)
    logger.info(res["text"].replace("**", "").replace("\n", " || "))
    msgs.append(res["text"])


async def tick(now: Optional[datetime] = None) -> List[str]:
    """Volá se z bota každých 5 minut. Vrací texty pro Discord. Výjimka jednoho trhu nezastaví druhý."""
    if not SWING_SHADOW_ENABLED:
        return []
    assert_shadow_only()
    now = now or datetime.now(market_hours.NY_TZ)
    msgs: List[str] = []
    # 0. Živá stopa A na T212 DEMO: sesouhlasení, vyplnění, nákupy, stopy, výstupy (chyba nezastaví stín)
    try:
        msgs += await asyncio.to_thread(swing_live.step, now)
    except Exception as e:
        logger.error(f"🛒 Swing A naživo: průchod selhal: {e}", exc_info=True)
    st = load_state()
    errors = []
    for m in markets():
        try:
            await _evenings(st, m, now, msgs)      # nejdřív večery (i zmeškané), ať ráno vidí aktuální pozice
            await _morning(st, m, now, msgs)
        except Exception as e:
            errors.append(e)
            logger.error(f"🌱 Swing {m} selhal: {e}", exc_info=True)
            st = load_state()
    if errors and not msgs:
        raise errors[0]
    return msgs
