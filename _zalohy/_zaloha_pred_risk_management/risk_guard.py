"""Pojistka proti sérii ztrát (circuit breaker, ECC skill llm-trading-agent-security).

Blokuje jen NOVÉ vstupy. Otevřené pozice bot hlídá dál (SL/TP, flat-by, časový stop).
Kontrola běží v kódu před každým nákupem, ne v promptu, takže ji model nemůže obejít.

Tři pojistky:
1. Denní limit: realizovaná ztráta bota za dnešek (UTC) >= DAILY_LOSS_LIMIT_USD -> stop do půlnoci UTC.
2. Série ztrát: posledních MAX_CONSECUTIVE_LOSSES uzavřených obchodů skončilo ve ztrátě -> pauza
   LOSS_STREAK_PAUSE_HOURS od poslední z nich.
3. Vypínač: ve složce bota existuje soubor STOP (smazáním se obchodování zase povolí).
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import DAILY_LOSS_LIMIT_USD, MAX_CONSECUTIVE_LOSSES, LOSS_STREAK_PAUSE_HOURS, KILL_SWITCH_FILE
from paper_trading import load_trades, is_bot_owned


def _closed_at(trade: Dict[str, Any]) -> Optional[datetime]:
    value = trade.get("closed_at")
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _pnl(trade: Dict[str, Any]) -> float:
    try:
        return float(trade.get("pnl_usd") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def entries_blocked(now: Optional[datetime] = None, trades: Optional[List[Dict[str, Any]]] = None,
                    kill_switch: Optional[Path] = None) -> Optional[str]:
    """Vrátí důvod, proč bot teď nesmí otevírat nové pozice, nebo None."""
    now = now or datetime.now(timezone.utc)
    kill_switch = KILL_SWITCH_FILE if kill_switch is None else kill_switch
    if kill_switch and Path(kill_switch).exists():
        return f"Vypínač: existuje soubor {Path(kill_switch).name} ve složce bota (smaž ho pro obnovení vstupů)"

    if trades is None:
        trades = load_trades()
    closed = [(t, _closed_at(t)) for t in trades if t.get("status") == "CLOSED" and is_bot_owned(t)]
    closed = sorted(((t, dt) for t, dt in closed if dt), key=lambda x: x[1])

    today = now.date()
    day_pnl = sum(_pnl(t) for t, dt in closed if dt.date() == today)
    if day_pnl <= -DAILY_LOSS_LIMIT_USD:
        return f"Denní limit ztráty: dnes {day_pnl:.2f} USD (limit −{DAILY_LOSS_LIMIT_USD:.0f} USD), nové vstupy až po půlnoci UTC"

    if MAX_CONSECUTIVE_LOSSES > 0 and len(closed) >= MAX_CONSECUTIVE_LOSSES:
        last = closed[-MAX_CONSECUTIVE_LOSSES:]
        if all(_pnl(t) < 0 for t, _ in last):
            until = last[-1][1] + timedelta(hours=LOSS_STREAK_PAUSE_HOURS)
            if now < until:
                return (f"{MAX_CONSECUTIVE_LOSSES} ztráty za sebou: pauza do "
                        f"{until.strftime('%H:%M')} UTC")
    return None
