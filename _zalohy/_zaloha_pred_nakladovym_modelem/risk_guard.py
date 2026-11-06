"""Pojistka proti sérii ztrát (circuit breaker, ECC skill llm-trading-agent-security).

Blokuje jen NOVÉ vstupy. Otevřené pozice bot hlídá dál (SL/TP, flat-by, časový stop).
Kontrola běží v kódu před každým nákupem, ne v promptu, takže ji model nemůže obejít.

Tři pojistky:
1. Denní limit: realizovaná ztráta bota za dnešek (UTC) >= DAILY_LOSS_LIMIT_USD -> stop do půlnoci UTC.
2. Série ztrát: posledních MAX_CONSECUTIVE_LOSSES uzavřených obchodů skončilo ve ztrátě -> pauza
   LOSS_STREAK_PAUSE_HOURS od poslední z nich.
3. Vypínač: ve složce bota existuje soubor STOP (smazáním se obchodování zase povolí).
4. Portfolio heat (revize 28. 10.): dnešní realizovaná ztráta + riziko všech otevřených pozic bota
   (kdyby všechny dojely na Stop-Loss) >= MAX_OPEN_RISK_USD -> žádné nové vstupy. Bez toho mohlo 10
   korelovaných krypto longů přeskočit denní limit najednou, protože ten počítal jen uzavřené obchody.
   Kontrolu nového obchodu dělá would_exceed_heat() s rizikem, které by obchod přidal.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import (DAILY_LOSS_LIMIT_USD, MAX_CONSECUTIVE_LOSSES, LOSS_STREAK_PAUSE_HOURS, KILL_SWITCH_FILE,
                    MAX_OPEN_RISK_USD)
from paper_trading import load_trades, is_bot_owned
from strategy_rules import loss_per_unit


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


def _usd_rate(trade: Dict[str, Any]) -> float:
    """Kolik USD je 1 jednotka měny instrumentu (pole usd_per_unit_ccy zapisuje bot.py při nákupu).

    Krypto a US akcie = 1. Starší obchody bez pole berou 1; u EUR akcií to riziko podhodnotí asi o 10 %.
    """
    try:
        return float(trade.get("usd_per_unit_ccy") or 1.0)
    except (TypeError, ValueError):
        return 1.0


def trade_open_risk_usd(trade: Dict[str, Any]) -> float:
    """Ztráta pozice bota v USD, kdyby teď dojela na Stop-Loss (vč. poplatků a skluzu). Stop nad entry = 0."""
    try:
        qty = float(trade.get("bot_qty", trade.get("shares", 0)) or 0)
        entry = float(trade.get("entry") or 0)
        sl = float(trade.get("stop_loss") or 0)
    except (TypeError, ValueError):
        return 0.0
    if qty <= 0 or entry <= 0 or sl <= 0 or sl >= entry:
        return 0.0
    return qty * loss_per_unit(entry, sl) * _usd_rate(trade)


def open_risk_usd(trades: List[Dict[str, Any]]) -> float:
    """Součet rizika všech otevřených pozic bota (ruční pozice uživatele se nepočítají)."""
    return sum(trade_open_risk_usd(t) for t in trades if t.get("status") == "OPEN" and is_bot_owned(t))


def _today_pnl(trades: List[Dict[str, Any]], now: datetime) -> float:
    total = 0.0
    for t in trades:
        dt = _closed_at(t)
        if t.get("status") == "CLOSED" and is_bot_owned(t) and dt and dt.date() == now.date():
            total += _pnl(t)
    return total


def would_exceed_heat(new_risk_usd: float, now: Optional[datetime] = None,
                      trades: Optional[List[Dict[str, Any]]] = None) -> Optional[str]:
    """Vrátí důvod, pokud by nový obchod s rizikem new_risk_usd přetáhl dnešní ztrátu + otevřené riziko přes limit."""
    now = now or datetime.now(timezone.utc)
    if trades is None:
        trades = load_trades()
    lost = max(-_today_pnl(trades, now), 0.0)
    heat = open_risk_usd(trades)
    if lost + heat + new_risk_usd > MAX_OPEN_RISK_USD:
        return (f"Portfolio heat: dnešní ztráta {lost:.0f} + otevřené riziko {heat:.0f} + nový obchod "
                f"{new_risk_usd:.0f} USD > limit {MAX_OPEN_RISK_USD:.0f} USD")
    return None


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

    heat = would_exceed_heat(0.0, now, trades)
    if heat:
        return heat

    if MAX_CONSECUTIVE_LOSSES > 0 and len(closed) >= MAX_CONSECUTIVE_LOSSES:
        last = closed[-MAX_CONSECUTIVE_LOSSES:]
        if all(_pnl(t) < 0 for t, _ in last):
            until = last[-1][1] + timedelta(hours=LOSS_STREAK_PAUSE_HOURS)
            if now < until:
                return (f"{MAX_CONSECUTIVE_LOSSES} ztráty za sebou: pauza do "
                        f"{until.strftime('%H:%M')} UTC")
    return None
