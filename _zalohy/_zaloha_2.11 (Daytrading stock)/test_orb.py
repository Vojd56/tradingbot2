"""Offline test ORB15 (orb_strategy.py) se syntetickými svíčkami, žádné API.
Spuštění:  python test_orb.py
"""
import os, sys
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
os.environ.update(BINANCE_API_KEY="k", BINANCE_SECRET="s", BINANCE_FUTURES_URL="https://testnet.fake",
                  TRADING_212_API_KEY="k", TRADING_212_SECRET_KEY="s", TRADING_212_ENV="demo")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import orb_strategy as orb
from cost_model import VenueCost

FAILS = []
def ok(cond, name, info=""):
    print(("OK   " if cond else "FAIL ") + name + (f"  ({info})" if info and not cond else ""))
    if not cond:
        FAILS.append(name)

NY = "America/New_York"
DAY = date(2026, 10, 28)
NO_COST = VenueCost(0.0, 0.0, 0.0)
COST = VenueCost(0.0, 0.15, 0.05)

def hist_days(n=15):
    out, d = [], DAY
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return sorted(out)

def bars_5m(today_bars, vol_hist=1000.0):
    """Historie 15 dní (plochá cena 100, objem vol_hist na svíčku) + dnešní svíčky [(čas, o, h, l, c, v)]."""
    rows = []
    for d in hist_days():
        for k in range(78):
            ts = pd.Timestamp(datetime.combine(d, dtime(9, 30)), tz=NY) + pd.Timedelta(minutes=5 * k)
            rows.append((ts, 100, 100.5, 99.5, 100, vol_hist))
    for t, o, h, l, c, v in today_bars:
        rows.append((pd.Timestamp(datetime.combine(DAY, t), tz=NY), o, h, l, c, v))
    df = pd.DataFrame([r[1:] for r in rows], columns=["Open", "High", "Low", "Close", "Volume"],
                      index=pd.DatetimeIndex([r[0] for r in rows]))
    return df

def daily(atr_range=4.0):
    idx = pd.bdate_range(end=pd.Timestamp(DAY), periods=25, tz=NY)
    return pd.DataFrame({"Open": 100.0, "High": 100 + atr_range / 2, "Low": 100 - atr_range / 2,
                         "Close": 100.0, "Volume": 1e6}, index=idx)

def T(h, m):
    return dtime(h, m)

# Pásmo 100-101, růstové, objem 3000 na svíčku (RVOL 3)
ORB = [(T(9, 30), 100.0, 100.6, 100.0, 100.4, 3000), (T(9, 35), 100.4, 101.0, 100.3, 100.8, 3000),
       (T(9, 40), 100.8, 100.9, 100.5, 100.7, 3000), (T(9, 45), 100.7, 100.9, 100.6, 100.8, 1000)]

s = orb.build_setup("TEST", bars_5m(ORB), daily(), DAY)
ok(s.skip_reason is None and s.orb_high == 101.0 and s.orb_low == 100.0, "platné pásmo 100-101", s)
ok(abs(s.rvol15 - 3.0) < 1e-9 and abs(s.atr14 - 4.0) < 1e-9, "RVOL15 = 3, ATR = 4", (s.rvol15, s.atr14))

low = [(t, o, h, l, c, 1000) for t, o, h, l, c, _ in ORB]
ok("RVOL15" in (orb.build_setup("TEST", bars_5m(low), daily(), DAY).skip_reason or ""), "nízký RVOL15 -> den se vynechá")
ok("ATR" in (orb.build_setup("TEST", bars_5m(ORB), daily(atr_range=0.5), DAY).skip_reason or ""), "pásmo širší než 1,5 x ATR -> vynechat den")
bear = [(T(9, 30), 101.0, 101.0, 100.0, 100.5, 3000)] + ORB[1:2] + [(T(9, 40), 100.8, 100.9, 100.0, 100.2, 3000)] + ORB[3:]
ok("růstových" in (orb.build_setup("TEST", bars_5m(bear), daily(), DAY).skip_reason or ""), "prvních 15 min klesá -> vynechat den")

