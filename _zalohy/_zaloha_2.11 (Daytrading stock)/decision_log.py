"""Záznam rozhodnutí (krok M0.2 plánu z 28. 10. 2026): každý signál do signals.jsonl i s tím, co bot v tu chvíli věděl.

Jeden řádek = jeden JSON objekt (ticker, strategie, cena, pohyb, objem, verdikt LLM, akce, důvod).
Slouží k pozdějšímu vyhodnocení (živé výsledky vs. backtest, kolik obchodů zastavila kontrola nákladů).
Zápis nikdy nevyhodí výjimku: chyba logu nesmí zastavit obchodování.
"""

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict

from config import BASE_DIR

logger = logging.getLogger("TradingBot.DecisionLog")
SIGNALS_FILE = BASE_DIR / "signals.jsonl"
_lock = threading.Lock()


def log_decision(record: Dict[str, Any], path=None) -> None:
    rec = {"ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), **record}
    try:
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with _lock, open(path or SIGNALS_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        logger.warning(f"Záznam rozhodnutí se nepodařilo zapsat: {e}")
