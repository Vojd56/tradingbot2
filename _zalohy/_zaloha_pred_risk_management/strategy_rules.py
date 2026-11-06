"""Čistá pravidla strategie SFP 15M (bez API a bez Discordu, kvůli offline testům).

Používají je gemini_team.py (vstup) a position_manager.py (správa otevřené pozice).
Ověřeno 26. 10. 2026 proti veřejným zdrojům:
- SFP: stop pod knot sweepu, jen uzavřená svíčka, jen výrazná dna (LuxAlgo, Swing Failure Pattern)
- Čistý posun stopu na nulu po 1R často ořeže vítězné obchody (Benci, 265 obchodů: 90 ze 143 skončilo na nule),
  proto se při +1R nejdřív prodá polovina a teprve pak jde stop zbytku na nulu (LuxAlgo, Breakeven Move Rules)
- Časový stop jako třetí bariéra (triple barrier), aby obchod nevisel donekonečna
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from price_utils import round_price

from config import (
    SFP_MIN_RISK_PCT,
    SFP_MAX_RISK_PCT,
    SFP_RR,
    SFP_MAX_CHASE_R,
    PARTIAL_TAKE_AT_R,
    BREAKEVEN_FEE_BUFFER_PCT,
    SFP_TIME_STOP_HOURS,
)

STRATEGY_SFP = "SFP15"


def apply_setup_levels(price: float, setup: Dict[str, Any]) -> Dict[str, Any]:
    """Vstup podle SFP setupu: SL ze setupu (pod knotem), entry = aktuální cena, TP = 2R od aktuální ceny.

    Vrací {"entry", "stop_loss", "take_profit", "rrr", "risk_pct", "skip_reason"}; skip_reason != None = nekupovat.
    """
    sl = float(setup.get("stop_loss") or 0.0)
    signal_entry = float(setup.get("entry") or price)
    entry = float(price or signal_entry)
    out: Dict[str, Any] = {"entry": round_price(entry), "stop_loss": round_price(sl), "take_profit": 0.0,
                           "rrr": f"1:{SFP_RR:.1f}", "risk_pct": 0.0, "skip_reason": None}

    if sl <= 0 or sl >= entry:
        out["skip_reason"] = "Cena už je pod Stop-Lossem setupu"
        return out

    signal_risk = signal_entry - sl
    if signal_risk > 0 and entry - signal_entry > SFP_MAX_CHASE_R * signal_risk:
        out["skip_reason"] = f"Cena od signální svíčky utekla o víc než {SFP_MAX_CHASE_R}R"
        return out

    risk = entry - sl
    risk_pct = risk / entry * 100
    out["risk_pct"] = round(risk_pct, 3)
    if not (SFP_MIN_RISK_PCT <= risk_pct <= SFP_MAX_RISK_PCT):
        out["skip_reason"] = f"Riziko {risk_pct:.2f} % mimo {SFP_MIN_RISK_PCT}-{SFP_MAX_RISK_PCT} %"
        return out

    out["take_profit"] = round_price(entry + SFP_RR * risk)
    return out


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def initial_risk(trade: Dict[str, Any]) -> float:
    """Velikost 1R v ceně (entry minus PŮVODNÍ Stop-Loss)."""
    entry = float(trade.get("entry") or 0.0)
    sl0 = float(trade.get("initial_stop_loss") or trade.get("stop_loss") or 0.0)
    return max(entry - sl0, 0.0)


def breakeven_price(entry: float) -> float:
    """Stop-Loss zbytku po částečném zisku: entry + poplatky."""
    return round_price(entry * (1 + BREAKEVEN_FEE_BUFFER_PCT / 100.0))


def management_action(trade: Dict[str, Any], price: float, now: Optional[datetime] = None) -> Optional[str]:
    """Co udělat s otevřenou SFP pozicí: "TIME_STOP", "PARTIAL" nebo None.

    Týká se jen obchodů otevřených touto strategií (strategy == SFP15); starší pozice běží po starém.
    """
    if trade.get("strategy") != STRATEGY_SFP:
        return None
    now = now or datetime.now(timezone.utc)

    opened = _parse_dt(trade.get("decided_at"))
    if opened and (now - opened).total_seconds() >= SFP_TIME_STOP_HOURS * 3600:
        return "TIME_STOP"

    if not trade.get("partial_taken"):
        r = initial_risk(trade)
        entry = float(trade.get("entry") or 0.0)
        if r > 0 and price >= entry + PARTIAL_TAKE_AT_R * r:
            return "PARTIAL"
    return None
