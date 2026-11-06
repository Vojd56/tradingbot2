"""Modul pro kontrolu obchodních hodin a ochranu proti plýtvání API kredity.

Sleduje časové pásmo uživatele (Europe/Prague, CET/CEST):
- Evropské akcie: Pondělí až Pátek 09:00 - 17:30 CET
- US akcie: Pondělí až Pátek 15:30 - 22:00 CET
- Kryptoměny: 24/7/365 bez přerušení
- Víkendy a noční hodiny: Skenování akcií se pozastaví a šetří se API hovory.
"""

from datetime import datetime
import zoneinfo
from typing import Dict, Any, List, Tuple
import discord

from config import (
    PRAGUE_TIMEZONE,
    EU_MARKET_OPEN_HOUR,
    EU_MARKET_OPEN_MIN,
    EU_MARKET_CLOSE_HOUR,
    EU_MARKET_CLOSE_MIN,
    US_MARKET_OPEN_HOUR,
    US_MARKET_OPEN_MIN,
    US_MARKET_CLOSE_HOUR,
    US_MARKET_CLOSE_MIN,
    US_STOCKS_WATCHLIST,
    EU_STOCKS_WATCHLIST,
    CRYPTO_WATCHLIST,
)


def get_prague_now() -> datetime:
    """Vrátí aktuální čas v časovém pásmu uživatele (Praha, CET/CEST)."""
    try:
        tz = zoneinfo.ZoneInfo(PRAGUE_TIMEZONE)
        return datetime.now(tz)
    except Exception:
        return datetime.now()


def is_eu_stocks_open(dt: datetime = None) -> bool:
    """Zkontroluje, zda jsou otevřeny evropské akciové burzy (09:00 - 17:30 v pracovní dny)."""
    now = dt or get_prague_now()
    if now.weekday() >= 5:  # Sobota = 5, Neděle = 6
        return False
    current_minutes = now.hour * 60 + now.minute
    open_minutes = EU_MARKET_OPEN_HOUR * 60 + EU_MARKET_OPEN_MIN
    close_minutes = EU_MARKET_CLOSE_HOUR * 60 + EU_MARKET_CLOSE_MIN
    return open_minutes <= current_minutes <= close_minutes


def is_us_stocks_open(dt: datetime = None) -> bool:
    """Zkontroluje, zda jsou otevřeny americké akciové burzy (15:30 - 22:00 v pracovní dny)."""
    now = dt or get_prague_now()
    if now.weekday() >= 5:  # Víkend
        return False
    current_minutes = now.hour * 60 + now.minute
    open_minutes = US_MARKET_OPEN_HOUR * 60 + US_MARKET_OPEN_MIN
    close_minutes = US_MARKET_CLOSE_HOUR * 60 + US_MARKET_CLOSE_MIN
    return open_minutes <= current_minutes <= close_minutes


def is_crypto_open(dt: datetime = None) -> bool:
    """Kryptoměny se obchodují nepřetržitě 24/7."""
    return True


def get_active_markets_and_watchlists() -> List[Tuple[str, str, List[str]]]:
    """Vrátí pouze ty trhy a seznamy aktiv, které jsou v tomto okamžiku otevřeny k obchodování.

    Návratová hodnota:
    Seznam trojic: (Název_trhu, Typ_aktiva, Seznam_tickerů)
    """
    now = get_prague_now()
    active_lists = []

    # 1. Krypto je aktivní VŽDY
    active_lists.append(("Kryptoměny (Binance Futures)", "CRYPTO", CRYPTO_WATCHLIST))

    # 2. Evropské akcie
    if is_eu_stocks_open(now):
        active_lists.append(("Evropské akcie (XETRA, Euronext)", "STOCK_EU", EU_STOCKS_WATCHLIST))

    # 3. US akcie
    if is_us_stocks_open(now):
        active_lists.append(("US akcie (NYSE, NASDAQ)", "STOCK_US", US_STOCKS_WATCHLIST))

    return active_lists


def get_market_status_dict() -> Dict[str, Any]:
    """Vrátí strukturovaný stav všech tří trhů pro logování a UI."""
    now = get_prague_now()
    eu_open = is_eu_stocks_open(now)
    us_open = is_us_stocks_open(now)

    return {
        "prague_time": now.strftime("%Y-%m-%d %H:%M:%S (%Z)"),
        "weekday_name": ["Pondělí", "Úterý", "Středa", "Čtvrtek", "Pátek", "Sobota", "Neděle"][now.weekday()],
        "is_weekend": now.weekday() >= 5,
        "eu_open": eu_open,
        "us_open": us_open,
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
        description=f"Časové pásmo: **Praha ({day_str}, {now_str})**",
        color=discord.Color.teal()
    )

    # Evropské trhy
    eu_icon = "🟢 OTEVŘENO" if status["eu_open"] else "🔴 ZAVŘENO"
    embed.add_field(
        name=f"🇪🇺 Evropské akcie: {eu_icon}",
        value=(
            f"• **Obchodní hodiny:** `09:00 - 17:30 CET` (Po-Pá)\n"
            f"• **Sledovaný watchlist:** `{', '.join(EU_STOCKS_WATCHLIST)}`\n"
            f"• *Mimo tyto hodiny a o víkendech se neskenují.*"
        ),
        inline=False
    )

    # US trhy
    us_icon = "🟢 OTEVŘENO" if status["us_open"] else "🔴 ZAVŘENO"
    embed.add_field(
        name=f"🇺🇸 US akcie: {us_icon}",
        value=(
            f"• **Obchodní hodiny:** `15:30 - 22:00 CET` (Po-Pá)\n"
            f"• **Sledovaný watchlist:** `{', '.join(US_STOCKS_WATCHLIST)}`\n"
            f"• *Mimo tyto hodiny a o víkendech se neskenují.*"
        ),
        inline=False
    )

    # Krypto trhy
    embed.add_field(
        name="🪙 Kryptoměny (Binance Futures): 🟢 OTEVŘENO 24/7",
        value=(
            f"• **Obchodní hodiny:** `Nonstop 24/7/365`\n"
            f"• **Sledované páry:** `{', '.join(CRYPTO_WATCHLIST)}`\n"
            f"• *Aktivní neustále včetně víkendů a nocí.*"
        ),
        inline=False
    )

    savings_text = "Ochrana aktivní: Bot volá Gemini pouze pro trhy, které jsou reálně otevřené, aby neplýtval kredity."
    embed.set_footer(text=f"AIBot 2.0 • {savings_text}")
    return embed
