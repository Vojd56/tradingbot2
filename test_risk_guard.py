"""Offline test pojistky proti sérii ztrát (risk_guard.entries_blocked).
Spuštění:  python test_risk_guard.py
"""
import sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from risk_guard import entries_blocked
from config import MAX_CONSECUTIVE_LOSSES

ok = lambda c, m: print(("PASS " if c else "FAIL ") + m) or (c or sys.exit(1))
NOW = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)
NO_FILE = Path(tempfile.mkdtemp()) / "STOP"

def t(pnl, minutes_ago, **kw):
    d = {"status": "CLOSED", "ticker": "X", "pnl_usd": pnl, "closed_at": (NOW - timedelta(minutes=minutes_ago)).isoformat()}
    d.update(kw); return d

ok(entries_blocked(NOW, [], NO_FILE) is None, "bez obchodů -> vstupy povoleny")
ok(entries_blocked(NOW, [t(-120, 300), t(50, 200), t(-150, 100)], NO_FILE) is None, "dnes −220 USD -> povoleno")
r = entries_blocked(NOW, [t(-120, 300), t(-100, 200), t(20, 150), t(-110, 100)], NO_FILE)
ok(r and "Denní limit" in r, "dnes −310 USD -> blokováno denním limitem")
ok(entries_blocked(NOW, [t(-400, 60 * 16)], NO_FILE) is None, "ztráta včera -> dnes povoleno")
N = MAX_CONSECUTIVE_LOSSES
r = entries_blocked(NOW, [t(-10, 30 + 10 * i) for i in range(N)], NO_FILE)
ok(r and "za sebou" in r, f"{N} ztrát za sebou, poslední před 30 min -> pauza")
ok(entries_blocked(NOW, [t(-10, 30 + 10 * i) for i in range(N - 1)], NO_FILE) is None, f"{N - 1} ztráty za sebou -> ještě povoleno")
ok(entries_blocked(NOW, [t(-10, 130 + 10 * i) for i in range(N)], NO_FILE) is None, f"{N} ztrát, poslední před víc než 2 h -> povoleno")
ok(entries_blocked(NOW, [t(-10, 40 + 10 * i) for i in range(N - 1)] + [t(5, 35), t(-10, 30)], NO_FILE) is None, "ztráty přerušené ziskem -> povoleno")
ok(entries_blocked(NOW, [t(-10, 40 + 10 * i) for i in range(N - 1)] + [t(-200, 30, manual=True)], NO_FILE) is None, "ruční obchody se nepočítají")
ok(entries_blocked(NOW, [t(-10, 90), t(-10, 60), {"status": "OPEN", "pnl_usd": None}], NO_FILE) is None, "otevřené pozice se nepočítají")
stop = Path(tempfile.mkdtemp()) / "STOP"; stop.write_text("")
r = entries_blocked(NOW, [], stop)
ok(r and "Vypínač" in r, "soubor STOP -> blokováno")
print("ALL OK")
