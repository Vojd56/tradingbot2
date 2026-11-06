"""Offline test limitů: pauza tickeru po analýze a kaskáda modelů Gemini při 429 (žádné API).
Spuštění:  python test_limits.py
"""
import os, sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
os.environ.update(BINANCE_API_KEY="k", BINANCE_SECRET="s", BINANCE_FUTURES_URL="https://testnet.fake",
                  TRADING_212_API_KEY="k", TRADING_212_SECRET_KEY="s", TRADING_212_ENV="demo")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scout import AlertTracker
from model_manager import ModelQuotaManager, MODEL_TIERS
from config import ALERT_TICKER_COOLDOWN_MIN

FAILS = []
def ok(cond, name):
    print(("OK   " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)

tmp = Path(tempfile.mkdtemp())

at = AlertTracker(tmp / "alerts.json")
ok(at.can_alert("BA"), "nový ticker smí dostat analýzu")
at.register_alert("BA")
ok(not at.can_alert("BA"), "hned po analýze ticker čeká")
ok(at.can_alert("HOOD"), "jiný ticker pauzou neblokuje")
at.data["last_alert_at"]["BA"] = (datetime.now(timezone.utc) - timedelta(minutes=ALERT_TICKER_COOLDOWN_MIN + 1)).isoformat()
ok(at.can_alert("BA"), f"po {ALERT_TICKER_COOLDOWN_MIN} min znovu smí")

qm = ModelQuotaManager(tmp / "quota.json")
first = MODEL_TIERS[0]["id"]
ok(qm.get_candidate_models()[0]["id"] == first, "bez chyb začíná nejlepší model")
qm.record_quota_error(first, "429 RESOURCE_EXHAUSTED quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier")
until = datetime.fromisoformat(qm.data["rate_limited_until"][first])
ok(until - datetime.now(timezone.utc) > timedelta(minutes=30), "denní kvóta -> pauza až do resetu, ne 15 min")
ok(qm.get_candidate_models()[0]["id"] != first, "vyčerpaný model jde na konec kaskády")
second = MODEL_TIERS[1]["id"]
qm.record_quota_error(second, "429 RESOURCE_EXHAUSTED GenerateRequestsPerMinutePerProjectPerModel")
until2 = datetime.fromisoformat(qm.data["rate_limited_until"][second])
ok(timedelta(minutes=14) < until2 - datetime.now(timezone.utc) <= timedelta(minutes=15), "minutový limit -> 15 min")
qm.data["date"] = "2000-01-01"
qm._check_and_reset_if_new_day()
ok(first in qm.data["rate_limited_until"], "pauza po denní kvótě přežije půlnoc v Praze")
ok(sum(m["daily_limit"] for m in MODEL_TIERS) >= 1000, "kaskáda má kapacitu přes 1000 volání denně")
ok(all(m["id"] != "gemini-2.5-flash" for m in MODEL_TIERS), "model, který API nevydá (404), v kaskádě není")

print()
print("VŠECHNO PROŠLO" if not FAILS else f"SELHALO {len(FAILS)}: {FAILS}")
sys.exit(1 if FAILS else 0)
