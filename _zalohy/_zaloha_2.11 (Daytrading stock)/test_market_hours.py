"""Offline test obchodních hodin (časová pásma, svátky, zkrácené dny, konec seance).
Spuštění:  python test_market_hours.py
"""
import sys
from pathlib import Path
from datetime import datetime
import zoneinfo
sys.path.insert(0, str(Path(__file__).resolve().parent))
import market_hours as mh

P = zoneinfo.ZoneInfo("Europe/Prague"); NY = zoneinfo.ZoneInfo("America/New_York")
def ok(c, m):
    print(("PASS " if c else "FAIL ") + m)
    if not c: sys.exit(1)
pr = lambda *a: datetime(*a, tzinfo=P)
ny = lambda *a: datetime(*a, tzinfo=NY)

# Týden posunu času: Praha už má zimní čas (od 25.10.), New York ještě letní (do 1.11.)
ok(mh.is_market_open("US", pr(2026, 10, 26, 14, 35)), "26.10. 14:35 Praha = 09:35 NY -> NYSE otevřená")
ok(not mh.entries_allowed("US", pr(2026, 10, 26, 14, 35)), "26.10. 14:35 Praha -> ještě žádné vstupy (před 09:45 NY)")
ok(mh.entries_allowed("US", pr(2026, 10, 26, 14, 50)), "26.10. 14:50 Praha = 09:50 NY -> vstupy povoleny")
ok(not mh.is_market_open("US", pr(2026, 10, 26, 21, 30)), "26.10. 21:30 Praha = 16:30 NY -> NYSE ZAVŘENÁ (starý kód tvrdil otevřeno)")
ok(mh.should_flatten("AAPL", None, pr(2026, 10, 26, 20, 55)) == "KONEC SEANCE", "26.10. 20:55 Praha = 15:55 NY -> uzavřít pozice bota")
ok(mh.should_flatten("AAPL", None, pr(2026, 10, 26, 20, 40)) is None, "20:40 Praha = 15:40 NY -> ještě držet")
# Normální týden
ok(mh.is_market_open("US", pr(2026, 11, 2, 15, 30)) and not mh.is_market_open("US", pr(2026, 11, 2, 15, 29)), "2.11. otevření NYSE 15:30 Praha")
# Svátky a zkrácené dny
ok(not mh.is_market_open("US", ny(2026, 11, 26, 11, 0)), "Thanksgiving 26.11. -> zavřeno")
ok(not mh.is_market_open("US", ny(2026, 11, 27, 13, 30)), "27.11. zkrácený den -> po 13:00 NY zavřeno")
ok(not mh.entries_allowed("US", ny(2026, 11, 27, 12, 40)), "27.11. -> vstupní okno končí 12:30 NY")
ok(mh.should_flatten("NVDA", None, ny(2026, 11, 27, 12, 55)) == "KONEC SEANCE", "27.11. 12:55 NY -> uzavřít (flat-by 12:50)")
ok(not mh.is_market_open("EU", pr(2026, 12, 24, 10, 0)), "24.12. XETRA zavřená")
ok(not mh.is_market_open("US", pr(2026, 10, 31, 16, 0)), "sobota zavřeno")
# EU
ok(mh.is_market_open("EU", pr(2026, 10, 26, 9, 10)) and not mh.entries_allowed("EU", pr(2026, 10, 26, 9, 10)), "EU 09:10 otevřeno, vstupy až od 09:15")
ok(mh.should_flatten("SAP.DE", None, pr(2026, 10, 26, 17, 25)) == "KONEC SEANCE", "EU 17:25 -> uzavřít pozice bota")
# Pozice přes noc
ok(mh.should_flatten("TSLA", "2026-10-23T18:00:00+00:00", pr(2026, 10, 26, 15, 0)) == "POZICE PŘES NOC", "pozice z pátku -> v pondělí po otevření uzavřít")
ok(mh.should_flatten("BTCUSDT", None, pr(2026, 10, 26, 21, 0)) is None, "krypto se nesrovnává")
print("ALL OK")
