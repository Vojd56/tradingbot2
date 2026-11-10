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
added = ms.merge_candidates(state, cands, "t1")["added"]
ok(len(added) == ms.MAX_DYNAMIC_TICKERS and added[0]["ticker"] == "T14", "max 10, nejsilnější první")
ok(all(x["ticker"] != "NVDA" for x in state["tickers"]), "ticker z pevného watchlistu se nepřidá")
again = ms.merge_candidates(state, [{"ticker": "T14", "change_pct": 8, "rel_volume": 4, "kind": "momentum", "reason": "r", "score": 50}], "t2")["added"]
ok(again == [] and state["tickers"][0]["last_change_pct"] == 8, "zařazený ticker se jen aktualizuje")

st2 = {"tickers": []}
d2 = [{"ticker": f"D{i}", "change_pct": 9, "rel_volume": 3, "kind": "day2", "reason": "r", "score": 100 + i} for i in range(8)]
ms.merge_candidates(st2, d2 + cands[:3], "t")
ok(sum(x["kind"] == "day2" for x in st2["tickers"]) == ms.MAX_DAY2_TICKERS and len(st2["tickers"]) == ms.MAX_DAY2_TICKERS + 3,
   "včerejší skoky zaberou nejvýš 2 místa")

import trading212
trading212._instruments_loaded = False
ok(trading212.to_t212_ticker("SOFI") == "IPOE_US_EQ", "SOFI se na T212 najde pod starým tickerem IPOE")

# Rotace: vyprchaná akcie uvolní místo silnějšímu novému kandidátovi, akcie s pozicí bota zůstává
st3 = {"tickers": []}
ms.merge_candidates(st3, cands[5:], "t1")                       # T5..T14, plno
lrcx = {"ticker": "LRCX", "change_pct": 5.2, "rel_volume": 2.5, "kind": "momentum", "reason": "r", "score": 30}
res = ms.merge_candidates(st3, cands[7:] + [lrcx], "t2", protected={"T5"})   # T5, T6 vyprchaly, T5 má pozici
tick = [x["ticker"] for x in st3["tickers"]]
ok([x["ticker"] for x in res["added"]] == ["LRCX"] and [x["ticker"] for x in res["removed"]] == ["T6"],
   "plný skener vymění vyprchanou akcii za nového kandidáta (LRCX)")
ok("T5" in tick and len(tick) == ms.MAX_DYNAMIC_TICKERS, "akcie s otevřenou pozicí se nevyhazuje")
res = ms.merge_candidates(st3, cands[7:] + [lrcx], "t3", protected={"T5"})
ok(res["added"] == [] and res["removed"] == [], "bez nového kandidáta se nic nevyhazuje")
ok(ms.merge_candidates(st3, cands[5:] + [lrcx], "t4")["removed"] == [] , "když nic nevyprchalo, nové místo není")

# Evropa: pohyb v seanci, ceny v pencích, nesmyslné skoky z IOB pryč
CET = market_hours.MARKETS["EU"]["tz"]
noon = datetime(2026, 10, 28, 13, 15, tzinfo=CET)                # polovina seance
eu = dict(symbol="SMSN.IL", quoteType="EQUITY", currency="USD", regularMarketPrice=1800.0, marketCap=3e11,
          averageDailyVolume3Month=40000, regularMarketVolume=60000, regularMarketChangePercent=6.1, fullExchangeName="IOB")
c = ms.evaluate_eu_quote(eu, noon)
ok(c and c["ticker"] == "SMSN.IL" and c["rel_volume"] == 3.0, "Samsung GDR +6 % při 3x objemu se zařadí")
ok(ms.evaluate_eu_quote({**eu, "regularMarketChangePercent": 450.0}, noon) is None, "chybná kotace +450 % ne")
ok(ms.evaluate_eu_quote({**eu, "regularMarketChangePercent": 2.0}, noon) is None, "malý pohyb ne")
ok(ms.evaluate_eu_quote({**eu, "marketCap": None}, noon) is not None, "GDR bez kapitalizace projde podle obratu")
ok(ms.evaluate_eu_quote({**eu, "regularMarketVolume": 14000}, noon) is None, "pohyb bez objemu (Samsung 28. 10.: 0,7x) se nezařadí")
# Výsledky firem v Evropě: stačí +3 % jako v USA (Adidas 29. 10.: +3,7 % po výsledcích skener minul)
ads = {**eu, "symbol": "ADS.DE", "regularMarketChangePercent": 3.7, "fullExchangeName": "XETRA"}
ok(ms.evaluate_eu_quote(ads, noon) is None, "EU +3,7 % bez výsledků se nezařadí (práh 4 %)")
ts_today = int(datetime(2026, 10, 28, 7, 0, tzinfo=CET).timestamp())
c_ads = ms.evaluate_eu_quote({**ads, "earningsTimestamp": ts_today}, noon)
ok(c_ads and "výsledky dnes" in c_ads["reason"], "EU +3,7 % s výsledky dnes (datum z kotace Yahoo) se zařadí")
ts_prev = int(datetime(2026, 10, 27, 7, 0, tzinfo=CET).timestamp())
ok("výsledky včera" in (ms.evaluate_eu_quote({**ads, "earningsTimestamp": ts_prev}, noon) or {}).get("reason", ""), "výsledky včera taky")
ok(ms.evaluate_eu_quote(ads, noon, {"ADS.DE": "2026-10-28"}) is None,
   "fáze 4: dnešní datum z ranního přehledu bez času práh nesnižuje (nevíme, jestli už vyšly)")
