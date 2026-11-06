"""Stínový režim ORB15 (M4): každých 5 minut přehraje dnešní den nad uzavřenými svíčkami, NIC nenakupuje.

Výsledky jdou do paper_orb.json (oddělené od paper_trades.json) a souhrn do logu a Discordu po zavření trhu.
Brána pro přepnutí na živé obchody (M5): aspoň 60 obchodů, čistý Profit Factor >= 1,35, max. propad < 12 %.
"""

import asyncio
import json
import logging
import threading
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import bars
import cost_model
import decision_log
import market_hours
import momentum_scanner
import news_briefing
import orb_strategy as orb
from config import BASE_DIR, US_STOCKS_WATCHLIST, RISK_PER_TRADE_PCT_LIVE

logger = logging.getLogger("TradingBot.ORB")

SHADOW_FILE = BASE_DIR / "paper_orb.json"
MAX_CANDIDATES = 10
MAX_SETUP_FETCHES = 30          # strop stahování historie za den (watchlist + skener)
TICKER_TIMEOUT_S = 25
_file_lock = threading.Lock()

# Cache na den: historie (5M za měsíc, denní za 3 měsíce) a ranní pásmo se stahují jednou na ticker
_day_cache: Dict[str, Any] = {"day": None, "setups": {}, "account_ccy": None}


