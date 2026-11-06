"""Ranní přehled zpráv: jednou denně posbírá novinky z trhů a kalendář událostí.

Zdroje (zdarma, bez API klíče):
- RSS: Yahoo Finance, CNBC (top, trhy, Evropa, výsledky), MarketWatch, CoinDesk, Cointelegraph
- Google News RSS: zprávy za poslední den ke každé firmě z watchlistu
- Kalendář výsledků (earnings) Nasdaq na dnešek a zítřek
- Ekonomický kalendář ForexFactory (Fed, ECB, CPI, zaměstnanost...) jen s vysokým dopadem
- yfinance Ticker.calendar: datum výsledků firem z watchlistu

Z posbíraných dat udělá JEDNO volání Gemini denně (nejlepší dostupný model z kaskády),
výsledek uloží do daily_briefing.json. Restart bota ten den nic znovu nestahuje.
Gemini analýza obchodu pak dostane výtah z přehledu (get_context_for_ticker).
"""

import asyncio
import json
import logging
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional

import market_hours
from config import (
    BASE_DIR,
    US_STOCKS_WATCHLIST,
    EU_STOCKS_WATCHLIST,
    CRYPTO_WATCHLIST,
)

logger = logging.getLogger("TradingBot.News")

BRIEFING_FILE = BASE_DIR / "daily_briefing.json"
HTTP_TIMEOUT = 15
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
MAX_HEADLINE_AGE_HOURS = 36   # starší titulky se do přehledu nedávají
MAX_EARNINGS_ROWS = 30        # z kalendáře výsledků jen největší firmy podle tržní kapitalizace
MAX_EXTRA_TICKERS = 5         # kolik firem mimo watchlist smí ranní přehled na den přidat (uživatel 27.10., včetně IPO)
SUMMARY_TIMEOUT_S = 120.0     # jediné volání za den, nejlepší model potřebuje na dlouhý prompt víc času

RSS_FEEDS = [
    ("CNBC Top", "https://www.cnbc.com/id/100003114/device/rss/rss.html", "trhy"),
    ("CNBC Markets", "https://www.cnbc.com/id/20910258/device/rss/rss.html", "trhy"),
    ("CNBC Europe", "https://www.cnbc.com/id/19794221/device/rss/rss.html", "evropa"),
    ("CNBC Earnings", "https://www.cnbc.com/id/15839135/device/rss/rss.html", "vysledky"),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories", "trhy"),
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/", "krypto"),
]
HEADLINES_PER_FEED = 12

# Jména firem a coinů pro hledání v titulcích a dotazy do Google News
TICKER_NAMES = {
    "NVDA": "Nvidia", "AAPL": "Apple", "TSLA": "Tesla", "AMD": "AMD", "MSFT": "Microsoft",
    "AMZN": "Amazon", "META": "Meta Platforms", "GOOGL": "Alphabet Google",
    "SAP.DE": "SAP", "ASML.AS": "ASML", "SIE.DE": "Siemens", "AIR.PA": "Airbus",
    "MC.PA": "LVMH", "TTE.PA": "TotalEnergies", "ALV.DE": "Allianz", "BMW.DE": "BMW",
    "BTCUSDT": "Bitcoin", "ETHUSDT": "Ethereum", "SOLUSDT": "Solana", "XRPUSDT": "XRP",
    "DOGEUSDT": "Dogecoin", "BNBUSDT": "BNB", "NEARUSDT": "NEAR Protocol", "SUIUSDT": "Sui",
    "AVAXUSDT": "Avalanche", "LINKUSDT": "Chainlink",
}

_lock = asyncio.Lock()
_cache: Optional[Dict[str, Any]] = None


# =====================================================================
# STAHOVÁNÍ ZDROJŮ
# =====================================================================

def _http_get(url: str, accept: str = "*/*") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read()


