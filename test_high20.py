"""Offline test strategie HIGH20 (daily_breakout.py), žádné API.
Spuštění:  python test_high20.py
"""
import os, sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
os.environ.update(BINANCE_API_KEY="k", BINANCE_SECRET="s", BINANCE_FUTURES_URL="https://testnet.fake",
                  TRADING_212_API_KEY="k", TRADING_212_SECRET_KEY="s", TRADING_212_ENV="demo")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import daily_breakout as db
from strategy_rules import management_action
from config import HIGH20_STOP_ATR

FAILS = []
def ok(cond, name, info=""):
    print(("OK   " if cond else "FAIL ") + name + (f"  ({info})" if info and not cond else ""))
    if not cond:
        FAILS.append(name)

DAY = 24 * 3600 * 1000
T0 = 1790000000000 - 1790000000000 % DAY   # půlnoc UTC

def bars(closes, today_open=True):
    """Denní svíčky: high = close + 1, low = close − 1. Volitelně ještě rozjetá dnešní svíčka."""
    out = [[T0 + i * DAY, c, c + 1, c - 1, c, 100, T0 + (i + 1) * DAY - 1] for i, c in enumerate(closes)]
    if today_open:
        n = len(closes)
        out.append([T0 + n * DAY, 999, 999, 999, 999, 1, T0 + (n + 1) * DAY - 1])
    return out

rising = [100 + i for i in range(30)]
now_ms = T0 + 30 * DAY + 60_000          # 00:01 UTC po zavření 30. svíčky
s = db.signal(bars(rising), now_ms)
ok(s is not None and s["close"] == 129, "close na 20denním maximu -> signál", s)
ok(s is not None and abs(s["atr"] - 2.0) < 1e-9, "ATR(14) z denních svíček (range 2, mezery 1)", s)

falling_last = rising[:-1] + [120]
ok(db.signal(bars(falling_last), now_ms) is None, "close pod maximem -> bez signálu")
ok(db.signal(bars(rising[:10]), T0 + 10 * DAY + 60_000) is None, "málo svíček -> bez signálu")
ok(db.signal(bars(rising), now_ms)["close"] != 999, "rozjetá dnešní svíčka se nepočítá (žádný look-ahead)")

lv = db.levels(129.0, 2.0)
ok(abs(lv["stop_loss"] - (129 - HIGH20_STOP_ATR * 2)) < 1e-9, "Stop-Loss = entry − 1,5x ATR", lv)
ok(lv["take_profit"] > 129, "pojistný Take-Profit nad vstupem", lv)

a = db.build_analysis("SOLUSDT", 129.0, s)
ok(a["chart"]["source"] == "HIGH20" and a["chart"]["stop_loss"] < 129.0, "analýza pro open_and_record_trade", a["chart"])

entry_t = datetime(2026, 10, 28, 0, 3, tzinfo=timezone.utc)
ok(db.exit_at(entry_t) == datetime(2026, 10, 29, tzinfo=timezone.utc), "výstup v půlnoci po dni vstupu")
ok(db.in_entry_window(datetime(2026, 10, 28, 1, 59, tzinfo=timezone.utc)), "01:59 UTC -> vstupní okno")
ok(not db.in_entry_window(datetime(2026, 10, 28, 2, 0, tzinfo=timezone.utc)), "02:00 UTC -> mimo okno")

trade = {"strategy": "HIGH20", "exit_at": db.exit_at(entry_t).isoformat(), "entry": 129.0, "stop_loss": 126.0}
ok(management_action(trade, 140.0, datetime(2026, 10, 28, 23, 59, tzinfo=timezone.utc)) is None, "před koncem dne se drží (ani při zisku)")
ok(management_action(trade, 120.0, datetime(2026, 10, 29, 0, 1, tzinfo=timezone.utc)) == "TIME_STOP", "po půlnoci -> časový výstup")
ok(db.expired({**trade, "status": "OPEN"}, datetime(2026, 10, 29, 0, 1, tzinfo=timezone.utc)), "expirovaná pozice se pozná")
ok(not db.expired({"strategy": "SFP15"}, datetime(2026, 10, 29, tzinfo=timezone.utc)), "jiné strategie nejsou HIGH20")

tmp = Path(tempfile.mkdtemp()) / "state.json"
st = db.load_state(tmp)
ok(st == {} and not db.already_done(st, entry_t), "bez stavu -> den ještě nezpracován")
db.save_state(db.done_state(entry_t, {"BTCUSDT": "bez signálu"}), tmp)
ok(db.already_done(db.load_state(tmp), entry_t + timedelta(hours=1)), "stejný den se nezpracuje dvakrát")
ok(not db.already_done(db.load_state(tmp), entry_t + timedelta(days=1)), "další den znovu")

print()
print("VŠECHNO PROŠLO" if not FAILS else f"SELHALO {len(FAILS)}: {FAILS}")
sys.exit(1 if FAILS else 0)
