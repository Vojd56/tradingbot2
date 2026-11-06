"""Offline test swing F0 stínu (swing_shadow.py): ráno -> večer -> další dny se syntetickými daty, bez sítě a bez Gemini.
Spuštění:  python test_swing_shadow.py
"""
import os, sys, asyncio, json, tempfile
from datetime import date, datetime
from pathlib import Path
os.environ.update(BINANCE_API_KEY="k", BINANCE_SECRET="s", BINANCE_FUTURES_URL="https://testnet.fake",
                  TRADING_212_API_KEY="k", TRADING_212_SECRET_KEY="s", TRADING_212_ENV="demo")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import market_hours
import momentum_scanner
import news_briefing
import swing_analyst
import swing_scanner
import swing_shadow as SH

FAILS = []
def ok(cond, name, info=""):
    print(("OK   " if cond else "FAIL ") + name + (f"  ({info})" if info and not cond else ""))
    if not cond:
        FAILS.append(name)

tmp = Path(tempfile.mkdtemp())
SH.LOG_FILE, SH.STATE_FILE = tmp / "swing.jsonl", tmp / "state.json"
PR = market_hours.PRAGUE_TZ
D1, D2, D3, D4, D5 = (date(2026, 11, d) for d in (2, 3, 4, 5, 6))

def cand(t, sector, score, stop=96.0, target=115.0):
    ind = {"close": 100.0, "atr14": 2.0, "ema20": 99, "ema50": 95, "ema200": 90, "rsi14": 60, "support": 97,
           "resistance": target, "ret20": 0.05, "rel_vol": 1.5, "sma50": 95, "sma200": 90, "atr22": 2.0, "hh22": 101}
    return {"ticker": t, "sector": sector, "score": score, "parts": {}, "headlines": [], "ind": ind,
            "plan": {"ok": True, "stop": stop, "target": target, "risk_ps": 4.4, "rr_net": 3.0, "entry_ref": 100.0}}

CANDS = [cand("AAA", "Tech", 0.8), cand("BBB", "Energy", 0.7), cand("CCC", "Tech", 0.65), cand("DDD", "Health", 0.62)]
scan_calls = []
def fake_scan(day, last_bo, dyn, brief, ccy, news_fn=None, **kw):
    scan_calls.append(day)
    return {"ok": True, "spy_close": 500.0, "spy_sma200": 450.0, "regime_ok": True, "side_rate": 0.002,
            "stats": {"tradable": 4, "passed_universe": 4, "score_ok": 4, "candidates": 4}, "candidates": list(CANDS), "rejected": []}

hist = pd.bdate_range(end="2026-10-30", periods=40)
DAYS = {  # (open, high, low, close)
    "AAA": {D1: (100, 102, 99, 101), D2: (101, 109, 100.5, 107), D3: (99, 99.5, 98, 99), D4: (99, 99, 99, 99), D5: (99, 99, 99, 99)},
    "BBB": {D1: (95, 96, 94, 95)},
    "DDD": {D1: (100, 101, 99, 100), D2: (100, 101, 99.5, 100), D3: (100, 101, 99.5, 100), D4: (100, 101, 99.5, 100), D5: (100, 101, 99.5, 100)},
    "SPY": {d: (500, 505, 495, 500 + i) for i, d in enumerate((D1, D2, D3, D4, D5))},
}
def frame(t):
    rows = [(ts, 100, 101, 99, 100, 1e6) for ts in hist]
    rows += [(pd.Timestamp(d), *v, 1e6) for d, v in sorted(DAYS.get(t, {}).items())]
    return pd.DataFrame(rows, columns=["Date", "Open", "High", "Low", "Close", "Volume"]).set_index("Date")