def load_trades() -> Dict[str, Dict[str, Any]]:
    try:
        return json.loads(SHADOW_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.error(f"Chyba při čtení {SHADOW_FILE}: {e}")
        return {}


def save_trades(data: Dict[str, Dict[str, Any]]) -> None:
    with _file_lock:
        tmp = SHADOW_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(SHADOW_FILE)


def candidates() -> List[str]:
    """US watchlist + dnešní US tickery ze skeneru, jen co jde koupit na T212."""
    import trading212
    out = []
    for t in list(US_STOCKS_WATCHLIST) + momentum_scanner.get_dynamic_tickers("US"):
        t = t.upper()
        if t not in out and "." not in t and trading212.to_t212_ticker(t) is not None:
            out.append(t)
    return out


def _account_ccy() -> str:
    if not _day_cache.get("account_ccy"):
        try:
            import trading212
            _day_cache["account_ccy"] = (trading212.get_account_summary_sync() or {}).get("currency") or None
        except Exception:
            pass
    return _day_cache.get("account_ccy") or "EUR"


def _to_ny(df):
    if df is None or df.empty:
        return df
    idx = df.index
    idx = idx.tz_localize("UTC") if idx.tz is None else idx
    return df.set_axis(idx.tz_convert(market_hours.NY_TZ))


def fetch_setup_sync(ticker: str, day: date, now: datetime) -> orb.OrbSetup:
    """Ranní pásmo z ověřených svíček (5M za měsíc pro RVOL15, denní pro ATR)."""
    import yfinance as yf
    t = yf.Ticker(ticker)
    try:
        b5 = _to_ny(bars.fetch_bars(ticker, "5m", "1mo", closed_only=True, yf_ticker=t, now=now))
        d1 = _to_ny(bars.fetch_bars(ticker, "1d", "3mo", closed_only=False, yf_ticker=t, now=now))
    except bars.BarsError as e:
        return orb.OrbSetup(ticker=ticker, day=day.isoformat(), skip_reason=f"data: {e}")
    return orb.build_setup(ticker, b5, d1, day)


def fetch_today_sync(ticker: str, now: datetime):
    """Dnešní uzavřené 5M svíčky, ověřené (interval, max. 15 min staré)."""
    import yfinance as yf
    df = bars.fetch_bars(ticker, "5m", "1d", max_age=timedelta(minutes=15), closed_only=True,
                         yf_ticker=yf.Ticker(ticker), now=now)
    return _to_ny(df)


async def _catalyst(ticker: str) -> Dict[str, Any]:
    import gemini_team
    entry = momentum_scanner.get_entry(ticker) or {}
    name = news_briefing.company_name(ticker, entry.get("name"))
    try:
        heads = await asyncio.to_thread(news_briefing.fetch_fresh_news, ticker, name or None)
    except Exception as e:
        logger.warning(f"ORB {ticker}: zprávy nejdou stáhnout: {e}")
        heads = []
    v = await gemini_team.classify_catalyst(ticker, name, heads)
    return {k: v.get(k) for k in ("direction", "catalyst", "about_target", "reason_cs", "model_used", "cached")}


async def run_pass(now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Jeden průchod po uzavření 5M svíčky. Vrací krátký souhrn pro log, nebo None mimo seanci."""
    now = (now or datetime.now(market_hours.NY_TZ)).astimezone(market_hours.NY_TZ)
    bounds = market_hours.session_bounds("US", now)
    if not bounds:
        return None
    open_dt, close_dt = bounds
    if now < open_dt + timedelta(minutes=15, seconds=10) or now > close_dt + timedelta(minutes=10):
        return None
    day = now.date()
    if _day_cache["day"] != day:
        _day_cache.update(day=day, setups={}, account_ccy=None)

    # 1. Ranní pásma (jednou za den na ticker; ticker přidaný skenerem později se spočítá zpětně)
    setups: Dict[str, orb.OrbSetup] = _day_cache["setups"]
    for t in candidates():
        if t in setups or len(setups) >= MAX_SETUP_FETCHES:
            continue
        try:
            s = await asyncio.wait_for(asyncio.to_thread(fetch_setup_sync, t, day, now), timeout=TICKER_TIMEOUT_S * 2)
        except Exception as e:
            logger.warning(f"ORB {t}: pásmo nejde spočítat: {e}")
            continue
        if s.skip_reason and s.skip_reason.startswith(("chybí svíčky", "data:")):
            continue    # Yahoo ještě nemá poslední svíčku pásma: zkusí se znovu v příštím průchodu
        setups[t] = s

    store = load_trades()
    valid = sorted((s for s in setups.values() if not s.skip_reason), key=lambda s: s.rvol15, reverse=True)[:MAX_CANDIDATES]
    for s in setups.values():
        key = f"{s.day}|{s.ticker}"
        if s.skip_reason and key not in store:
            store[key] = {"ticker": s.ticker, "day": s.day, "status": "NO_SETUP", "reason": s.skip_reason, "setup": s.to_dict()}

    # 2. Přehrání dne u kandidátů s platným pásmem
    acct = _account_ccy()
    counts: Dict[str, int] = {}
    last_bar_end = None
    for s in valid:
        key = f"{s.day}|{s.ticker}"
        prev = store.get(key) or {}
        if prev.get("status") == "CLOSED":
            counts["CLOSED"] = counts.get("CLOSED", 0) + 1
            continue
        try:
            today = await asyncio.wait_for(asyncio.to_thread(fetch_today_sync, s.ticker, now), timeout=TICKER_TIMEOUT_S)
        except Exception as e:
            logger.info(f"📉 ORB {s.ticker}: dnešní svíčky nepoužity ({e})")
            counts["data"] = counts.get("data", 0) + 1
            continue
        if today is not None and not today.empty:
            end = today.index[-1] + orb.BAR
            last_bar_end = end if last_bar_end is None else max(last_bar_end, end)
        cost = cost_model.stock_cost(s.ticker, "USD", acct)
        res = orb.simulate_day(s, today, cost, now=now, close_ny=close_dt.time())
        res["catalyst"] = prev.get("catalyst")
        if res["status"] in ("PENDING", "OPEN", "CLOSED") and not res["catalyst"]:
            res["catalyst"] = await _catalyst(s.ticker)    # Gemini jen u tickeru se signálem, jednou za den
        if res["status"] != prev.get("status") and res["status"] in ("OPEN", "CLOSED"):
            decision_log.log_decision({
                "strategy": "ORB15_SHADOW", "ticker": s.ticker, "market": "STOCK_US",
                "action": "SHADOW_ENTRY" if res["status"] == "OPEN" else "SHADOW_EXIT",
                "entry": res.get("entry"), "stop": res.get("stop"), "cost_share_r": res.get("cost_share_r"),
                "r_net_a": (res.get("variant_a") or {}).get("r_net"), "r_net_b": (res.get("variant_b") or {}).get("r_net"),
                "catalyst": res["catalyst"], "setup": s.to_dict()})
            logger.info(f"🌗 ORB15 stín {s.ticker}: {res['status']} vstup {res.get('entry')} stop {res.get('stop')}"
                        f" (náklady {res.get('cost_share_r')} R, katalyzátor {(res['catalyst'] or {}).get('direction')})")
        store[key] = res
        counts[res["status"]] = counts.get(res["status"], 0) + 1
    save_trades(store)
    lag = (now - last_bar_end).total_seconds() if last_bar_end is not None else None
    return {"setups": len(setups), "valid": len(valid), "counts": counts, "lag_s": lag}


def day_summary(day: Optional[str] = None) -> str:
    """Text pro Discord a log: dnešek + kumulativní stav brány M4."""
    store = list(load_trades().values())
    day = day or datetime.now(market_hours.NY_TZ).date().isoformat()
    today = [t for t in store if t.get("day") == day]
    traded = [t for t in today if t.get("status") in ("OPEN", "CLOSED")]
    rr = ", ".join(f"{t['ticker']} {(t.get('variant_a') or {}).get('r_net')}R" for t in traded) or "žádný"
    all_a = orb.summarize(store, "variant_a")
    all_b = orb.summarize(store, "variant_b")
    gate = orb.summarize(store, "variant_a", only_cost_ok=True, only_catalyst=True)
    dd_pct = lambda s: round(s["max_dd_r"] * RISK_PER_TRADE_PCT_LIVE, 1)
    return (f"🌗 **ORB15 stín {day}**: pásmo platné u {sum(1 for t in today if t.get('status') != 'NO_SETUP')} "
            f"z {len(today)} tickerů, obchody: {rr}.\n"
            f"Celkem A (cíle + VWAP): {all_a['n']} obchodů, {all_a['net_r']} R, PF {all_a['pf']}, propad {dd_pct(all_a)} % | "
            f"B (do 15:50): {all_b['n']}, {all_b['net_r']} R, PF {all_b['pf']} | "
            f"brána (A, náklady OK, katalyzátor): {gate['n']}/60 obchodů, PF {gate['pf']} (cíl 1,35), propad {dd_pct(gate)} % (max 12)")
