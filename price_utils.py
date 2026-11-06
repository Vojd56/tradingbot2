"""Pomocné funkce pro práci s cenami různých řádů (BTC ~ 100 000 USD vs. DOGE ~ 0.18 USD).

Pevné zaokrouhlení na 2 desetinná místa u levných coinů ničilo Stop-Loss (u DOGE vycházel SL == Entry).
Přesné zaokrouhlení na tickSize burzy dělá až binance_client.format_price při zadávání příkazu.
"""


def price_decimals(price: float) -> int:
    """Počet desetinných míst tak, aby cena měla aspoň ~6 platných číslic (min. 2)."""
    p = abs(float(price or 0))
    if p >= 1000:
        return 2
    if p >= 100:
        return 3
    if p >= 1:
        return 4
    if p >= 0.01:
        return 6
    return 8


def round_price(price: float) -> float:
    """Zaokrouhlí cenu na rozumný počet desetinných míst podle jejího řádu."""
    return round(float(price or 0), price_decimals(price))


def fmt_price(price: float) -> str:
    """Naformátuje cenu pro výpis (Discord, logy)."""
    try:
        return f"{float(price):.{price_decimals(price)}f}"
    except (TypeError, ValueError):
        return str(price)
