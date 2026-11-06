"""Dynamický výběr US akcií: kde se na trhu právě něco děje (uživatel 28.10.).

Bot nesmí sledovat jen pevný watchlist. Každých 30 minut v obchodní den USA (od 07:00 do 16:00 New York,
tedy i v premarketu) projde:
- Yahoo screenery: největší růst dne, nejobchodovanější tituly, růst menších firem,
- firmy, které dnes nebo včera zveřejnily výsledky (kalendář Nasdaq),
a vybere akcie s velkým pohybem nahoru a nadprůměrným objemem (gap v premarketu, breakout s objemem,
pokračování po včerejším skoku). Ty se zařadí ke sledování na zbytek dne a Scout na nich hledá
stejné nákupní signály jako na watchlistu. Bot nakupuje jen long, proto se hledají jen pohyby nahoru.

Výběr se ukládá do dynamic_tickers.json (jen dnešní den), důvod zařazení dostane i Gemini do analýzy.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import market_hours
from config import BASE_DIR, US_STOCKS_WATCHLIST, EU_STOCKS_WATCHLIST, CRYPTO_WATCHLIST

logger = logging.getLogger("TradingBot.Momentum")

DYNAMIC_FILE = BASE_DIR / "dynamic_tickers.json"
MAX_DYNAMIC_TICKERS = 10          # kolik akcií smí skener za den přidat
MAX_DAY2_TICKERS = 4              # z toho nejvýš tolik "včerejších skoků", zbytek pro dnešní pohyby
SCREENS = ["day_gainers", "most_actives", "small_cap_gainers"]
SCREEN_COUNT = 50
MAX_EARNINGS_CHECKS = 30          # z kalendáře výsledků se dotazují jen největší firmy (yfinance info je pomalé)

MIN_PRICE = 5.0                   # žádné penny akcie
MIN_MARKET_CAP = 1e9              # žádné mikrofirmy (snadno manipulovatelné)
MIN_AVG_DOLLAR_VOLUME = 20e6      # průměrný denní obrat v USD, aby šlo vstoupit i vystoupit
MIN_CHANGE_PCT = 4.0              # pohyb dne / gap v premarketu
MIN_CHANGE_PCT_EARNINGS = 3.0     # u výsledků stačí menší pohyb, katalyzátor je jasný
MIN_REL_VOLUME = 1.5              # objem proti průměru (v seanci přepočtený na uplynulý čas)
DAY2_MIN_CHANGE_PCT = 6.0         # včerejší skok, který se dnes v premarketu drží
DAY2_MIN_REL_VOLUME = 2.0
DAY2_MAX_PREMARKET_DROP = -1.0

SCAN_FROM_NY = "07:00"
SCAN_TO_NY = "16:00"

_earnings_cache: Dict[str, Any] = {"date": None, "symbols": {}}


# =====================================================================
# VYHODNOCENÍ JEDNÉ KOTACE (čistá funkce, testuje se bez sítě)
# =====================================================================

def session_fraction(now_ny: datetime) -> float:
    """Kolik z běžné seance (09:30-16:00 NY) už uplynulo, 0..1."""
    start = now_ny.replace(hour=9, minute=30, second=0, microsecond=0)
    frac = (now_ny - start).total_seconds() / (6.5 * 3600)
    return max(0.0, min(1.0, frac))


def evaluate_quote(q: Dict[str, Any], earnings_note: Optional[str], now_ny: datetime) -> Optional[Dict[str, Any]]:
    """Kotace z Yahoo -> kandidát {ticker, change_pct, rel_volume, kind, reason, score}, nebo None."""
    sym = str(q.get("symbol") or "").upper()
    if not sym or "." in sym or "-" in sym or q.get("quoteType", "EQUITY") != "EQUITY":
        return None
    state = str(q.get("marketState") or "").upper()
    reg_price = float(q.get("regularMarketPrice") or 0)
    pre_price = float(q.get("preMarketPrice") or 0)
    in_premarket = state in ("PRE", "PREPRE")
    price = pre_price if in_premarket and pre_price else reg_price
    avg_vol = float(q.get("averageDailyVolume3Month") or q.get("averageDailyVolume10Day") or 0)
    if price < MIN_PRICE or float(q.get("marketCap") or 0) < MIN_MARKET_CAP or avg_vol * price < MIN_AVG_DOLLAR_VOLUME:
        return None

    reg_chg = float(q.get("regularMarketChangePercent") or 0)
    reg_vol = float(q.get("regularMarketVolume") or 0)
    min_chg = MIN_CHANGE_PCT_EARNINGS if earnings_note else MIN_CHANGE_PCT
    catalyst = f", {earnings_note}" if earnings_note else ""

    if in_premarket:
        # regularMarket* je v premarketu ještě včerejší seance
        pre_chg = float(q.get("preMarketChangePercent") or 0)
        y_rel = reg_vol / avg_vol if avg_vol else 0.0
        if pre_chg >= min_chg:
            return {"ticker": sym, "change_pct": round(pre_chg, 2), "rel_volume": None, "kind": "gap",
                    "reason": f"gap v premarketu {pre_chg:+.1f} %{catalyst}", "score": pre_chg * (2 if earnings_note else 1)}
        if reg_chg >= DAY2_MIN_CHANGE_PCT and y_rel >= DAY2_MIN_REL_VOLUME and pre_chg >= DAY2_MAX_PREMARKET_DROP:
            return {"ticker": sym, "change_pct": round(reg_chg, 2), "rel_volume": round(y_rel, 1), "kind": "day2",
                    "reason": f"včera {reg_chg:+.1f} % při {y_rel:.1f}× objemu, v premarketu drží ({pre_chg:+.1f} %){catalyst}",
                    "score": reg_chg * min(y_rel, 5) / 2}
        return None

    frac = session_fraction(now_ny) if state == "REGULAR" else 1.0
    rel = (reg_vol / avg_vol) / max(frac, 0.25) if avg_vol else 0.0  # spodní mez 0.25 jako ve Scoutovi
    if reg_chg >= min_chg and rel >= MIN_REL_VOLUME:
        return {"ticker": sym, "change_pct": round(reg_chg, 2), "rel_volume": round(rel, 1), "kind": "momentum",
                "reason": f"dnes {reg_chg:+.1f} % při {rel:.1f}× běžného objemu{catalyst}",
                "score": reg_chg * min(rel, 5) * (2 if earnings_note else 1)}
    return None


# =====================================================================
# ZDROJE
# =====================================================================

def fetch_screen_quotes() -> List[Dict[str, Any]]:
    import yfinance as yf
    quotes: Dict[str, Dict[str, Any]] = {}
    for name in SCREENS:
        try:
            for q in (yf.screen(name, count=SCREEN_COUNT) or {}).get("quotes", []):
                quotes.setdefault(q.get("symbol"), q)
        except Exception as e:
            logger.warning(f"🚀 Yahoo screener {name} nedostupný: {e}")
    return list(quotes.values())


def earnings_symbols(now_prague: datetime) -> Dict[str, str]:
    """{symbol: poznámka} pro firmy s výsledky dnes a minulý obchodní den. Stahuje se jednou denně."""
    import news_briefing
    today = now_prague.date()
    if _earnings_cache["date"] == today:
        return _earnings_cache["symbols"]
    prev = today - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    out: Dict[str, str] = {}
    # Dnešní kalendář výsledků už stáhl ranní přehled, znovu se nestahuje
    briefing_earnings = ((news_briefing.load_briefing() or {}).get("raw") or {}).get("earnings") or {}
    for d, label in ((prev, "včera"), (today, "dnes")):
        try:
            rows = briefing_earnings.get(d.isoformat())
            if rows is None:
                rows = news_briefing.fetch_earnings_calendar(d.isoformat())
            for r in rows:
                sym = str(r.get("symbol") or "").upper()
                if not sym:
                    continue
                if label == "včera" and r.get("time") == "před otevřením":
                    continue  # včerejší výsledky před otevřením už trh včera vstřebal
                out[sym] = f"výsledky {label} ({r.get('time')})"
        except Exception as e:
            logger.warning(f"🚀 Kalendář výsledků ({d}) nedostupný: {e}")
    _earnings_cache.update(date=today, symbols=out)
    return out


def fetch_info_quotes(symbols: List[str]) -> List[Dict[str, Any]]:
    """Kotace firem, které nejsou ve screenerech (yfinance .info má stejná pole jako screener)."""
    import yfinance as yf
    out = []
    for s in symbols:
        try:
            info = yf.Ticker(s).info or {}
            if info.get("symbol"):
                out.append(info)
        except Exception as e:
            logger.debug(f"yfinance info {s}: {e}")
    return out


# =====================================================================
# ULOŽENÍ A POUŽITÍ
# =====================================================================

def _today() -> str:
    return market_hours.get_prague_now().date().isoformat()


def load_state() -> Dict[str, Any]:
    try:
        with open(DYNAMIC_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("date") == _today():
            return data
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.error(f"Chyba při čtení {DYNAMIC_FILE}: {e}")
    return {"date": _today(), "tickers": [], "last_scan": None}


def _save(data: Dict[str, Any]) -> None:
    tmp = DYNAMIC_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(DYNAMIC_FILE)


def get_dynamic_tickers() -> List[str]:
    """Akcie, které dnes zařadil skener (Scout je skenuje s US watchlistem)."""
    return [x["ticker"] for x in load_state().get("tickers", [])]


def get_entry(ticker: str) -> Optional[Dict[str, Any]]:
    return next((x for x in load_state().get("tickers", []) if x.get("ticker") == ticker.upper()), None)


def is_scan_time(now: Optional[datetime] = None) -> bool:
    """Obchodní den USA mezi SCAN_FROM_NY a SCAN_TO_NY (premarket i seance)."""
    now_ny = (now or datetime.now(market_hours.NY_TZ)).astimezone(market_hours.NY_TZ)
    if not market_hours.session_bounds("US", now_ny):
        return False
    t = now_ny.strftime("%H:%M")
    return SCAN_FROM_NY <= t < SCAN_TO_NY


def merge_candidates(state: Dict[str, Any], candidates: List[Dict[str, Any]], now_iso: str) -> List[Dict[str, Any]]:
    """Doplní nové kandidáty do dnešního výběru (max MAX_DYNAMIC_TICKERS), už zařazeným obnoví čísla.

    Zařazené tickery zůstávají do konce dne, aby Scout nepřišel o rozjetý setup. Vrací nově přidané.
    """
    fixed = set(US_STOCKS_WATCHLIST + EU_STOCKS_WATCHLIST + CRYPTO_WATCHLIST)
    current = {x["ticker"]: x for x in state.setdefault("tickers", [])}
    added = []
    for c in sorted(candidates, key=lambda c: c["score"], reverse=True):
        if c["ticker"] in fixed:
            continue
        if c["ticker"] in current:
            current[c["ticker"]].update(last_change_pct=c["change_pct"], last_rel_volume=c["rel_volume"], updated_at=now_iso)
            continue
        if len(state["tickers"]) >= MAX_DYNAMIC_TICKERS:
            continue
        if c["kind"] == "day2" and sum(x.get("kind") == "day2" for x in state["tickers"]) >= MAX_DAY2_TICKERS:
            continue  # místo musí zůstat na dnešní gappery a pohyby v seanci
        entry = {"ticker": c["ticker"], "reason": c["reason"], "kind": c["kind"], "change_pct": c["change_pct"],
                 "rel_volume": c["rel_volume"], "added_at": now_iso, "headlines": []}
        state["tickers"].append(entry)
        current[c["ticker"]] = entry
        added.append(entry)
    return added


def run_scan() -> List[Dict[str, Any]]:
    """Jeden průchod skeneru (blokující, volat přes asyncio.to_thread). Vrací nově zařazené akcie."""
    import news_briefing
    import trading212

    now_prague = market_hours.get_prague_now()
    now_ny = now_prague.astimezone(market_hours.NY_TZ)
    earnings = earnings_symbols(now_prague)

    quotes = fetch_screen_quotes()
    seen = {q.get("symbol") for q in quotes}
    fixed = set(US_STOCKS_WATCHLIST)
    missing = [s for s in earnings if s not in seen and s not in fixed][:MAX_EARNINGS_CHECKS]
    quotes += fetch_info_quotes(missing)

    candidates = []
    for q in quotes:
        c = evaluate_quote(q, earnings.get(str(q.get("symbol") or "").upper()), now_ny)
        if c and trading212.to_t212_ticker(c["ticker"]):
            candidates.append(c)

    state = load_state()
    now_iso = now_prague.isoformat(timespec="seconds")
    added = merge_candidates(state, candidates, now_iso)
    if added:
        try:
            news = news_briefing.fetch_company_news([x["ticker"] for x in added], per_ticker=3)
            for x in added:
                x["headlines"] = news.get(x["ticker"], [])
        except Exception as e:
            logger.warning(f"🚀 Zprávy k novým tickerům nejde stáhnout: {e}")
    state["last_scan"] = now_iso
    _save(state)
    logger.info(
        f"🚀 MOMENTUM SKENER: prověřeno {len(quotes)} kotací, kandidátů {len(candidates)}, "
        f"nově zařazeno: {', '.join(x['ticker'] for x in added) or 'nic'} | sleduji {len(state['tickers'])}/{MAX_DYNAMIC_TICKERS}"
    )
    return added


def get_context(ticker: str) -> str:
    """Věta pro prompt Gemini: proč je akcie dnes sledovaná."""
    x = get_entry(ticker)
    if not x:
        return ""
    text = f"{x['ticker']} zařadil dnes momentum skener: {x['reason']}."
    if x.get("headlines"):
        text += " Zprávy: " + " | ".join(x["headlines"][:3])
    return text


def format_added_message(added: List[Dict[str, Any]]) -> str:
    lines = ["🚀 **Nově sleduji (skener pohybů a zpráv):**"]
    for x in added:
        head = f" – _{x['headlines'][0]}_" if x.get("headlines") else ""
        lines.append(f"• **{x['ticker']}**: {x['reason']}{head}")
    lines.append("Scout na nich teď hledá stejné nákupní signály jako na watchlistu.")
    text = "\n".join(lines)
    return text if len(text) <= 1900 else text[:1897] + "..."


def format_today_section() -> List[str]:
    tickers = load_state().get("tickers", [])
    if not tickers:
        return []
    return ["\n🚀 **Dnes zařazeno skenerem:**"] + [f"• **{x['ticker']}**: {x['reason']}" for x in tickers]
