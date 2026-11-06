"""Offline test swing F0: matematika (swing_rules), skener nad syntetickými svíčkami, parsery odpovědí Gemini.
Spuštění:  python test_swing_rules.py
"""
import os, sys
from datetime import date
from pathlib import Path
os.environ.update(BINANCE_API_KEY="k", BINANCE_SECRET="s", BINANCE_FUTURES_URL="https://testnet.fake",
                  TRADING_212_API_KEY="k", TRADING_212_SECRET_KEY="s", TRADING_212_ENV="demo")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd
import swing_rules as R
import swing_scanner as S
import swing_analyst as A

FAILS = []
def ok(cond, name, info=""):
    print(("OK   " if cond else "FAIL ") + name + (f"  ({info})" if info and not cond else ""))
    if not cond:
        FAILS.append(name)

def bars(n=260, start=50.0, drift=0.002, vol=1_000_000, end="2026-10-30", wave=0.0):
    idx = pd.bdate_range(end=end, periods=n)
    c = start * np.cumprod(np.full(n, 1 + drift)) * (1 + wave * np.sin(np.arange(n) / 6.0))
    return pd.DataFrame({"Open": c * 0.998, "High": c * 1.01, "Low": c * 0.99, "Close": c, "Volume": float(vol)}, index=idx)

# --- indikátory a filtr
up = bars(wave=0.04)
ind = R.indicators(up)
ok(ind is not None and ind["close"] > ind["sma200"] and ind["atr14"] > 0, "indikátory z rostoucí řady", ind)
ok(R.indicators(bars(100)) is None, "málo dat -> None")
ok(R.universe_reject(ind) is None, "rostoucí likvidní akcie projde vesmírem")
ok(R.universe_reject(R.indicators(bars(vol=10_000))) == "obrat < 10 mil. USD", "nízký obrat vyřazen")
ok(R.universe_reject(R.indicators(bars(start=2.0, vol=10_000_000))) == "cena < 5 USD", "levná akcie vyřazena")
ok(R.universe_reject(R.indicators(bars(drift=-0.002))) == "pod SMA50/SMA200", "klesající akcie vyřazena")
ok(R.regime_ok(500, 480) and not R.regime_ok(470, 480), "režim SPY vs SMA200")

# --- skóre
p = R.score_parts(ind, 0.0)
ok(p["trend"] == 1.0 and 0 <= R.total_score(p) <= 1, "trend 1.0 u čistého růstu", p)
ok(abs(sum(R.WEIGHTS.values()) - 1.0) < 1e-9, "váhy dávají 1")
ok(R.news_score([]) == 0.0 and R.news_score(["Acme beats estimates, raises guidance"]) == 1.0
   and R.news_score(["Acme misses, downgrade"]) == 0.0 and R.news_score(["Acme holds meeting"]) == 0.5, "zprávy: tón titulků")

# --- plán obchodu
side = 0.002
pl = R.plan_trade(100.0, 2.0, 97.0, 115.0, side)
ok(pl["ok"] and pl["stop"] == 96.0, "SL = nižší z (support 97, entry-2ATR 96)", pl)
pl2 = R.plan_trade(100.0, 2.0, 94.0, 115.0, side)
ok(pl2["stop"] == 94.0, "support pod 2ATR -> SL na supportu", pl2)
ok(not R.plan_trade(100.0, 2.0, 97.0, 105.0, side)["ok"], "RRR < 2 po nákladech zamítnuto")
ok(not R.plan_trade(100.0, 2.0, 97.0, None, side)["ok"], "bez rezistence zamítnuto")
rr_gross = (115 - 100) / (100 - 96)
ok(pl["rr_net"] < rr_gross, "náklady snižují RRR", pl["rr_net"])

# --- velikost
sz = R.size_position(1000.0, 1000.0, 100.0, 96.0, side, 0.9)
risk_ps = (4 + 0.2 + 0.192) * 0.9
ok(abs(sz["risk_acct"] - 5.0) < 0.05 or sz["limited_by"] != "risk", "riziko 0,5 % účtu vč. nákladů", sz)
ok(sz["notional_acct"] <= 200.0 + 1e-6, "strop 20 % účtu", sz)
ok(R.size_position(1000.0, 10.0, 100.0, 96.0, side, 0.9)["limited_by"] == "hotovost", "strop volná hotovost")

