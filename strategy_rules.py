"""Čistá pravidla strategie SFP 15M a risk managementu (bez API a bez Discordu, kvůli offline testům).

Používají je gemini_team.py (vstup), bot.py (velikost pozice) a position_manager.py (správa otevřené pozice).

Zdroje pravidel SFP (26. 10. 2026):
- SFP: stop pod knot sweepu, jen uzavřená svíčka, jen výrazná dna (LuxAlgo, Swing Failure Pattern)
- Při +1R prodat polovinu, teprve pak stop zbytku na nulu (Benci: čistý BE po 1R ořezal 90 ze 143 vítězů)
- Časový stop jako třetí bariéra (triple barrier)

Revize 28. 10. 2026 (backtest 30 dní, 10 párů, 377 signálů):
- SFP je ztrátové už PŘED poplatky (−27,3R), poplatky přidají −96R. Žádný řez daty (šířka stopu, bez částečného
  prodeje) není kladný, proto se prahy neladí (overfitting) a živé vstupy vypíná SFP_LIVE_ENABLED.
- Náklady (poplatek + skluz, obě strany) se nově počítají do rizika i do RRR. Stop 0,15 % s round-tripem 0,14 %
  znamenal, že burza brala skoro celé R.
"""

import math
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from price_utils import round_price
from cost_model import VenueCost

from config import (
    SFP_MIN_RISK_PCT,
    SFP_MAX_RISK_PCT,
    SFP_RR,
    SFP_MAX_CHASE_R,
    PARTIAL_TAKE_AT_R,
    BREAKEVEN_FEE_BUFFER_PCT,
    SFP_TIME_STOP_HOURS,
    FEE_PCT_PER_SIDE,
    SLIPPAGE_PCT_PER_SIDE,
    MAX_COST_SHARE_OF_R,
    MIN_NET_RR,
    RISK_PER_TRADE_PCT_MAX,
)

STRATEGY_SFP = "SFP15"
STRATEGY_HIGH20 = "HIGH20"  # stejný název jako daily_breakout.STRATEGY_HIGH20


# =====================================================================
# NÁKLADY A RIZIKO
# =====================================================================

def _cost_rate(fee_pct: float = FEE_PCT_PER_SIDE, slip_pct: float = SLIPPAGE_PCT_PER_SIDE) -> float:
    return (fee_pct + slip_pct) / 100.0


def round_trip_cost(entry: float, exit_price: float, cost: Optional[VenueCost] = None) -> float:
    """Náklady na 1 kus za nákup + prodej: poplatek z obou plnění + skluz na obou stranách (v ceně).

    cost = náklady trhu (cost_model); None = Binance (krypto, jako dřív). Daň z nákupu jen ze vstupu.
    """
    if cost is None:
        return (entry + exit_price) * _cost_rate()
    return (entry + exit_price) * cost.side_rate() + entry * cost.buy_tax_pct / 100.0


def loss_per_unit(entry: float, stop_loss: float, cost: Optional[VenueCost] = None) -> float:
    """Skutečná ztráta na 1 kus, když obchod dojede na Stop-Loss: vzdálenost stopu + náklady obou stran."""
    return max(entry - stop_loss, 0.0) + round_trip_cost(entry, stop_loss, cost)


def net_rr(entry: float, stop_loss: float, take_profit: float, cost: Optional[VenueCost] = None) -> float:
    """RRR po nákladech: (zisk na TP − náklady) / (ztráta na SL + náklady)."""
    loss = loss_per_unit(entry, stop_loss, cost)
    gain = (take_profit - entry) - round_trip_cost(entry, take_profit, cost)
    return gain / loss if loss > 0 else 0.0


def min_tp_for_net_rr(entry: float, stop_loss: float, rr: float = MIN_NET_RR,
                      cost: Optional[VenueCost] = None) -> float:
    """Nejnižší TP, při kterém je RRR po nákladech aspoň rr (řeší tp − entry − (entry + tp)·k = rr·loss)."""
    k = cost.side_rate() if cost else _cost_rate()
    tax = entry * cost.buy_tax_pct / 100.0 if cost else 0.0
    return (rr * loss_per_unit(entry, stop_loss, cost) + entry * (1 + k) + tax) / (1 - k)


def risk_sized_qty(equity: float, entry: float, stop_loss: float, risk_pct: float,
                   max_notional: float, cost: Optional[VenueCost] = None) -> float:
    """Počet kusů tak, aby ztráta na stopu VČETNĚ nákladů byla risk_pct % kapitálu.

    risk_pct se ořízne na RISK_PER_TRADE_PCT_MAX (2 %). Nominál nikdy nepřesáhne max_notional
    (volná hotovost / alokační strop), takže při úzkém stopu se pozice nenafoukne nad účet.
    Zaokrouhlení na krok burzy dělá volající přes floor_qty, VŽDY dolů.
    """
    risk_pct = min(max(risk_pct, 0.0), RISK_PER_TRADE_PCT_MAX)
    per_unit = loss_per_unit(entry, stop_loss, cost)
    if equity <= 0 or entry <= 0 or stop_loss <= 0 or stop_loss >= entry or per_unit <= 0:
        return 0.0
    qty = equity * risk_pct / 100.0 / per_unit
    return max(0.0, min(qty, max(max_notional, 0.0) / entry))