def _parse_date(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    text = text.strip()
    try:
        dt = parsedate_to_datetime(text)
    except Exception:
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_feed(xml_bytes: bytes, source: str, limit: int = HEADLINES_PER_FEED) -> List[Dict[str, Any]]:
    """Přečte RSS 2.0 i Atom. Vrací [{title, source, published}] (published = ISO UTC nebo None)."""
    root = ET.fromstring(xml_bytes)
    items = [el for el in root.iter() if _local(el.tag) in ("item", "entry")]
    out = []
    for it in items:
        fields = {_local(ch.tag): (ch.text or "").strip() for ch in it}
        title = re.sub(r"\s+", " ", fields.get("title", "")).strip()
        if not title:
            continue
        dt = _parse_date(fields.get("pubDate") or fields.get("published") or fields.get("updated"))
        out.append({
            "title": title,
            "source": source,
            "published": dt.astimezone(timezone.utc).isoformat() if dt else None,
        })
        if len(out) >= limit:
            break
    return out


def _is_fresh(item: Dict[str, Any], now: datetime) -> bool:
    if not item.get("published"):
        return True  # bez data: necháme, zdroj je "top stories"
    try:
        return now - datetime.fromisoformat(item["published"]) <= timedelta(hours=MAX_HEADLINE_AGE_HOURS)
    except Exception:
        return True


def fetch_rss_headlines(now: datetime) -> Dict[str, List[Dict[str, Any]]]:
    """Titulky podle kategorie (trhy, evropa, vysledky, krypto), bez duplicit."""
    by_cat: Dict[str, List[Dict[str, Any]]] = {}
    seen = set()
    for name, url, cat in RSS_FEEDS:
        try:
            items = parse_feed(_http_get(url, "application/rss+xml, application/xml, text/xml"), name)
        except Exception as e:
            logger.warning(f"📰 RSS {name} nedostupné: {e}")
            continue
        for it in items:
            key = it["title"].lower()
            if key in seen or not _is_fresh(it, now):
                continue
            seen.add(key)
            by_cat.setdefault(cat, []).append(it)
    return by_cat


def _google_news_query(ticker: str) -> str:
    name = TICKER_NAMES.get(ticker, ticker)
    base = ticker.split(".")[0]
    q = f'"{name}" stock' if name.upper() != base else f"{base} stock"
    return f"{q} when:1d"


def fetch_company_news(tickers: List[str], per_ticker: int = 4) -> Dict[str, List[str]]:
    """Google News RSS za poslední den ke každé akcii z watchlistu."""
    out: Dict[str, List[str]] = {}
    for t in tickers:
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
            {"q": _google_news_query(t), "hl": "en-US", "gl": "US", "ceid": "US:en"})
        try:
            items = parse_feed(_http_get(url), "Google News", limit=per_ticker)
            if items:
                out[t] = [i["title"] for i in items]
        except Exception as e:
            logger.warning(f"📰 Google News pro {t} nedostupné: {e}")
    return out


def fetch_earnings_calendar(day: str) -> List[Dict[str, Any]]:
    """Kalendář výsledků Nasdaq pro den YYYY-MM-DD, největší firmy první."""
    url = f"https://api.nasdaq.com/api/calendar/earnings?date={day}"
    data = json.loads(_http_get(url, "application/json"))
    rows = ((data or {}).get("data") or {}).get("rows") or []

    def mcap(r):
        try:
            return float(str(r.get("marketCap", "0")).replace("$", "").replace(",", "") or 0)
        except ValueError:
            return 0.0

    rows.sort(key=mcap, reverse=True)
    time_map = {"time-pre-market": "před otevřením", "time-after-hours": "po zavření", "time-not-supplied": "čas neuveden"}
    return [{
        "symbol": r.get("symbol"),
        "name": r.get("name"),
        "time": time_map.get(r.get("time"), r.get("time") or ""),
        "eps_forecast": r.get("epsForecast") or "",
    } for r in rows[:MAX_EARNINGS_ROWS]]


