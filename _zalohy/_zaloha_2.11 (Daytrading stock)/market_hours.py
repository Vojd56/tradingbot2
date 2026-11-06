"""Modul pro kontrolu obchodních hodin, vstupních oken a konce seance.

Všechny časy se počítají v ČASOVÉM PÁSMU BURZY (ne v pražském čase), takže letní/zimní čas
sedí i v týdnech, kdy USA a Evropa přecházejí v jiné dny (březen a říjen/listopad):
- US akcie (NYSE/NASDAQ): 09:30 - 16:00 America/New_York, svátky NYSE, zkrácené dny do 13:00
- Evropské akcie (XETRA, Euronext): 09:00 - 17:30 Europe/Berlin, svátky XETRA/Euronext
- Kryptoměny: 24/7/365

Nad rámec otevření burzy hlídá:
- vstupní okno (žádné nové nákupy v prvních minutách po otevření a těsně před koncem),
- "flat by" čas, kdy bot své akciové pozice uzavře, aby je nedržel přes noc bez dozoru.
"""

from datetime import datetime, date, time as dtime, timedelta
import zoneinfo
from typing import Dict, Any, List, Tuple, Optional
import discord

from config import (
    PRAGUE_TIMEZONE,
    US_STOCKS_WATCHLIST,
    EU_STOCKS_WATCHLIST,
    CRYPTO_WATCHLIST,
    US_NO_ENTRIES_BEFORE,
    US_NO_ENTRIES_AFTER,
    US_FLAT_BY,
    EU_NO_ENTRIES_BEFORE,
    EU_NO_ENTRIES_AFTER,
    EU_FLAT_BY,
)

PRAGUE_TZ = zoneinfo.ZoneInfo(PRAGUE_TIMEZONE)
NY_TZ = zoneinfo.ZoneInfo("America/New_York")
EU_TZ = zoneinfo.ZoneInfo("Europe/Berlin")

# Svátky NYSE (burza zavřená) – stejný seznam jako .agent/skills/trader-risk/check.py
NYSE_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
    "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
    "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}
# Zkrácené obchodní dny NYSE (zavírá ve 13:00 New York)
NYSE_EARLY_CLOSES = {"2026-11-27", "2026-12-24", "2027-11-26"}

# Svátky XETRA / Euronext (sjednocení – v tyto dny bot evropské akcie neobchoduje)
EU_HOLIDAYS = {
    "2026-01-01", "2026-04-03", "2026-04-06", "2026-05-01", "2026-12-24", "2026-12-25", "2026-12-31",
    "2027-01-01", "2027-03-26", "2027-03-29", "2027-12-24", "2027-12-31",
}

MARKETS = {
    "US": {"tz": NY_TZ, "open": dtime(9, 30), "close": dtime(16, 0), "early_close": dtime(13, 0),
           "holidays": NYSE_HOLIDAYS, "early": NYSE_EARLY_CLOSES,
           "entries_from": US_NO_ENTRIES_BEFORE, "entries_to": US_NO_ENTRIES_AFTER, "flat_by": US_FLAT_BY},
    "EU": {"tz": EU_TZ, "open": dtime(9, 0), "close": dtime(17, 30), "early_close": None,
           "holidays": EU_HOLIDAYS, "early": set(),
           "entries_from": EU_NO_ENTRIES_BEFORE, "entries_to": EU_NO_ENTRIES_AFTER, "flat_by": EU_FLAT_BY},
}


def _parse_hm(value: str) -> dtime:
    h, m = value.split(":")
    return dtime(int(h), int(m))


def get_prague_now() -> datetime:
    """Vrátí aktuální čas v časovém pásmu uživatele (Praha, CET/CEST)."""
    return datetime.now(PRAGUE_TZ)


def market_for_ticker(ticker: str) -> Optional[str]:
    """'US', 'EU' nebo None (krypto). EU tickery mají příponu burzy (SAP.DE, ASML.AS, AIR.PA)."""
    t = ticker.upper().strip()
    if t.endswith("USDT") or t in CRYPTO_WATCHLIST:
        return None
    if t in EU_STOCKS_WATCHLIST or "." in t:
        return "EU"
    return "US"


