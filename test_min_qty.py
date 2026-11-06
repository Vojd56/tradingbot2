"""Offline test minima obchodu T212 (HTTP 400 min-quantity-exceeded): naučení z chyby, příkaz pod minimem se neodešle,
swing nekupuje pod minimem a polovinu nedělí, když by polovina nebo zbytek byly pod minimem.
Spuštění:  python test_min_qty.py
"""
import os, sys, tempfile
from datetime import date, datetime
from pathlib import Path
os.environ.update(BINANCE_API_KEY="k", BINANCE_SECRET="s", BINANCE_FUTURES_URL="https://testnet.fake",
                  TRADING_212_API_KEY="k", TRADING_212_SECRET_KEY="s", TRADING_212_ENV="demo")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import market_hours
import trading212 as T
import swing_live as L
import swing_shadow

FAILS = []
def ok(cond, name, info=""):
    print(("OK   " if cond else "FAIL ") + name + (f"  ({info})" if info and not cond else ""))
    if not cond:
        FAILS.append(name)

tmp = Path(tempfile.mkdtemp())
T.MIN_QTY_FILE, T._min_qty = tmp / "minq.json", None
err = 'HTTP 400: {"type":"/api-errors/min-quantity-exceeded","title":"Error","status":400,"detail":"must trade at least 0.01223171"}'
ok(T.parse_min_qty(err) == 0.01223171 and T.parse_min_qty("HTTP 400: insufficient funds") is None, "minimum z chyby T212")
T.remember_min_qty("NXT", err)
ok(T.min_quantity("NXT") == 0.01223171 and T.min_quantity("AAPL") == 0.0, "minimum uložené k tickeru")
calls = []
T._http = lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("nemělo se volat API"))
r = T.place_market_order_sync("NXT", 0.01)
ok(not r["success"] and r.get("below_min") and not calls, "nákup 0,01 ks pod minimem se vůbec neodešle", r)
r = T.place_limit_buy_sync("NXT", 0.01, 90.0)
ok(not r["success"] and r.get("below_min") and not calls, "limitní nákup pod minimem se neodešle")

# swing: nákup pod minimem zrušen, polovina pod minimem se nedělí
L.STATE_FILE = tmp / "live.json"
swing_shadow.LOG_FILE = tmp / "log.jsonl"
L.global_block = lambda t: None
L.intraday_qty = lambda t: 0.0
NY = market_hours.NY_TZ
class B(L.Broker):
    def __init__(self, mq): self.mq, self.calls, self.held, self.orders, self.n = mq, [], {}, [], 0
    def summary(self): return {"currency": "EUR", "totalValue": 2000.0, "cash": {"availableToTrade": 2000.0}}
    def portfolio(self): return [{"ticker": t, "quantity": q} for t, q in self.held.items()]
    def pending(self): return list(self.orders)
    def qty(self, pf, s): return next((p["quantity"] for p in pf if p["ticker"] == s), 0.0)
    def avg_price(self, pf, s): return 100.0
    def cur_price(self, pf, s): return 200.0
    def t212_ticker(self, s): return s
    def fx(self, a, c): return 1.0
    def last_price(self, s): return 100.0
    def min_qty(self, s): return self.mq
    def limit_buy(self, s, q, px):
        self.n += 1; self.orders.append({"id": self.n, "ticker": s}); self.calls.append(("buy", s, q)); return {"success": True, "order_id": self.n}
    def stop_sell(self, s, q, stop):
        self.n += 1; self.orders.append({"id": self.n, "ticker": s, "stop": True}); self.calls.append(("stop", s, q)); return {"success": True, "ref": {"id": self.n}}
    def cancel(self, oid): self.calls.append(("cancel", oid)); return {"success": True}
    def sell_own(self, s, q, o): self.calls.append(("sell", s, q)); return {"success": True, "quantity": q}
cand = lambda t: {"ticker": t, "sector": "Tech", "score": 0.8, "ind": {"atr14": 2.0}, "plan": {"stop": 96.0}}
b = B(50.0)
L.plan_entries([cand("BIG")], date(2026, 11, 3))
L.step(datetime(2026, 11, 3, 9, 50, tzinfo=NY), b)
p = L.load_state()["positions"][0]
ok(p["status"] == "CANCELLED" and "minimem" in p["cancel_reason"] and not any(c[0] == "buy" for c in b.calls),
   "swing nekupuje pod minimem T212", p)
L.save_state({"positions": [{"id": "L-x", "ticker": "SM", "market": "US", "status": "OPEN", "entry_day": "2026-11-02",
                             "entry": 100.0, "stop": 96.0, "r_ps": 4.0, "target": 108.8, "qty": 0.03, "qty_open": 0.03,
                             "cost_side": 0.002, "fx_entry": 1.0, "stop_order_id": "1", "seen_held": 0.03}], "blocked": None})
b2 = B(0.02); b2.held["SM"] = 0.03; b2.orders.append({"id": 1, "ticker": "SM", "stop": True})
msgs = L.step(datetime(2026, 11, 3, 10, 0, tzinfo=NY), b2)
p = L.load_state()["positions"][0]
ok(not any(c[0] in ("sell", "cancel") for c in b2.calls) and p["partial_done"] and p["qty_open"] == 0.03 and p["stop_order_id"] == "1",
   "polovina pod minimem: nedělí se, stop na celé pozici zůstává", (b2.calls, msgs))

print("\nVŠECHNO OK" if not FAILS else f"\n{len(FAILS)} SELHALO: {FAILS}")
sys.exit(1 if FAILS else 0)
