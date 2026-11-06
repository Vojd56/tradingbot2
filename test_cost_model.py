"""Offline test nákladového modelu akcií (cost_model.py) a kontroly nákladů (strategy_rules.stock_cost_check)."""
import sys

from cost_model import stock_cost, cost_from_trade, cost_fields, BINANCE_COST
from strategy_rules import (round_trip_cost, loss_per_unit, net_rr, risk_sized_qty, stock_cost_check)
from config import MAX_COST_SHARE_OF_R, T212_FX_FEE_PCT_PER_SIDE, STOCK_SLIPPAGE_PCT_PER_SIDE

failed = 0


def ok(cond, name, detail=""):
    global failed
    print(("OK   " if cond else "FAIL ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        failed += 1


fx = T212_FX_FEE_PCT_PER_SIDE
slip = STOCK_SLIPPAGE_PCT_PER_SIDE

# Převod měny jen když se měna akcie liší od měny účtu
c = stock_cost("NVDA", "USD", "CZK")
ok(c.fx_pct_per_side == fx and abs(c.pct_per_side - (fx + slip)) < 1e-12, "NVDA na CZK účtu platí převod měny", c)
ok(stock_cost("NVDA", "USD", "EUR").fx_pct_per_side == fx, "NVDA na EUR účtu platí převod měny")
ok(stock_cost("SAP.DE", "EUR", "EUR").fx_pct_per_side == 0.0, "SAP.DE na EUR účtu bez převodu")
ok(stock_cost("SAP.DE", "EUR", "CZK").fx_pct_per_side == fx, "SAP.DE na CZK účtu s převodem")
ok(stock_cost("STLAP.PA", "EUR", "EUR").buy_tax_pct == 0.0, "Paříž intradenně bez daně (ověřit v T212)")
ok(stock_cost("BARC.L", "GBX", "GBP").fx_pct_per_side == 0.0 and stock_cost("BARC.L", "GBX", "GBP").buy_tax_pct == 0.5,
   "pence = libry, britská akcie má daň 0,5 %")
ok(stock_cost("NVDA", None, "EUR") is None and stock_cost("NVDA", "USD", "") is None, "neznámá měna -> None")

# Krypto beze změny: bez cost se počítá jako dřív (Binance)
ok(abs(round_trip_cost(100.0, 99.0) - round_trip_cost(100.0, 99.0, BINANCE_COST)) < 1e-12, "krypto náklady beze změny")

# Daň jen ze vstupu
t = stock_cost("BARC.L", "GBX", "GBP")
ok(abs(round_trip_cost(100.0, 99.0, t) - ((199.0) * slip / 100 + 0.5)) < 1e-9, "daň z nákupu jen ze vstupní ceny")

# Velikost pozice: ztráta na stopu vč. nákladů = riziko
q = risk_sized_qty(10000, 100.0, 98.0, 0.5, 1e9, c)
ok(abs(q * loss_per_unit(100.0, 98.0, c) - 50.0) < 1e-6, "0,5 % z 10 000 = 50 vč. převodu měny", q)
ok(q < risk_sized_qty(10000, 100.0, 98.0, 0.5, 1e9), "s převodem měny menší pozice než s náklady Binance")

# Kontrola nákladů: US akcie, stop 2 %, převod měny -> náklady ~0,2R > 0,15R
r = stock_cost_check(100.0, 98.0, 104.0, c)
ok(r["skip_reason"] is not None and r["cost_share_r"] > MAX_COST_SHARE_OF_R, "US stop 2 % s převodem měny neprojde", r)
# Xetra na EUR účtu, stop 2 % -> jen skluz, projde
e = stock_cost("SAP.DE", "EUR", "EUR")
r2 = stock_cost_check(100.0, 98.0, 104.0, e)
ok(r2["skip_reason"] is None, "SAP.DE na EUR účtu se stopem 2 % projde", r2)
ok(r2["net_rr"] >= 1.99 and r2["take_profit"] >= 104.0, "TP zvednutý na RRR 1:2 po nákladech", r2)
ok(abs(net_rr(100.0, 98.0, r2["take_profit"], e) - r2["net_rr"]) < 0.01, "net_rr odpovídá TP")
# US akcie se širším stopem 3 % projde
ok(stock_cost_check(100.0, 97.0, 106.0, c)["skip_reason"] is None, "US stop 3 % s převodem měny projde")
ok(stock_cost_check(100.0, 100.0, 104.0, c)["skip_reason"] is not None, "stop na vstupu neprojde")

# Uložení k obchodu a zpětné načtení pro risk_guard
tr = {**cost_fields(c)}
back = cost_from_trade(tr)
ok(abs(loss_per_unit(100.0, 98.0, back) - loss_per_unit(100.0, 98.0, c)) < 1e-9, "náklady z uloženého obchodu = původní")
ok(cost_from_trade({}) is None, "starší obchod bez pole -> po staru")

print(f"\n{'VŠE PROŠLO' if not failed else f'SELHALO {failed}'}")
sys.exit(1 if failed else 0)
