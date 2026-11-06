"""Offline test ochrany pozic (nasimulovaná burza, žádné skutečné API volání).
Spuštění:  python test_position_manager.py
Nesahá na paper_trades.json – používá dočasný soubor.
"""
import os, sys, json, asyncio, tempfile
from pathlib import Path
os.environ.update(TRADING_212_API_KEY="k", TRADING_212_SECRET_KEY="s", BINANCE_API_KEY="k", BINANCE_SECRET="s",
                  BINANCE_FUTURES_URL="https://testnet.fake", TRADING_212_ENV="demo")
sys.path.insert(0, str(Path(__file__).resolve().parent))
TMP = Path(tempfile.mkdtemp()) / "paper_trades_test.json"
import requests, time
time.sleep = lambda s: None

class R:
    def __init__(s, code, data): s.status_code=code; s._d=data; s.text=json.dumps(data)
    def json(s): return s._d

STATE = {}
LOG = []
def reset(**kw):
    STATE.clear(); LOG.clear()
    STATE.update(dict(bin_amt={}, algo_ok=True, legacy_ok=True, buy_ok=True, sell_ok=True,
                      t212_qty={}, t212_pending={}, next_id=100, t212_sell_ok=True))
    STATE.update(kw)
    TMP.write_text("[]")

def nid():
    STATE["next_id"] += 1; return STATE["next_id"]

def fake_request(method, url, params=None, json=None, headers=None, timeout=None, auth=None):
    path = url.split("fake")[-1] if "fake" in url else url.split("/api/v0")[-1]
    LOG.append((method, path, dict(params or {}), json))
    p = params or {}
    if "testnet.fake" in url:
        if path == "/fapi/v2/positionRisk":
            return R(200, [{"symbol": p["symbol"], "positionAmt": str(STATE["bin_amt"].get(p["symbol"], 0))}])
        if path == "/fapi/v2/account":
            return R(200, {"positions": [{"symbol": k, "positionAmt": str(v)} for k, v in STATE["bin_amt"].items()]})
        if path == "/fapi/v1/order" and method == "POST":
            if p["type"] == "MARKET":
                ok = STATE["buy_ok"] if p["side"] == "BUY" else STATE["sell_ok"]
                if not ok: return R(400, {"code": -2019, "msg": "Margin is insufficient."})
                q = float(p["quantity"]); s = p["symbol"]
                STATE["bin_amt"][s] = STATE["bin_amt"].get(s, 0) + (q if p["side"] == "BUY" else -q)
                return R(200, {"orderId": nid(), "status": "FILLED", "executedQty": str(q), "avgPrice": "0.18234"})
            if not STATE["legacy_ok"]: return R(400, {"code": -4120, "msg": "use algo"})
            return R(200, {"orderId": nid()})
        if path == "/fapi/v1/algoOrder" and method == "POST":
            if not STATE["algo_ok"]: return R(404, {"msg": "not found"})
            return R(200, {"algoId": nid()})
        if method == "DELETE":
            return R(200, {"code": 200})
    else:
        if path == "/equity/portfolio":
            return R(200, [{"ticker": k, "quantity": v, "currentPrice": STATE.get("t212_price", {}).get(k, 0)} for k, v in STATE["t212_qty"].items()])
        if path == "/equity/orders" and method == "GET":
            return R(200, [{"id": k} for k in STATE["t212_pending"]])
        if path == "/equity/orders/market":
            q = json["quantity"]; t = json["ticker"]
            if q < 0 and not STATE["t212_sell_ok"]: return R(400, {"code": "SellingEquityNotOwned"})
            if q > 0 and STATE.get("t212_reject"): return R(200, {"id": nid(), "status": "NEW"})  # přijat, pak zamítnut
            STATE["t212_qty"][t] = STATE["t212_qty"].get(t, 0) + q
            return R(200, {"id": nid(), "status": "FILLED"})
        if path == "/equity/orders/stop":
            i = nid(); STATE["t212_pending"][i] = json; return R(200, {"id": i})
        if path.startswith("/equity/orders/") and method == "GET":
            i = int(path.split("/")[-1]); return R(200, {"status": "NEW"}) if i in STATE["t212_pending"] else R(404, {})
        if path.startswith("/equity/orders/") and method == "DELETE":
            i = int(path.split("/")[-1])
            if i in STATE["t212_pending"]: del STATE["t212_pending"][i]; return R(200, {})
            return R(404, {})
    raise AssertionError(f"unexpected {method} {url}")

requests.request = fake_request
requests.get = lambda url, **kw: (R(200, {"symbols": [{"symbol": "DOGEUSDT", "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.00001"}]}]})
                                  if "exchangeInfo" in url else fake_request("GET", url, **kw))
requests.post = lambda url, **kw: fake_request("POST", url, **kw)

import paper_trading as pt, position_manager as pm, binance_client as bc
pt.PAPER_TRADES_FILE = TMP
def rec(ticker, res, crypto):
    return pt.record_new_trade(ticker=ticker, action="AUTO_BUY", status="OPEN", entry=0.18234, stop_loss=res["stop_loss"],
        take_profit=res["take_profit"], rrr="1:2", allocated_usd=500, shares=res["fill_qty"], news_summary="", skeptik_reasons=[],
        user_name="bot", user_id=0, binance_order_id=res["order_id"] if crypto else None, t212_order_id=None if crypto else res["order_id"],
        market_type="CRYPTO" if crypto else "STOCK", extra=res["extra"])
