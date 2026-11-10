"""Testy fáze 4 (bez sítě): datum dat v promptech (bars.as_of), vstup papírového swingu bez denního open,
čekání na intradenní data. Načasování výsledků testuje test_momentum_scanner.py.
Spuštění:  python test_faze4_datum.py
"""
import os, sys, tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
os.environ.update(BINANCE_API_KEY="k", BINANCE_SECRET="s", BINANCE_FUTURES_URL="https://testnet.fake",
                  TRADING_212_API_KEY="k", TRADING_212_SECRET_KEY="s", TRADING_212_ENV="demo")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import bars
import gemini_team
import swing_analyst
import crypto_old_paper
import swing_shadow as SH

SH.EVENTS_FILE = Path(tempfile.mkdtemp()) / "events.jsonl" if hasattr(SH, "EVENTS_FILE") else None
SH.log_event = lambda ev: None        # žádný zápis do skutečného logu stínu

FAILS = []
NY = ZoneInfo("America/New_York")


def ok(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


# ---------------------------------------------------------------- as_of
daily = pd.DataFrame({"Close": [1.0, 2.0]}, index=pd.to_datetime(["2026-11-05", "2026-11-06"]))
ok(bars.as_of(daily, "1d") == "6. 11. 2026 (denní close)", f"denní: {bars.as_of(daily, '1d')}")
h1 = pd.DataFrame({"Close": [1.0, 2.0]},
                  index=pd.DatetimeIndex([datetime(2026, 11, 9, 9, 30, tzinfo=NY), datetime(2026, 11, 9, 10, 30, tzinfo=NY)]))
now = datetime(2026, 11, 9, 10, 55, tzinfo=NY)       # 16:55 Praha, poslední 1h svíčka (10:30-11:30 NY) běží
ok(bars.as_of(h1, "1h", closed=None, now=now) == "9. 11. 16:55 Praha (1h svíčka, neuzavřená)",
   f"rozjetá hodinová: {bars.as_of(h1, '1h', closed=None, now=now)}")
later = datetime(2026, 11, 9, 12, 0, tzinfo=NY)
ok(bars.as_of(h1, "1h", closed=None, now=later) == "9. 11. 17:30 Praha (1h svíčka, uzavřená)",
   f"uzavřená hodinová = konec svíčky: {bars.as_of(h1, '1h', closed=None, now=later)}")
ok(bars.as_of(None, "1d") == "" and bars.as_of(pd.DataFrame(), "1h") == "", "chybějící data -> prázdný řetězec")
ok(bars.as_of_time(datetime(2026, 11, 9, 8, 1, tzinfo=timezone.utc), "1m svíčka Binance")
   == "9. 11. 09:01 Praha (1m svíčka Binance)", "čas z Binance v čase Prahy")

# ---------------------------------------------------------------- prompty
md = {"day_change_pct": 2.5, "data_as_of": "9. 11. 16:55 Praha (1h svíčka, neuzavřená)"}
p = gemini_team.build_catalyst_prompt("AAPL", "Apple", ["Apple beats"], market_data=md)
ok("Data k 9. 11. 16:55 Praha (1h svíčka, neuzavřená)." in p, "katalyzátor: řádek s datem")
p0 = gemini_team.build_catalyst_prompt("AAPL", "Apple", ["Apple beats"], market_data={"day_change_pct": 2.5})
ok("Data k" not in p0 and "+2.50 %" in p0, "katalyzátor bez data_as_of: fráze chybí, zbytek stejný")
pb = gemini_team.build_batch_prompt([{"ticker": "AAPL", "name": "Apple", "headlines": [], "market_data": md},
                                     {"ticker": "MSFT", "name": "", "headlines": [], "market_data": {"day_change_pct": 1}}])
ok(pb.count("Data k ") == 1, "dávka: datum jen u bloku, který ho má")
ind = {"close": 10, "ema20": 9, "ema50": 8, "ema200": 7, "rsi14": 55.0, "atr14": 0.3, "support": 9.5, "resistance": 11,
       "ret20": 0.05, "rel_vol": 1.2, "sma50": 8, "sma200": 7, "atr22": 0.3, "hh22": 10.5,
       "data_as_of": "6. 11. 2026 (denní close)"}
c = {"ticker": "AAPL", "ind": ind, "plan": {"stop": 9.4, "target": 11.2, "rr_net": 2.0}, "score": 0.7, "parts": {}}
ok(swing_analyst._numbers(c).startswith("data k 6. 11. 2026 (denní close), close 10"), "swing čísla začínají datem")
ok(swing_analyst._numbers({**c, "ind": {k: v for k, v in ind.items() if k != "data_as_of"}}).startswith("close 10"),
   "swing bez data: beze změny")
ok("do close dne uvedeného u čísel" in swing_analyst.RULES, "pravidla už neříkají 'včerejší close'")
pc, _ = swing_analyst.build_check_prompt([{"id": "P1", "ticker": "AAPL", "entry": 10, "stop": 9, "close": 10.5,
                                           "close_day": "2026-11-06", "r_now": 0.5, "days_held": 2, "headlines": []}])
ok("close 10.5 (close k 2026-11-06)" in pc, "večerní kontrola: den close")
cp = crypto_old_paper.build_prompt("BTCUSDT", 100.0, 1.0, 2.0, [], 0, data_as_of="9. 11. 09:01 Praha (1m svíčka Binance)")
ok("- Data k 9. 11. 09:01 Praha (1m svíčka Binance).\n- 1hodinový pohyb" in cp, "stará metoda krypto: datum ve vstupních datech")
ok("Data k" not in crypto_old_paper.build_prompt("BTCUSDT", 100.0, 1.0, 2.0, [], 0), "krypto bez data: fráze chybí")

# ---------------------------------------------------------------- vstup papírového swingu
tz = NY
m5 = pd.DataFrame({"Open": [100, 101, 102], "High": [103, 104, 105], "Low": [99, 100, 98], "Close": [101, 102, 103]},
                  index=pd.DatetimeIndex([datetime(2026, 11, 9, 9, 30, tzinfo=tz), datetime(2026, 11, 9, 9, 45, tzinfo=tz),
                                          datetime(2026, 11, 9, 9, 50, tzinfo=tz)]))
e = SH.entry_from_bars(m5, datetime(2026, 11, 9, 9, 45, tzinfo=tz), date(2026, 11, 9), "5M")
ok(e == {"fill": 101.0, "high": 105.0, "low": 98.0, "src": "5M 09:45"}, f"vstup = open první svíčky od fill_after: {e}")
h = pd.DataFrame({"Open": [100, 104], "High": [106, 107], "Low": [97, 103], "Close": [104, 105]},
                 index=pd.DatetimeIndex([datetime(2026, 11, 9, 9, 30, tzinfo=tz), datetime(2026, 11, 9, 10, 30, tzinfo=tz)]))
e = SH.entry_from_bars(h, datetime(2026, 11, 9, 9, 45, tzinfo=tz), date(2026, 11, 9), "1H")
ok(e and e["fill"] == 104.0 and e["low"] == 103.0 and e["src"] == "1H 10:30",
   f"1h náhrada: svíčka 09:30 (začala před fill_after) se nebere: {e}")
ok(SH.entry_from_bars(None, datetime(2026, 11, 9, 9, 45, tzinfo=tz), date(2026, 11, 9), "5M") is None, "bez dat -> None")


def fresh():
    tr = SH._new_track(2000.0)
    pos = {"id": "B-2026-11-09-AAA", "track": "B", "market": "US", "ccy": "USD", "ticker": "AAA", "sector": "X",
           "status": "PENDING", "entry_day": "2026-11-09", "fill_after": "2026-11-09T09:45-05:00", "plan_stop": 95.0,
           "target": 112.0, "entry_ref": 100.0, "score": 0.7, "atr14": 2.0, "cost_side": 0.001,
           "planned_risk_acct": 10.0, "planned_notional_acct": 200.0, "note": {}}
    tr["positions"].append(pos)
    return {"tracks": {"B": tr}}, pos


bar = {"open": 90.0, "high": 120.0, "low": 85.0, "close": 100.0}     # denní open pod stopem: dřív by vstup proběhl za 90
st, pos = fresh()
SH._fill_entry(st, "B", pos, date(2026, 11, 9), None, bar, 1.0)
ok(pos["status"] == "PENDING" and pos.get("fill_tries") == 1, "bez intradenních dat: žádný denní open, pozice čeká")
SH._fill_entry(st, "B", pos, date(2026, 11, 9), None, None, 1.0, now_day=date(2026, 11, 10))
ok(pos["status"] == "PENDING" and pos["fill_tries"] == 2, "druhý den pořád čeká")
SH._fill_entry(st, "B", pos, date(2026, 11, 9), None, None, 1.0, now_day=date(2026, 11, 11))
ok(pos["status"] == "CANCELLED" and pos["cancel_reason"] == "bez intradenních dat", "po 2 obchodních dnech zrušeno")

st, pos = fresh()
SH._fill_entry(st, "B", pos, date(2026, 11, 9), None, None, 1.0)
SH._fill_entry(st, "B", pos, date(2026, 11, 9), {"fill": 101.0, "high": 104.0, "low": 99.0, "src": "1H 10:30"}, None, 1.0,
               now_day=date(2026, 11, 10))
ok(pos["status"] == "OPEN" and pos["entry"] == 101.0 and pos["fill_src"] == "1H 10:30", "pozdější pokus vstoupí za 1h open")
d_ix = pd.to_datetime(["2026-11-09", "2026-11-10", "2026-11-11"])
df = pd.DataFrame({"Open": [100, 100, 100], "High": [104, 103, 102], "Low": [99, 94, 98], "Close": [101, 96, 99]}, index=d_ix)
SH._catch_up(st, "B", pos, date(2026, 11, 9), date(2026, 11, 11), df, 1.0)
ok(pos.get("qty_open", 1) == 0 or pos["status"] != "OPEN", "dohnání: stop 95 zasažený 10. 11. se započítá")

st, pos = fresh()
SH._fill_entry(st, "B", pos, date(2026, 11, 9), {"fill": 101.0, "high": 104.0, "low": 99.0, "src": "5M 09:45"}, bar, 1.0)
ok(pos["status"] == "OPEN" and pos["entry"] == 101.0, "s 5M daty se vstoupí hned jako dřív (denní svíčka ignorována)")

print("\nVšechny testy fáze 4 prošly." if not FAILS else f"\nSELHALO {len(FAILS)}: {FAILS}")
sys.exit(1 if FAILS else 0)
