"""Offline test převodu symbolů na tickery Trading 212 (trading212.to_t212_ticker).
Spuštění:  python test_t212_ticker.py
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import requests
import config
import trading212
from trading212 import to_t212_ticker

ok = lambda c, m: print(("PASS " if c else "FAIL ") + m) or (c or sys.exit(1))

ok(to_t212_ticker("META") == "FB_US_EQ", "META -> FB_US_EQ")
ok(to_t212_ticker("NVDA") == "NVDA_US_EQ", "NVDA -> NVDA_US_EQ")
ok(to_t212_ticker("TTE.PA") == "FPp_EQ", "TTE.PA -> FPp_EQ")
ok(to_t212_ticker("SAP.DE") == "SAPd_EQ", "SAP.DE -> SAPd_EQ")
ok(to_t212_ticker("SAPd_EQ") == "SAPd_EQ", "ticker T212 zůstane beze změny")
ok(to_t212_ticker("XYZ.DE") is None, "neznámý evropský symbol XYZ.DE -> None")

# Nákup nenamapovaného symbolu nesmí odejít na API
calls = []
def no_http(*a, **kw):
    calls.append((a, kw)); raise AssertionError("HTTP volání nemělo proběhnout")
orig = (requests.post, requests.request, trading212.ENABLE_TRADING_212)
requests.post = requests.request = no_http
trading212.ENABLE_TRADING_212 = True
try:
    r = trading212.place_market_order_sync("XYZ.DE", 1.0)
    ok(r.get("success") is False and "není odpovídající instrument" in r.get("error", ""), "nákup XYZ.DE -> success False s chybou")
    r = trading212.place_stop_order_sync("XYZ.DE", 1.0, 10.0)
    ok(r.get("success") is False, "stop XYZ.DE -> success False")
    r = trading212.close_bot_position_sync("XYZ.DE", 1.0)
    ok(r.get("success") is False and not r.get("already_flat"), "prodej XYZ.DE -> success False, ne already_flat")
    ok(not calls, "žádné HTTP volání")
finally:
    requests.post, requests.request, trading212.ENABLE_TRADING_212 = orig

ok(trading212.find_portfolio_qty([{"ticker": "FB_US_EQ", "quantity": 2}], "META") == 2.0, "portfolio: META najde FB_US_EQ")
ok(trading212.find_portfolio_qty([{"ticker": "X_US_EQ", "quantity": 2}], "XYZ.DE") == 0.0, "portfolio: nenamapovaný -> 0.0")
ok(trading212.find_portfolio_price([{"ticker": "X_US_EQ", "currentPrice": 5}], "XYZ.DE") is None, "portfolio: cena nenamapovaného -> None")

known = {i["ticker"] for i in json.load(open(config.T212_INSTRUMENTS_FILE, encoding="utf-8"))}
for sym in config.US_STOCKS_WATCHLIST + config.EU_STOCKS_WATCHLIST:
    t = to_t212_ticker(sym)
    ok(t is not None and t in known, f"watchlist {sym} -> {t} existuje na T212")
print("ALL OK")
