"""Offline test K2: úrovně Chartu vynucuje kód, ne model (gemini_team.enforce_chart_levels).
Spuštění:  python test_chart_levels.py
"""
import os, sys
from pathlib import Path
os.environ.setdefault("GEMINI_API_KEY", "")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from gemini_team import enforce_chart_levels

ok = lambda c, m: print(("PASS " if c else "FAIL ") + m) or (c or sys.exit(1))

c = enforce_chart_levels(180.0, {"entry": 190.0, "stop_loss": 186.2, "take_profit": 199.0})
ok(c["entry"] == 180.0 and c["stop_loss"] < 180.0, f"entry od modelu nad trhem ignorováno, SL {c['stop_loss']} pod cenou")
c = enforce_chart_levels(180.0, {"entry": 90.0, "stop_loss": 88.0, "take_profit": 95.0})
ok(c["entry"] == 180.0 and int(500 / c["entry"]) == 2, "entry pod trhem ignorováno, počet kusů podle tržní ceny")
c = enforce_chart_levels(100.0, {"stop_loss": 99.0, "take_profit": 500.0})
ok(c["take_profit"] == 103.0, f"TP omezen na 3R ({c['take_profit']})")
c = enforce_chart_levels(100.0, {"stop_loss": 99.0, "take_profit": 100.5})
ok(c["take_profit"] == 102.0, f"TP zvýšen na 2R ({c['take_profit']})")
c = enforce_chart_levels(100.0, {"entry": "nesmysl", "stop_loss": None, "take_profit": "x"})
ok(c["entry"] == 100.0 and c["stop_loss"] == 98.0 and c["take_profit"] == 104.0, "nečíselná odpověď modelu -> výchozí 2 % / 2R")
print("ALL OK")