run = asyncio.run
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m) or (c or sys.exit(1))

# 1 crypto happy
reset()
r = run(pm.open_bot_position("DOGEUSDT", True, 2700, 0.17869, 0.18965))
ok(r["success"] and r["extra"]["sl_order"]["api"] == "algo" and r["extra"]["tp_order"], "crypto open: SL+TP na burze")
sl_call = [l for l in LOG if l[1] == "/fapi/v1/algoOrder" and l[2]["type"] == "STOP_MARKET"][0][2]
ok(sl_call["reduceOnly"] == "true" and float(sl_call["quantity"]) == 2700 and sl_call["triggerPrice"] == 0.17869, f"SL reduceOnly, qty bota, tick ok {sl_call['triggerPrice']}")
# 2 manual position exists
reset(bin_amt={"DOGEUSDT": 500})
r = run(pm.open_bot_position("DOGEUSDT", True, 2700, 0.178, 0.19))
ok(not r["success"] and not any(l[1] == "/fapi/v1/order" for l in LOG), "ruční pozice na Binance: bot neobchoduje")
# 3 SL fails -> emergency close
reset(algo_ok=False, legacy_ok=False)
r = run(pm.open_bot_position("DOGEUSDT", True, 2700, 0.178, 0.19))
ok(not r["success"] and r["emergency_close"]["success"] and STATE["bin_amt"]["DOGEUSDT"] == 0, "SL nejde založit -> okamžité uzavření")
# 3b fallback legacy endpoint
reset(algo_ok=False)
r = run(pm.open_bot_position("DOGEUSDT", True, 2700, 0.178, 0.19))
ok(r["success"] and r["extra"]["sl_order"]["api"] == "order", "fallback na /fapi/v1/order")
# 4 SL >= entry corrected
reset()
r = run(pm.open_bot_position("DOGEUSDT", True, 2700, 0.18, 0.18))
ok(r["success"] and r["stop_loss"] < 0.18234 and r["take_profit"] > 0.18234 and r["warnings"], f"SL/TP opraveny {r['stop_loss']:.5f}/{r['take_profit']:.5f}")
# 5 exchange closed position (SL filled)
reset()
r = run(pm.open_bot_position("DOGEUSDT", True, 2700, 0.17869, 0.18965)); t = rec("DOGEUSDT", r, True)
STATE["bin_amt"]["DOGEUSDT"] = 0
snap = run(pm.fetch_snapshot(pt.get_open_positions()))
ev = run(pm.sync_trade(pt.get_open_positions()[0], 0.1785, snap))
ok(ev and ev["type"] == "closed" and pt.get_open_positions() == [] and sum(1 for l in LOG if l[0] == "DELETE") == 2, "SL na burze proběhl -> CLOSED + zrušení zbylých příkazů")
# 6 close failure keeps OPEN
reset()
r = run(pm.open_bot_position("DOGEUSDT", True, 2700, 0.17869, 0.18965)); t = rec("DOGEUSDT", r, True)
STATE["sell_ok"] = False
c = run(pm.close_bot_trade(pt.get_open_positions()[0], 0.19, "test"))
ok(not c["success"] and len(pt.get_open_positions()) == 1, "prodej selže -> pozice zůstává OPEN")
STATE["sell_ok"] = True
c = run(pm.close_bot_trade(pt.get_open_positions()[0], 0.19, "test"))
ok(c["success"] and not pt.get_open_positions() and STATE["bin_amt"]["DOGEUSDT"] == 0, "prodej projde -> CLOSED")
# 7 T212 manual shares protected
reset(t212_qty={"AAPL_US_EQ": 5})
r = run(pm.open_bot_position("AAPL", False, 2, 190.0, 200.0)); t = rec("AAPL", r, False)
stop = list(STATE["t212_pending"].values())[0]
ok(r["success"] and stop["quantity"] == -2 and r["extra"]["t212_qty_before"] == 5, "T212 stop jen na 2 kusy bota")
c = run(pm.close_bot_trade(pt.get_open_positions()[0], 201, "TP"))
ok(c["success"] and STATE["t212_qty"]["AAPL_US_EQ"] == 5 and not STATE["t212_pending"], "T212 TP: zrušen stop, prodány jen 2 kusy, 5 ručních zůstává")
# 8 T212 stop filled on exchange
reset(t212_qty={"AAPL_US_EQ": 5})
r = run(pm.open_bot_position("AAPL", False, 2, 190.0, 200.0)); t = rec("AAPL", r, False)
STATE["t212_pending"].clear(); STATE["t212_qty"]["AAPL_US_EQ"] = 5
snap = run(pm.fetch_snapshot(pt.get_open_positions()))
ev = run(pm.sync_trade(pt.get_open_positions()[0], 189, snap))
ok(ev and ev["type"] == "closed" and not any(l[1] == "/equity/orders/market" and l[3]["quantity"] < 0 for l in LOG), "T212 stop vyplněn -> CLOSED bez dalšího prodeje")
# 9 T212 stop cancelled manually -> re-placed
reset(t212_qty={"AAPL_US_EQ": 5})
r = run(pm.open_bot_position("AAPL", False, 2, 190.0, 200.0)); t = rec("AAPL", r, False)
STATE["t212_pending"].clear()
snap = run(pm.fetch_snapshot(pt.get_open_positions()))
ev = run(pm.sync_trade(pt.get_open_positions()[0], 195, snap))
ok(ev is None and len(STATE["t212_pending"]) == 1 and pt.get_open_positions()[0]["sl_order"], "T212 stop zmizel, kusy tu jsou -> nový stop")
# 10 legacy trade without exchange order id: no sell
reset(t212_qty={"TSLA_US_EQ": 3})
pt.record_new_trade(ticker="TSLA", action="APPROVED", status="OPEN", entry=372, stop_loss=366, take_profit=386, rrr="1:2", allocated_usd=500,
                    shares=1, news_summary="", skeptik_reasons=[], user_name="x", user_id=0)
