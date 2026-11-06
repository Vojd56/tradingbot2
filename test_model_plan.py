"""Offline test plánu modelů Gemini (2. 11. 2026): silné modely jen na rozhodování, swing nikdy na Flash Lite,
Scout nesmí sníst vyhrazené silné volání, 3 hlasy = 3 různé modely, spotřeba podle částí.
Spuštění:  python test_model_plan.py
"""
import os, sys, asyncio, tempfile
from pathlib import Path
os.environ.update(BINANCE_API_KEY="k", BINANCE_SECRET="s", BINANCE_FUTURES_URL="https://testnet.fake",
                  TRADING_212_API_KEY="k", TRADING_212_SECRET_KEY="s", TRADING_212_ENV="demo")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import model_manager as MM
import gemini_team
import swing_analyst as A

FAILS = []
def ok(cond, name, info=""):
    print(("OK   " if cond else "FAIL ") + name + (f"  ({info})" if info and not cond else ""))
    if not cond:
        FAILS.append(name)

qm = MM.ModelQuotaManager(Path(tempfile.mkdtemp()) / "q.json")
A.quota_manager = qm
ids = {m["id"] for m in MM.MODEL_TIERS}
ok(set(MM.STRONG_MODEL_IDS) <= ids and set(MM.LITE_MODEL_IDS) <= ids, "plán používá jen modely, které bot zná", ids)
ok(all(m["id"] in MM.STRONG_MODEL_IDS for m in qm.strong_models()), "swing: jen silné modely, nikdy Flash Lite")
ok(qm.strong_models(["gemini-3-flash-preview"])[0]["id"] == "gemini-3-flash-preview", "výběr: preferovaný 3 Flash")
ok(qm.strong_models(["gemini-3.6-flash"])[-1]["id"] == MM.STRONG_RESERVE_ID, "záloha je poslední")
for m in MM.STRONG_MODEL_IDS:
    qm.data["counts"][m] = 20
ok(qm.strong_models() == [], "silná kvóta pryč -> žádný model")
qm.data["counts"] = {}
ok(qm.get_candidate_models("routine")[0]["id"] in MM.LITE_MODEL_IDS, "rutina (dávky) jde na Flash Lite")
ok(qm.get_candidate_models("signal")[0]["id"] in MM.STRONG_MODEL_IDS, "Scout rozhodnutí: silný model, dokud má podíl")
qm.data["by_component"] = {"signal": {"gemini-3.8-flash": MM.SIGNAL_STRONG_CAP}}
ok(qm.get_candidate_models("signal")[0]["id"] in MM.LITE_MODEL_IDS, "Scout po vyčerpání podílu -> Flash Lite (swing rezerva zůstane)")
qm.record_call_success("gemini-3.6-flash", "evening")
ok(qm.usage_by_component()["evening"] == {"gemini-3.6-flash": 1} and "Spotřeba" in str(qm.get_status_embed().to_dict()),
   "spotřeba podle částí v /models")

# 3 hlasy = 3 různé modely, swing se silnou kvótou pryč -> BudgetExceeded
qm.data["by_component"], qm.data["counts"] = {}, {}
used = []
async def fake_call(prompt, label, schema=None, temperature=0.0, timeout=14.0, models=None, component="signal"):
    used.append((models[0]["id"], component))
    return '{"decision": "NONE", "counterarguments": [], "reason_cs": "", "cited_ids": []}', models[0]["id"]
gemini_team._call_models = fake_call
c = {"ticker": "X", "sector": "S", "score": 0.7, "parts": {}, "headlines": [],
     "ind": {k: 1.0 for k in ("close", "ema20", "ema50", "ema200", "rsi14", "atr14", "support", "resistance", "ret20",
                              "rel_vol", "sma50", "sma200", "atr22", "hh22")},
     "plan": {"stop": 0.9, "target": 1.2, "rr_net": 2.0}}
asyncio.run(A.vote(c, A.Budget(0, 10), None, market="EU"))
ok([u[0] for u in used] == ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3-flash-preview"] and
   all(u[1] == "swing_EU" for u in used), "3 hlasy: 3.8 / 3.6 / 3 Flash, účtováno swing_EU", used)
for m in MM.STRONG_MODEL_IDS:
    qm.data["counts"][m] = 20
try:
    asyncio.run(A.vote(c, A.Budget(0, 10), None)); ok(False, "bez silné kvóty swing nerozhoduje")
except A.BudgetExceeded:
    ok(True, "bez silné kvóty swing nerozhoduje (A vynechá, Lite se nepoužije)")

print("\nVŠECHNO OK" if not FAILS else f"\n{len(FAILS)} SELHALO: {FAILS}")
sys.exit(1 if FAILS else 0)
