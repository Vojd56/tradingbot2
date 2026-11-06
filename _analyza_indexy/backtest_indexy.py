"""Analýza indexů a kovů pro bota (29. 10. 2026). Nic nekupuje, nic v botovi nemění, Gemini nevolá.

Spuštění ze složky bota:  python _analyza_indexy\\backtest_indexy.py
Výstup: _analyza_indexy\\vysledky.txt a vysledky.json

1. Obchodovatelnost: T212 (t212_instruments.json) a Binance Futures (testnet z configu i ostrý fapi).
2. Základní podmínky: objem, ATR %, náklady jako podíl R při stopu 1,5x ATR.
3. HIGH20 na denních svíčkách (pravidla zmrazená jako u krypta, bez ladění):
   close >= max close za 20 dní -> vstup open dalšího dne, SL entry - 1,5x ATR(14), TP 4x ATR, výstup close téhož dne.
   Pro srovnání "každý den" = stejný obchod bez signálu (je signál lepší než náhodný den?).
   Robustnost: lookback 10 a 50, první a druhá polovina období.
4. ORB15 (orb_strategy.py beze změn) na 5M svíčkách za 60 dní pro US ETF.
"""
import json
import sys
import traceback
from pathlib import Path

BOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BOT))
OUT = Path(__file__).resolve().parent

import numpy as np
import pandas as pd
import requests
import yfinance as yf

import config

lines = []
res = {}


