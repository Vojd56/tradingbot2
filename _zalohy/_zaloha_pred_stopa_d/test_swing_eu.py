"""Offline test evropské části swingu: vesmír z T212, filtr obratu v EUR, náklady podle měny, plán EU dne,
společné limity US + EU, živá stopa A v hodinách EU. Bez sítě a bez Gemini.
Spuštění:  python test_swing_eu.py
"""
import os, sys, asyncio, json, tempfile
from datetime import date, datetime
from pathlib import Path
os.environ.update(BINANCE_API_KEY="k", BINANCE_SECRET="s", BINANCE_FUTURES_URL="https://testnet.fake",
                  TRADING_212_API_KEY="k", TRADING_212_SECRET_KEY="s", TRADING_212_ENV="demo")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd
import market_hours
import momentum_scanner
import news_briefing
import swing_analyst
import swing_live as L
import swing_scanner as S
import swing_shadow as SH

FAILS = []
def ok(cond, name, info=""):
    print(("OK   " if cond else "FAIL ") + name + (f"  ({info})" if info and not cond else ""))
    if not cond:
        FAILS.append(name)

tmp = Path(tempfile.mkdtemp())
SH.LOG_FILE, SH.STATE_FILE, L.STATE_FILE = tmp / "log.jsonl", tmp / "state.json", tmp / "live.json"

# --- vesmír z T212
uni = S.load_universe_eu()
ok(len(uni) > 500 and uni.get("SAP.DE", {}).get("ccy") == "EUR", "vesmír EU z t212_instruments.json (SAP.DE v EUR)", len(uni))
ok(uni.get("NESN.SW", {}).get("ccy") == "CHF", "Curych v CHF")
ok(not any(k.endswith(".L") for k in uni), "Londýn vynechán")
ok(set(k.rsplit(".", 1)[1] for k in uni) <= {"DE", "PA", "AS", "MC", "SW"}, "jen Xetra, Paříž, Amsterdam, Madrid, Curych")

# --- sken EU nad syntetikou
def bars(n=260, start=50.0, drift=0.002, vol=1_000_000, end="2026-10-30", wave=0.05):
    idx = pd.bdate_range(end=end, periods=n)
    c = start * np.cumprod(np.full(n, 1 + drift)) * (1 + wave * np.sin(np.arange(n) / 6.0))
    return pd.DataFrame({"Open": c * 0.998, "High": c * 1.01, "Low": c * 0.99, "Close": c, "Volume": float(vol)}, index=idx)

u = {"AAA.DE": {"ccy": "EUR", "sector": "Industrials"}, "BBB.SW": {"ccy": "CHF", "sector": "Health Care"},
     "THIN.PA": {"ccy": "EUR", "sector": "Utilities"}, "LOW.DE": {"ccy": "EUR", "sector": "Energy"}}
data = {"AAA.DE": bars(vol=400_000), "BBB.SW": bars(vol=300_000), "THIN.PA": bars(vol=20_000), "LOW.DE": bars(start=2.0, vol=9e6),
        "^STOXX": bars(start=500, wave=0.0)}
res = S.scan(date(2026, 11, 2), date(2026, 11, 9), [], [], "EUR", news_fn=lambda t: [], earnings_fn=lambda t: None,
             data=data, universe=u, market="EU", fx_eur={"CHF": 1.06})
ok(res["ok"] and res["regime_sym"] == "^STOXX" and res["regime_ok"], "EU režim přes STOXX 600 nad SMA200", res.get("reason"))
ok(res["stats"]["min_adv_eur"] == 5e6, "málo akcií nad 10 mil. EUR -> limit 5 mil. EUR", res["stats"])
ok(res["stats"]["rejects"].get("obrat < 5 mil. EUR") == 1 and res["stats"]["rejects"].get("cena < 5 EUR") == 1,
   "obrat v EUR a cena v místní měně", res["stats"]["rejects"])
c_by = {c["ticker"]: c for c in res["candidates"]}
e_aaa = S.evaluate("AAA.DE", S.closed_before(data["AAA.DE"], date(2026, 11, 2)), date(2026, 11, 2),
                   S._side_for("AAA.DE", "EUR", "EUR"), 5e6, 1.0, "EUR", "EUR")
ok(abs(S._side_for("AAA.DE", "EUR", "EUR") - 0.0005) < 1e-9 and abs(S._side_for("BBB.SW", "CHF", "EUR") - 0.002) < 1e-9,
   "náklady: EUR akcie jen skluz 0,05 %, CHF + převod 0,15 %")
