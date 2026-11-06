"""Offline test momentum skeneru (dynamický výběr akcií). Nic nestahuje.
Spuštění:  python test_momentum_scanner.py
"""
import sys
import tempfile
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import market_hours
import momentum_scanner as ms

ok = lambda c, m: print(("PASS " if c else "FAIL ") + m) or (c or sys.exit(1))

NY = market_hours.NY_TZ
pre = datetime(2026, 10, 28, 8, 0, tzinfo=NY)
mid = datetime(2026, 10, 28, 12, 45, tzinfo=NY)  # polovina seance


def q(**kw):
    base = {"symbol": "SOFI", "quoteType": "EQUITY", "marketState": "REGULAR", "regularMarketPrice": 16.5,
            "marketCap": 18e9, "averageDailyVolume3Month": 50e6, "regularMarketChangePercent": 0.0,
            "regularMarketVolume": 0, "preMarketChangePercent": 0.0, "preMarketPrice": 0}
    base.update(kw)
    return base


ok(abs(ms.session_fraction(mid) - 0.5) < 1e-9 and ms.session_fraction(pre) == 0.0, "podíl uplynulé seance")

# V seanci: +13 % při 3× objemu (přepočteno na půl dne) -> zařadit
c = ms.evaluate_quote(q(regularMarketChangePercent=13.4, regularMarketVolume=75e6), None, mid)
ok(c and c["kind"] == "momentum" and c["rel_volume"] == 3.0, "pohyb +13 % s 3× objemem se zařadí")
ok(ms.evaluate_quote(q(regularMarketChangePercent=13.4, regularMarketVolume=20e6), None, mid) is None,
   "velký pohyb bez objemu se nezařadí")
ok(ms.evaluate_quote(q(regularMarketChangePercent=-9, regularMarketVolume=90e6), None, mid) is None,
   "propad se nezařadí (bot kupuje jen long)")

# Filtry kvality
ok(ms.evaluate_quote(q(regularMarketPrice=3.2, regularMarketChangePercent=20, regularMarketVolume=90e6), None, mid) is None, "penny akcie pod 5 USD ne")
ok(ms.evaluate_quote(q(marketCap=4e8, regularMarketChangePercent=20, regularMarketVolume=90e6), None, mid) is None, "mikrofirma pod 1 mld ne")
ok(ms.evaluate_quote(q(averageDailyVolume3Month=5e5, regularMarketChangePercent=20, regularMarketVolume=9e6), None, mid) is None, "malý obrat ne")
ok(ms.evaluate_quote(q(symbol="SAP.DE", regularMarketChangePercent=20, regularMarketVolume=90e6), None, mid) is None, "jen US symboly")

# Výsledky: stačí menší pohyb a důvod se uvede
c = ms.evaluate_quote(q(regularMarketChangePercent=3.5, regularMarketVolume=50e6), "výsledky dnes (před otevřením)", mid)
ok(c and "výsledky dnes" in c["reason"], "u výsledků stačí +3,5 % a důvod je v textu")

# Premarket: gap nahoru, a včerejší skok, který se drží (případ SOFI 28.10.)
c = ms.evaluate_quote(q(marketState="PRE", preMarketChangePercent=6.2, preMarketPrice=17.5), None, pre)
ok(c and c["kind"] == "gap", "gap v premarketu +6 % se zařadí")
c = ms.evaluate_quote(q(marketState="PRE", regularMarketChangePercent=13.4, regularMarketVolume=160e6,
                        preMarketChangePercent=0.8, preMarketPrice=16.6), None, pre)
ok(c and c["kind"] == "day2" and "včera +13.4 %" in c["reason"], "včerejší skok s objemem, který se drží, se zařadí")
ok(ms.evaluate_quote(q(marketState="PRE", regularMarketChangePercent=13.4, regularMarketVolume=160e6,
                       preMarketChangePercent=-3, preMarketPrice=16.0), None, pre) is None, "včerejší skok, který se v premarketu sype, ne")

# Sloučení: limit, pevný watchlist se nepřidává, zařazené zůstávají a jen se aktualizují
state = {"date": "x", "tickers": []}
cands = [{"ticker": f"T{i}", "change_pct": 5, "rel_volume": 2, "kind": "momentum", "reason": "r", "score": i} for i in range(15)]
cands.append({"ticker": "NVDA", "change_pct": 9, "rel_volume": 3, "kind": "momentum", "reason": "r", "score": 999})
added = ms.merge_candidates(state, cands, "t1")
ok(len(added) == ms.MAX_DYNAMIC_TICKERS and added[0]["ticker"] == "T14", "max 10, nejsilnější první")
ok(all(x["ticker"] != "NVDA" for x in state["tickers"]), "ticker z pevného watchlistu se nepřidá")
again = ms.merge_candidates(state, [{"ticker": "T14", "change_pct": 8, "rel_volume": 4, "kind": "momentum", "reason": "r", "score": 50}], "t2")
ok(again == [] and state["tickers"][0]["last_change_pct"] == 8, "zařazený ticker se jen aktualizuje")

st2 = {"tickers": []}
d2 = [{"ticker": f"D{i}", "change_pct": 9, "rel_volume": 3, "kind": "day2", "reason": "r", "score": 100 + i} for i in range(8)]
ms.merge_candidates(st2, d2 + cands[:3], "t")
ok(sum(x["kind"] == "day2" for x in st2["tickers"]) == ms.MAX_DAY2_TICKERS and len(st2["tickers"]) == ms.MAX_DAY2_TICKERS + 3,
   "včerejší skoky zaberou nejvýš 4 místa")

import trading212
trading212._instruments_loaded = False
ok(trading212.to_t212_ticker("SOFI") == "IPOE_US_EQ", "SOFI se na T212 najde pod starým tickerem IPOE")

# Čas skenování: obchodní den 07:00-16:00 NY
ok(ms.is_scan_time(datetime(2026, 10, 28, 7, 30, tzinfo=NY)), "premarket 07:30 NY se skenuje")
ok(not ms.is_scan_time(datetime(2026, 10, 28, 6, 0, tzinfo=NY)), "06:00 NY ne")
ok(not ms.is_scan_time(datetime(2026, 10, 31, 10, 0, tzinfo=NY)), "sobota ne")

# Uložení a kontext pro Gemini
with tempfile.TemporaryDirectory() as d:
    ms.DYNAMIC_FILE = Path(d) / "dyn.json"
    st = ms.load_state()
    st["tickers"] = [{"ticker": "SOFI", "reason": "včera +13.4 % při 3.1× objemu", "headlines": ["SoFi beats estimates"]}]
    ms._save(st)
    ok(ms.get_dynamic_tickers() == ["SOFI"], "uložený výběr se načte")
    ok("SoFi beats" in ms.get_context("SOFI") and ms.get_context("AAPL") == "", "kontext pro Gemini")
    ok("SOFI" in ms.format_added_message(st["tickers"]) and ms.format_today_section(), "zpráva do Discordu")

print("Všechny testy momentum skeneru prošly.")