def fetch_economic_calendar(days: List[str]) -> List[Dict[str, Any]]:
    """Makro události s vysokým dopadem (USD, EUR) na zadané dny, časy v Praze."""
    data = json.loads(_http_get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", "application/json"))
    out = []
    for ev in data:
        if ev.get("impact") != "High" or ev.get("country") not in ("USD", "EUR"):
            continue
        dt = _parse_date(ev.get("date"))
        if not dt:
            continue
        local = dt.astimezone(market_hours.PRAGUE_TZ)
        if local.date().isoformat() not in days:
            continue
        out.append({
            "when": local.strftime("%d.%m. %H:%M"),
            "country": ev.get("country"),
            "title": ev.get("title"),
            "forecast": ev.get("forecast") or "",
            "previous": ev.get("previous") or "",
        })
    return out


def fetch_watchlist_earnings_dates(tickers: List[str], today) -> Dict[str, str]:
    """Datum příštích výsledků firem z watchlistu (do 14 dní) z yfinance."""
    import yfinance as yf
    out = {}
    for t in tickers:
        try:
            cal = yf.Ticker(t).calendar or {}
            dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
            if not dates:
                continue
            d = dates[0] if isinstance(dates, (list, tuple)) else dates
            if isinstance(d, datetime):
                d = d.date()
            if today <= d <= today + timedelta(days=14):
                out[t] = d.isoformat()
        except Exception as e:
            logger.debug(f"yfinance calendar {t}: {e}")
    return out


def refresh_t212_instruments() -> Optional[int]:
    """Stáhne aktuální seznam instrumentů T212, aby šly ověřit i čerstvé IPO. Vrací počet, None = beze změny."""
    import trading212
    if not trading212.is_configured():
        return None
    status, data = trading212._request("GET", "/equity/metadata/instruments", timeout=60)
    if status != 200 or not isinstance(data, list) or len(data) < 1000:
        logger.warning(f"📰 Seznam instrumentů T212 se neobnovil ({status}), zůstává uložený")
        return None
    path = trading212.T212_INSTRUMENTS_FILE
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    tmp.replace(path)
    trading212._instruments_loaded = False  # další dotaz načte nový soubor
    trading212._instrument_currencies = None
    return len(data)


def collect_raw_briefing() -> Dict[str, Any]:
    """Stáhne všechny zdroje. Výpadek jednoho zdroje nevadí, zbytek se použije."""
    now = datetime.now(timezone.utc)
    today = datetime.now(market_hours.PRAGUE_TZ).date()
    days = [today.isoformat(), (today + timedelta(days=1)).isoformat()]
    stocks = US_STOCKS_WATCHLIST + EU_STOCKS_WATCHLIST
    raw: Dict[str, Any] = {"sources_failed": []}

    raw["headlines"] = fetch_rss_headlines(now)
    raw["company_news"] = fetch_company_news(stocks)

    raw["earnings"] = {}
    for d in days:
        try:
            raw["earnings"][d] = fetch_earnings_calendar(d)
        except Exception as e:
            logger.warning(f"📅 Kalendář výsledků Nasdaq ({d}) nedostupný: {e}")
            raw["sources_failed"].append(f"nasdaq-earnings {d}")
    try:
        raw["economic"] = fetch_economic_calendar(days)
    except Exception as e:
        logger.warning(f"📅 Ekonomický kalendář nedostupný: {e}")
        raw["economic"] = []
        raw["sources_failed"].append("forexfactory")
    try:
        raw["watchlist_earnings"] = fetch_watchlist_earnings_dates(stocks, today)
    except Exception as e:
        logger.warning(f"📅 Data výsledků z yfinance nedostupná: {e}")
        raw["watchlist_earnings"] = {}

    try:
        n = refresh_t212_instruments()
        if n:
            logger.info(f"📰 Seznam instrumentů T212 obnoven ({n} instrumentů)")
    except Exception as e:
        logger.warning(f"📰 Obnova seznamu instrumentů T212 selhala: {e}")

    n_head = sum(len(v) for v in raw["headlines"].values())
    logger.info(
        f"📰 Ranní sběr: {n_head} titulků, zprávy k {len(raw['company_news'])} firmám, "
        f"výsledky {sum(len(v) for v in raw['earnings'].values())} firem, {len(raw['economic'])} makro událostí"
    )
    return raw


# =====================================================================
# SHRNUTÍ JEDNÍM VOLÁNÍM GEMINI
# =====================================================================

def build_summary_prompt(raw: Dict[str, Any], today: str) -> str:
    lines = [f"Datum: {today}. Jsi ranní analytik trading bota. Bot obchoduje US akcie {', '.join(US_STOCKS_WATCHLIST)}, "
             f"evropské akcie {', '.join(EU_STOCKS_WATCHLIST)} a krypto {', '.join(CRYPTO_WATCHLIST)}.",
             "Z níže uvedených zpráv a kalendářů udělej ranní přehled. Nic si nevymýšlej, používej jen uvedená data.", ""]
    for cat, items in raw.get("headlines", {}).items():
        lines.append(f"TITULKY ({cat}):")
        lines += [f"- [{i['source']}] {i['title']}" for i in items]
    if raw.get("company_news"):
        lines.append("ZPRÁVY K FIRMÁM Z WATCHLISTU (poslední den):")
        for t, heads in raw["company_news"].items():
            lines.append(f"{t}: " + " | ".join(heads))
    for d, rows in raw.get("earnings", {}).items():
        if rows:
            lines.append(f"VÝSLEDKY HOSPODAŘENÍ {d} (největší firmy):")
            lines += [f"- {r['symbol']} {r['name']} ({r['time']}, odhad EPS {r['eps_forecast'] or '?'})" for r in rows]
    if raw.get("watchlist_earnings"):
        lines.append("BLÍŽÍCÍ SE VÝSLEDKY FIREM Z WATCHLISTU: " +
                     ", ".join(f"{t} {d}" for t, d in raw["watchlist_earnings"].items()))
    if raw.get("economic"):
        lines.append("MAKRO UDÁLOSTI S VYSOKÝM DOPADEM (čas Praha):")
        lines += [f"- {e['when']} {e['country']} {e['title']} (odhad {e['forecast'] or '?'}, minule {e['previous'] or '?'})"
                  for e in raw["economic"]]

    lines.append("""
Odpověz POUZE validním JSON v tomto formátu, česky:
{
  "market_mood": "RISK-ON" | "RISK-OFF" | "NEUTRÁLNÍ",
  "summary": "3-5 vět: co hýbe trhy, hlavní témata dne",
  "events_today": ["Konkrétní události dnes a zítra s časem: výsledky firem, tiskové konference, Fed/ECB, data CPI apod."],
  "tickers": {"TICKER": "1 věta: co je nového u této firmy/coinu z watchlistu a jestli je to plus, minus nebo riziko"},
  "crypto": "1-2 věty o kryptu",
  "risks": ["Hlavní rizika dne pro obchodování"],
  "new_tickers": [{"ticker": "SYMBOL", "reason": "1 věta: jaká konkrétní zpráva dnes s firmou hýbe", "ipo": false}]
}
Do "tickers" dej jen tickery z watchlistu, ke kterým v datech opravdu něco je (používej přesné tickery jako NVDA, SAP.DE, BTCUSDT).
Do "new_tickers" dej nejvýš 5 US akcií MIMO watchlist, kolem kterých je dnes podle dat velká konkrétní zpráva
(vstup na burzu/IPO, schválení nebo představení léku, akvizice, výsledky, velký kontrakt). Jen burzovní symbol US akcie
(např. MRNA), "ipo": true u firmy, která právě vstupuje nebo vstoupila na burzu. Když nic takového v datech není, dej [].""")
    return "\n".join(lines)


async def summarize_with_gemini(raw: Dict[str, Any], today: str) -> Dict[str, Any]:
    """Jedno úspěšné volání Gemini denně: nejlepší dostupný model, další jen když ten selže."""
    from google.genai import types
    from gemini_team import get_gemini_client
    from model_manager import quota_manager

    client = get_gemini_client()
    prompt = build_summary_prompt(raw, today)
    last_error = None
    for m in quota_manager.get_candidate_models():
        model_name = m["id"]
        try:
            logger.info(f"📰 Ranní přehled: volám {model_name} (jediné volání za den)...")
            resp = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.2),
                ),
                timeout=SUMMARY_TIMEOUT_S,
            )
            data = json.loads(resp.text or "")
            quota_manager.record_call_success(model_name)
            data["model_used"] = model_name
            return data
        except Exception as e:
            last_error = e
            logger.warning(f"📰 Ranní přehled: {model_name} selhal ({type(e).__name__}: {e}), zkouším další model...")
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                quota_manager.record_rate_limit(model_name, cooldown_minutes=15)
    raise RuntimeError(f"Žádný model nevrátil ranní přehled: {last_error}")