# Knot nad pásmem, close pod ním -> žádný vstup
wick = ORB + [(T(9, 50), 100.8, 102.0, 100.7, 100.9, 1000), (T(9, 55), 100.9, 100.95, 100.8, 100.9, 1000)]
r = orb.simulate_day(s, bars_5m(wick), NO_COST)
ok(r["status"] == "WAITING", "falešný průraz knotem -> nevstoupit", r.get("status"))

# Průraz nad ORB high, ale pod VWAP -> nevstoupit
ok(not orb.entry_signal(s, pd.Timestamp(datetime.combine(DAY, T(9, 50)), tz=NY), 101.2, vwap=101.5), "průraz pod VWAP -> nevstoupit")
ok(not orb.entry_signal(s, pd.Timestamp(datetime.combine(DAY, T(14, 30)), tz=NY), 102, vwap=100), "svíčka končící po 14:30 -> nevstoupit")
ok(not orb.entry_signal(s, pd.Timestamp(datetime.combine(DAY, T(9, 40)), tz=NY), 102, vwap=100), "svíčka končící 09:45 -> ještě ne")

# Správný vstup: svíčka 09:50 zavře 101.5 nad pásmem i VWAP -> vstup za open svíčky 09:55
brk = ORB + [(T(9, 50), 100.8, 101.6, 100.8, 101.5, 2000)]
r = orb.simulate_day(s, bars_5m(brk), NO_COST)
ok(r["status"] == "PENDING", "signál na poslední svíčce -> čeká na open další", r.get("status"))
day_up = brk + [(T(9, 55), 101.6, 101.7, 101.5, 101.6, 1000)]
r = orb.simulate_day(s, bars_5m(day_up), NO_COST)
ok(r["status"] == "OPEN" and r["entry"] == 101.6 and r["entry_at"].startswith("2026-10-28T09:55"), "vstup za open další svíčky", r)
vw = float(orb.vwap_series(orb._day_bars(bars_5m(brk), DAY)).iloc[-1])
ok(abs(r["stop"] - max(100.0, 101.6 - 1.0, vw)) < 1e-9, "stop = max(ORB low, entry - 0,25 ATR, VWAP)", (r["stop"], vw))

# Stop: další svíčka propadne pod stop -> A i B -1R (bez nákladů)
stop_day = day_up + [(T(10, 0), 101.5, 101.5, 100.0, 100.2, 1000)]
r = orb.simulate_day(s, bars_5m(stop_day), NO_COST)
ok(r["status"] == "CLOSED" and r["variant_a"]["r_net"] == -1.0 and r["variant_b"]["r_net"] == -1.0, "stop -> -1R", r.get("variant_a"))
rc = orb.simulate_day(s, bars_5m(stop_day), COST)
ok(rc["variant_a"]["r_net"] < -1.0 and rc["cost_share_r"] > 0, "s náklady je ztráta větší než 1R", rc.get("variant_a"))

# +2R půlka, pak close pod VWAP; B drží do 15:50
R = r["risk"]
e = 101.6
run = day_up + [(T(10, 0), e, e + 2.1 * R, e, e + 2 * R, 1000)]
t = T(10, 5)
px = e + 2 * R
while t < T(15, 50):
    run.append((t, px, px + 0.01, px - 0.01, px, 1000))
    t = (datetime.combine(DAY, t) + timedelta(minutes=5)).time()
run.append((T(15, 50), px, px, px - 0.01, px, 1000))
r = orb.simulate_day(s, bars_5m(run), NO_COST)
fa = r["variant_a"]["fills"]
ok(fa[0][3] == "+2R půlka" and abs(r["variant_a"]["r_net"] - 2.0) < 0.05, "A: půlka na +2R, zbytek ~+2R do konce dne", r["variant_a"])
ok(r["variant_b"]["fills"][-1][3] == "konec dne" and abs(r["variant_b"]["r_net"] - 2.0) < 0.05, "B: výstup v 15:50", r["variant_b"])
ok(r["status"] == "CLOSED", "po 15:50 je den uzavřený")

