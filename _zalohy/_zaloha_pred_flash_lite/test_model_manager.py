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
print("ALL OK")
