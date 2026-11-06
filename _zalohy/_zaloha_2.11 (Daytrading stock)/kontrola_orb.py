"""Živá kontrola ORB15 (stahuje Yahoo, Gemini nevolá, nic nekupuje ani neukládá):
přehraje poslední dokončené obchodní dny US watchlistu a vypíše pásmo a stínový výsledek.
Spuštění:  python kontrola_orb.py [počet_dní]
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import yfinance as yf
import bars
import cost_model
import orb_strategy as orb
import orb_shadow
from config import US_STOCKS_WATCHLIST
import market_hours

days_back = int(sys.argv[1]) if len(sys.argv) > 1 else 5
now = datetime.now(market_hours.NY_TZ)
cost = cost_model.stock_cost("X", "USD", "EUR")
trades = []
for t in US_STOCKS_WATCHLIST:
    tk = yf.Ticker(t)
    b5 = orb_shadow._to_ny(bars.fetch_bars(t, "5m", "1mo", closed_only=True, yf_ticker=tk))
    d1 = orb_shadow._to_ny(bars.fetch_bars(t, "1d", "3mo", yf_ticker=tk))
    days = sorted({ts.date() for ts in b5.index if ts.date() < now.date()})[-days_back:]
    for d in days:
        s = orb.build_setup(t, b5, d1, d)
        r = orb.simulate_day(s, b5, cost)
        trades.append(r)
        if r["status"] in ("OPEN", "CLOSED"):
            print(f"{d} {t:5} OBCHOD vstup {r['entry']:.2f} stop {r['stop']:.2f} náklady {r['cost_share_r']} R "
                  f"| A {r['variant_a']['r_net']} R | B {r['variant_b']['r_net']} R")
        else:
            print(f"{d} {t:5} {r['status']:9} {r.get('reason') or ''} (RVOL15 {s.rvol15:.2f})")
print("\nSouhrn A:", orb.summarize(trades, "variant_a"))
print("Souhrn B:", orb.summarize(trades, "variant_b"))
print("A jen s náklady do 15 % R:", orb.summarize(trades, "variant_a", only_cost_ok=True))