def floor_qty(qty: float, step: float) -> float:
    """Zaokrouhlení dolů na krok (0,01 akcie, lot size burzy). Dolů, aby se riziko nikdy nezvětšilo."""
    if step <= 0:
        return qty
    return math.floor(qty / step + 1e-9) * step


def stock_cost_check(entry: float, stop_loss: float, take_profit: float, cost: VenueCost) -> Dict[str, Any]:
    """Kontrola akciového obchodu (krok M1.2): náklady jako podíl R a TP pro RRR 1:2 PO nákladech.

    Vrací {"take_profit", "net_rr", "cost_share_r", "skip_reason"}. TP se jen zvedá (nikdy nesnižuje),
    skip_reason != None = náklady sní víc než MAX_COST_SHARE_OF_R rizika.
    """
    risk = entry - stop_loss
    out: Dict[str, Any] = {"take_profit": take_profit, "net_rr": 0.0, "cost_share_r": 0.0, "skip_reason": None}
    if entry <= 0 or risk <= 0:
        out["skip_reason"] = "Stop-Loss neleží pod vstupem"
        return out
    share = round_trip_cost(entry, stop_loss, cost) / risk
    out["cost_share_r"] = round(share, 3)
    tp = max(take_profit, min_tp_for_net_rr(entry, stop_loss, cost=cost))
    out["take_profit"] = round_price(tp)
    out["net_rr"] = round(net_rr(entry, stop_loss, out["take_profit"], cost), 2)
    if share > MAX_COST_SHARE_OF_R:
        out["skip_reason"] = (f"Náklady {share:.2f}R > {MAX_COST_SHARE_OF_R}R ({cost.describe()}; "
                              f"stop {risk / entry * 100:.2f} % je na ně moc úzký)")
    return out


# =====================================================================
# VSTUP SFP
# =====================================================================

def apply_setup_levels(price: float, setup: Dict[str, Any]) -> Dict[str, Any]:
    """Vstup podle SFP setupu: SL ze setupu (pod knotem), entry = aktuální cena, TP aspoň 1:2 PO nákladech.

    Bez look-ahead: setup pochází z poslední UZAVŘENÉ 15M svíčky (binance_client.analyze_sfp), entry je cena
    v okamžiku rozhodnutí, ne close signální svíčky.
    Vrací {"entry", "stop_loss", "take_profit", "rrr", "risk_pct", "net_rr", "cost_share_r", "skip_reason"};
    skip_reason != None = nekupovat.
    """
    sl = float(setup.get("stop_loss") or 0.0)
    signal_entry = float(setup.get("entry") or price)
    entry = float(price or signal_entry)
    out: Dict[str, Any] = {"entry": round_price(entry), "stop_loss": round_price(sl), "take_profit": 0.0,
                           "rrr": f"1:{SFP_RR:.1f}", "risk_pct": 0.0, "net_rr": 0.0, "cost_share_r": 0.0,
                           "skip_reason": None}

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

    # Náklady jako podíl R: při úzkém stopu by poplatky a skluz sežraly většinu edge
    cost_share = round_trip_cost(entry, sl) / risk
    out["cost_share_r"] = round(cost_share, 3)
    if cost_share > MAX_COST_SHARE_OF_R:
        out["skip_reason"] = (f"Náklady {cost_share:.2f}R > {MAX_COST_SHARE_OF_R}R "
                              f"(stop {risk_pct:.2f} % je na poplatky moc úzký)")
        return out

    tp = max(entry + SFP_RR * risk, min_tp_for_net_rr(entry, sl))
    out["take_profit"] = round_price(tp)
    out["net_rr"] = round(net_rr(entry, sl, tp), 2)
    out["rrr"] = f"1:{(tp - entry) / risk:.1f}"
    return out


# =====================================================================
# SPRÁVA OTEVŘENÉ SFP POZICE
# =====================================================================

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
    HIGH20 (daily_breakout.py) má jen časový výstup: exit_at = konec dne vstupu.
    """
    now = now or datetime.now(timezone.utc)
    if trade.get("strategy") == STRATEGY_HIGH20:
        until = _parse_dt(trade.get("exit_at"))
        return "TIME_STOP" if until and now >= until else None
    if trade.get("strategy") != STRATEGY_SFP:
        return None

    opened = _parse_dt(trade.get("decided_at"))
    if opened and (now - opened).total_seconds() >= SFP_TIME_STOP_HOURS * 3600:
        return "TIME_STOP"

    if not trade.get("partial_taken"):
        r = initial_risk(trade)
        entry = float(trade.get("entry") or 0.0)
        if r > 0 and price >= entry + PARTIAL_TAKE_AT_R * r:
            return "PARTIAL"
    return None