# Zkrácená seance: vstupy končí 11:30
ok(orb.entry_deadline(dtime(13, 0)) == dtime(11, 30) and orb.entry_deadline(dtime(16, 0)) == dtime(14, 30), "konec vstupů u zkrácené seance")

# Souhrn pro bránu M4
trades = [{"day": "2026-10-01", "status": "CLOSED", "cost_ok": True, "catalyst": {"direction": "YES"}, "variant_a": {"r_net": 2.0}},
          {"day": "2026-10-02", "status": "CLOSED", "cost_ok": True, "catalyst": {"direction": "YES"}, "variant_a": {"r_net": -1.0}},
          {"day": "2026-10-03", "status": "CLOSED", "cost_ok": False, "catalyst": {"direction": "YES"}, "variant_a": {"r_net": -1.0}},
          {"day": "2026-10-04", "status": "CLOSED", "cost_ok": True, "catalyst": {"direction": "NO"}, "variant_a": {"r_net": -1.0}}]
sm = orb.summarize(trades)
ok(sm["n"] == 4 and sm["net_r"] == -1.0 and sm["pf"] == round(2 / 3, 2) and sm["max_dd_r"] == 3.0, "souhrn: n, R, PF, propad", sm)
g = orb.summarize(trades, only_cost_ok=True, only_catalyst=True)
ok(g["n"] == 2 and g["pf"] == 2.0, "brána: jen obchody s náklady OK a katalyzátorem", g)

# --- Stínová smyčka (orb_shadow.run_pass) s podstrčenými daty ---
import asyncio, tempfile
import orb_shadow, decision_log
tmp = Path(tempfile.mkdtemp())
orb_shadow.SHADOW_FILE = tmp / "paper_orb.json"
logged = []
orb_shadow.decision_log.log_decision = lambda rec, path=None: logged.append(rec)
orb_shadow.candidates = lambda: ["TEST", "FLAT"]
orb_shadow._account_ccy = lambda: "EUR"
def fake_setup(t, day, now):
    return s if t == "TEST" else orb.build_setup("FLAT", bars_5m(low), daily(), DAY)
orb_shadow.fetch_setup_sync = fake_setup
orb_shadow.fetch_today_sync = lambda t, now: orb._day_bars(bars_5m(day_up), DAY)
async def fake_cat(t):
    return {"direction": "YES", "catalyst": "EARNINGS_BEAT"}
orb_shadow._catalyst = fake_cat
now = pd.Timestamp(datetime.combine(DAY, T(10, 0, )), tz=NY).to_pydatetime() + timedelta(seconds=20)
res = asyncio.run(orb_shadow.run_pass(now))
store = orb_shadow.load_trades()
ok(res and res["valid"] == 1 and store["2026-10-28|TEST"]["status"] == "OPEN" and store["2026-10-28|FLAT"]["status"] == "NO_SETUP",
   "průchod: TEST otevřen ve stínu, FLAT bez pásma", res)
ok(store["2026-10-28|TEST"]["catalyst"]["direction"] == "YES" and logged and logged[0]["action"] == "SHADOW_ENTRY",
   "katalyzátor a záznam do signals.jsonl", logged)
ok(abs(store["2026-10-28|TEST"]["cost_pct_per_side"] - 0.2) < 1e-9, "náklady účtu v EUR u US akcie: 0,2 % na stranu")
ok("ORB15 stín 2026-10-28" in orb_shadow.day_summary("2026-10-28") and "/60" in orb_shadow.day_summary("2026-10-28"), "denní souhrn pro Discord")
ok(asyncio.run(orb_shadow.run_pass(pd.Timestamp(datetime.combine(DAY, T(9, 40)), tz=NY).to_pydatetime())) is None, "před 09:45 se nic nedělá")

print("\nVŠECHNO OK" if not FAILS else f"\n{len(FAILS)} SELHALO: {FAILS}")
sys.exit(1 if FAILS else 0)