SH.fetch_daily = lambda tickers: {t: frame(t) for t in tickers if t in DAYS}
SH.fetch_entry_prices = lambda tickers, day: {t: v for t, v in {
    "AAA": {"fill": 100.0, "high": 102.0, "low": 99.0, "src": "5M 09:45"},
    "BBB": {"fill": 95.0, "high": 96.0, "low": 94.0, "src": "5M 09:45"}}.items() if t in tickers}
SH.fetch_news = lambda t: []
SH._account_info = lambda: {"real_ccy": "EUR", "start_eur": 1000.0, "source": "test"}
SH._usd_eur = lambda: 1.0
swing_scanner.scan = fake_scan
news_briefing.get_extra_tickers = lambda: []
momentum_scanner.get_dynamic_tickers = lambda region="US": []

gem = {"pick": 0, "vote": 0}
async def fake_pick(cands, budget):
    budget.take(); gem["pick"] += 1
    return {"picks": [{"id": "C1", "ticker": "AAA", "reason_cs": "r", "catalyst": "k", "invalidation": "i", "cited_ids": ["C1"]},
                      {"id": "C2", "ticker": "BBB", "reason_cs": "r", "catalyst": "k", "invalidation": "i", "cited_ids": ["C2"]}],
            "rejected": [], "model": "fake", "prompt_hash": "h"}
async def fake_vote(c, budget, info=None):
    for _ in range(3):
        budget.take()
    gem["vote"] += 1
    b = 2 if c["ticker"] in ("AAA", "BBB") else 0
    return {"ticker": c["ticker"], "buy": b >= 2, "buy_votes": b, "votes": [{"model": "fake"}], "prompt_hash": "h"}
swing_analyst.pick, swing_analyst.vote = fake_pick, fake_vote

def at(d, h, m):
    return datetime(d.year, d.month, d.day, h, m, tzinfo=PR)
def events(kind=None):
    rows = [json.loads(x) for x in SH.LOG_FILE.read_text(encoding="utf-8").splitlines()]
    return [r for r in rows if kind is None or r["type"] == kind]
run = lambda now: asyncio.run(SH.tick(now))

# --- před ranním časem nic
ok(run(at(D1, 8, 0)) == [] and not SH.STATE_FILE.exists(), "před 09:10 Praha se nic neděje")

# --- ráno D1
msgs = run(at(D1, 9, 15))
st = SH.load_state()
pend = {k: [p["ticker"] for p in st["tracks"][k]["positions"]] for k in "ABC"}
ok(len(msgs) == 1 and "Swing stín 2026-11-02" in msgs[0], "ranní zpráva", msgs)
ok(pend["A"] == ["AAA", "BBB"], "A: Gemini vybral AAA, BBB a oba mají 2/3 BUY", pend)
ok(pend["B"] == ["AAA", "BBB", "DDD"], "B: podle skóre, CCC vyřazen (sektor Tech obsazen)", pend)
ok(len(pend["C"]) == 3 and events("random_pick")[0]["seed"] == 20261102, "C: náhodně se seedem z data", pend)
ok(st["gemini"]["used"] == 7 and st["tracks"]["A"]["equity_start"] == 1000.0, "rozpočet Gemini 1 + 2x3 volání, start 1000 EUR")
ok(any(e["reason"].startswith("sektor") for e in events("risk") if e["track"] == "B"), "zamítnutí sektoru je v logu")
ok(run(at(D1, 9, 30)) == [] and len(scan_calls) == 1, "ráno jen jednou za den")

# --- večer D1: vstupy
msgs = run(at(D1, 22, 35))
st = SH.load_state()
A = st["tracks"]["A"]
aaa = next(p for p in A["positions"] if p["ticker"] == "AAA")
ok(len(msgs) == 1 and "Swing stín 2026-11-02" in msgs[0] and "A Gemini" in msgs[0], "večerní souhrn stop", msgs)
ok(aaa["status"] == "OPEN" and aaa["entry"] == 100.0 and aaa["stop"] == 96.0 and aaa["fill_src"] == "5M 09:45",
   "AAA vstup za cenu 09:45, stop 96", aaa)