# =====================================================================
# ULOŽENÍ, NAČTENÍ A POUŽITÍ
# =====================================================================

def validate_extra_tickers(summary: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Návrhy Gemini -> max MAX_EXTRA_TICKERS US akcií mimo watchlist, které jdou koupit na T212."""
    import trading212
    if not summary or not isinstance(summary.get("new_tickers"), list):
        return []
    watch = set(US_STOCKS_WATCHLIST + EU_STOCKS_WATCHLIST + CRYPTO_WATCHLIST)
    out, rejected = [], []
    for item in summary["new_tickers"]:
        if not isinstance(item, dict):
            continue
        sym = str(item.get("ticker", "")).upper().strip().lstrip("$")
        if not re.fullmatch(r"[A-Z]{1,5}", sym) or sym in watch or any(x["ticker"] == sym for x in out):
            rejected.append(sym or "?")
            continue
        if not trading212.to_t212_ticker(sym):
            rejected.append(f"{sym} (není na T212)")
            continue
        out.append({"ticker": sym, "reason": str(item.get("reason", "")).strip(), "ipo": bool(item.get("ipo"))})
        if len(out) >= MAX_EXTRA_TICKERS:
            break
    if rejected:
        logger.info(f"📰 Nové tickery zamítnuté: {', '.join(rejected)}")
    if out:
        logger.info(f"📰 Dnes navíc sleduji: {', '.join(x['ticker'] for x in out)}")
    return out


def get_extra_tickers() -> List[str]:
    """US akcie, které dnešní ranní přehled přidal ke sledování (Scout je skenuje s US watchlistem)."""
    b = load_briefing()
    return [x["ticker"] for x in (b or {}).get("extra_tickers", [])]


def _today_str() -> str:
    return datetime.now(market_hours.PRAGUE_TZ).date().isoformat()


def load_briefing() -> Optional[Dict[str, Any]]:
    """Dnešní přehled ze souboru, jinak None."""
    global _cache
    today = _today_str()
    if _cache and _cache.get("date") == today:
        return _cache
    try:
        with open(BRIEFING_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("date") == today:
            _cache = data
            return data
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.error(f"Chyba při čtení {BRIEFING_FILE}: {e}")
    return None


def _save(data: Dict[str, Any]) -> None:
    global _cache
    with open(BRIEFING_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    _cache = data


async def ensure_today_briefing() -> tuple:
    """Vrátí (přehled, nově_vytvořen). Dnešní přehled se vytváří jen jednou."""
    async with _lock:
        existing = load_briefing()
        if existing:
            return existing, False
        today = _today_str()
        raw = await asyncio.to_thread(collect_raw_briefing)
        try:
            summary = await summarize_with_gemini(raw, today)
        except Exception as e:
            # Bez Gemini aspoň surová data; nový pokus až zítra, ať se nepálí volání
            logger.error(f"📰 Shrnutí ranního přehledu selhalo: {e}")
            summary = None
        try:
            extra = validate_extra_tickers(summary)
        except Exception as e:
            logger.error(f"📰 Ověření nových tickerů selhalo: {e}")
            extra = []
        data = {
            "date": today,
            "extra_tickers": extra,
            "created_at": datetime.now(market_hours.PRAGUE_TZ).isoformat(timespec="seconds"),
            "summary": summary,
            "raw": raw,
        }
        _save(data)
        return data, True


def _mentions(text: str, ticker: str) -> bool:
    base = ticker.replace("USDT", "").split(".")[0]
    name = TICKER_NAMES.get(ticker, "")
    words = [w for w in [base, *name.split()] if len(w) >= 3]
    return any(re.search(rf"\b{re.escape(w)}\b", text, re.IGNORECASE) for w in words)


def get_context_for_ticker(ticker: str, briefing: Optional[Dict[str, Any]] = None, max_chars: int = 1500) -> str:
    """Výtah z ranního přehledu pro prompt analýzy obchodu. Prázdný řetězec, když dnešní přehled není."""
    b = briefing if briefing is not None else load_briefing()
    if not b:
        return ""
    ticker = ticker.upper()
    is_crypto = ticker.endswith("USDT")
    s = b.get("summary") or {}
    raw = b.get("raw") or {}
    parts = []
    if s.get("market_mood") or s.get("summary"):
        parts.append(f"Nálada trhu: {s.get('market_mood', '?')}. {s.get('summary', '')}".strip())
    note = (s.get("tickers") or {}).get(ticker)
    if note:
        parts.append(f"K {ticker}: {note}")
    extra = next((x for x in b.get("extra_tickers") or [] if x.get("ticker") == ticker), None)
    if extra:
        ipo = " Čerstvé IPO: krátká cenová historie, vysoká volatilita." if extra.get("ipo") else ""
        parts.append(f"{ticker} je dnes sledován navíc kvůli ranní zprávě: {extra.get('reason', '')}{ipo}")
    if is_crypto and s.get("crypto"):
        parts.append(f"Krypto: {s['crypto']}")
    if not is_crypto:
        ed = (raw.get("watchlist_earnings") or {}).get(ticker)
        if ed:
            parts.append(f"⚠️ {ticker} zveřejní výsledky {ed}.")
        own = (raw.get("company_news") or {}).get(ticker) or []
        if own:
            parts.append("Zprávy k firmě za poslední den: " + " | ".join(own[:4]))
    if s.get("events_today"):
        parts.append("Události dnes/zítra: " + "; ".join(s["events_today"][:6]))
    if s.get("risks"):
        parts.append("Rizika dne: " + "; ".join(s["risks"][:3]))
    if not s:
        # Gemini shrnutí chybí -> aspoň titulky, které ticker zmiňují
        heads = [i["title"] for items in (raw.get("headlines") or {}).values() for i in items if _mentions(i["title"], ticker)]
        if heads:
            parts.append("Ranní titulky: " + " | ".join(heads[:5]))
    text = "\n".join(f"- {p}" for p in parts)
    return text[:max_chars]


def format_discord_message(b: Dict[str, Any]) -> str:
    """Zpráva do Discordu, max ~1900 znaků."""
    s = b.get("summary") or {}
    raw = b.get("raw") or {}
    lines = [f"🗞️ **Ranní přehled trhu {b.get('date')}**"]
    if s:
        lines.append(f"Nálada: **{s.get('market_mood', '?')}**")
        if s.get("summary"):
            lines.append(s["summary"])
        if s.get("events_today"):
            lines.append("\n📅 **Události:**")
            lines += [f"• {e}" for e in s["events_today"][:8]]
        if s.get("tickers"):
            lines.append("\n🎯 **Watchlist:**")
            lines += [f"• **{t}**: {n}" for t, n in list(s["tickers"].items())[:10]]
        if b.get("extra_tickers"):
            lines.append("\n🆕 **Dnes navíc sleduji:**")
            lines += [f"• **{x['ticker']}**{' (IPO)' if x.get('ipo') else ''}: {x.get('reason', '')}" for x in b["extra_tickers"]]
        if s.get("crypto"):
            lines.append(f"\n🪙 **Krypto:** {s['crypto']}")
        if s.get("risks"):
            lines.append("\n⚠️ **Rizika:** " + "; ".join(s["risks"][:3]))
    else:
        lines.append("Shrnutí od Gemini se nepovedlo, bot použije aspoň surové titulky.")
        if raw.get("watchlist_earnings"):
            lines.append("📅 Výsledky: " + ", ".join(f"{t} {d}" for t, d in raw["watchlist_earnings"].items()))
    failed = raw.get("sources_failed") or []
    if failed:
        lines.append(f"\n_(nedostupné zdroje: {', '.join(failed)})_")
    text = "\n".join(lines)
    return text if len(text) <= 1900 else text[:1897] + "..."


if __name__ == "__main__":
    # Ruční kontrola zdrojů bez Gemini:  python news_briefing.py
    import sys
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO)
    r = collect_raw_briefing()
    for cat, items in r["headlines"].items():
        print(f"\n== {cat} ({len(items)}) ==")
        for i in items[:3]:
            print(" ", i["source"], "|", i["title"])
    print("\n== firmy ==", {t: len(v) for t, v in r["company_news"].items()})
    print("== výsledky ==", {d: [x["symbol"] for x in v[:8]] for d, v in r["earnings"].items()})
    print("== makro ==", r["economic"][:5])
    print("== watchlist výsledky ==", r["watchlist_earnings"])
    print("== selhalo ==", r["sources_failed"])