# --- portfolio
posx = lambda t, sec, risk=0.0: {"ticker": t, "sector": sec, "status": "PENDING", "planned_risk_acct": risk}
ok(R.portfolio_block([posx(f"T{i}", f"S{i}") for i in range(5)], "X", 1, 1000, 1) is not None, "max 5 pozic")
ok("sektor" in (R.portfolio_block([posx("A", "Tech")], "Tech", 1, 1000, 1) or ""), "max 1 na sektor")
ok("riziko" in (R.portfolio_block([posx("A", "S1", 25.0)], "S2", 6.0, 1000, 1) or ""), "otevřené riziko <= 3 %")
ok(R.portfolio_block([posx("A", "S1", 20.0)], "S2", 5.0, 1000, 1) is None, "v limitu projde")
ok("ticker" in (R.portfolio_block([posx("A", "S1")], "S2", 1.0, 1000, 1, "A") or ""), "stejný ticker podruhé ne")

# --- správa pozice
pos = {"ticker": "X", "entry": 100.0, "stop": 96.0, "r_ps": 4.0, "qty_open": 10.0, "cost_side": side}
ok(R.process_bar(dict(pos), 101, 103, 97, entry_day=True) == [], "den vstupu: bez SL nic")
ok(R.process_bar(dict(pos), 101, 110, 95, entry_day=True)[0]["reason"] == "STOP", "den vstupu: jen SL (ne polovina)")
ok(R.process_bar(dict(pos), 101, 109, 99, entry_day=False)[0] == {"qty": 5.0, "price": 108.0, "reason": "PARTIAL_2R"}, "polovina na +2R")
g = R.process_bar(dict(pos), 94, 95, 93, entry_day=False)[0]
ok(g["reason"] == "GAP_STOP" and g["price"] == 94, "gap pod stop -> open", g)
ok(R.process_bar(dict(pos), 99, 109, 95, entry_day=False)[0]["reason"] == "STOP", "stop i +2R ve stejný den -> stop (konzervativně)")
p2 = dict(pos, partial_done=True)
ok(R.process_bar(p2, 101, 120, 99, entry_day=False) == [], "polovina jen jednou")
p3 = dict(pos); R.update_excursions(p3, 104.5, 99)
ns = R.next_stop(p3, 104.5, 10.0)
ok(ns["moved"] and ns["why"] == ["BE"] and abs(ns["stop"] - 100.4) < 1e-6, "BE na +1R (entry + náklady)", ns)
ns2 = R.next_stop(dict(p3, stop=100.4), 130.0, 3.0)
ok(ns2["why"] == ["CHANDELIER"] and ns2["stop"] == 121.0, "chandelier HH22 - 3×ATR22", ns2)
ok(not R.next_stop(dict(p3, stop=125.0), 130.0, 3.0)["moved"], "stop se nikdy nesnižuje")
ok(R.tighten_stop(96.0, 110.0, 2.0) == 106.0 and R.tighten_stop(108.0, 110.0, 2.0) == 108.0, "TIGHTEN jen nahoru")
ok(R.time_stop_due({"days_held": 10, "mfe_r": 0.3}) and not R.time_stop_due({"days_held": 10, "mfe_r": 0.6})
   and not R.time_stop_due({"days_held": 9, "mfe_r": 0.0}), "časový stop 10 dní / +0,5R")
ok(R.position_open_risk(dict(pos, stop=101.0), 1.0) == 0.0 and R.position_open_risk(pos, 1.0) > 40, "otevřené riziko pozice")

# --- skener nad syntetikou (bez sítě)
data = {"GOOD": bars(wave=0.05, vol=2_000_000), "DOWN": bars(drift=-0.002), "THIN": bars(vol=1000), "SPY": bars(start=400)}
uni = {"GOOD": "Information Technology", "DOWN": "Energy", "THIN": "Utilities"}
S.t212_tradable = lambda s: True
res = S.scan(date(2026, 11, 2), date(2026, 11, 9), [], ["GOOD"], "EUR", news_fn=lambda t: ["Good beats estimates"],
             earnings_fn=lambda t: None, data=data, universe=uni)
ok(res["ok"] and res["regime_ok"], "sken: SPY nad SMA200", res.get("reason"))
ok(res["stats"]["rejects"].get("pod SMA50/SMA200") == 1 and res["stats"]["rejects"].get("obrat < 10 mil. USD") == 1,
   "sken: vyřazení podle filtru", res["stats"])
