"""Fáze 3 datové pipeline: swing skener bere denní svíčky z datového skladu data\\ místo stahování 2 let z Yahoo.

- Jednou denně (první sken dne) se zdroj obnoví přes data\\_skripty\\update_all.run: stáhne jen pár posledních dní.
- Tickery, které Yahoo vůbec nemá (odklad 7 dní), a tickery s chybou z kontroly kvality (fáze 2) se vynechají.
- Tickery, které ve skladu nejsou (např. navíc z ranního přehledu), vrátí v `fallback` a skener je stáhne postaru.
Sklad drží jen uzavřené dny do včerejška, což skener potřebuje (closed_before(day)).
"""

import logging
import sys
import threading
from datetime import date
from typing import Dict, List, Optional, Tuple

import pandas as pd

from config import BASE_DIR

logger = logging.getLogger("TradingBot.DataStore")

SKRIPTY = BASE_DIR / "data" / "_skripty"
_lock = threading.Lock()
_RENAME = {"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"}


def _mods():
    if str(SKRIPTY) not in sys.path:
        sys.path.insert(0, str(SKRIPTY))
    import sklad
    import update_all
    return sklad, update_all


def source_of(ticker: str) -> str:
    """US tickery jsou bez tečky (BRK-B), evropské mají burzu za tečkou (SAP.DE); index ^STOXX patří k EU."""
    return "yahoo_eu" if ("." in ticker or ticker == "^STOXX") else "yahoo_us"


def ensure_fresh(source: str, today: Optional[date] = None) -> bool:
    """Obnoví zdroj, pokud dnes ještě obnovený nebyl. True = proběhla obnova."""
    sklad, update_all = _mods()
    today = today or date.today()
    with _lock:
        st = sklad.load_state()
        done_today = any(k.startswith(source + "/") and isinstance(v, dict) and v.get("checked") == today.isoformat()
                         for k, v in st.items())
        if done_today:
            return False
        logger.info(f"📦 Sklad: obnova {source} (první použití dnes)")
        update_all.run([source], vault=True, today=today)
        return True


def load(tickers: List[str], today: Optional[date] = None) -> Tuple[Dict[str, pd.DataFrame], List[str], Dict[str, int]]:
    """(data ve formátu yfinance: Open/High/Low/Close/Volume, tickery ke stažení postaru, počty vynechaných)."""
    sklad, _ = _mods()
    for src in sorted({source_of(t) for t in tickers}):
        try:
            ensure_fresh(src, today)
        except Exception as e:
            logger.warning(f"📦 Sklad: obnova {src} selhala ({e}), použiji uložená data")
    st = sklad.load_state()
    memo = st.get("_kontrola", {})
    data: Dict[str, pd.DataFrame] = {}
    fallback: List[str] = []
    skipped = {"zdroj ticker nemá": 0, "chyba v kontrole dat": 0}
    for t in dict.fromkeys(tickers):
        src = source_of(t)
        s = st.get(sklad.key(src, t)) or {}
        if s.get("retry_after") and not s.get("ok"):
            skipped["zdroj ticker nemá"] += 1
            continue
        m = memo.get(f"{src}/{sklad.file_name(t)}") or {}
        if any(not i.startswith("upozornění") for i in m.get("issues", [])):
            skipped["chyba v kontrole dat"] += 1
            continue
        df = sklad.read(src, t)
        if df is None or df.empty:
            fallback.append(t)
            continue
        data[t] = df.rename(columns=_RENAME)[list(_RENAME.values())]
    return data, fallback, skipped
