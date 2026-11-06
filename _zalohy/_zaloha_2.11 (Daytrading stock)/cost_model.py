"""Náklady obchodu podle trhu a měny účtu (krok M1.1 plánu z 28. 10. 2026).

Dřív se u akcií počítalo se stejnými náklady jako u Binance (poplatek 0,05 % + skluz 0,02 % na stranu).
Trading 212 nebere poplatek za obchod, ale když se měna akcie liší od měny účtu, strhne za převod
T212_FX_FEE_PCT_PER_SIDE (0,15 %) při nákupu i prodeji. Účet v CZK tak platí konverzi u všech akcií,
účet v EUR jen u akcií mimo eurozónu (US, Curych, londýnské GDR v USD).

Daň z nákupu: britské akcie (SDRT 0,5 %) skener od 28. 10. vynechává, tabulka je pojistka pro ruční /scout.
Paříž a Madrid: daň z transakcí se počítá z čistého nákupu za den, intradenní obchod by ji platit neměl
(ověřit v historii poplatků T212), proto 0.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional

from config import (
    FEE_PCT_PER_SIDE,
    SLIPPAGE_PCT_PER_SIDE,
    T212_FX_FEE_PCT_PER_SIDE,
    STOCK_SLIPPAGE_PCT_PER_SIDE,
)

# Daň z nákupu podle přípony symbolu Yahoo (v % z objemu nákupu)
BUY_TAX_PCT_BY_SUFFIX = {".L": 0.5}


@dataclass(frozen=True)
class VenueCost:
    fee_pct_per_side: float
    fx_pct_per_side: float
    slippage_pct_per_side: float
    buy_tax_pct: float = 0.0

    @property
    def pct_per_side(self) -> float:
        return self.fee_pct_per_side + self.fx_pct_per_side + self.slippage_pct_per_side

    def side_rate(self) -> float:
        return self.pct_per_side / 100.0

    def describe(self) -> str:
        parts = [f"{self.pct_per_side:.2f} % na stranu"]
        if self.fx_pct_per_side:
            parts.append(f"z toho převod měny {self.fx_pct_per_side:.2f} %")
        if self.buy_tax_pct:
            parts.append(f"daň z nákupu {self.buy_tax_pct:.2f} %")
        return ", ".join(parts)


BINANCE_COST = VenueCost(FEE_PCT_PER_SIDE, 0.0, SLIPPAGE_PCT_PER_SIDE)


def _norm_ccy(ccy: Optional[str]) -> Optional[str]:
    if not ccy:
        return None
    c = str(ccy).strip()
    if c in ("GBX", "GBp", "GBx"):
        return "GBP"  # pence jsou pořád libry, převod měny je stejný
    return c.upper()


def buy_tax_pct(ticker: str) -> float:
    t = ticker.upper()
    return next((v for suf, v in BUY_TAX_PCT_BY_SUFFIX.items() if t.endswith(suf.upper())), 0.0)


def stock_cost(ticker: str, instrument_ccy: Optional[str], account_ccy: Optional[str]) -> Optional[VenueCost]:
    """Náklady akcie na T212. None = neznámá měna (pak se nekupuje)."""
    i, a = _norm_ccy(instrument_ccy), _norm_ccy(account_ccy)
    if not i or not a:
        return None
    fx = T212_FX_FEE_PCT_PER_SIDE if i != a else 0.0
    return VenueCost(0.0, fx, STOCK_SLIPPAGE_PCT_PER_SIDE, buy_tax_pct(ticker))


def cost_fields(cost: VenueCost) -> Dict[str, float]:
    """Pole, která se ukládají k obchodu (risk_guard z nich počítá otevřené riziko)."""
    return {"cost_pct_per_side": round(cost.pct_per_side, 4), "buy_tax_pct": cost.buy_tax_pct}


def cost_from_trade(trade: Dict[str, Any]) -> Optional[VenueCost]:
    """Náklady uložené u obchodu; starší obchody bez pole -> None (počítá se po staru)."""
    try:
        pct = trade.get("cost_pct_per_side")
        if pct is None:
            return None
        return VenueCost(0.0, 0.0, float(pct), float(trade.get("buy_tax_pct") or 0.0))
    except (TypeError, ValueError):
        return None
