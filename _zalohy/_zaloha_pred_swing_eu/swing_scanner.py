"""Swing F0 skener: jednou denně po ranním přehledu projde US akcie (S&P 1500 + tickery z přehledu a momentum skeneru)
nad denními svíčkami do včerejšího close. Všechno synchronní, bot ho volá přes asyncio.to_thread.

Výstup: kandidáti se skóre >= 0,6, kteří prošli filtrem vesmíru, výsledky (5 obchodních dní) a plánem obchodu
(RRR po nákladech >= 2). Stejní kandidáti jdou do všech tří stínových stop (A Gemini, B skóre, C náhoda).
"""

import io
import json
import logging
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

import cost_model
import swing_rules as R
from config import BASE_DIR, SWING_MIN_SCORE, US_STOCKS_WATCHLIST

logger = logging.getLogger("TradingBot.SwingScanner")

UNIVERSE_FILE = BASE_DIR / "swing_universe.json"
UNIVERSE_MAX_AGE_DAYS = 7
WIKI = {"S&P 500": "List_of_S%26P_500_companies", "S&P 400": "List_of_S%26P_400_companies",
        "S&P 600": "List_of_S%26P_600_companies"}
DOWNLOAD_CHUNK = 200
NEWS_PRESCORE_MIN = SWING_MIN_SCORE - R.WEIGHTS["news"]   # zprávy přidají max 0,15 -> níž nemá smysl stahovat
MAX_NEWS_FETCH = 30
MAX_EARNINGS_CHECKS = 25
MAX_STALE_DAYS = 5


def fetch_universe_wiki() -> Dict[str, str]:
    """ticker -> GICS sektor ze seznamů S&P 500/400/600 na Wikipedii (tečka -> pomlčka jako u Yahoo)."""
    import requests
    out: Dict[str, str] = {}
    for page in WIKI.values():
        html = requests.get("https://en.wikipedia.org/wiki/" + page,
                            headers={"User-Agent": "Mozilla/5.0 AIBot2.0 swing"}, timeout=30).text
        for t in pd.read_html(io.StringIO(html)):
            cols = {c: str(c).strip().lower() for c in t.columns}
            sym = next((c for c, l in cols.items() if l in ("symbol", "ticker symbol", "ticker")), None)
            sec = next((c for c, l in cols.items() if l.startswith("gics sector") or l == "sector"), None)
            if sym is not None and sec is not None and len(t) > 100:
                for s, g in zip(t[sym].astype(str), t[sec].astype(str)):
                    out[s.strip().upper().replace(".", "-")] = g.strip()
                break
    return out


def load_universe() -> Dict[str, str]:
    """S&P 1500 se sektory, obnova jednou týdně. Bez sítě starý soubor, jinak watchlist (sektor neznámý)."""
    old: Dict[str, Any] = {}
    try:
        old = json.loads(UNIVERSE_FILE.read_text(encoding="utf-8"))
        if (date.today() - date.fromisoformat(old["date"])).days < UNIVERSE_MAX_AGE_DAYS and len(old["tickers"]) > 1000:
            return old["tickers"]
    except Exception:
        pass
    try:
        uni = fetch_universe_wiki()
        if len(uni) > 1000:
            UNIVERSE_FILE.write_text(json.dumps({"date": date.today().isoformat(), "tickers": uni}, indent=0),
                                     encoding="utf-8")
            return uni
        logger.warning(f"📚 Swing: Wikipedie vrátila jen {len(uni)} tickerů")
    except Exception as e:
        logger.warning(f"📚 Swing: seznam S&P 1500 nejde stáhnout: {e}")
    return old.get("tickers") or {t: "Unknown" for t in US_STOCKS_WATCHLIST}


def t212_tradable(sym: str) -> bool:
    """Jde koupit na T212? Bez varování do logu (1500 tickerů), proto ne trading212.to_t212_ticker."""
    import trading212
    known = trading212._known_tickers()
    if known is None:
        return True
    s = sym.upper()
    return (f"{s}_US_EQ" in known or s in trading212._us_by_short_name
            or f"{s.replace('-', '.')}_US_EQ" in known or s.replace("-", ".") in trading212._us_by_short_name)


def download_daily(tickers: List[str], period: str = "2y") -> Dict[str, pd.DataFrame]:
    """Denní svíčky (auto_adjust) po dávkách. Vrací ticker -> DataFrame bez prázdných řádků."""
    import yfinance as yf
    out: Dict[str, pd.DataFrame] = {}
    for i in range(0, len(tickers), DOWNLOAD_CHUNK):
        chunk = tickers[i:i + DOWNLOAD_CHUNK]
        try:
            raw = yf.download(chunk, period=period, interval="1d", auto_adjust=True, progress=False,
                              threads=True, group_by="ticker")
        except Exception as e:
            logger.warning(f"📚 Swing: stažení dávky {i}-{i + len(chunk)} selhalo: {e}")
            continue
        for t in chunk:
            try:
                df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
                df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
                if not df.empty:
                    out[t] = df
            except Exception:
                continue
    return out


def closed_before(df: pd.DataFrame, day: date) -> pd.DataFrame:
    """Jen svíčky před dnem `day` (ráno = data do včerejšího close)."""
    idx = pd.DatetimeIndex(df.index)
    dates = idx.tz_localize(None).date if idx.tz is not None else idx.date
    return df[[d < day for d in dates]]


def earnings_date(ticker: str) -> Optional[date]:
    import yfinance as yf
    try:
        cal = yf.Ticker(ticker).calendar or {}
        dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if not dates:
            return None
        d = dates[0] if isinstance(dates, (list, tuple)) else dates
        return d.date() if isinstance(d, datetime) else d
    except Exception as e:
        logger.debug(f"Swing výsledky {ticker}: {e}")
        return None