spy_dn = dict(data, **{"^STOXX": bars(start=500, drift=-0.002, wave=0.0)})
ok(S.scan(date(2026, 11, 2), date(2026, 11, 9), [], [], "EUR", data=spy_dn, universe=u, market="EU",
          fx_eur={"CHF": 1.06}, earnings_fn=lambda t: None)["regime_ok"] is False, "STOXX pod SMA200 -> bez EU vstupů")

# --- stín: EU ráno 08:30, pozdní sken, společné limity s US
PR = market_hours.PRAGUE_TZ
D = date(2026, 11, 3)
def cand(t, sector, ccy="EUR"):
    ind = {"close": 100.0, "atr14": 2.0, "ema20": 99, "ema50": 95, "ema200": 90, "rsi14": 60, "support": 97, "resistance": None,
           "ret20": 0.05, "rel_vol": 1.5, "sma50": 95, "sma200": 90, "atr22": 2.0, "hh22": 101}
    return {"ticker": t, "sector": sector, "ccy": ccy, "market": "EU", "score": 0.8, "parts": {}, "headlines": [], "ind": ind,
            "plan": {"ok": True, "stop": 96.0, "target": 108.9, "risk_ps": 4.4, "rr_net": 2.0, "entry_ref": 100.0}}
EU_CANDS = [cand("SAP.DE", "Information Technology"), cand("NESN.SW", "Consumer Staples", "CHF")]
def fake_scan(day, last_bo, dyn, brief, ccy, news_fn=None, earn=None, data=None, universe=None, market="US", fx=None):
    return {"ok": True, "market": market, "regime_sym": "^STOXX", "spy_close": 600.0, "spy_sma200": 550.0, "regime_ok": True,
            "stats": {"tradable": 2, "passed_universe": 2, "score_ok": 2, "candidates": 2}, "candidates": list(EU_CANDS), "rejected": []}
S.scan = fake_scan
SH.SWING_MARKETS["EU"]["morning"] = "08:30"   # test počítá s EU skenem 08:30 Praha
SH.markets = lambda: ["EU"]
SH._account_info = lambda: {"real_ccy": "EUR", "start_eur": 1000.0, "source": "test"}
SH.fx_map = lambda st: {"EUR": 1.0, "USD": 0.9, "CHF": 1.06}
SH.fetch_news = lambda t, name=None: ["fresh headline"]
news_briefing.get_extra_tickers = lambda: []
momentum_scanner.get_dynamic_tickers = lambda region="US": []
calls = {"vote_heads": []}
async def fake_pick(cands, budget, **kw):
    budget.take()
    return {"picks": [{"id": f"C{i+1}", "ticker": c["ticker"], "reason_cs": "r", "catalyst": "k", "invalidation": "i",
                       "cited_ids": [f"C{i+1}"]} for i, c in enumerate(cands)], "rejected": [], "model": "fake", "prompt_hash": "h"}
async def fake_vote(c, budget, info=None, **kw):
    for _ in range(3):
        budget.take()
    calls["vote_heads"].append(c.get("headlines"))
    return {"ticker": c["ticker"], "buy": True, "buy_votes": 3, "votes": [{"model": "fake"}], "prompt_hash": "h"}
swing_analyst.pick, swing_analyst.vote = fake_pick, fake_vote
L.step = lambda now=None, broker=None, path=None: []

# US pozice v IT už existuje -> EU IT (SAP) se nesmí přidat (1 na sektor dohromady)
SH.save_state({"tracks": {k: dict(SH._new_track(1000.0), positions=[
    {"id": f"{k}-x-MSFT", "ticker": "MSFT", "market": "US", "ccy": "USD", "sector": "Information Technology", "status": "OPEN",
     "entry": 100.0, "stop": 96.0, "qty_open": 1.0, "cost_side": 0.002, "fx_entry": 0.9, "entry_day": "2026-11-02"}])
    for k in "ABC"}, "real_ccy": "EUR", "first_day": "2026-11-02", "markets": {}})
ok(asyncio.run(SH.tick(datetime(2026, 11, 3, 8, 0, tzinfo=PR))) == [], "před 08:30 Praha EU sken neběží")
msgs = asyncio.run(SH.tick(datetime(2026, 11, 3, 8, 35, tzinfo=PR)))
st = SH.load_state()
eu = {k: [p["ticker"] for p in st["tracks"][k]["positions"] if p.get("market") == "EU"] for k in "ABC"}
ok(msgs and "🇪🇺 EU" in msgs[0], "EU ranní zpráva", msgs)
ok(eu["B"] == ["NESN.SW"], "SAP vyřazen: sektor IT už drží US pozice (limity společné)", eu)
pn = next(p for p in st["tracks"]["B"]["positions"] if p["ticker"] == "NESN.SW")
ok(pn["ccy"] == "CHF" and abs(pn["cost_side"] - 0.002) < 1e-9 and pn["fill_after"].startswith("2026-11-03T09:15"),
   "EU pozice: měna CHF, náklady s převodem, vstup 09:15", pn)
