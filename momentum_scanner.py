"""Dynamický výběr US akcií: kde se na trhu právě něco děje (uživatel 28.10.).

Bot nesmí sledovat jen pevný watchlist. Každých 30 minut v obchodní den USA (od 07:00 do 16:00 New York,
tedy i v premarketu) projde:
- Yahoo screenery: největší růst dne, nejobchodovanější tituly, růst menších firem,
- firmy, které dnes nebo včera zveřejnily výsledky (kalendář Nasdaq),
a vybere akcie s velkým pohybem nahoru a nadprůměrným objemem (gap v premarketu, breakout s objemem,
pokračování po včerejším skoku). Ty se zařadí ke sledování na zbytek dne a Scout na nich hledá
stejné nákupní signály jako na watchlistu. Bot nakupuje jen long, proto se hledají jen pohyby nahoru.

Výběr se ukládá do dynamic_tickers.json (jen dnešní den), důvod zařazení dostane i Gemini do analýzy.

Doplněno 28. 10. odpoledne (uživatel: chyběly LRCX +5 % a Samsung +6 %):
- Evropa: v seanci EU (09:00-17:30 Berlín) stejný průchod přes Yahoo screener burz Londýn, Xetra, Paříž,
  Amsterdam, Madrid a Curych (+ londýnská GDR jako Samsung SMSN.IL). Jen akcie, které jsou na T212.
- Rotace: místa se už nezaplní na celý den. Akcie, která v tomto průchodu nesplňuje podmínky (pohyb vyprchal),
  uvolní místo silnějšímu kandidátovi; akcie s otevřenou pozicí bota se nevyhazuje.
"""

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import market_hours
from config import BASE_DIR, US_STOCKS_WATCHLIST, EU_STOCKS_WATCHLIST, CRYPTO_WATCHLIST

logger = logging.getLogger("TradingBot.Momentum")

DYNAMIC_FILE = BASE_DIR / "dynamic_tickers.json"
MAX_DYNAMIC_TICKERS = 10          # kolik US akcií sleduje skener najednou (vyprchané se vyměňují)
MAX_DAY2_TICKERS = 2              # z toho nejvýš tolik "včerejších skoků", zbytek pro dnešní pohyby
MAX_DYNAMIC_TICKERS_EU = 8        # kolik evropských akcií sleduje skener najednou
# Burzy pro evropský screener (kód Yahoo -> přípona symbolu, kterou zná trading212.to_t212_ticker)
# 28. 10.: bez "LSE" (britské akcie: daň SDRT 0,5 % z nákupu + převod GBP ~0,8 % na obchod). IOB (GDR v USD,
# např. Samsung SMSN.IL) britskou daň nemá, zůstává.
EU_EXCHANGES = ["IOB", "GER", "PAR", "AMS", "MCE", "EBS"]
EU_SCREEN_SIZE = 40
MAX_EU_CHANGE_PCT = 40.0          # nad tím jde u Yahoo EU skoro vždy o chybnou kotaci (staré ceny na IOB)
MIN_PRICE_EU = 1.0                # v Evropě mají i velké firmy nízkou cenu (Stellantis ~4 EUR), likviditu hlídá obrat
EU_MIN_AVG_SHARES = 10_000        # předfiltr screeneru: průměrný denní objem v kusech (GDR jako Samsung mají ~20 tis. kusů po 4 500 USD)
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


def eu_session_fraction(now: datetime) -> float:
    """Kolik z evropské seance (09:00-17:30 Berlín) už uplynulo, 0..1."""
    local = now.astimezone(market_hours.MARKETS["EU"]["tz"])
    start = local.replace(hour=9, minute=0, second=0, microsecond=0)
    return max(0.0, min(1.0, (local - start).total_seconds() / (8.5 * 3600)))


def _quote_name(q: Dict[str, Any]) -> str:
    """Jméno firmy z kotace Yahoo: bez něj Google News hledal podle zkratky a vracel cizí firmy."""
    return str(q.get("shortName") or q.get("longName") or q.get("displayName") or "").strip()


