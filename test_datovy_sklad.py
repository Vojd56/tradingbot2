"""Testy fáze 3: swing skener čte denní svíčky z datového skladu (bez sítě)."""

import sys
import tempfile
import types
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

import datovy_sklad
import swing_scanner

datovy_sklad._mods()      # data\_skripty na sys.path
import sklad              # noqa: E402
import update_all         # noqa: E402


def bars(n=300):
    idx = pd.bdate_range(end="2026-11-06", periods=n)
    c = [100.0 + i * 0.1 for i in range(n)]
    return pd.DataFrame({"open": c, "high": [x + 1 for x in c], "low": [x - 1 for x in c], "close": c,
                         "volume": [1e6] * n}, index=idx)


class DatovySkladTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.saved = (sklad.DATA_DIR, sklad.STATE_FILE, sklad.BACKUP_DIR)
        sklad.DATA_DIR, sklad.STATE_FILE, sklad.BACKUP_DIR = d, d / "_stav.json", d / "_zalohy"
        self.today = date(2026, 11, 9)
        self.runs = []
        self.saved_run = update_all.run
        update_all.run = lambda sources, vault=True, today=None, force=False: self.runs.append(list(sources))

    def tearDown(self):
        sklad.DATA_DIR, sklad.STATE_FILE, sklad.BACKUP_DIR = self.saved
        update_all.run = self.saved_run
        self.tmp.cleanup()

    def test_load_and_skips(self):
        for t in ("AAPL", "SPY"):
            sklad.write_full("yahoo_us", t, bars(), self.today)
        sklad.write_full("yahoo_eu", "BAD.MC", bars(), self.today)
        state = {"yahoo_us/AAPL": {"checked": "2026-11-09", "ok": True},
                 "yahoo_us/GONE": {"checked": "2026-11-09", "ok": False, "retry_after": "2026-11-16"},
                 "yahoo_eu/BAD.MC": {"checked": "2026-11-08", "ok": True},
                 "_kontrola": {"yahoo_eu/BAD.MC.csv": {"mtime": 0, "issues": ["high < max(open, close): 3×"]},
                               "yahoo_us/SPY.csv": {"mtime": 0, "issues": ["upozornění: skok close > 50 %: 1×"]}}}
        sklad.save_state(state)
        data, fallback, skipped = datovy_sklad.load(["AAPL", "SPY", "GONE", "BAD.MC", "NEW"], self.today)
        self.assertEqual(sorted(data), ["AAPL", "SPY"])            # upozornění nevadí
        self.assertEqual(fallback, ["NEW"])
        self.assertEqual(skipped, {"zdroj ticker nemá": 1, "chyba v kontrole dat": 1})
        self.assertEqual(list(data["AAPL"].columns), ["Open", "High", "Low", "Close", "Volume"])
        self.assertEqual(self.runs, [["yahoo_eu"]])                 # US už dnes obnovený, EU ne
        # sklad drží jen dny do včerejška -> closed_before(den skenu) nic neubere
        self.assertEqual(len(swing_scanner.closed_before(data["AAPL"], self.today)), 300)

    def test_scanner_falls_back_to_yahoo(self):
        calls = []
        orig = swing_scanner.download_daily
        swing_scanner.download_daily = lambda ts, period="2y", **k: calls.append(list(ts)) or {t: bars() for t in ts}
        try:
            fake = types.SimpleNamespace(load=lambda ts: ({"AAPL": bars()}, ["NEW"], {}))
            sys.modules["datovy_sklad"] = fake
            d = swing_scanner.load_daily(["AAPL", "NEW"])
            self.assertEqual(sorted(d), ["AAPL", "NEW"])
            self.assertEqual(calls, [["NEW"]])
            # sklad spadne -> všechno postaru z Yahoo
            fake.load = lambda ts: (_ for _ in ()).throw(RuntimeError("x"))
            d = swing_scanner.load_daily(["AAPL", "NEW"])
            self.assertEqual(calls[-1], ["AAPL", "NEW"])
        finally:
            sys.modules["datovy_sklad"] = datovy_sklad
            swing_scanner.download_daily = orig


if __name__ == "__main__":
    unittest.main()
