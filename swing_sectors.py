"""Sektor akcie pro limit „1 pozice na sektor“ (3. 11. 2026).

US: GICS sektor ze seznamu S&P 1500 (Wikipedie). EU a ostatní: sektor z yfinance (info["sector"]) převedený na stejné
GICS názvy. Výsledek se ukládá do swing_sectors.json, aby se Yahoo nedotazovalo znovu (a při limitu 429 se nezaseklo).
Neznámý sektor se NEslučuje: každý ticker bez sektoru je sám sobě sektorem ("Unknown:<ticker>"), zapíše se varování.
"""

import json
import logging
import threading
import time
from typing import Dict, Optional

from config import BASE_DIR

logger = logging.getLogger("TradingBot.SwingSectors")

CACHE_FILE = BASE_DIR / "swing_sectors.json"
_lock = threading.Lock()
_cache: Optional[Dict[str, str]] = None

# yfinance (Morningstar) -> GICS názvy jako u US vesmíru
YF_TO_GICS = {
    "technology": "Information Technology", "healthcare": "Health Care", "financial services": "Financials",
    "consumer cyclical": "Consumer Discretionary", "consumer defensive": "Consumer Staples",
    "basic materials": "Materials", "communication services": "Communication Services", "industrials": "Industrials",
    "energy": "Energy", "utilities": "Utilities", "real estate": "Real Estate",
}
GICS = set(YF_TO_GICS.values())


def normalize(sector: Optional[str]) -> Optional[str]:
    if not sector:
        return None
    s = str(sector).strip()
    if s in GICS:
        return s
    return YF_TO_GICS.get(s.lower())


def is_unknown(sector: Optional[str]) -> bool:
    return not sector or str(sector).startswith("Unknown")


def unknown_for(ticker: str) -> str:
    return f"Unknown:{ticker.upper()}"


def _load() -> Dict[str, str]:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            _cache = {}
    return _cache


def _save() -> None:
    try:
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_load(), ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")
        tmp.replace(CACHE_FILE)
    except Exception as e:
        logger.warning(f"swing_sectors.json nejde uložit: {e}")


def fetch_yf(ticker: str, retries: int = 2) -> Optional[str]:
    import yfinance as yf
    for k in range(retries + 1):
        try:
            return normalize((yf.Ticker(ticker).info or {}).get("sector"))
        except Exception as e:
            if k == retries:
                logger.debug(f"Sektor {ticker}: {e}")
            else:
                time.sleep(3 * (k + 1))
    return None


def resolve(ticker: str, known: Optional[str] = None, fetch=None) -> str:
    """GICS sektor tickeru; neznámý = vlastní 'Unknown:<ticker>' (nikdy společný 'Unknown')."""
    t = ticker.upper()
    k = normalize(known)
    if k:
        return k
    with _lock:
        hit = _load().get(t)
    if hit:
        return hit
    sec = (fetch or fetch_yf)(t)
    if sec:
        with _lock:
            _load()[t] = sec
            _save()
        return sec
    logger.warning(f"⚠️ Swing: sektor {t} neznámý, bere se jako samostatný sektor")
    return unknown_for(t)