def eu_earnings_note(q: Dict[str, Any], now: datetime, known: Optional[Dict[str, str]] = None) -> Optional[str]:
    """'výsledky dnes' / 'výsledky včera' pro evropskou firmu. Kalendář výsledků je jen americký, proto datum
    z kotace Yahoo (earningsTimestamp), jinak z ranního přehledu (výsledky firem z watchlistu)."""
    tz = market_hours.MARKETS["EU"]["tz"]
    today = now.astimezone(tz).date()
    prev = today - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    sym = str(q.get("symbol") or "").upper()
    day, when = None, None
    ts = q.get("earningsTimestamp") or q.get("earningsTimestampStart")
    try:
        if ts:
            when = datetime.fromtimestamp(int(ts), tz)
            day = when.date()
        elif known and known.get(sym):
            day = date.fromisoformat(str(known[sym])[:10])
    except (TypeError, ValueError, OSError):
        day = None
    if day == today:
        # Fáze 4: dnešní výsledky jsou katalyzátor, jen když už vyšly (známý čas před teď). Bez času nebo
        # s časem po teď (typicky po zavření) se práh nesnižuje: trh je ještě nezná.
        if when is not None and when <= now.astimezone(tz):
            return "výsledky dnes"
        return None
    if day == prev:
        return "výsledky včera"
    return None


