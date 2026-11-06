"""Offline test živé swing stopy A (swing_live.py) s falešným brokerem: demo pojistka, velikost, vlastnictví kusů,
stop jen nahoru, žádný prodej v den vstupu, sesouhlasení s T212. Žádné API.
Spuštění:  python test_swing_live.py
"""
import os, sys, tempfile
from datetime import date, datetime
from pathlib import Path
os.environ.update(BINANCE_API_KEY="k", BINANCE_SECRET="s", BINANCE_FUTURES_URL="https://testnet.fake",
                  TRADING_212_API_KEY="k", TRADING_212_SECRET_KEY="s", TRADING_212_ENV="demo")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import market_hours
import swing_live as L
import swing_shadow

FAILS = []
def ok(cond, name, info=""):
    print(("OK   " if cond else "FAIL ") + name + (f"  ({info})" if info and not cond else ""))
    if not cond:
        FAILS.append(name)

tmp = Path(tempfile.mkdtemp())
L.STATE_FILE = tmp / "live.json"
swing_shadow.LOG_FILE = tmp / "log.jsonl"
L.global_block = lambda t: None
L.intraday_qty = lambda t: 0.0
NY = market_hours.NY_TZ
D1, D2 = date(2026, 11, 3), date(2026, 11, 4)
at = lambda d, h, m: datetime(d.year, d.month, d.day, h, m, tzinfo=NY)


class FakeBroker(L.Broker):
    def __init__(self):
        self.held, self.orders, self.calls, self.price, self.n = {}, [], [], {}, 100
        self.acct = {"currency": "EUR", "totalValue": 2000.0, "cash": {"availableToTrade": 2000.0}}
    def summary(self): return self.acct
    def portfolio(self): return [{"ticker": f"{t}_US_EQ", "quantity": q, "averagePrice": 100.0, "currentPrice": self.price.get(t, 100.0)}
                                 for t, q in self.held.items() if q > 0]
    def pending(self): return list(self.orders)
    def qty(self, pf, s): return next((p["quantity"] for p in pf if p["ticker"] == f"{s}_US_EQ"), 0.0)
    def avg_price(self, pf, s): return 100.0
    def cur_price(self, pf, s): return self.price.get(s, 100.0)
    def t212_ticker(self, s): return f"{s}_US_EQ"
    def fx(self, a, b): return 1.0
    def last_price(self, s): return self.price.get(s, 100.0)
    def _oid(self):
        self.n += 1
        return self.n
    def limit_buy(self, s, q, px):
        oid = self._oid(); self.orders.append({"id": oid, "ticker": f"{s}_US_EQ", "type": "LIMIT", "qty": q})
        self.calls.append(("limit_buy", s, q, px)); return {"success": True, "order_id": oid}
    def stop_sell(self, s, q, stop):
        oid = self._oid(); self.orders.append({"id": oid, "ticker": f"{s}_US_EQ", "type": "STOP", "qty": q, "stop": stop})
        self.calls.append(("stop", s, q, stop)); return {"success": True, "ref": {"id": oid}}
    def cancel(self, oid):
        self.calls.append(("cancel", oid))
        before = len(self.orders)
        self.orders = [o for o in self.orders if o["id"] != int(oid)]
        return {"success": True} if len(self.orders) < before else {"success": False, "not_found": True}
    def sell_own(self, s, q, others):
        self.calls.append(("sell", s, q, others))
        sell = min(q, self.held.get(s, 0) - others)
        self.held[s] -= sell
        return {"success": True, "quantity": sell, "exit_price": self.price.get(s, 100.0)}
    def fill(self, s):
        o = next(o for o in self.orders if o["type"] == "LIMIT" and o["ticker"] == f"{s}_US_EQ")
        self.orders.remove(o); self.held[s] = self.held.get(s, 0) + o["qty"]
    def stops(self, s): return [o for o in self.orders if o["type"] == "STOP" and o["ticker"] == f"{s}_US_EQ"]

def cand(t, sector="Tech", stop=96.0):
    return {"ticker": t, "sector": sector, "score": 0.8, "ind": {"atr14": 2.0}, "plan": {"stop": stop}}
def pos(t):
    return next(p for p in L.load_state()["positions"] if p["ticker"] == t)

# --- demo pojistka
ok(L.demo_guard("demo", "https://demo.trading212.com/api/v0") is None, "demo účet: naživo dovoleno")
ok(L.demo_guard("live", "https://live.trading212.com/api/v0") is not None, "live účet: odmítnuto")
ok(L.demo_guard("demo", "https://live.trading212.com/api/v0") is not None, "demo env, ale live URL: odmítnuto")

# --- ráno plán, 09:50 nákup se správnou velikostí
b = FakeBroker()
b.held["MAN"] = 3.0                       # ruční pozice uživatele v jiném tickeru
ok(L.plan_entries([cand("AAA"), cand("MAN", "Energy"), cand("BBB", "Tech")], D1) == ["AAA", "MAN", "BBB"], "plán 3 tickerů")
ok(L.step(at(D1, 9, 40), b) == [] and pos("AAA")["status"] == "PLANNED", "před 09:45 se nenakupuje")
msgs = L.step(at(D1, 9, 50), b)
buy = [c for c in b.calls if c[0] == "limit_buy"]
ok(len(buy) == 1 and buy[0][1] == "AAA" and buy[0][3] == 100.3, "limitní nákup AAA za cenu + 0,3 %", b.calls)
risk = buy[0][2] * ((100.3 - 96) + 100.3 * 0.002 + 96 * 0.002)
ok(abs(risk - 10.0) < 0.1 and buy[0][2] * 100.3 <= 400.0, "velikost: riziko 0,5 % z 2000 vč. nákladů, max 20 %", (buy, risk))
ok(pos("MAN")["status"] == "CANCELLED" and "portfoliu" in pos("MAN")["cancel_reason"], "ticker s ruční pozicí se nekupuje")
ok(pos("BBB")["status"] == "CANCELLED" and "sektor" in pos("BBB")["cancel_reason"], "1 pozice na sektor")