c = run(pm.close_bot_trade(pt.get_open_positions()[0], 360, "SL"))
ok(c["success"] and c.get("paper_only") and STATE["t212_qty"]["TSLA_US_EQ"] == 3, "starý obchod bez order ID -> jen evidence, ruční TSLA nedotčeny")
# 11 T212 buy fails -> no open
reset(); 
import trading212
orig = trading212.place_market_order_sync
trading212.place_market_order_sync = lambda **k: {"success": False, "error": "HTTP 400"}
r = run(pm.open_bot_position("AAPL", False, 2, 190.0, 200.0))
ok(not r["success"], "T212 nákup selže -> success False (nic se nezapíše)")
trading212.place_market_order_sync = orig
# 12 K1: T212 nákup zamítnut, uživatel má ruční akcie -> nic se nezapíše, žádný stop na ruční kusy
reset(t212_qty={"NVDA_US_EQ": 10}, t212_reject=True)
r = run(pm.open_bot_position("NVDA", False, 3, 176.0, 190.0))
ok(not r["success"] and not STATE["t212_pending"], "K1: zamítnutý nákup -> success False, žádný stop na ruční NVDA")
# 13 K1: evidence říká OPEN, ale kusy bota v portfoliu nejsou -> nic neprodat (ruční zůstanou)
reset(t212_qty={"NVDA_US_EQ": 10})
pt.record_new_trade(ticker="NVDA", action="AUTO_BUY", status="OPEN", entry=180, stop_loss=176, take_profit=190, rrr="1:2", allocated_usd=500,
                    shares=3, news_summary="", skeptik_reasons=[], user_name="bot", user_id=0, t212_order_id="999", market_type="STOCK",
                    extra={"opened_by_bot": True, "bot_qty": 3, "t212_qty_before": 10, "sl_order": None})
c = run(pm.close_bot_trade(pt.get_open_positions()[0], 181, "flat-by"))
sells = [l for l in LOG if l[1] == "/equity/orders/market" and l[3]["quantity"] < 0]
ok(c["success"] and not sells and STATE["t212_qty"]["NVDA_US_EQ"] == 10, "K1: zavření neprodá ruční akcie (prodej omezen na held - qty_before)")
# 14 K1: nákup čekal, pak zmizel bez vyplnění -> evidence uzavřena, nic se neprodá
reset(t212_qty={"NVDA_US_EQ": 10})
pt.record_new_trade(ticker="NVDA", action="AUTO_BUY", status="OPEN", entry=180, stop_loss=176, take_profit=190, rrr="1:2", allocated_usd=500,
                    shares=3, news_summary="", skeptik_reasons=[], user_name="bot", user_id=0, t212_order_id="999", market_type="STOCK",
                    extra={"opened_by_bot": True, "bot_qty": 3, "t212_qty_before": 10, "t212_buy_pending": True, "sl_order": None})
snap = run(pm.fetch_snapshot(pt.get_open_positions()))
ev = run(pm.sync_trade(pt.get_open_positions()[0], 181, snap))
ok(ev and ev["type"] == "closed" and not pt.get_open_positions() and not STATE["t212_pending"] and STATE["t212_qty"]["NVDA_US_EQ"] == 10,
   "K1: nevyplněný čekající nákup -> CLOSED jen v evidenci, žádný stop ani prodej")
# 15 K2: SL nad skutečnou cenou po vyplnění -> přepočet pod cenu
reset(t212_qty={"NVDA_US_EQ": 0}, t212_price={"NVDA_US_EQ": 180.0})
r = run(pm.open_bot_position("NVDA", False, 2, 186.2, 199.5))
stop = list(STATE["t212_pending"].values())[0]
ok(r["success"] and r["stop_loss"] < 180 and stop["stopPrice"] < 180 and r["take_profit"] > 180 and r["fill_price"] == 180.0,
   f"K2: SL nad trhem opraven na {r['stop_loss']} (cena 180)")
print("ALL OK")