def evaluate_eu_quote(q: Dict[str, Any], now: datetime, known_earnings: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
    """Evropská kotace z Yahoo -> kandidát jen pro pohyb v dnešní seanci (EU nemá premarket jako US).
    U firem s výsledky dnes nebo včera stačí menší pohyb (MIN_CHANGE_PCT_EARNINGS), stejně jako v USA."""
    sym = str(q.get("symbol") or "").upper()
    if "." not in sym or q.get("quoteType", "EQUITY") != "EQUITY":
        return None
    if sym.endswith(".L"):
        return None  # britské akcie z Londýna (SDRT 0,5 %), viz EU_EXCHANGES
    price = float(q.get("regularMarketPrice") or 0)
    if str(q.get("currency") or "") in ("GBp", "GBX"):
        price /= 100.0  # londýnské ceny jsou v pencích
    avg_vol = float(q.get("averageDailyVolume3Month") or q.get("averageDailyVolume10Day") or 0)
    # Obrat se porovnává v měně kotace (EUR, GBP, CHF ~ USD), pro hrubý filtr likvidity to stačí
    mcap = q.get("marketCap")  # u londýnských GDR (SMSN.IL) Yahoo kapitalizaci neuvádí, pak rozhoduje obrat
    if price < MIN_PRICE_EU or (mcap is not None and float(mcap) < MIN_MARKET_CAP) or avg_vol * price < MIN_AVG_DOLLAR_VOLUME:
        return None
    chg = float(q.get("regularMarketChangePercent") or 0)
    if chg > MAX_EU_CHANGE_PCT:
        return None
    rel = (float(q.get("regularMarketVolume") or 0) / avg_vol) / max(eu_session_fraction(now), 0.25) if avg_vol else 0.0
    note = eu_earnings_note(q, now, known_earnings)
    min_chg = MIN_CHANGE_PCT_EARNINGS if note else MIN_CHANGE_PCT
    if chg >= min_chg and rel >= MIN_REL_VOLUME:
        catalyst = f", {note}" if note else ""
        return {"ticker": sym, "name": _quote_name(q), "change_pct": round(chg, 2), "rel_volume": round(rel, 1), "kind": "momentum",
                "reason": f"dnes {chg:+.1f} % při {rel:.1f}× běžného objemu ({q.get('fullExchangeName') or q.get('exchange')}){catalyst}",
                "score": chg * min(rel, 5) * (2 if note else 1)}
    return None


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
            return {"ticker": sym, "name": _quote_name(q), "change_pct": round(pre_chg, 2), "rel_volume": None, "kind": "gap",
                    "reason": f"gap v premarketu {pre_chg:+.1f} %{catalyst}", "score": pre_chg * (2 if earnings_note else 1)}
        if reg_chg >= DAY2_MIN_CHANGE_PCT and y_rel >= DAY2_MIN_REL_VOLUME and pre_chg >= DAY2_MAX_PREMARKET_DROP:
            return {"ticker": sym, "name": _quote_name(q), "change_pct": round(reg_chg, 2), "rel_volume": round(y_rel, 1), "kind": "day2",
                    "reason": f"včera {reg_chg:+.1f} % při {y_rel:.1f}× objemu, v premarketu drží ({pre_chg:+.1f} %){catalyst}",
                    "score": reg_chg * min(y_rel, 5) / 2}
        return None

    frac = session_fraction(now_ny) if state == "REGULAR" else 1.0
    rel = (reg_vol / avg_vol) / max(frac, 0.25) if avg_vol else 0.0  # spodní mez 0.25 jako ve Scoutovi
    if reg_chg >= min_chg and rel >= MIN_REL_VOLUME:
        return {"ticker": sym, "name": _quote_name(q), "change_pct": round(reg_chg, 2), "rel_volume": round(rel, 1), "kind": "momentum",
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


def fetch_eu_quotes() -> List[Dict[str, Any]]:
    """Největší dnešní růst na evropských burzách (Yahoo screener podle burzy, min. 1 mld. kapitalizace)."""
    import yfinance as yf
    from yfinance import EquityQuery as Q
    quotes: Dict[str, Dict[str, Any]] = {}
    for ex in EU_EXCHANGES:
        try:
            query = Q("and", [Q("eq", ["exchange", ex]), Q("gt", ["percentchange", MIN_CHANGE_PCT]),
                              Q("lt", ["percentchange", MAX_EU_CHANGE_PCT]),
                              Q("gt", ["avgdailyvol3m", EU_MIN_AVG_SHARES])])
            res = yf.screen(query, sortField="percentchange", sortAsc=False, size=EU_SCREEN_SIZE) or {}
            for q in res.get("quotes", []):
                quotes.setdefault(q.get("symbol"), q)
        except Exception as e:
            logger.warning(f"🚀 Yahoo EU screener {ex} nedostupný: {e}")
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
                if label == "dnes" and r.get("time") != "před otevřením":
                    continue  # fáze 4: dnešní výsledky po zavření (nebo bez času) trh během seance ještě nezná
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
    return {"date": _today(), "tickers": [], "tickers_eu": [], "last_scan": None}


def _save(data: Dict[str, Any]) -> None:
    tmp = DYNAMIC_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(DYNAMIC_FILE)


def get_dynamic_tickers(region: str = "US") -> List[str]:
    """Akcie, které dnes zařadil skener (Scout je skenuje s watchlistem daného trhu)."""
    return [x["ticker"] for x in load_state().get(_key(region), [])]


def _key(region: str) -> str:
    return "tickers_eu" if region == "EU" else "tickers"


def get_entry(ticker: str) -> Optional[Dict[str, Any]]:
    state = load_state()
    return next((x for x in state.get("tickers", []) + state.get("tickers_eu", [])
                 if x.get("ticker") == ticker.upper()), None)


def is_us_scan_time(now: Optional[datetime] = None) -> bool:
    """Obchodní den USA mezi SCAN_FROM_NY a SCAN_TO_NY (premarket i seance)."""
    now_ny = (now or datetime.now(market_hours.NY_TZ)).astimezone(market_hours.NY_TZ)
    if not market_hours.session_bounds("US", now_ny):
        return False
    t = now_ny.strftime("%H:%M")
    return SCAN_FROM_NY <= t < SCAN_TO_NY


def is_eu_scan_time(now: Optional[datetime] = None) -> bool:
    """Evropská seance (bez premarketu, Yahoo ho pro EU nemá)."""
    return market_hours.is_market_open("EU", now)


def is_scan_time(now: Optional[datetime] = None) -> bool:
    return is_us_scan_time(now) or is_eu_scan_time(now)


def merge_candidates(state: Dict[str, Any], candidates: List[Dict[str, Any]], now_iso: str,
                     region: str = "US", protected: Optional[set] = None) -> Dict[str, List[Dict[str, Any]]]:
    """Doplní nové kandidáty do dnešního výběru, už zařazeným obnoví čísla, vyprchané vymění za silnější.

    Vyprchaná = zařazená akcie, která v tomto průchodu nesplnila podmínky skeneru. Místo uvolní jen
    pro nového kandidáta a nikdy, když na ní bot drží pozici (protected). Vrací {"added", "removed"}.
    """
    limit = MAX_DYNAMIC_TICKERS_EU if region == "EU" else MAX_DYNAMIC_TICKERS
    protected = protected or set()
    fixed = set(US_STOCKS_WATCHLIST + EU_STOCKS_WATCHLIST + CRYPTO_WATCHLIST)
    tickers = state.setdefault(_key(region), [])
    current = {x["ticker"]: x for x in tickers}
    live = {c["ticker"] for c in candidates}
    for t in current:
        if t in live:
            current[t].pop("faded_at", None)
        else:
            current[t].setdefault("faded_at", now_iso)
    added, removed = [], []
    for c in sorted(candidates, key=lambda c: c["score"], reverse=True):
        if c["ticker"] in fixed:
            continue
        if c["ticker"] in current:
            current[c["ticker"]].update(last_change_pct=c["change_pct"], last_rel_volume=c["rel_volume"], updated_at=now_iso)
            if c.get("name") and not current[c["ticker"]].get("name"):
                current[c["ticker"]]["name"] = c["name"]
            continue
        if c["kind"] == "day2" and sum(x.get("kind") == "day2" for x in tickers) >= MAX_DAY2_TICKERS:
            continue  # místo musí zůstat na dnešní gappery a pohyby v seanci
        if len(tickers) >= limit:
            faded = [x for x in tickers if x.get("faded_at") and x["ticker"] not in protected]
            if not faded:
                continue
            out = min(faded, key=lambda x: float(x.get("last_change_pct", x.get("change_pct")) or 0))
            tickers.remove(out)
            current.pop(out["ticker"], None)
            removed.append(out)
        entry = {"ticker": c["ticker"], "reason": c["reason"], "kind": c["kind"], "change_pct": c["change_pct"],
                 "rel_volume": c["rel_volume"], "added_at": now_iso, "headlines": [], "name": c.get("name", "")}
        tickers.append(entry)
        current[c["ticker"]] = entry
        added.append(entry)
    return {"added": added, "removed": removed}


def run_scan() -> List[Dict[str, Any]]:
    """Jeden průchod skeneru (blokující, volat přes asyncio.to_thread). Vrací nově zařazené akcie."""
    import news_briefing
    import trading212

    from paper_trading import get_open_positions

    now_prague = market_hours.get_prague_now()
    now_ny = now_prague.astimezone(market_hours.NY_TZ)
    now_iso = now_prague.isoformat(timespec="seconds")
    state = load_state()
    protected = {str(p.get("ticker") or "").upper() for p in get_open_positions()}
    added: List[Dict[str, Any]] = []
    log_parts = []

    if is_us_scan_time(now_prague):
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
        res = merge_candidates(state, candidates, now_iso, "US", protected)
        added += res["added"]
        log_parts.append(_log_part("US", len(quotes), candidates, res, state["tickers"], MAX_DYNAMIC_TICKERS))

    if is_eu_scan_time(now_prague):
        quotes = fetch_eu_quotes()
        known = ((news_briefing.load_briefing() or {}).get("raw") or {}).get("watchlist_earnings") or {}
        candidates = []
        for q in quotes:
            c = evaluate_eu_quote(q, now_prague, known)
            if c and trading212.to_t212_ticker(c["ticker"]):
                candidates.append(c)
        res = merge_candidates(state, candidates, now_iso, "EU", protected)
        added += res["added"]
        log_parts.append(_log_part("EU", len(quotes), candidates, res, state.setdefault("tickers_eu", []),
                                   MAX_DYNAMIC_TICKERS_EU))

    if added:
        try:
            news = news_briefing.fetch_company_news([x["ticker"] for x in added], per_ticker=3,
                                                   names={x["ticker"]: x.get("name", "") for x in added})
            for x in added:
                x["headlines"] = news.get(x["ticker"], [])
        except Exception as e:
            logger.warning(f"🚀 Zprávy k novým tickerům nejde stáhnout: {e}")
    state["last_scan"] = now_iso
    _save(state)
    logger.info("🚀 MOMENTUM SKENER: " + " || ".join(log_parts))
    return added


def _log_part(region: str, n_quotes: int, candidates: List[Dict[str, Any]], res: Dict[str, List[Dict[str, Any]]],
              tickers: List[Dict[str, Any]], limit: int) -> str:
    removed = f", vyřazeno (vyprchalo): {', '.join(x['ticker'] for x in res['removed'])}" if res["removed"] else ""
    return (f"{region}: prověřeno {n_quotes} kotací, kandidátů {len(candidates)}, "
            f"nově zařazeno: {', '.join(x['ticker'] for x in res['added']) or 'nic'}{removed} | sleduji {len(tickers)}/{limit}")


def get_context(ticker: str) -> str:
    """Věta pro prompt Gemini: proč je akcie dnes sledovaná."""
    x = get_entry(ticker)
    if not x:
        return ""
    who = f"{x['ticker']} ({x['name']})" if x.get("name") else x["ticker"]
    text = f"{who} zařadil dnes momentum skener: {x['reason']}."
    if x.get("headlines"):
        text += " Zprávy při zařazení: " + " | ".join(x["headlines"][:3])
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
    state = load_state()
    tickers = state.get("tickers", []) + state.get("tickers_eu", [])
    if not tickers:
        return []
    return ["\n🚀 **Dnes zařazeno skenerem:**"] + [f"• **{x['ticker']}**: {x['reason']}" for x in tickers]
