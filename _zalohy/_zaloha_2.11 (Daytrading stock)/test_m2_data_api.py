"""Offline test M2: ověřené svíčky (bars.py, Scout, HIGH20) a limiter Trading 212. Žádné API.
Spuštění:  python test_m2_data_api.py
"""
import os, sys, threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
os.environ.update(BINANCE_API_KEY="k", BINANCE_SECRET="s", BINANCE_FUTURES_URL="https://testnet.fake",
                  TRADING_212_API_KEY="k", TRADING_212_SECRET_KEY="s", TRADING_212_ENV="demo")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import bars
import daily_breakout as db
import scout
import trading212

FAILS = []
def ok(cond, name, info=""):
    print(("OK   " if cond else "FAIL ") + name + (f"  ({info})" if info and not cond else ""))
    if not cond:
        FAILS.append(name)

def raises(fn):
    try:
        fn()
    except bars.BarsError:
        return True
    return False

NY = "America/New_York"
NOW = pd.Timestamp("2026-10-28 14:10", tz=NY).to_pydatetime()   # středa, seance běží

def frame(index, close=100.0, volume=1000.0):
    n = len(index)
    return pd.DataFrame({"Open": [close] * n, "High": [close + 1] * n, "Low": [close - 1] * n,
                         "Close": [close] * n, "Volume": [volume] * n}, index=index)

def hourly(last_start, days=3):
    """US hodinové svíčky 9:30-15:30 za několik dní, končí svíčkou last_start (může být rozjetá)."""
    idx = []
    day = pd.Timestamp(last_start).normalize()
    for d in range(days, -1, -1):
        dd = day - pd.Timedelta(days=d)
        if dd.weekday() >= 5:
            continue
        for h in range(7):
            t = dd + pd.Timedelta(hours=9, minutes=30) + pd.Timedelta(hours=h)
            if t <= pd.Timestamp(last_start):
                idx.append(t)
    return frame(pd.DatetimeIndex(idx))

def daily(last_day, n=22):
    idx = pd.bdate_range(end=pd.Timestamp(last_day).normalize(), periods=n, tz=NY)
    return frame(idx, volume=1_000_000.0)

# --- bars.py ---
h_ok = hourly(pd.Timestamp("2026-10-28 13:30", tz=NY))
ok(not raises(lambda: bars.validate(h_ok, "1h", max_age=timedelta(minutes=90), now=NOW)), "čerstvé hodinové svíčky projdou")
ok(raises(lambda: bars.validate(daily("2026-10-28"), "1h", now=NOW)), "denní tabulka místo hodinové -> BarsError")
h_old = hourly(pd.Timestamp("2026-10-27 15:30", tz=NY))
ok(raises(lambda: bars.validate(h_old, "1h", max_age=timedelta(minutes=90), now=NOW)), "včerejší hodinové svíčky -> BarsError (stará data)")
ok(not raises(lambda: bars.validate(daily("2026-10-28"), "1d", max_age=timedelta(hours=24), now=NOW)), "dnešní denní svíčka projde (přes víkendy)")
ok(raises(lambda: bars.validate(daily("2026-10-27"), "1d", max_age=timedelta(hours=24), now=NOW)), "chybí dnešní denní svíčka -> BarsError")
ok(raises(lambda: bars.validate(frame(pd.DatetimeIndex([NOW])), "1h", now=NOW)), "málo svíček -> BarsError")
closed = bars.validate(h_ok, "1h", closed_only=True, now=NOW)
ok(len(closed) == len(h_ok) - 1, "rozjetá svíčka 13:30 se při closed_only zahodí")
h_done = hourly(pd.Timestamp("2026-10-28 12:30", tz=NY))
ok(len(bars.validate(h_done, "1h", closed_only=True, now=NOW)) == len(h_done), "uzavřená svíčka 12:30 zůstane")
# den s mezerou (halt) nemění medián kroku
h_gap = h_ok.drop(h_ok.index[-3])
ok(not raises(lambda: bars.validate(h_gap, "1h", now=NOW)), "mezera v datech (halt) není chyba intervalu")

# --- Scout: špatná data nespustí signál, cena se vrací dál ---
class FakeTicker:
    def __init__(self, h1, d1):
        self.h1, self.d1 = h1, d1
    def history(self, period, interval):
        return self.h1 if interval == "1h" else self.d1

def run_scout(h1, d1):
    orig_t, orig_info, orig_news = scout.yf.Ticker, scout._company_info, scout._relevant_stock_headlines
    scout.yf.Ticker = lambda t: FakeTicker(h1, d1)
    scout._company_info = lambda t, ticker: ("Firma", "USD")
    scout._relevant_stock_headlines = lambda t, ticker, name: []
    orig_v = bars.validate
    bars.validate = lambda df, iv, max_age=None, closed_only=False, now=None: orig_v(df, iv, max_age, closed_only, NOW)
    try:
        return scout.fetch_stock_data_sync("AAPL")
    finally:
        scout.yf.Ticker, scout._company_info, scout._relevant_stock_headlines = orig_t, orig_info, orig_news
        bars.validate = orig_v