ok(not any(p["ticker"] == "BBB" for p in A["positions"]) and any(e["ticker"] == "BBB" for e in events("cancel")),
   "BBB: open pod stopem -> zrušeno")
ok(abs(aaa["initial_risk_acct"] - 5.0) < 0.05, "riziko 0,5 % z 1000 EUR vč. nákladů", aaa["initial_risk_acct"])
ok(st["last_evening"] == "2026-11-02" and run(at(D1, 22, 45)) == [], "večer jen jednou")

# --- D2: +2R polovina, BE stop
run(at(D2, 22, 35))
st = SH.load_state()
aaa = next(p for p in st["tracks"]["A"]["positions"] if p["ticker"] == "AAA")
ok(aaa["partial_done"] and abs(aaa["qty_open"] - aaa["qty"] / 2) < 1e-3, "D2: polovina prodána na +2R", aaa)
su = [e for e in events("stop_update") if e["id"] == aaa["id"]]
ok(aaa["stop"] >= 100.4 and "BE" in su[-1]["why"], "D2: stop aspoň na break-even (entry + náklady), chandelier výš", su)
ok(any(e["type"] == "morning_missed" and e["day"] == "2026-11-03" for e in events()), "D2: zmeškané ráno zapsáno")

# --- D3: gap pod BE -> výstup za open, uzavřeno s R > 0 (polovina na 2R)
run(at(D3, 22, 35))
st = SH.load_state()
A = st["tracks"]["A"]
cl = [e for e in events("close") if e["track"] == "A" and e["ticker"] == "AAA"]
ok(cl and cl[0]["last_reason"] == "GAP_STOP" and cl[0]["r_net"] > 0.5, "D3: gap pod stop, uzavřeno v zisku", cl)
ok(A["closed_n"] == 1 and A["wins"] == 1 and not any(p["ticker"] == "AAA" for p in A["positions"]), "statistika stopy A")
ok(abs(A["equity"] - A["cash"]) < 1e-3 and A["equity"] > 1000, "A bez pozic: equity = hotovost > start", (A["equity"], A["cash"], A["positions"]))
ok(st["tracks"]["B"]["bench_equity"] and "SPY" in SH.status_text(), "srovnání se SPY a /swing text")

# --- D5 ráno: večer D4 se dožene, vyčerpaný rozpočet -> A vynechána, B a C pokračují
async def broke_pick(cands, budget):
    raise swing_analyst.BudgetExceeded("limit")
swing_analyst.pick = broke_pick
msgs = run(at(D5, 9, 15))
st = SH.load_state()
ok(st["last_evening"] == "2026-11-05" and st["last_morning"] == "2026-11-06", "D5: dohnán večer D4, pak ráno", st.get("last_evening"))
ok(any(e["type"] == "track_skip" and e["track"] == "A" for e in events()) and "vynechána" in msgs[-1], "limit Gemini: A vynechána", msgs)
ok(events("scan")[-1]["day"] == "2026-11-06", "B a C dál dostávají kandidáty")

# --- živá cesta neexistuje
SH.SWING_LIVE_ENABLED = True
try:
    run(at(D5, 9, 20)); ok(False, "SWING_LIVE_ENABLED zastaví stín")
except SH.LiveDisabled:
    ok(True, "SWING_LIVE_ENABLED = True zastaví stín, nic se neodešle")
SH.SWING_LIVE_ENABLED = False
ok("trading212" not in Path(SH.__file__).read_text(encoding="utf-8").split("def _account_info")[0], "stín neimportuje příkazy T212")
ok("place_" not in Path(SH.__file__).read_text(encoding="utf-8"), "ve swing_shadow není žádné place_* volání")

print("\nVŠECHNO OK" if not FAILS else f"\n{len(FAILS)} SELHALO: {FAILS}")
sys.exit(1 if FAILS else 0)