def p(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    lines.append(s)


# trh -> kandidáti: (yahoo, t212 ticker nebo None, měna, náklady % na stranu, popis)
STOCK_SLIP = config.STOCK_SLIPPAGE_PCT_PER_SIDE
FX = config.T212_FX_FEE_PCT_PER_SIDE
BIN = config.FEE_PCT_PER_SIDE + config.SLIPPAGE_PCT_PER_SIDE
CANDS = {
    "NAS100": [("SXRV.DE", "SXRVd_EQ", "EUR", STOCK_SLIP, "iShares NASDAQ 100 (Xetra, EUR)"),
               ("QQQ", "QQQ_EQ", "USD", STOCK_SLIP + FX, "Invesco QQQ (US, USD)")],
    "SP500": [("SXR8.DE", "SXR8d_EQ", "EUR", STOCK_SLIP, "iShares Core S&P 500 (Xetra, EUR)"),
              ("IVV", "IVV_EQ", "USD", STOCK_SLIP + FX, "iShares Core S&P 500 (US, USD)")],
    "US2000": [("XRS2.DE", "XRS2d_EQ", "EUR", STOCK_SLIP, "Xtrackers Russell 2000 (Xetra, EUR)"),
               ("ZPRR.DE", "ZPRRd_EQ", "EUR", STOCK_SLIP, "SPDR Russell 2000 (Xetra, EUR)"),
               ("IWM", None, "USD", STOCK_SLIP + FX, "iShares Russell 2000 (jen datový proxy, na T212 není)")],
    "XAUUSD": [("4GLD.DE", "4GLDd_EQ", "EUR", STOCK_SLIP, "Xetra-Gold (EUR)"),
               ("IAU", "IAU_EQ", "USD", STOCK_SLIP + FX, "iShares Gold Trust (US, USD)"),
               ("GC=F", None, "USD", BIN, "zlatý future (proxy pro Binance XAUUSDT, náklady Binance)")],
    "XAGUSD": [("XAD6.DE", "XAD6d_EQ", "EUR", STOCK_SLIP, "Xtrackers Physical Silver (EUR)"),
               ("SI=F", None, "USD", BIN, "stříbrný future (proxy pro Binance XAGUSDT, náklady Binance)")],
}
ORB_SYMS = [("QQQ", STOCK_SLIP + FX), ("IVV", STOCK_SLIP + FX), ("IAU", STOCK_SLIP + FX),
            ("SPY", STOCK_SLIP + FX), ("IWM", STOCK_SLIP + FX), ("GLD", STOCK_SLIP + FX), ("SLV", STOCK_SLIP + FX)]
BINANCE_SYMS = ["XAUUSDT", "XAGUSDT", "PAXGUSDT"]


# ---------------- 1. obchodovatelnost ----------------
def check_t212():
    items = json.loads((BOT / "t212_instruments.json").read_text(encoding="utf-8"))
    by = {i["ticker"]: i for i in items}
    out = {}
    for mkt, cs in CANDS.items():
        for y, t, ccy, _, desc in cs:
            if t:
                i = by.get(t)
                out[t] = {"found": bool(i), "name": i and i["name"], "ccy": i and i["currencyCode"],
                          "extendedHours": i and i.get("extendedHours"), "maxOpenQuantity": i and i.get("maxOpenQuantity")}
                p(f"T212 {mkt:7} {t:10} {'OK' if i else 'CHYBÍ'} {i and i['name']} {i and i['currencyCode']}")
    return out


def check_binance():
    out = {}
    for name, url in (("testnet", config.BINANCE_FUTURES_URL), ("ostrý", "https://fapi.binance.com")):
        try:
            d = requests.get(f"{url}/fapi/v1/exchangeInfo", timeout=15).json()
            syms = {s["symbol"]: s for s in d.get("symbols", [])}
            other = sorted(s["symbol"] for s in d.get("symbols", [])
                           if s.get("underlyingType") not in (None, "COIN") and s.get("status") == "TRADING")
            out[name] = {s: (syms[s]["status"] if s in syms else None) for s in BINANCE_SYMS}
            out[name + "_noncoin"] = other
            p(f"Binance {name} ({url}): " + ", ".join(f"{s}={out[name][s]}" for s in BINANCE_SYMS))
            p(f"  ne-kryptové kontrakty (underlyingType != COIN): {other[:60]}")
        except Exception as e:
            out[name] = f"chyba: {e}"
            p(f"Binance {name}: chyba {e}")
    return out


def binance_daily(symbol):
    r = requests.get("https://fapi.binance.com/fapi/v1/klines",
                     params={"symbol": symbol, "interval": "1d", "limit": 1500}, timeout=15)
    k = r.json()
    if not isinstance(k, list) or not k:
        return None
    df = pd.DataFrame(k, columns=list("tohlcvCqnVQi")[:12])
    df = pd.DataFrame({"Open": df["o"].astype(float).values, "High": df["h"].astype(float).values,
                       "Low": df["l"].astype(float).values, "Close": df["c"].astype(float).values,
                       "Volume": df["q"].astype(float).values},
                      index=pd.to_datetime(df["t"].values, unit="ms", utc=True))
    return df.iloc[:-1]  # bez rozjeté svíčky


# ---------------- 2.+3. denní data, podmínky, HIGH20 ----------------
def wilder_atr(df, n=14):
    pc = df["Close"].shift(1)
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - pc).abs(), (df["Low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def basics(df, cost_pct):
    last = df.iloc[-60:]
    atr = wilder_atr(df).iloc[-1]
    price = df["Close"].iloc[-1]
    turnover = float((last["Close"] * last["Volume"]).median())
    cost_r = 2 * cost_pct / 100 * price / (1.5 * atr)
    return {"price": round(float(price), 4), "atr_pct": round(float(atr / price * 100), 2),
            "median_turnover_60d": round(turnover), "zero_volume_days_60d": int((last["Volume"] <= 0).sum()),
            "cost_share_of_R_high20": round(float(cost_r), 3)}


def high20(df, cost_pct, lookback=20, every_day=False):
    d = df.copy()
    d["atr"] = wilder_atr(d)
    d["hh"] = d["Close"].rolling(lookback).max()
    rate = cost_pct / 100
    tr = []
    for i in range(max(lookback, 15), len(d) - 1):
        sig = every_day or d["Close"].iloc[i] >= d["hh"].iloc[i]
        if not sig:
            continue
        a = d["atr"].iloc[i]
        nx = d.iloc[i + 1]
        e = nx["Open"]
        sl, tp = e - 1.5 * a, e + 4.0 * a
        risk = e - sl
        if nx["Low"] <= sl:
            x = sl
        elif nx["High"] >= tp:
            x = tp
        else:
            x = nx["Close"]
        gross = (x - e) / risk
        net = gross - (e + x) * rate / risk
        tr.append({"day": str(d.index[i + 1].date()), "r_gross": gross, "r_net": net})
    return tr


def stats(tr):
    if not tr:
        return {"n": 0}
    r = np.array([t["r_net"] for t in tr])
    g = np.array([t["r_gross"] for t in tr])
    pos, neg = r[r > 0].sum(), -r[r < 0].sum()
    eq = np.cumsum(r)
    dd = float((np.maximum.accumulate(eq) - eq).max())
    return {"n": len(r), "win_pct": round(float((r > 0).mean() * 100), 1), "avg_r_net": round(float(r.mean()), 3),
            "avg_r_gross": round(float(g.mean()), 3), "sum_r_net": round(float(r.sum()), 1),
            "pf_net": round(float(pos / neg), 2) if neg > 0 else None, "max_dd_r": round(dd, 1),
            "t_stat": round(float(r.mean() / (r.std(ddof=1) / np.sqrt(len(r)))), 2) if len(r) > 2 else None}


def daily_section():
    out = {}
    for mkt, cs in CANDS.items():
        for y, t, ccy, cost, desc in cs:
            try:
                df = yf.Ticker(y).history(period="5y", interval="1d", auto_adjust=False)
                df = df[df["Volume"].notna() & (df["High"] > 0)]
                if len(df) < 200:
                    p(f"{mkt} {y}: málo dat ({len(df)})")
                    out[y] = {"error": f"málo dat {len(df)}"}
                    continue
                b = basics(df, cost)
                half = df.index[len(df) // 2]
                h = high20(df, cost)
                rec = {"market": mkt, "t212": t, "desc": desc, "cost_pct_side": cost, "days": len(df),
                       "from": str(df.index[0].date()), "basics": b,
                       "high20": stats(h),
                       "high20_last2y": stats([x for x in h if x["day"] >= str((df.index[-1] - pd.Timedelta(days=730)).date())]),
                       "high20_h1": stats([x for x in h if x["day"] < str(half.date())]),
                       "high20_h2": stats([x for x in h if x["day"] >= str(half.date())]),
                       "lb10": stats(high20(df, cost, 10)), "lb50": stats(high20(df, cost, 50)),
                       "every_day": stats(high20(df, cost, every_day=True))}
                out[y] = rec
                s = rec["high20"]
                p(f"\n{mkt} {y} ({desc}), {len(df)} dní od {rec['from']}, náklady {cost:.2f} %/str.")
                p(f"  podmínky: {b}")
                for k in ("high20", "high20_last2y", "high20_h1", "high20_h2", "lb10", "lb50", "every_day"):
                    p(f"  {k:14} {rec[k]}")
            except Exception as e:
                out[y] = {"error": str(e)}
                p(f"{mkt} {y}: chyba {e}")
    for s in ("XAUUSDT", "XAGUSDT"):
        try:
            df = binance_daily(s)
            if df is None or len(df) < 60:
                p(f"\nBinance {s}: denní data nejsou / málo ({0 if df is None else len(df)})")
                out[s] = {"error": "málo dat"}
                continue
            rec = {"market": s, "desc": "Binance perpetual", "cost_pct_side": BIN, "days": len(df),
                   "from": str(df.index[0].date()), "basics": basics(df, BIN),
                   "high20": stats(high20(df, BIN)), "every_day": stats(high20(df, BIN, every_day=True))}
            out[s] = rec
            p(f"\nBinance {s}: {len(df)} dní od {rec['from']}")
            for k in ("basics", "high20", "every_day"):
                p(f"  {k:14} {rec[k]}")
        except Exception as e:
            out[s] = {"error": str(e)}
            p(f"Binance {s}: chyba {e}")
    return out


# ---------------- 4. ORB15 ----------------
def orb_section():
    import bars
    import orb_shadow
    import orb_strategy as orb
    from cost_model import VenueCost
    out = {}
    for sym, cost in ORB_SYMS:
        try:
            tk = yf.Ticker(sym)
            b5 = orb_shadow._to_ny(bars.fetch_bars(sym, "5m", "60d", closed_only=True, yf_ticker=tk))
            d1 = orb_shadow._to_ny(bars.fetch_bars(sym, "1d", "6mo", closed_only=False, yf_ticker=tk))
            vc = VenueCost(0.0, 0.0, cost)
            days = sorted({ts.date() for ts in b5.index})[:-1]
            trades, skips = [], {}
            for d in days:
                s = orb.build_setup(sym, b5, d1, d)
                r = orb.simulate_day(s, b5, vc)
                trades.append(r)
                if r["status"] == "NO_SETUP":
                    k = (r.get("reason") or "?").split(" (")[0][:40]
                    skips[k] = skips.get(k, 0) + 1
            rec = {"days": len(days), "A": orb.summarize(trades, "variant_a"), "B": orb.summarize(trades, "variant_b"),
                   "skips": skips, "closed": sum(1 for t in trades if t["status"] == "CLOSED")}
            out[sym] = rec
            p(f"\nORB15 {sym}: {len(days)} dní, obchodů {rec['closed']}, vynechané: {skips}")
            p(f"  A: {rec['A']}")
            p(f"  B: {rec['B']}")
        except Exception as e:
            out[sym] = {"error": str(e)}
            p(f"ORB15 {sym}: chyba {e}")
    return out


for name, fn in (("t212", check_t212), ("binance", check_binance), ("daily", daily_section), ("orb", orb_section)):
    p(f"\n===== {name} =====")
    try:
        res[name] = fn()
    except Exception:
        p(traceback.format_exc())
        res[name] = {"error": traceback.format_exc()}

(OUT / "vysledky.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
(OUT / "vysledky.txt").write_text("\n".join(lines), encoding="utf-8")
p("\nHOTOVO, uloženo do", OUT)
