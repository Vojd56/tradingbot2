"""Offline test risk managementu 2.0: náklady v riziku a RRR, risk sizing, portfolio heat (žádné API).
Spuštění:  python test_risk_management.py
"""
import os, sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
os.environ.update(BINANCE_API_KEY="k", BINANCE_SECRET="s", BINANCE_FUTURES_URL="https://testnet.fake",
                  TRADING_212_API_KEY="k", TRADING_212_SECRET_KEY="s", TRADING_212_ENV="demo")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import paper_trading
paper_trading.PAPER_TRADES_FILE = Path(tempfile.mkdtemp()) / "paper_trades_test.json"

from config import RISK_PER_TRADE_PCT_MAX, MAX_OPEN_RISK_USD, MIN_NET_RR
from strategy_rules import (apply_setup_levels, risk_sized_qty, floor_qty, loss_per_unit, net_rr,
                            round_trip_cost)
from risk_guard import open_risk_usd, would_exceed_heat, entries_blocked

FAILS = []
def ok(cond, name, info=""):
    print(("OK   " if cond else "FAIL ") + name + (f"  ({info})" if info and not cond else ""))
    if not cond:
        FAILS.append(name)

# --- Náklady ---
c = round_trip_cost(100.0, 99.0)
ok(abs(c - 199.0 * 0.0007) < 1e-9, "round-trip náklady = (entry + exit) x (poplatek + skluz)", c)
ok(abs(loss_per_unit(100.0, 99.0) - (1.0 + c)) < 1e-9, "ztráta na stopu zahrnuje náklady")

# --- Risk sizing ---
q = risk_sized_qty(10000, 100.0, 99.0, 1.0, 1e9)
ok(abs(q * loss_per_unit(100.0, 99.0) - 100.0) < 1e-6, "1 % z 10 000 -> ztráta na stopu přesně 100 USD vč. nákladů", q)
q2 = risk_sized_qty(10000, 100.0, 99.0, 5.0, 1e9)
ok(abs(q2 * loss_per_unit(100.0, 99.0) - 10000 * RISK_PER_TRADE_PCT_MAX / 100) < 1e-6, "risk_pct se ořízne na 2 %")
ok(risk_sized_qty(10000, 100.0, 99.9, 1.0, 500.0) == 5.0, "úzký stop -> nominál omezen stropem 500 USD")
ok(risk_sized_qty(10000, 100.0, 100.0, 1.0, 1e9) == 0.0, "stop na entry -> 0 kusů")
ok(risk_sized_qty(10000, 100.0, 101.0, 1.0, 1e9) == 0.0, "stop nad entry -> 0 kusů")
ok(risk_sized_qty(0, 100.0, 99.0, 1.0, 1e9) == 0.0, "nulový kapitál -> 0 kusů")
ok(floor_qty(1.239, 0.01) == 1.23 and floor_qty(0.29, 0.01) == 0.29, "zaokrouhlení vždy dolů, bez chyby plovoucí čárky")

# --- Vstup SFP: náklady a RRR po nákladech ---
r = apply_setup_levels(100.0, {"entry": 100.0, "stop_loss": 99.8})   # stop 0,2 %: náklady 0,7R
ok(r["skip_reason"] and "Náklady" in r["skip_reason"], "stop 0,2 % -> nekupovat, náklady sežerou R", r)
r = apply_setup_levels(100.0, {"entry": 100.0, "stop_loss": 98.8})   # stop 1,2 %
ok(r["skip_reason"] is None, "stop 1,2 % -> obchod se bere", r)
ok(r["net_rr"] >= MIN_NET_RR - 0.01, "RRR po nákladech aspoň 1:2", r)
ok(net_rr(100.0, 98.8, r["take_profit"]) >= MIN_NET_RR - 0.01, "TP z výstupu opravdu dává 1:2 po nákladech")
r = apply_setup_levels(100.0, {"entry": 99.0, "stop_loss": 98.0})    # cena utekla o 1R
ok(r["skip_reason"] and "utekla" in r["skip_reason"], "honění ceny > 0,5R -> nekupovat")

# --- Portfolio heat ---
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
def open_t(qty, entry, sl, manual=False):
    return {"status": "OPEN", "bot_qty": qty, "entry": entry, "stop_loss": sl,
            "opened_by_bot": not manual, "action": "BUY" if manual else "AUTO_BUY"}
def closed_t(pnl, minutes_ago):
    return {"status": "CLOSED", "pnl_usd": pnl, "opened_by_bot": True, "action": "AUTO_BUY",
            "closed_at": (NOW - timedelta(minutes=minutes_ago)).isoformat()}

trades = [open_t(10, 100.0, 95.0), open_t(1, 100.0, 101.0)]
heat = open_risk_usd(trades)
ok(abs(heat - 10 * loss_per_unit(100.0, 95.0)) < 1e-6, "otevřené riziko = qty x ztráta na stopu; stop nad entry = 0", heat)
ok(open_risk_usd([open_t(10, 100.0, 95.0, manual=True)]) == 0.0, "ruční pozice uživatele se do rizika nepočítají")

ok(would_exceed_heat(10.0, NOW, trades) is None, "malé riziko pod limitem -> povoleno")
r = would_exceed_heat(MAX_OPEN_RISK_USD, NOW, trades)
ok(r is not None and "heat" in r, "nový obchod by přetáhl limit -> blokováno", r)
r = would_exceed_heat(60.0, NOW, trades + [closed_t(-200, 30)])
ok(r is not None, "dnešní ztráta 200 + otevřené ~51 + nový 60 > 300 -> blokováno", r)
ok(would_exceed_heat(60.0, NOW, trades + [closed_t(-200, 60 * 20)]) is None, "včerejší ztráta se nepočítá")

hot = [open_t(10, 100.0, 70.0)]   # 10 x ~30 USD = ~301 USD otevřeného rizika
r = entries_blocked(NOW, hot, Path(tempfile.mkdtemp()) / "STOP")
ok(r is not None and "heat" in r, "entries_blocked hlásí plné otevřené riziko", r)

print()
print("VŠECHNO PROŠLO" if not FAILS else f"SELHALO {len(FAILS)}: {FAILS}")
sys.exit(1 if FAILS else 0)
