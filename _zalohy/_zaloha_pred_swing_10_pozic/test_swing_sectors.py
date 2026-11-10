"""Offline test sektorů swingu (3. 11.): převod yfinance -> GICS, neznámý sektor se neslučuje, limit 1 na sektor
zvlášť pro US a EU (SWING_SECTOR_LIMIT_SCOPE = "per_market").
Spuštění:  python test_swing_sectors.py
"""
import os, sys, tempfile
from pathlib import Path
os.environ.update(BINANCE_API_KEY="k", BINANCE_SECRET="s", BINANCE_FUTURES_URL="https://testnet.fake",
                  TRADING_212_API_KEY="k", TRADING_212_SECRET_KEY="s", TRADING_212_ENV="demo")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import swing_sectors as SS
import swing_rules as R

FAILS = []
def ok(cond, name, info=""):
    print(("OK   " if cond else "FAIL ") + name + (f"  ({info})" if info and not cond else ""))
    if not cond:
        FAILS.append(name)

SS.CACHE_FILE, SS._cache = Path(tempfile.mkdtemp()) / "s.json", None
ok(SS.normalize("Technology") == "Information Technology" and SS.normalize("Healthcare") == "Health Care"
   and SS.normalize("Financial Services") == "Financials" and SS.normalize("Health Care") == "Health Care", "yfinance -> GICS")
ok(SS.resolve("CAP.PA", fetch=lambda t: "Information Technology") == "Information Technology", "sektor z yfinance")
ok(SS.resolve("CAP.PA", fetch=lambda t: None) == "Information Technology", "podruhé z cache bez dotazu")
ok(SS.resolve("ZZZ.DE", fetch=lambda t: None) == "Unknown:ZZZ.DE" and SS.resolve("YYY.DE", fetch=lambda t: None) != "Unknown:ZZZ.DE",
   "neznámý sektor = vlastní sektor tickeru (neslučuje se)")
ok(SS.resolve("AAPL", "Information Technology") == "Information Technology", "známý sektor (US) se použije")

pos = lambda t, sec, m: {"ticker": t, "sector": sec, "market": m, "status": "PENDING", "planned_risk_acct": 0.0}
held = [pos("EHC", "Health Care", "US"), pos("DSY.PA", "Information Technology", "EU")]
ok(R.portfolio_block(held, "Health Care", 1.0, 1000, 1.0, "GRF.MC", market="EU") is None, "EU Health Care volný, i když US má EHC")
ok("sektor" in (R.portfolio_block(held, "Information Technology", 1.0, 1000, 1.0, "CAP.PA", market="EU") or ""),
   "EU IT obsazený DSY.PA -> CAP.PA ne")
ok("sektor" in (R.portfolio_block(held, "Health Care", 1.0, 1000, 1.0, "UNH", market="US") or ""), "US Health Care obsazený EHC")
ok(R.portfolio_block(held, "Unknown:A.DE", 1.0, 1000, 1.0, "A.DE", market="EU") is None, "neznámé sektory se navzájem neblokují")
five = [pos(f"T{i}", f"S{i}", "US") for i in range(5)]
ok("plno" in (R.portfolio_block(five, "X", 1.0, 1000, 1.0, "Q.DE", market="EU") or ""), "max 5 pozic zůstává společný pro US + EU")

print("\nVŠECHNO OK" if not FAILS else f"\n{len(FAILS)} SELHALO: {FAILS}")
sys.exit(1 if FAILS else 0)