ok(ms.evaluate_eu_quote(ads, noon, {"ADS.DE": "2026-10-27"}) is not None, "včerejší datum z ranního přehledu práh snižuje")
ts_late = int(datetime(2026, 10, 28, 17, 45, tzinfo=CET).timestamp())
ok(ms.eu_earnings_note({"symbol": "ADS.DE", "earningsTimestamp": ts_late}, noon) is None,
   "fáze 4: výsledky dnes po zavření nejsou v poledne katalyzátor")
ok(ms.eu_earnings_note({"symbol": "ADS.DE", "earningsTimestamp": ts_late}, datetime(2026, 10, 29, 10, 0, tzinfo=CET))
   == "výsledky včera", "druhý den po zavření = výsledky včera (beze změny)")
import news_briefing as _nb
_saved = (_nb.load_briefing, _nb.fetch_earnings_calendar)
try:
    _nb.load_briefing = lambda: {"raw": {"earnings": {
        "2026-10-28": [{"symbol": "AAA", "time": "před otevřením"}, {"symbol": "BBB", "time": "po zavření"},
                       {"symbol": "CCC", "time": "čas neuveden"}],
        "2026-10-27": [{"symbol": "DDD", "time": "po zavření"}, {"symbol": "EEE", "time": "před otevřením"}]}}}
    _nb.fetch_earnings_calendar = lambda d: []
    ms._earnings_cache.update(date=None, symbols={})
    es = ms.earnings_symbols(datetime(2026, 10, 28, 15, 0, tzinfo=CET))
    ok(set(es) == {"AAA", "DDD"}, f"fáze 4: US dnes jen před otevřením, včera jen po zavření ({sorted(es)})")
finally:
    _nb.load_briefing, _nb.fetch_earnings_calendar = _saved
    ms._earnings_cache.update(date=None, symbols={})
ts_old = int(datetime(2026, 10, 20, 7, 0, tzinfo=CET).timestamp())
ok(ms.evaluate_eu_quote({**ads, "earningsTimestamp": ts_old}, noon) is None, "starší výsledky práh nesnižují")
mon = datetime(2026, 10, 26, 13, 15, tzinfo=CET)
ok(ms.eu_earnings_note({"symbol": "X.DE", "earningsTimestamp": int(datetime(2026, 10, 23, 7, 0, tzinfo=CET).timestamp())}, mon) == "výsledky včera",
   "v pondělí je 'včera' pátek")
gbx = dict(symbol="BARC.L", quoteType="EQUITY", currency="GBp", regularMarketPrice=600.0, marketCap=6e10,
           averageDailyVolume3Month=4e7, regularMarketVolume=4e7, regularMarketChangePercent=5.0)
ok(ms.evaluate_eu_quote(gbx, noon) is None, "britská akcie z Londýna se nezařadí (daň SDRT 0,5 %)")
ok("LSE" not in ms.EU_EXCHANGES and "IOB" in ms.EU_EXCHANGES, "screener bez LSE, GDR (IOB) zůstávají")
ok(ms.evaluate_eu_quote({**gbx, "regularMarketPrice": 50.0}, noon) is None, "0,50 GBP je pod minimální cenou 1")
ok(ms.evaluate_eu_quote({**eu, "symbol": "NVDA"}, noon) is None, "US symbol do evropského skeneru nepatří")
st4 = {"tickers": []}
r = ms.merge_candidates(st4, [c], "t", "EU")
ok(st4["tickers_eu"][0]["ticker"] == "SMSN.IL" and st4["tickers"] == [], "evropské akcie mají vlastní místa")
ok(trading212.to_t212_ticker("SMSN.IL") == "SMSNl_EQ" and trading212.to_t212_ticker("BARC.L") == "BARCl_EQ"
   and trading212.to_t212_ticker("SAP.DE") == "SAPd_EQ", "Yahoo symboly evropských burz -> T212")

# Čas skenování: US obchodní den 07:00-16:00 NY, EU v seanci 09:00-17:30 Berlín
ok(ms.is_us_scan_time(datetime(2026, 10, 28, 7, 30, tzinfo=NY)), "premarket 07:30 NY se skenuje")
ok(not ms.is_us_scan_time(datetime(2026, 10, 28, 6, 0, tzinfo=NY)), "06:00 NY ne")
ok(ms.is_eu_scan_time(datetime(2026, 10, 28, 10, 0, tzinfo=CET)) and ms.is_scan_time(datetime(2026, 10, 28, 10, 0, tzinfo=CET)),
   "10:00 Berlín: EU seance se skenuje")
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