def earnings_block(ed: Optional[date], day: date, last_blackout_day: date) -> Optional[str]:
    """Výsledky mezi dneškem a +5 obchodními dny -> nekupovat. Výsledky už zveřejněné (před dneškem) = katalyzátor, ne překážka."""
    if ed and day <= ed <= last_blackout_day:
        return f"výsledky {ed.isoformat()} v příštích 5 obchodních dnech"
    return None


def sector_of(ticker: str, universe: Dict[str, str]) -> str:
    sec = universe.get(ticker)
    if sec and sec != "Unknown":
        return sec
    try:
        import yfinance as yf
        return (yf.Ticker(ticker).info or {}).get("sector") or "Unknown"
    except Exception:
        return "Unknown"


def evaluate(ticker: str, df: pd.DataFrame, day: date, side_rate: float) -> Dict[str, Any]:
    """Filtr vesmíru + skóre bez zpráv + plán obchodu z včerejšího close. Čistá funkce nad tabulkou svíček."""
    ind = R.indicators(df)
    if ind is None:
        return {"ticker": ticker, "reject": "málo dat"}
    if (day - date.fromisoformat(ind["last_date"])).days > MAX_STALE_DAYS:
        return {"ticker": ticker, "reject": f"stará data ({ind['last_date']})"}
    why = R.universe_reject(ind)
    if why:
        return {"ticker": ticker, "reject": why}
    parts = R.score_parts(ind, 0.0)
    plan = R.plan_trade(ind["close"], ind["atr14"], ind["support"], ind["resistance"], side_rate)
    return {"ticker": ticker, "reject": None, "ind": ind, "parts": parts, "prescore": R.total_score(parts), "plan": plan}


def scan(day: date, last_blackout_day: date, extra_tickers: List[str], briefing_tickers: List[str],
         account_ccy: str = "EUR", news_fn: Optional[Callable[[str], List[str]]] = None,
         earnings_fn: Callable[[str], Optional[date]] = earnings_date,
         data: Optional[Dict[str, pd.DataFrame]] = None, universe: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Celý ranní sken. data/universe/news_fn/earnings_fn jdou podstrčit v testu (bez sítě)."""
    t0 = time.time()
    universe = universe if universe is not None else load_universe()
    names = sorted(set(universe) | {t.upper() for t in extra_tickers + briefing_tickers if "." not in t})
    tradable = [t for t in names if t212_tradable(t)]
    if data is None:
        data = download_daily(tradable + ["SPY"])
    stats: Dict[str, Any] = {"universe": len(names), "tradable": len(tradable), "with_data": 0, "rejects": {}}

    spy = data.get("SPY")
    spy_df = closed_before(spy, day) if spy is not None else None
    if spy_df is None or len(spy_df) < 200:
        return {"ok": False, "reason": "chybí data SPY", "stats": stats, "candidates": []}
    spy_close = float(spy_df["Close"].iloc[-1])
    spy_sma200 = float(spy_df["Close"].rolling(200).mean().iloc[-1])

    cost = cost_model.stock_cost("SPY", "USD", account_ccy) or cost_model.stock_cost("SPY", "USD", "EUR")
    side = cost.side_rate()
    evals = []
    for t in tradable:
        df = data.get(t)
        if df is None:
            continue
        stats["with_data"] += 1
        e = evaluate(t, closed_before(df, day), day, side)
        if e["reject"]:
            key = e["reject"].split(" (")[0]
            stats["rejects"][key] = stats["rejects"].get(key, 0) + 1
            continue
        evals.append(e)
    stats["passed_universe"] = len(evals)

    # Zprávy jen u nadějných (skóre bez zpráv + max 0,15 by mohlo dosáhnout 0,6)
    hopeful = sorted((e for e in evals if e["prescore"] >= NEWS_PRESCORE_MIN), key=lambda e: e["prescore"], reverse=True)
    bonus = {t.upper() for t in extra_tickers + briefing_tickers}
    for e in hopeful[:MAX_NEWS_FETCH]:
        heads: List[str] = []
        if news_fn:
            try:
                heads = news_fn(e["ticker"]) or []
            except Exception as ex:
                logger.debug(f"Swing zprávy {e['ticker']}: {ex}")
        e["headlines"] = heads[:5]
        e["parts"]["news"] = R.news_score(heads, e["ticker"] in bonus)
        e["score"] = R.total_score(e["parts"])
    for e in hopeful[MAX_NEWS_FETCH:]:
        e["headlines"], e["score"] = [], e["prescore"]

    scored = sorted((e for e in hopeful if e["score"] >= SWING_MIN_SCORE), key=lambda e: e["score"], reverse=True)
    stats["score_ok"] = len(scored)
    candidates, rejected = [], []
    checks = 0
    for e in scored:
        if not e["plan"]["ok"]:
            rejected.append({"ticker": e["ticker"], "score": e["score"], "reason": e["plan"]["reason"]})
            continue
        if checks >= MAX_EARNINGS_CHECKS:
            break
        checks += 1
        ed = earnings_fn(e["ticker"])
        block = earnings_block(ed, day, last_blackout_day)
        if block:
            rejected.append({"ticker": e["ticker"], "score": e["score"], "reason": block})
            continue
        e["earnings_date"] = ed.isoformat() if ed else None
        e["sector"] = sector_of(e["ticker"], universe)
        candidates.append(e)
    stats["candidates"] = len(candidates)
    stats["seconds"] = round(time.time() - t0, 1)
    return {"ok": True, "spy_close": round(spy_close, 4), "spy_sma200": round(spy_sma200, 4),
            "regime_ok": R.regime_ok(spy_close, spy_sma200), "side_rate": side, "stats": stats,
            "candidates": candidates, "rejected": rejected[:40]}
