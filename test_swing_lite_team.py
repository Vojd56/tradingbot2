"""Offline test stopy D „Lite tým“ (swing_lite_team.py): jen Flash Lite, bez jasného důvodu ze zpráv SKIP,
nákup jen při SKEPTIK = BUY a >= 2/3 hlasů. Bez sítě.
Spuštění:  python test_swing_lite_team.py
"""
import os, sys, asyncio, json, tempfile
from pathlib import Path
os.environ.update(BINANCE_API_KEY="k", BINANCE_SECRET="s", BINANCE_FUTURES_URL="https://testnet.fake",
                  TRADING_212_API_KEY="k", TRADING_212_SECRET_KEY="s", TRADING_212_ENV="demo")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import model_manager as MM
import gemini_team
import swing_analyst as A
import swing_lite_team as D

FAILS = []
def ok(cond, name, info=""):
    print(("OK   " if cond else "FAIL ") + name + (f"  ({info})" if info and not cond else ""))
    if not cond:
        FAILS.append(name)

qm = MM.ModelQuotaManager(Path(tempfile.mkdtemp()) / "q.json")
D.quota_manager = qm
ok([m["id"] for m in qm.lite_models()] == ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"], "D: 3.5 Lite, pak 3.1 Lite")
qm.data["counts"]["gemini-3.5-flash-lite"] = 500 - MM.LITE_KEEP_FOR_SCOUT
ok([m["id"] for m in qm.lite_models()] == ["gemini-3.1-flash-lite"], "D nesáhne na rezervu Flash Lite pro Scout")
qm.data["counts"] = {}

calls = []
def make(answers):
    async def fake(prompt, label, schema=None, temperature=0.0, timeout=14.0, models=None, component="signal"):
        calls.append((label, [m["id"] for m in models], component))
        return json.dumps(answers.pop(0)), models[0]["id"]
    return fake
c = {"ticker": "X", "sector": "S", "score": 0.7, "parts": {}, "headlines": ["X wins big contract"],
     "ind": {k: 1.0 for k in ("close", "ema20", "ema50", "ema200", "rsi14", "atr14", "support", "resistance", "ret20",
                              "rel_vol", "sma50", "sma200", "atr22", "hh22")}, "plan": {"stop": 0.9, "target": 1.2, "rr_net": 2.0}}

gemini_team._call_models = make([{"clear_reason": False, "reason_cs": "nic", "cited_ids": []},
                                 {"setup": True, "reason_cs": "ok"}, {"reasons": ["a", "b", "c"], "verdict": "BUY"}])
r = asyncio.run(D.team_decide(c, A.Budget(0, 60)))
ok(not r["buy"] and r["skeptic"]["verdict"] == "SKIP" and len(calls) == 3, "bez jasného důvodu ze zpráv SKIP, hlasy se nevolají", r)
ok(all(set(m) <= set(MM.LITE_MODEL_IDS) and comp == "swing_D" for _, m, comp in calls), "jen Flash Lite, účtováno swing_D", calls)

calls.clear()
gemini_team._call_models = make([{"clear_reason": True, "reason_cs": "zakázka", "cited_ids": ["C1-N1"]},
                                 {"setup": True, "reason_cs": "trend"}, {"reasons": ["a", "b", "c"], "verdict": "BUY"},
                                 {"decision": "BUY", "reason_cs": ""}, {"decision": "NONE", "reason_cs": ""},
                                 {"decision": "BUY", "reason_cs": ""}])
r = asyncio.run(D.team_decide(c, A.Budget(0, 60)))
ok(r["buy"] and r["buy_votes"] == 2 and len(calls) == 6, "SKEPTIK BUY + 2/3 BUY -> nákup (6 volání)", r)

calls.clear()
gemini_team._call_models = make([{"clear_reason": True, "reason_cs": "x", "cited_ids": ["Z9"]},
                                 {"setup": True, "reason_cs": ""}, {"reasons": [], "verdict": "BUY"}])
ok(not asyncio.run(D.team_decide(c, A.Budget(0, 60)))["buy"], "citace zprávy mimo vstup = žádný jasný důvod")

gemini_team._call_models = make([{"clear_reason": True, "reason_cs": "x", "cited_ids": ["C1-N1"]}] * 6)
try:
    asyncio.run(D.team_decide(c, A.Budget(59, 60))); ok(False, "rozpočet D")
except A.BudgetExceeded:
    ok(True, "rozpočet D (60/den) se hlídá")

print("\nVŠECHNO OK" if not FAILS else f"\n{len(FAILS)} SELHALO: {FAILS}")
sys.exit(1 if FAILS else 0)