def session_bounds(market: str, now: Optional[datetime] = None) -> Optional[Tuple[datetime, datetime]]:
    """(otevření, zavření) dnešní seance v čase burzy, nebo None, pokud se dnes neobchoduje."""
    m = MARKETS[market]
    local = (now or datetime.now(m["tz"])).astimezone(m["tz"])
    d: date = local.date()
    if local.weekday() >= 5 or d.isoformat() in m["holidays"]:
        return None
    close_t = m["early_close"] if d.isoformat() in m["early"] else m["close"]
    return (datetime.combine(d, m["open"], m["tz"]), datetime.combine(d, close_t, m["tz"]))


def is_market_open(market: str, now: Optional[datetime] = None) -> bool:
    bounds = session_bounds(market, now)
    if not bounds:
        return False
    local = (now or datetime.now(MARKETS[market]["tz"])).astimezone(MARKETS[market]["tz"])
    return bounds[0] <= local < bounds[1]


def entries_allowed(market: str, now: Optional[datetime] = None) -> bool:
    """Smí bot na tomto trhu právě teď otevírat NOVÉ pozice?

    Okno: od X min po otevření do Y před koncem; při zkráceném dni se konec okna posune stejně dřív.
    """
    bounds = session_bounds(market, now)
    if not bounds:
        return False
    m = MARKETS[market]
    local = (now or datetime.now(m["tz"])).astimezone(m["tz"])
    open_dt, close_dt = bounds
    start = datetime.combine(open_dt.date(), _parse_hm(m["entries_from"]), m["tz"])
    end = datetime.combine(open_dt.date(), _parse_hm(m["entries_to"]), m["tz"])
    regular_close = datetime.combine(open_dt.date(), m["close"], m["tz"])
    end -= (regular_close - close_dt)  # zkrácený den -> okno končí o to dřív
    return start <= local < end