jump = h_ok.copy(); jump.iloc[-1, jump.columns.get_loc("Close")] = 110.0   # +10 % za hodinu
d_ok = daily("2026-10-28")
r = run_scout(jump, d_ok)
ok(r and r["triggered"] and not r["bars_error"], "čerstvá data + skok ceny -> signál", r)
jump_old = h_old.copy(); jump_old.iloc[-1, jump_old.columns.get_loc("Close")] = 110.0
r = run_scout(jump_old, d_ok)
ok(r and not r["triggered"] and "stará data" in r["bars_error"], "stará data + skok ceny -> žádný signál", r)
ok(r and r["price"] == 110.0, "cena se při špatných datech vrací (správa pozic ji potřebuje)")
r = run_scout(jump, daily("2026-10-27"))
ok(r and not r["triggered"] and r["bars_error"], "bez dnešní denní svíčky -> žádný signál (falešný objem)", r)

# --- HIGH20: stará nebo nedenní data ---
DAY = db.DAY_MS
T0 = 1790000000000 - 1790000000000 % DAY
def kl(closes, step=DAY):
    return [[T0 + i * step, c, c + 1, c - 1, c, 100, T0 + (i + 1) * step - 1] for i, c in enumerate(closes)]
rising = [100 + i for i in range(30)]
ok(db.signal(kl(rising), T0 + 30 * DAY + 60_000) is not None, "HIGH20: včerejší svíčka -> signál")
ok(db.signal(kl(rising), T0 + 32 * DAY + 60_000) is None, "HIGH20: poslední uzavřená svíčka 3 dny stará -> bez signálu")
ok(db.signal(kl(rising, step=DAY // 24), T0 + 31 * DAY // 24) is None, "HIGH20: hodinové svíčky místo denních -> bez signálu")

# --- Limiter T212 ---
class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.lock = threading.Lock()
    def now(self):
        with self.lock:
            return self.t
    def sleep(self, s):
        with self.lock:
            self.t += max(0.0, s)

def with_clock(fn):
    c = FakeClock()
    o = (trading212._clock, trading212._sleep, dict(trading212._next_slot))
    trading212._clock, trading212._sleep = c.now, c.sleep
    trading212._next_slot.clear()
    try:
        return fn(c)
    finally:
        trading212._clock, trading212._sleep = o[0], o[1]
        trading212._next_slot.clear(); trading212._next_slot.update(o[2])

def spread(c):
    waits = [trading212._throttle("GET", "/equity/portfolio") for _ in range(60)]
    return waits
waits = with_clock(spread)
ok(abs(sum(waits) - 59 * 5.0) < 1e-6 or sum(waits) >= 59 * 5.0 - 1e-6, "60 požadavků na portfolio se rozloží po 5 s", sum(waits))
ok(trading212._rate_key("GET", "/equity/orders/123")[1] == 1.0 and trading212._rate_key("GET", "/equity/orders")[1] == 5.0,
   "detail příkazu a seznam příkazů mají vlastní limity")
ok(trading212._rate_key("POST", "/equity/orders/stop")[0] != trading212._rate_key("POST", "/equity/orders/market")[0],
   "stop a market příkaz se neblokují navzájem")

def threaded(c):
    starts = []
    def work():
        trading212._throttle("GET", "/equity/account/summary")
        starts.append(c.now())
    ts = [threading.Thread(target=work) for _ in range(10)]
    [t.start() for t in ts]; [t.join() for t in ts]
    return sorted(trading212._next_slot.values())
slots = with_clock(threaded)
ok(len(slots) == 1 and abs(slots[0] - (1000.0 + 10 * 5.0)) < 1e-6, "10 vláken dostane 10 různých slotů", slots)

class Resp:
    def __init__(self, code, headers=None, body=None):
        self.status_code, self.headers, self._b = code, headers or {}, body if body is not None else {}
        self.text = str(self._b)
    def json(self):
        return self._b

def fake_requests(responses):
    calls = []
    def request(method, url, **kw):
        calls.append((method, url))
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    return calls, request

orig_req = trading212.requests.request
orig_cfg = trading212.is_configured, trading212.to_t212_ticker
trading212.is_configured = lambda: True
trading212.to_t212_ticker = lambda s: "AAPL_US_EQ"
try:
    calls, trading212.requests.request = fake_requests([TimeoutError("read timeout")])
    res = with_clock(lambda c: trading212.place_market_order_sync("AAPL", 1.0))
    ok(not res["success"] and len(calls) == 1, "nákup po chybě sítě se NEopakuje (mohl projít)", calls)

    calls, trading212.requests.request = fake_requests([Resp(429, {"Retry-After": "3"}), Resp(200, body={"id": 7})])
    res = with_clock(lambda c: trading212.place_market_order_sync("AAPL", 1.0))
    ok(res["success"] and res["order_id"] == 7 and len(calls) == 2, "429 -> jeden další pokus, pak úspěch", calls)

    calls, trading212.requests.request = fake_requests([Resp(429), Resp(429), Resp(200)])
    status, _ = with_clock(lambda c: trading212._request("GET", "/equity/portfolio"))
    ok(status == 429 and len(calls) == 2, "po druhém 429 už se neopakuje", calls)

    calls, trading212.requests.request = fake_requests([Resp(200, body=[{"ticker": "AAPL_US_EQ", "quantity": 1}])])
    pf = with_clock(lambda c: trading212.get_portfolio_sync())
    ok(pf and pf[0]["ticker"] == "AAPL_US_EQ" and calls[0][0] == "GET", "portfolio jde přes limiter a vrací data")
finally:
    trading212.requests.request = orig_req
    trading212.is_configured, trading212.to_t212_ticker = orig_cfg

print("\nVŠECHNO OK" if not FAILS else f"\n{len(FAILS)} SELHALO: {FAILS}")
sys.exit(1 if FAILS else 0)