ok(abs(res["side_rate"] - 0.002) < 1e-9, "náklady US akcie z účtu v EUR = 0,2 % na stranu")
ok(S.earnings_block(date(2026, 11, 5), date(2026, 11, 2), date(2026, 11, 9)) and
   S.earnings_block(date(2026, 10, 30), date(2026, 11, 2), date(2026, 11, 9)) is None, "výsledky do 5 obch. dní blokují, minulé ne")
spy_down = {"SPY": bars(start=400, drift=-0.002), "GOOD": data["GOOD"]}
ok(S.scan(date(2026, 11, 2), date(2026, 11, 9), [], [], "EUR", data=spy_down, universe=uni)["regime_ok"] is False, "SPY pod SMA200")
df_cut = S.closed_before(data["GOOD"], date(2026, 10, 30))
ok(df_cut.index[-1].date() < date(2026, 10, 30), "ráno jen svíčky do včerejška")

# --- parsery Gemini
cands = [{"ticker": "AAA", "score": 0.7, "parts": {}, "sector": "X", "headlines": ["AAA wins contract"],
          "ind": dict(ind), "plan": pl}, {"ticker": "BBB", "score": 0.65, "parts": {}, "sector": "Y", "headlines": [],
          "ind": dict(ind), "plan": pl}]
prompt, idm, valid = A.build_pick_prompt(cands)
ok(idm == {"C1": "AAA", "C2": "BBB"} and "C1-N1" in valid and "<untrusted_news>" in prompt, "výběr: id kandidátů a zpráv")
picks, bad = A.parse_picks({"picks": [
    {"id": "C1", "reason_cs": "r", "catalyst": "k", "invalidation": "i", "cited_ids": ["C1", "C1-N1"]},
    {"id": "C9", "reason_cs": "r", "catalyst": "k", "invalidation": "i", "cited_ids": ["C9"]},
    {"id": "C2", "reason_cs": "r", "catalyst": "k", "invalidation": "i", "cited_ids": ["C2-N7"]},
    {"id": "C1", "reason_cs": "dup", "catalyst": "k", "invalidation": "i", "cited_ids": ["C1"]}]}, idm, valid)
ok([p["ticker"] for p in picks] == ["AAA"] and len(bad) == 2, "výběr: neznámé id a cizí citace zahozeny", (picks, bad))
v = A.parse_vote({"decision": "BUY", "counterarguments": ["a"], "reason_cs": "x", "cited_ids": ["C1"]}, {"C1"})
ok(v["decision"] == "NONE", "BUY s 1 protiargumentem -> NONE")
v = A.parse_vote({"decision": "BUY", "counterarguments": ["a", "b"], "reason_cs": "x", "cited_ids": ["C1"]}, {"C1"})
ok(v["decision"] == "BUY", "BUY se 2 protiargumenty a citací platí")
ok(A.parse_vote({"decision": "BUY", "counterarguments": ["a", "b"], "cited_ids": ["Z1"]}, {"C1"})["decision"] == "NONE", "BUY s cizí citací -> NONE")
chk = A.parse_check({"results": [{"id": "P1", "action": "SELL", "cited_ids": [], "reason_cs": "x"},
                                 {"id": "P2", "action": "SELL", "cited_ids": ["P2-N1"], "reason_cs": "y"},
                                 {"id": "P3", "action": "TIGHTEN", "cited_ids": [], "reason_cs": "z"}]},
                    {"P1": {"P1-N1"}, "P2": {"P2-N1"}, "P3": set()})
ok(chk["P1"]["action"] == "HOLD" and chk["P1"]["ignored"] and chk["P2"]["action"] == "SELL" and chk["P3"]["action"] == "TIGHTEN",
   "večerní kontrola: SELL bez citace zprávy ignorován", chk)
b = A.Budget(10, 11); b.take()
try:
    b.take(); ok(False, "rozpočet Gemini")
except A.BudgetExceeded:
    ok(True, "rozpočet Gemini: 12. volání odmítnuto")

print("\nVŠECHNO OK" if not FAILS else f"\n{len(FAILS)} SELHALO: {FAILS}")
sys.exit(1 if FAILS else 0)