def flat_by_time(market: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """Čas, kdy má bot dnes srovnat (uzavřít) své akciové pozice."""
    bounds = session_bounds(market, now)
    if not bounds:
        return None
    m = MARKETS[market]
    open_dt, close_dt = bounds
    flat = datetime.combine(open_dt.date(), _parse_hm(m["flat_by"]), m["tz"])
    regular_close = datetime.combine(open_dt.date(), m["close"], m["tz"])
    return flat - (regular_close - close_dt)


def should_flatten(ticker: str, opened_at_iso: Optional[str], now: Optional[datetime] = None) -> Optional[str]:
    """Vrátí důvod k uzavření akciové pozice kvůli konci seance, jinak None.

    - během seance po čase flat_by -> "KONEC SEANCE"
    - během seance, pokud byla pozice otevřena před dnešním otevřením (zůstala přes noc) -> "POZICE PŘES NOC"
    """
    market = market_for_ticker(ticker)
    if market is None or not is_market_open(market, now):
        return None
    m = MARKETS[market]
    local = (now or datetime.now(m["tz"])).astimezone(m["tz"])
    flat = flat_by_time(market, now)
    if flat and local >= flat:
        return "KONEC SEANCE"
    if opened_at_iso:
        try:
            opened = datetime.fromisoformat(opened_at_iso).astimezone(m["tz"])
            if opened < session_bounds(market, now)[0]:
                return "POZICE PŘES NOC"
        except ValueError:
            pass
    return None


def is_eu_stocks_open(dt: datetime = None) -> bool:
    """Zkontroluje, zda jsou otevřeny evropské akciové burzy (09:00 - 17:30 Berlín, mimo svátky)."""
    return is_market_open("EU", dt)


def is_us_stocks_open(dt: datetime = None) -> bool:
    """Zkontroluje, zda jsou otevřeny americké akciové burzy (09:30 - 16:00 New York, mimo svátky)."""
    return is_market_open("US", dt)


def is_crypto_open(dt: datetime = None) -> bool:
    """Kryptoměny se obchodují nepřetržitě 24/7."""
    return True


def get_active_markets_and_watchlists() -> List[Tuple[str, str, List[str]]]:
    """Vrátí trhy, na kterých smí bot PRÁVĚ TEĎ otevírat nové pozice (vstupní okno), a jejich watchlisty.

    Návratová hodnota:
    Seznam trojic: (Název_trhu, Typ_aktiva, Seznam_tickerů)
    """
    active_lists = [("Kryptoměny (Binance Futures)", "CRYPTO", CRYPTO_WATCHLIST)]
    if entries_allowed("EU"):
        active_lists.append(("Evropské akcie (XETRA, Euronext)", "STOCK_EU", EU_STOCKS_WATCHLIST))
    if entries_allowed("US"):
        active_lists.append(("US akcie (NYSE, NASDAQ)", "STOCK_US", US_STOCKS_WATCHLIST))
    return active_lists


def _prague_hm(market: str, t: dtime) -> str:
    """Převede čas burzy na dnešní pražský čas pro výpis."""
    m = MARKETS[market]
    today = datetime.now(m["tz"]).date()
    return datetime.combine(today, t, m["tz"]).astimezone(PRAGUE_TZ).strftime("%H:%M")


def get_market_status_dict() -> Dict[str, Any]:
    """Vrátí strukturovaný stav všech tří trhů pro logování a UI."""
    now = get_prague_now()
    eu_open = is_eu_stocks_open()
    us_open = is_us_stocks_open()

    return {
        "prague_time": now.strftime("%Y-%m-%d %H:%M:%S (%Z)"),
        "weekday_name": ["Pondělí", "Úterý", "Středa", "Čtvrtek", "Pátek", "Sobota", "Neděle"][now.weekday()],
        "is_weekend": now.weekday() >= 5,
        "eu_open": eu_open,
        "us_open": us_open,
        "eu_entries": entries_allowed("EU"),
        "us_entries": entries_allowed("US"),
        "crypto_open": True,
        "active_markets_count": 1 + int(eu_open) + int(us_open)
    }


def build_market_hours_embed() -> discord.Embed:
    """Vytvoří přehledný Discord Embed se stavem burz a ochranou kreditů."""
    status = get_market_status_dict()
    now_str = status["prague_time"]
    day_str = status["weekday_name"]

    embed = discord.Embed(
        title="⏰ Stav Obchodních Hodin & Ochrana Kreditů",
        description=f"Časové pásmo: **Praha ({day_str}, {now_str})**\nČasy níže jsou přepočtené na dnešní pražský čas.",
        color=discord.Color.teal()
    )

    for market, flag, label, open_key, entries_key, watchlist in (
        ("EU", "🇪🇺", "Evropské akcie", "eu_open", "eu_entries", EU_STOCKS_WATCHLIST),
        ("US", "🇺🇸", "US akcie", "us_open", "us_entries", US_STOCKS_WATCHLIST),
    ):
        m = MARKETS[market]
        icon = "🟢 OTEVŘENO" if status[open_key] else "🔴 ZAVŘENO"
        entries = "✅ bot smí nakupovat" if status[entries_key] else "⛔ nové nákupy zastaveny"
        embed.add_field(
            name=f"{flag} {label}: {icon}",
            value=(
                f"• **Seance:** `{_prague_hm(market, m['open'])} - {_prague_hm(market, m['close'])}` Praha (Po-Pá, mimo svátky)\n"
                f"• **Vstupní okno:** `{_prague_hm(market, _parse_hm(m['entries_from']))} - {_prague_hm(market, _parse_hm(m['entries_to']))}` | {entries}\n"
                f"• **Uzavření pozic bota:** `{_prague_hm(market, _parse_hm(m['flat_by']))}`\n"
                f"• **Sledovaný watchlist:** `{', '.join(watchlist)}`"
            ),
            inline=False
        )

    embed.add_field(
        name="🪙 Kryptoměny (Binance Futures): 🟢 OTEVŘENO 24/7",
        value=(
            f"• **Obchodní hodiny:** `Nonstop 24/7/365`\n"
            f"• **Sledované páry:** `{', '.join(CRYPTO_WATCHLIST)}`"
        ),
        inline=False
    )

    embed.set_footer(text="AIBot 2.0 • Bot volá Gemini jen pro trhy, kde smí právě nakupovat.")
    return embed
