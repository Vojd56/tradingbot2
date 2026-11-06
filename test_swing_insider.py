"""Offline test stopy E (swing_insider.py): XML Form 4, rutinní insider, souhrn nákupů, cenové podmínky. Bez sítě.
Spuštění:  python test_swing_insider.py
"""
import os, sys
from datetime import date
from pathlib import Path
os.environ.update(BINANCE_API_KEY="k", BINANCE_SECRET="s", BINANCE_FUTURES_URL="https://testnet.fake",
                  TRADING_212_API_KEY="k", TRADING_212_SECRET_KEY="s", TRADING_212_ENV="demo")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np
import pandas as pd
import swing_insider as SI

FAILS = []
def ok(cond, name, info=""):
    print(("OK   " if cond else "FAIL ") + name + (f"  ({info})" if info and not cond else ""))
    if not cond:
        FAILS.append(name)

XML = b"""<SEC-DOCUMENT>
<XML>
<?xml version="1.0"?>
<ownershipDocument>
 <issuer><issuerCik>0000012345</issuerCik><issuerTradingSymbol>abc</issuerTradingSymbol></issuer>
 <reportingOwner><reportingOwnerId><rptOwnerCik>0000999</rptOwnerCik></reportingOwnerId>
  <reportingOwnerRelationship><isOfficer>1</isOfficer><officerTitle>CEO</officerTitle></reportingOwnerRelationship></reportingOwner>
 <aff10b5One>0</aff10b5One>
 <nonDerivativeTable><nonDerivativeTransaction>
  <transactionDate><value>2026-11-02</value></transactionDate>
  <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
  <transactionAmounts><transactionShares><value>2000</value></transactionShares>
   <transactionPricePerShare><value>60.5</value></transactionPricePerShare>
   <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts>
 </nonDerivativeTransaction></nonDerivativeTable>
 <footnotes><footnote id="F1">Open market purchase.</footnote></footnotes>
</ownershipDocument>
</XML>
</SEC-DOCUMENT>"""
x = SI.parse_form4(XML)
ok(x and x["symbol"] == "ABC" and x["owners"][0]["officer"] and x["owners"][0]["cik"] == "0000000999", "XML: vydavatel a officer")
ok(x and x["tx"][0]["code"] == "P" and x["tx"][0]["shares"] == 2000 and not x["plan"], "XML: transakce P, bez 10b5-1")
x2 = SI.parse_form4(XML.replace(b"Open market purchase.", b"Made under a Rule 10b5-1 plan."))
ok(x2 and x2["plan"], "10b5-1 z poznámky")

hist = {"0000000999": {"2025-11", "2024-11"}, "0000000777": {"2025-11"}}
ok(SI.is_routine("0000000999", "2026-11-02", hist), "rutinní: stejný měsíc ve 2 ze 3 let")
ok(not SI.is_routine("0000000777", "2026-11-02", hist), "nerutinní: jen 1 rok")

T = date(2026, 11, 2)
bd = [date(2026, 11, d) for d in (2, 1)] + [date(2026, 10, 30), date(2026, 10, 29), date(2026, 10, 28)]
sd = bd + [date(2026, 10, d) for d in range(27, 17, -1)]
def tr(t, owner, code, usd, filed="2026-11-02", officer=True, plan=False, dt="2026-11-02"):
    return {"filed": filed, "acc": f"{t}{owner}{code}{filed}", "ticker": t, "owner": owner, "officer": officer, "title": "CEO",
            "plan": plan, "code": code, "ad": "A" if code == "P" else "D", "date": dt, "shares": usd / 50, "price": 50.0}
trades = [tr("AAA", "0000000111", "P", 150_000),                      # projde
          tr("BBB", "0000000999", "P", 500_000),                      # rutinní insider -> ne
          tr("CCC", "0000000222", "P", 60_000), tr("CCC", "0000000333", "P", 50_000, filed="2026-10-29"),  # 110k za 5 dní
          tr("DDD", "0000000444", "P", 200_000), tr("DDD", "0000000555", "S", 10_000, dt="2026-10-22"),     # prodej 15 dní -> ne
          tr("EEE", "0000000666", "P", 300_000, officer=False),       # bez officera -> ne
          tr("FFF", "0000000888", "P", 300_000, plan=True),           # plán 10b5-1 -> ne
          tr("GGG", "0000000123", "P", 300_000, filed="2026-10-30")]  # žádné nové podání v den t -> ne
fl = SI.flags(trades, T, bd, sd, hist)
okset = {t for t, v in fl.items() if v["ok"]}
ok(okset == {"AAA", "CCC"}, "souhrn: projdou jen AAA a CCC", str(fl))
ok(fl["CCC"]["usd"] == 110_000 and fl["CCC"]["owners"] == 2, "součet za 5 dní a počet insiderů")

n = 260
idx = pd.bdate_range(end="2026-11-02", periods=n)
c = np.r_[np.linspace(50, 100, n - 20), np.linspace(100, 88, 15), [88, 89, 90, 91, 92]]
df = pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c, "Volume": 1e6}, index=idx)
pc = SI.price_check(df, 0.002)
ok(pc["ok"] and pc["plan"]["stop"] < pc["close"] < pc["plan"]["target"], "cena: korekce s odrazem nad SMA200 projde", str(pc))
df2 = df.copy(); df2["Close"] = np.linspace(50, 100, n); df2["High"] = df2["Close"] * 1.01; df2["Low"] = df2["Close"] * 0.99
ok(not SI.price_check(df2, 0.002)["ok"], "cena: u maxima (bez korekce) neprojde")
df3 = df.copy(); df3["Volume"] = 1e4
ok("obrat" in SI.price_check(df3, 0.002)["reason"], "cena: malý obrat neprojde")

import swing_rules as R
p = {"days_held": 15, "mfe_r": 0.3, "time_stop_days": 15}
ok(R.time_stop_due(p) and not R.time_stop_due({"days_held": 12, "mfe_r": 0.3, "time_stop_days": 15}), "časový stop 15 dní pro E")
ok(R.time_stop_due({"days_held": 40, "mfe_r": 3.0, "max_hold_days": 40}), "max 40 dní")
ok(R.time_stop_due({"days_held": 10, "mfe_r": 0.3}), "ostatní stopy dál 10 dní")

print(f"\n{len(FAILS)} chyb" if FAILS else "\nVše OK")
sys.exit(1 if FAILS else 0)
