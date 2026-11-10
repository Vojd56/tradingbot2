"""Testy stínu vstupu po poklesu: syntetické 5M svíčky, bez sítě."""

import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import pandas as pd

import market_hours
import pullback_shadow as PB

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

NY = market_hours.MARKETS["US"]["tz"]
OPEN = datetime(2026, 11, 10, 9, 30, tzinfo=NY)
fails = 0


def ok(cond, name, detail=""):
    global fails
    print(("OK  " if cond else "CHYBA ") + name + (f" | {detail}" if not cond and detail else ""))
    if not cond:
        fails += 1


def frame(rows, start=OPEN):
    """rows = (open, high, low, close)"""
    idx = [start + timedelta(minutes=5 * i) for i in range(len(rows))]
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=pd.DatetimeIndex(idx))


flat = [(100, 100.2, 99.8, 100)] * 3

# Bez poklesu se po poklesu nekupuje, HIGH koupí hned na další svíčce
m = frame(flat * 4)
ok(PB.replay(m, OPEN, "PB", 2.2)["status"] == "NO_ENTRY", "bez poklesu žádný vstup")
h = PB.replay(m, OPEN, "HIGH", 2.2)
ok(h["status"] == "DONE" and h["entry"] == 100 and h["min_since_open"] == 5, "HIGH: nákup na open další svíčky", h)
ok(h["reason"] == "KONEC SEANCE" and abs(h["r"]) < 1e-9, "HIGH: bez stopu a cíle prodej na konci seance", h)
ok(abs(h["r_net"] - (0 - 0.4 / 2.2)) < 0.001, "čisté R odečte náklady", h)

# Pokles do pásma: rozhoduje závěr svíčky, nakupuje se na open další, cíl 2R
m = frame([(100, 100, 99.9, 100), (100, 100, 96.8, 97), (97.5, 98, 97.4, 97.8), (97.8, 110, 97.7, 109)])
p = PB.replay(m, OPEN, "PB", 2.2)
ok(p["status"] == "DONE" and p["entry"] == 97.5, "vstup na open po svíčce v pásmu, ne na jejím závěru", p)
ok(p["reason"] == "CIL" and p["r"] == 2.0, "cíl 2R", p)

# Pokles mimo pásmo (přes 5,5 %) se nekupuje
m = frame([(100, 100, 99.9, 100), (100, 100, 93, 93.5), (93.5, 94, 93, 93.5)])
ok(PB.replay(m, OPEN, "PB", 2.2)["status"] == "NO_ENTRY", "pokles přes 5,5 % se nekupuje")

# Stop má přednost před cílem ve stejné svíčce
m = frame([(100, 100, 99.9, 100), (100, 100, 96.8, 97), (97, 120, 90, 100), (100, 100, 100, 100)])
s = PB.replay(m, OPEN, "PB", 2.2)
ok(s["reason"] == "STOP" and abs(s["r"] + 1) < 1e-9, "spor ve svíčce: stop první", s)

# Signál se nedívá zpět: pokles před signálem nestačí, musí přijít po něm
m = frame([(100, 100, 99.9, 100), (100, 100, 96.8, 97), (99.9, 100, 99.8, 100), (100, 100, 99.9, 100)])
ok(PB.replay(m, OPEN + timedelta(minutes=12), "PB", 2.2)["status"] == "NO_ENTRY", "pokles před signálem se nepočítá")

# Časové okno: vstup po 180 minutách už ne, vstup před 30 minutami se odloží
rows = [(100, 100, 99.9, 100)] * 40 + [(100, 100, 96.8, 97), (97, 97.1, 96.9, 97), (97, 97.1, 96.9, 97)]
m = frame(rows)
ok(PB.replay(m, OPEN, "PB", 2.2)["status"] == "DONE", "bez okna pozdní vstup projde")
ok(PB.replay(m, OPEN, "PB_TIME", 2.2)["status"] == "NO_ENTRY", "po 180 minutách už ne")
rows = [(100, 100, 99.9, 100), (100, 100, 97, 97)] + [(97, 97.1, 96.9, 97)] * 8
t = PB.replay(frame(rows), OPEN, "PB_TIME", 2.2)
ok(t["status"] == "DONE" and t["min_since_open"] == 30, "před 30 minutami čeká na začátek okna", t)

# Stop podle ATR a ATR jen ze svíček před dnem
ok(PB.stop_pct(2.0) == 2.2 and abs(PB.stop_pct(5.0) - 3.5) < 1e-9, "stop = větší z 2,2 % a 0,7 ATR")
dd = pd.DataFrame({"High": [101.0] * 20, "Low": [99.0] * 20, "Close": [100.0] * 20},
                  index=pd.DatetimeIndex([OPEN - timedelta(days=20 - i) for i in range(20)]))