# --- vyplnění -> stop na burze
b.fill("AAA")
msgs = L.step(at(D1, 9, 55), b)
p = pos("AAA")
ok(p["status"] == "OPEN" and p["stop_order_id"] and len(b.stops("AAA")) == 1 and b.stops("AAA")[0]["stop"] == 96.0,
   "po vyplnění hned Stop-Loss na T212", (p, msgs))

# --- den vstupu: ani +2R ani naplánovaný výstup neprodá
b.price["AAA"] = 130.0
st = L.load_state(); st["positions"][0]["pending_exit"] = "TIME_STOP"; L.save_state(st)
L.step(at(D1, 11, 0), b)
ok(not any(c[0] == "sell" for c in b.calls) and pos("AAA")["qty_open"] == p["qty"], "v den vstupu žádný prodej (jen stop na burze)")
st = L.load_state(); st["positions"][0].pop("pending_exit"); L.save_state(st)

# --- další den: polovina na +2R, jen vlastní kusy, zbytek znovu chráněn
b.held["AAA"] += 5.0                      # uživatel si ručně přikoupil 5 ks AAA
L.step(at(D2, 10, 0), b)
p2 = pos("AAA")
sells = [c for c in b.calls if c[0] == "sell"]
ok(p2["partial_done"] and sells and abs(sells[0][3] - 5.0) < 1e-6, "polovina na +2R, ruční kusy vyloučené z prodeje", sells)
ok(abs(b.held["AAA"] - (5.0 + p2["qty_open"])) < 1e-6, "ruční kusy zůstaly nedotčené", b.held)
ok(len(b.stops("AAA")) == 1 and abs(b.stops("AAA")[0]["qty"] - p2["qty_open"]) < 1e-6, "zbytek má nový stop jen na swing množství")

# --- večer: stop jen nahoru
before = list(b.calls)
L.evening(D2, {"AAA": {"high": 131, "low": 120, "close": 125}}, {"AAA": {"hh22": 131, "atr22": 1.0, "atr14": 2.0}}, {}, b)
new_stop = pos("AAA")["stop"]
ok(new_stop == 128.0 and b.stops("AAA")[0]["stop"] == 128.0, "chandelier zvedl stop i na T212", new_stop)
calls_n = len(b.calls)
L.evening(D2, {"AAA": {"high": 126, "low": 110, "close": 112}}, {"AAA": {"hh22": 120, "atr22": 5.0, "atr14": 2.0}}, {}, b)
ok(pos("AAA")["stop"] == 128.0 and len(b.calls) == calls_n, "nižší stop se nikdy nezadá")
ok(not L.move_stop(b, pos("AAA"), 100.0, ["X"], []), "move_stop odmítne snížení")

# --- stop na burze proběhl, ruční kusy zůstanou, nic se neprodá
own = pos("AAA")["qty_open"]
b.orders = [o for o in b.orders if o["type"] != "STOP"]; b.held["AAA"] -= own
n_sells = len([c for c in b.calls if c[0] == "sell"])
msgs = L.step(at(D2, 11, 0), b)
ok(pos("AAA")["status"] == "CLOSED" and b.held["AAA"] == 5.0 and len([c for c in b.calls if c[0] == "sell"]) == n_sells,
   "stop na T212 -> uzavřeno, ruční kusy nedotčené", msgs)

# --- nesoulad: kusy zmizely, stop čeká -> blokace nových nákupů + upozornění
b2 = FakeBroker(); L.STATE_FILE = tmp / "live2.json"
L.plan_entries([cand("CCC")], D1); L.step(at(D1, 9, 50), b2); b2.fill("CCC"); L.step(at(D1, 9, 55), b2)
b2.held["CCC"] = 0.0
msgs = L.step(at(D1, 10, 0), b2)
ok(L.load_state()["blocked"] and any("nesoulad" in m for m in msgs), "nesoulad -> Discord upozornění a blokace", msgs)
L.plan_entries([cand("DDD", "Health")], D1)
L.step(at(D1, 10, 5), b2)
ok(pos("DDD")["status"] == "CANCELLED" and "nesoulad" in pos("DDD")["cancel_reason"], "při nesouladu žádné nové nákupy")

# --- intradenní modul swing tickery nekupuje a počítá je do limitu
import paper_trading
L.STATE_FILE = tmp / "live3.json"
L.save_state({"positions": [{"id": "L-x", "ticker": "ZZZ", "status": "OPEN"}], "blocked": None})
info = paper_trading.get_positions_limit_info("ZZZ")
ok(info["already_open"] and not info["can_open"], "intradenní modul swing ticker nekoupí", info)
ok(not any(str(t.get("trade_id", "")).startswith("L-") for t in paper_trading.load_trades()),
   "swing pozice nejsou v paper_trades.json (intradenní flat 15:50 na ně nesáhne)")

print("\nVŠECHNO OK" if not FAILS else f"\n{len(FAILS)} SELHALO: {FAILS}")
sys.exit(1 if FAILS else 0)