ok(calls["vote_heads"] and calls["vote_heads"][0] == ["fresh headline"], "čerstvé zprávy těsně před hlasy analytika")
lv = L.load_state()
ok([(p["ticker"], p["market"]) for p in lv["positions"]] == [("SAP.DE", "EU"), ("NESN.SW", "EU")], "A naživo: EU tickery naplánované", lv)
ok(st["markets"]["EU"]["last_morning"] == "2026-11-03" and any(e.get("market") == "EU" for e in
   [json.loads(x) for x in SH.LOG_FILE.read_text(encoding="utf-8").splitlines()]), "záznamy s market = EU")

# --- pozdní sken: vstup až po skenu
SH.save_state({"tracks": {k: SH._new_track(1000.0) for k in "ABC"}, "real_ccy": "EUR", "first_day": "2026-11-02", "markets": {}})
asyncio.run(SH.tick(datetime(2026, 11, 3, 14, 30, tzinfo=PR)))
pl = SH.load_state()["tracks"]["B"]["positions"][0]
ok(pl["fill_after"].startswith("2026-11-03T14:35"), "pozdní sken (14:30): papírový vstup až od 14:35", pl["fill_after"])
ok(asyncio.run(SH.tick(datetime(2026, 11, 3, 17, 10, tzinfo=PR))) == [], "po 17:00 se EU sken už nespouští")

# --- vyčerpaný rozpočet Gemini: A vynechána jen pro EU
async def broke(cands, budget, **kw):
    raise swing_analyst.BudgetExceeded("limit")
swing_analyst.pick = broke
SH.save_state({"tracks": {k: SH._new_track(1000.0) for k in "ABC"}, "real_ccy": "EUR", "first_day": "2026-11-02", "markets": {}})
msgs = asyncio.run(SH.tick(datetime(2026, 11, 3, 8, 40, tzinfo=PR)))
ok("vynechána (EU)" in msgs[0] and SH.load_state()["tracks"]["B"]["positions"], "limit Gemini: A vynechána jen pro EU, B pokračuje", msgs)

# --- živá stopa A: EU nákup jen v hodinách EU, kurz CHF
import importlib
L2 = importlib.reload(L)
L2.STATE_FILE = tmp / "live_eu.json"
L2.global_block = lambda t: None
L2.intraday_qty = lambda t: 0.0
class B(L2.Broker):
    def __init__(self): self.calls, self.held, self.orders = [], {}, []
    def summary(self): return {"currency": "EUR", "totalValue": 2000.0, "cash": {"availableToTrade": 2000.0}}
    def portfolio(self): return []
    def pending(self): return []
    def qty(self, pf, s): return 0.0
    def t212_ticker(self, s): return s
    def fx(self, a, b): self.calls.append(("fx", a, b)); return 1.06 if a == "CHF" else 1.0
    def last_price(self, s): return 100.0
    def limit_buy(self, s, q, px): self.calls.append(("buy", s, q, px)); return {"success": True, "order_id": 7}
b = B()
L2.plan_entries([cand("NESN.SW", "Consumer Staples", "CHF")], D, None, "EU")
L2.step(datetime(2026, 11, 3, 9, 10, tzinfo=PR), b)
ok(not any(c[0] == "buy" for c in b.calls), "EU před 09:15 Praha nic nenakupuje")
L2.step(datetime(2026, 11, 3, 9, 20, tzinfo=PR), b)
buy = [c for c in b.calls if c[0] == "buy"]
ok(buy and buy[0][1] == "NESN.SW" and ("fx", "CHF", "EUR") in b.calls, "EU nákup v 09:20 s kurzem CHF -> EUR", b.calls)
risk = buy[0][2] * ((100.3 - 96) + 100.3 * 0.002 + 96 * 0.002) * 1.06
ok(abs(risk - 10.0) < 0.15, "velikost EU: 0,5 % z 2000 EUR vč. převodu měny", risk)

print("\nVŠECHNO OK" if not FAILS else f"\n{len(FAILS)} SELHALO: {FAILS}")
sys.exit(1 if FAILS else 0)