ok(abs(PB.atr_pct_before(dd, "2026-11-10") - 2.0) < 1e-9, "ATR v procentech")
big = pd.concat([dd, pd.DataFrame({"High": [500.0], "Low": [1.0], "Close": [100.0]}, index=pd.DatetimeIndex([OPEN]))])
ok(abs(PB.atr_pct_before(big, "2026-11-10") - 2.0) < 1e-9, "dnešní svíčka se do ATR nepočítá")

# Výběr signálů, celý průběh a nezasahování do ostatních souborů
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    PB.SIGNALS_FILE, PB.SHADOW_FILE = td / "signals.jsonl", td / "paper_pullback.json"
    base = {"strategy": "SCOUT", "market": "STOCK_US", "price": 100}
    sigs = [dict(base, ts_utc="2026-11-06T15:00:00+00:00", ticker="OLD", verdict="KÚPIŤ"),
            dict(base, ts_utc="2026-11-10T15:00:00+00:00", ticker="AAA", verdict="KÚPIŤ"),
            dict(base, ts_utc="2026-11-10T16:00:00+00:00", ticker="AAA", verdict="KÚPIŤ"),
            dict(base, ts_utc="2026-11-10T15:00:00+00:00", ticker="BBB", verdict="POČKAŤ", news="Překonala odhady."),
            dict(base, ts_utc="2026-11-10T15:00:00+00:00", ticker="CCC", verdict="POČKAŤ", news="Žádný jasný důvod. Gemini není dostupný."),
            dict(base, ts_utc="2026-11-10T15:00:00+00:00", ticker="DDD", verdict="PRESKOČIŤ"),
            {"strategy": "ORB15_SHADOW", "ts_utc": "2026-11-10T15:00:00+00:00", "ticker": "EEE", "action": "SHADOW_ENTRY"}]
    PB.SIGNALS_FILE.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in sigs) + "\n", encoding="utf-8")
    got = PB.load_signals()
    ok(sorted(s["ticker"] for s in got) == ["AAA", "BBB"], "jen signály s katalyzátorem od FORWARD_FROM", [s["ticker"] for s in got])
    ok(next(s for s in got if s["ticker"] == "AAA")["ts"].hour == 10, "první signál na ticker a den, čas burzy")

    m5 = frame([(100, 100, 99.9, 100)] * 12 + [(100, 100, 96.8, 97), (97, 97.1, 96.9, 97), (97, 97.1, 96.9, 97)])
    daily = dd.tz_convert(NY) if dd.index.tz else dd
    calls = []

    def fake_fetch(ticker, interval, period, **kw):
        calls.append(ticker)
        return m5 if interval == "5m" else daily

    before_close = datetime(2026, 11, 10, 15, 0, tzinfo=NY)
    after_close = datetime(2026, 11, 10, 16, 30, tzinfo=NY)
    with mock.patch.object(PB.bars, "fetch_bars", fake_fetch), mock.patch("yfinance.Ticker", lambda t: t):
        ok(PB.run_pass(before_close) == [] and not calls, "před zavřením trhu nic nestahuje")
        new = PB.run_pass(after_close)
        ok(sorted(new) == ["2026-11-10|AAA", "2026-11-10|BBB"], "po zavření vyhodnotí oba signály", new)
        n = len(calls)
        ok(PB.run_pass(after_close) == [] and len(calls) == n, "už vyhodnocené se znovu nestahuje")
    res = PB.load_results()
    ok("HIGH" in res["2026-11-10|AAA"]["variants"], "KÚPIŤ má i srovnávací variantu HIGH")
    ok("HIGH" not in res["2026-11-10|BBB"]["variants"] and "PB" in res["2026-11-10|BBB"]["variants"], "POČKAŤ jen po poklesu")
    sb = PB.snapshot_block()
    ok(sb["stats"]["PB"]["signals"] == 2 and len(sb["recent"]) == 2 and sb["from"] == PB.FORWARD_FROM, "blok pro web", sb["stats"])
    ok("Po poklesu" in PB.summary_text(res) and "průměr" in PB.summary_text(res), "souhrn", PB.summary_text(res))

    def broken(*a, **k):
        raise RuntimeError("výpadek")

    PB.save_results({})
    with mock.patch.object(PB.bars, "fetch_bars", broken), mock.patch("yfinance.Ticker", lambda t: t):
        ok(PB.run_pass(after_close) == [] and PB.load_results() == {}, "výpadek dat: zkusí to znovu později")
        late = PB.run_pass(after_close + timedelta(days=4))
        ok(len(late) == 2 and PB.load_results()["2026-11-10|AAA"]["status"] == "NO_DATA", "po 3 dnech to vzdá")

print("\nCHYBY:", fails)
sys.exit(1 if fails else 0)
