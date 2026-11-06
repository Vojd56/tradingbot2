"""Offline test kaskády Gemini (model_manager.py): den počítadel v pacifickém čase, pauza po 429. Žádné API.
Spuštění:  python test_model_manager.py
"""
import logging, sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import model_manager as mm

ok = lambda c, m: print(("PASS " if c else "FAIL ") + m) or (c or sys.exit(1))

m = mm.ModelQuotaManager(Path(tempfile.mkdtemp()) / "model_quota.json")
ok(m._get_today_str() == datetime.now(mm.PACIFIC_TZ).date().isoformat(), "počítadla se nulují o půlnoci pacifického času (reset Googlu)")

msgs = []
class H(logging.Handler):
    def emit(self, r): msgs.append(r.getMessage())
mm.logger.addHandler(H())
m.record_rate_limit("gemini-3.8-flash", 15)
until = datetime.now(timezone.utc) + timedelta(minutes=15)
ok(m.is_model_rate_limited("gemini-3.8-flash"), "po 429 je model v pauze")
ok(f"do {until.astimezone(mm.PRAGUE_TZ).strftime('%H:%M')}" in msgs[-1] and "Praha" in msgs[-1], "konec pauzy se v logu ukazuje v pražském čase")
ok(m.get_candidate_models()[-1]["id"] == "gemini-3.8-flash", "model v pauze jde na konec kaskády")
m2 = mm.ModelQuotaManager(Path(tempfile.mkdtemp()) / "model_quota.json")
ok(m2.get_candidate_models()[0]["id"] == "gemini-3.8-flash" and m2.get_candidate_models()[-1]["id"] == "gemini-3.1-flash-lite",
   "kaskáda: nejdřív Flash, Flash Lite jako záloha")
m2.data["counts"].update({"gemini-3.8-flash": 18, "gemini-3.6-flash": 18, "gemini-3.5-flash": 18, "gemini-3-flash-preview": 18})
ok(m2.get_candidate_models()[0]["id"] == "gemini-3.5-flash-lite", "signály nečerpají rezervu Flash modelů -> Flash Lite")
ok(m2.get_candidate_models("briefing")[0]["id"] == "gemini-3.8-flash", "ranní přehled dostane Flash i z rezervy")
m2.data["counts"]["gemini-3.8-flash"] = 20
ok(m2.get_candidate_models("briefing")[0]["id"] == "gemini-3.6-flash", "vyčerpaný model ani přehled nepoužije")
ok(m2.is_auth_error("401 UNAUTHENTICATED. API key not valid") and not m2.is_auth_error("429 RESOURCE_EXHAUSTED"),
   "neplatný klíč se pozná a liší se od limitu 429")
print("ALL OK")
