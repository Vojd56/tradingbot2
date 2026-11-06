"""Backtest kandidátů z výzkumu (1. 11. 2026). Nic nekupuje, nic v botovi nemění, Gemini nevolá.

Spuštění ze složky bota:  python _analyza_swing\\backtest_kandidati.py
Výstup: _analyza_swing\\kandidati.txt a kandidati.json

S1 IBS návrat k průměru na Xetra ETF   S2 přelom měsíce (TOM)   S3 krypto průraz s filtry (Binance)
S4 dual momentum ETF                     S5 PEAD na US akciích (jen cenová kostra, Gemini nejde testovat)
Pravidla podle výzkumu, k tomu varianty, které ukazují citlivost (vstup další den ráno, plná alokace).
Brána: >= 100 obchodů, PF >= 1,3, max pokles < 15 %, kladný výnos po nákladech; S4 se hodnotí proti držení indexu.
"""
import json
import math
import time
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parent
START = 5000.0
RISK = 0.005
SPLIT = pd.Timestamp("2020-01-01")
lines, RES = [], {}


def p(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    lines.append(s)


# ------------------------------------------------------------------ společné
def atr(d, n=14):
    pc = d["Close"].shift(1)
    tr = pd.concat([d["High"] - d["Low"], (d["High"] - pc).abs(), (d["Low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def rsi(c, n=14):
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def stats(trades, eq, start_eq=START, bench=None):
    out = {"obchody": len(trades)}
    if len(eq) < 2:
        return out
    yrs = max((eq.index[-1] - eq.index[0]).days / 365.25, 0.1)
    out["vynos_pct"] = round(100 * float(eq.iloc[-1] / start_eq - 1), 1)
    out["CAGR_pct"] = round(100 * float((eq.iloc[-1] / start_eq) ** (1 / yrs) - 1), 2)
    out["max_pokles_pct"] = round(100 * float((eq / eq.cummax() - 1).min()), 1)
    r = eq.pct_change().dropna()
    out["sharpe"] = round(float(r.mean() / r.std() * math.sqrt(252)), 2) if r.std() > 0 else None
    if trades:
        pnl = np.array([t["pnl"] for t in trades])
        R = np.array([t.get("R", np.nan) for t in trades], float)
        gw, gl = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
        out["PF"] = round(float(gw / gl), 2) if gl > 0 else None
        out["vyhry_pct"] = round(100 * float((pnl > 0).mean()), 1)
        out["prumer_obchod_pct"] = round(100 * float(np.mean([t["ret"] for t in trades])), 3)
        if np.isfinite(R).any():
            out["prumer_R"] = round(float(np.nanmean(R)), 3)
        for lab, cond in (("pred_2020", lambda t: pd.Timestamp(t["d0"]) < SPLIT),
                          ("od_2020", lambda t: pd.Timestamp(t["d0"]) >= SPLIT)):
            sub = [t for t in trades if cond(t)]
            if sub:
                ps = np.array([t["pnl"] for t in sub])
                w, l = ps[ps > 0].sum(), -ps[ps < 0].sum()
                out[lab] = {"n": len(sub), "PF": round(float(w / l), 2) if l > 0 else None,
                            "prumer_obchod_pct": round(100 * float(np.mean([t["ret"] for t in sub])), 3)}
    if bench is not None:
        b = bench.reindex(eq.index).ffill().dropna()
        if len(b) > 1:
            out["benchmark_vynos_pct"] = round(100 * float(b.iloc[-1] / b.iloc[0] - 1), 1)
            out["benchmark_CAGR_pct"] = round(100 * float((b.iloc[-1] / b.iloc[0]) ** (1 / yrs) - 1), 2)
            out["benchmark_max_pokles_pct"] = round(100 * float((b / b.cummax() - 1).min()), 1)
    return out


def gate(s):
    return (s.get("obchody", 0) >= 100 and (s.get("PF") or 0) >= 1.3 and s.get("max_pokles_pct", -100) > -15
            and s.get("vynos_pct", -1) > 0)


def report(name, s, note=""):
    s = dict(s)
    s["brana"] = gate(s)
    RES[name] = s
    p(f"\n== {name} == {note}")
    p("  " + ", ".join(f"{k}={v}" for k, v in s.items()))


def yf_daily(tickers, period="max"):
    import yfinance as yf
    out = {}
    for t in tickers:
        for attempt in range(3):
            try:
                d = yf.download(t, period=period, interval="1d", auto_adjust=True, progress=False,
                                multi_level_index=False)
                break
            except Exception as e:
                p("  stažení selhalo", t, e)
                time.sleep(3)
        else:
            continue
        if d is None or d.empty:
            p("  bez dat:", t)
            continue
        d = d[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Open", "High", "Low", "Close"])
        d = d[(d["Close"] > 0) & (d["High"] >= d["Low"])]
        d.index = pd.to_datetime(d.index).tz_localize(None).normalize()
        out[t] = d.astype(float)
    return out


# ------------------------------------------------------------------ S1 + S2: ETF, vstup na close
ETF_COST = 0.0005 + 0.0002  # skluz Xetra + rezerva na exekuci v 17:25 místo close


def sim_etf(data, strat, fill="close", sizing="risk"):
    """strat = 'S1' | 'S2'. fill: 'close' (17:25 ~ close) nebo 'open' (PC vypnuté, další ráno).
    sizing: 'risk' = pravidla výzkumu (0,5 % riziko + strop), 'plna' = pevný podíl účtu na ETF."""
    prep = {}
    for t, d in data.items():
        x = d.copy()
        x["ATR"] = atr(x)
        x["SMA200"] = x["Close"].rolling(200).mean()
        x["IBS"] = (x["Close"] - x["Low"]) / (x["High"] - x["Low"]).replace(0, np.nan)
        x["RSI"] = rsi(x["Close"])
        x["PH"] = x["High"].shift(1)
        m = x.index.to_period("M")
        x["POS_END"] = x.groupby(m).cumcount(ascending=False)  # 0 = poslední obchodní den měsíce
        x["POS_START"] = x.groupby(m).cumcount()               # 0 = první obchodní den měsíce
        prep[t] = x
    dates = sorted(set().union(*[set(x.index) for x in prep.values()]))
    dates = [d for d in dates if d >= pd.Timestamp("2011-01-01")]
    cap = 0.25 if strat == "S1" else 0.33
    stop_mult = 2.5 if strat == "S1" else 3.0
    cash, pos, trades, eq, pend = START, {}, [], [], {}
    last = {}

    def equity():
        return cash + sum(q["qty"] * last.get(t, q["entry"]) for t, q in pos.items())

    def sell(t, px, dt, why):
        nonlocal cash
        q = pos.pop(t)
        cash += q["qty"] * px * (1 - ETF_COST)
        pnl = q["qty"] * px * (1 - ETF_COST) - q["cost"]
        trades.append({"t": t, "d0": q["d0"], "d1": str(dt.date()), "pnl": pnl, "ret": pnl / q["cost"],
                       "R": pnl / q["risk"], "why": why})

    def buy(t, px, a, dt):
        nonlocal cash
        e = equity()
        stop = px - stop_mult * a
        if sizing == "risk":
            frac = min(cap, RISK / (stop_mult * a / px + 2 * ETF_COST))
        else:
            frac = cap
        val = min(frac * e, cash)
        if val < 50:
            return
        qty = val / (px * (1 + ETF_COST))
        cash -= val
        pos[t] = {"qty": qty, "entry": px, "stop": stop, "cost": val, "d0": str(dt.date()), "n": 0,
                  "risk": max(qty * (px - stop) + 2 * ETF_COST * val, 1e-9)}

    for dt in dates:
        for t, x in prep.items():
            if dt not in x.index:
                continue
            r = x.loc[dt]
            # ranní vstup (varianta open)
            if t in pend and t not in pos:
                a = pend.pop(t)
                buy(t, r["Open"], a, dt)
            q = pos.get(t)
            if q is not None:
                q["n"] += 1
                if r["Low"] <= q["stop"]:
                    sell(t, min(r["Open"], q["stop"]), dt, "stop")
                    q = None
            last[t] = r["Close"]
            if q is not None:
                why = None
                if strat == "S1":
                    if r["Close"] > r["PH"] or r["IBS"] > 0.8:
                        why = "zisk"
                    elif q["n"] >= 5:
                        why = "cas"
                else:
                    if r["POS_START"] == 2 and q["n"] >= 2:
                        why = "T+3"
                    elif r["Close"] < r["SMA200"] * 0.98:
                        why = "trend"
                    elif q["n"] >= 9:
                        why = "cas"
                if why:
                    sell(t, r["Close"], dt, why)
                continue
            if not (r["Close"] > r["SMA200"]) or not np.isfinite(r["ATR"]):
                continue
            sig = False
            if strat == "S1":
                sig = r["IBS"] < 0.15 and (r["High"] - r["Low"]) > 0.8 * r["ATR"]
            else:
                sig = r["POS_END"] == 4 and r["RSI"] < 75
            if sig:
                if fill == "close":
                    buy(t, r["Close"], r["ATR"], dt)
                else:
                    pend[t] = r["ATR"]
        pend = {t: a for t, a in pend.items() if t not in pos}
        eq.append((dt, equity()))
    for t in list(pos):
        sell(t, last[t], dates[-1], "konec")
    return trades, pd.Series([e for _, e in eq], index=[d for d, _ in eq])


# ------------------------------------------------------------------ S4: dual momentum
def sim_gem(data, every="M", sizing="plna", use_stop=False):
    risky = [t for t in ("SXR8.DE", "SXRV.DE", "EXS1.DE", "IS3N.DE", "4GLD.DE") if t in data]
    cash_t = "XEON.DE"
    px = pd.DataFrame({t: data[t]["Close"] for t in risky + ([cash_t] if cash_t in data else [])}).ffill()
    px = px.dropna()
    if len(px) < 300:
        return [], pd.Series(dtype=float)
    a = {t: atr(data[t]).reindex(px.index).ffill() for t in risky}
    sma = px.rolling(200).mean()
    r126 = px.shift(5) / px.shift(131) - 1
    r252 = px.shift(5) / px.shift(257) - 1
    raw252 = px / px.shift(252) - 1
    score = 0.5 * r126 + 0.5 * r252
    m = px.index.to_period("M")
    pos_end = pd.Series(px.groupby(m).cumcount(ascending=False).values, index=px.index)
    rebal = pos_end == 0 if every == "M" else (pos_end == 0) | (pos_end == 10)
    cash, hold, trades, eq = START, {}, [], []
    cash_ret = px[cash_t].pct_change().fillna(0) if cash_t in px else pd.Series(0.0, index=px.index)
    started = False
    for i, dt in enumerate(px.index):
        if i < 260:
            continue
        cash *= (1 + cash_ret.iloc[i]) if started else 1  # volná hotovost v XEON (úrok)
        started = True
        for t in list(hold):
            h = hold[t]
            c = px[t].iloc[i]
            if use_stop:
                h["stop"] = max(h["stop"], c - 3.5 * a[t].iloc[i])
                if c <= h["stop"]:
                    val = h["qty"] * c * (1 - ETF_COST)
                    cash += val
                    trades.append({"t": t, "d0": h["d0"], "pnl": val - h["cost"], "ret": val / h["cost"] - 1})
                    del hold[t]
        if rebal.iloc[i]:
            ok = [t for t in risky if np.isfinite(score[t].iloc[i])
                  and raw252[t].iloc[i] > (raw252[cash_t].iloc[i] if cash_t in px else 0)
                  and px[t].iloc[i] > sma[t].iloc[i]]
            top = sorted(ok, key=lambda t: score[t].iloc[i], reverse=True)[:2]
            for t in list(hold):
                if t not in top:
                    h = hold.pop(t)
                    val = h["qty"] * px[t].iloc[i] * (1 - ETF_COST)
                    cash += val
                    trades.append({"t": t, "d0": h["d0"], "pnl": val - h["cost"], "ret": val / h["cost"] - 1})
            e = cash + sum(h["qty"] * px[t].iloc[i] for t, h in hold.items())
            for t in top:
                if t in hold:
                    continue
                c = px[t].iloc[i]
                frac = 0.5 if sizing == "plna" else min(0.5, RISK / (3.5 * a[t].iloc[i] / c + 2 * ETF_COST))
                val = min(frac * e, cash)
                if val < 50:
                    continue
                cash -= val
                hold[t] = {"qty": val / (c * (1 + ETF_COST)), "cost": val, "d0": str(dt.date()),
                           "stop": c - 3.5 * a[t].iloc[i]}
        eq.append((dt, cash + sum(h["qty"] * px[t].iloc[i] for t, h in hold.items())))
    return trades, pd.Series([e for _, e in eq], index=[d for d, _ in eq])


# ------------------------------------------------------------------ S5: PEAD kostra
US5 = """AAPL MSFT NVDA AMZN GOOGL META TSLA AVGO AMD NFLX ORCL CRM ADBE INTC CSCO QCOM TXN MU AMAT LRCX KLAC MRVL
PLTR SNOW CRWD PANW ZS NET DDOG MDB SHOP UBER ABNB DASH COIN HOOD PYPL JPM BAC WFC GS MS C SCHW V MA AXP BLK UNH
LLY JNJ PFE MRK ABBV AMGN GILD VRTX REGN BMY ISRG DXCM TMO DHR ABT MDT WMT COST HD LOW TGT NKE SBUX MCD CMG LULU
DECK ELF XOM CVX COP OXY SLB HAL CAT DE BA GE HON LMT RTX UPS FDX DAL UAL CCL RCL DIS CMCSA T VZ TMUS KO PEP PG
PM MO NEE DUK LIN FCX NEM ROKU SPOT TTD APP RBLX DKNG CVNA ETSY EBAY ANET VRT CEG VST NRG ONON CELH INTU NOW WDAY
TEAM HUBS OKTA TWLO DOCU ZM F GM ENPH FSLR AXON DELL HPE IBM ACN TJX BKNG MAR HLT EXPE LYFT PINS SNAP CHWY BURL
ULTA WSM RH DPZ YUM KR DG DLTR CAH MCK HUM CI CVS ELV ON MPWR ADI NXPI WDC STX ALB CF NUE STLD URI PWR ETN EMR
ROK PH ITW MMM GD NOC HWM TDG""".split()
US_COST = 0.0020


def reaction_days(idx, ed_list):
    """Datum reakce: výsledky před 12:00 NY (BMO) -> ten den, jinak (AMC) -> další obchodní den."""
    out = set()
    for ts in ed_list:
        try:
            tl = ts.tz_convert("America/New_York") if ts.tzinfo else ts
        except Exception:
            tl = ts
        day = pd.Timestamp(tl.date())
        amc = tl.hour >= 12 if (tl.hour or tl.minute) else False
        j = idx.searchsorted(day)
        if j >= len(idx):
            continue
        if idx[j] != day:  # výsledky v nebursovní den -> reakce první obchodní den po něm
            out.add(idx[j])
            continue
        if amc and j + 1 < len(idx):
            out.add(idx[j + 1])
        else:
            out.add(idx[j])
    return out


def sim_pead(data, react, fill="close"):
    prep = {}
    for t, d in data.items():
        x = d.copy()
        x["ATR"] = atr(x)
        for n in (20, 50, 200):
            x[f"SMA{n}"] = x["Close"].rolling(n).mean()
        x["V20"] = x["Volume"].rolling(20).mean().shift(1)
        x["RET"] = x["Close"] / x["Close"].shift(1) - 1
        x["IBS"] = (x["Close"] - x["Low"]) / (x["High"] - x["Low"]).replace(0, np.nan)
        x["DV"] = (x["Close"] * x["Volume"]).rolling(20).mean()
        prep[t] = x
    dates = sorted(set().union(*[set(x.index) for x in prep.values()]))
    dates = [d for d in dates if d >= dates[0] + pd.Timedelta(days=300)]
    cash, pos, trades, eq, pend, last = START, {}, [], [], {}, {}

    def equity():
        return cash + sum(q["qty"] * last.get(t, q["entry"]) for t, q in pos.items())

    def sell(t, px, dt, why):
        nonlocal cash
        q = pos.pop(t)
        v = q["qty"] * px * (1 - US_COST)
        cash += v
        trades.append({"t": t, "d0": q["d0"], "d1": str(dt.date()), "pnl": v - q["cost"], "ret": v / q["cost"] - 1,
                       "R": (v - q["cost"]) / q["risk"], "why": why})

    def buy(t, px, s, dt):
        nonlocal cash
        stop = max(s["low"], px - 2 * s["atr"])
        if px <= stop or len(pos) >= 5:
            return
        e = equity()
        frac = min(0.15, RISK / ((px - stop) / px + 2 * US_COST))
        val = min(frac * e, cash)
        if val < 50:
            return
        cash -= val
        qty = val / (px * (1 + US_COST))
        pos[t] = {"qty": qty, "entry": px, "stop": stop, "r0": px - stop, "tp": px + 3.5 * (px - stop),
                  "cost": val, "d0": str(dt.date()), "n": 0, "trail_on": False,
                  "risk": max(qty * (px - stop) + 2 * US_COST * val, 1e-9)}

    for dt in dates:
        for t, x in prep.items():
            if dt not in x.index:
                continue
            r = x.loc[dt]
            if t in pend and t not in pos:
                s = pend.pop(t)
                if r["Open"] <= s["c"] + 0.75 * s["atr"]:
                    buy(t, r["Open"], s, dt)
            q = pos.get(t)
            last[t] = r["Close"]
            if q is not None:
                q["n"] += 1
                if r["Low"] <= q["stop"]:
                    sell(t, min(r["Open"], q["stop"]), dt, "stop")
                    continue
                if r["High"] >= q["tp"]:
                    sell(t, max(r["Open"], q["tp"]), dt, "TP")
                    continue
                if r["Close"] - q["entry"] >= 1.5 * q["r0"]:
                    q["trail_on"] = True
                if q["trail_on"]:
                    q["stop"] = max(q["stop"], r["Close"] - 2.5 * r["ATR"])
                if q["n"] >= 10:
                    sell(t, r["Close"], dt, "cas10")
                elif r["Close"] < r["SMA20"]:
                    sell(t, r["Close"], dt, "SMA20")
                continue
            if dt not in react.get(t, ()):
                continue
            if not (0.04 <= r["RET"] <= 0.18 and r["V20"] > 0 and r["Volume"] >= 2 * r["V20"]
                    and r["IBS"] >= 0.7 and r["Close"] > r["SMA50"] and r["Close"] > r["SMA200"]
                    and r["Close"] >= 15 and r["DV"] >= 50e6):
                continue
            s = {"low": r["Low"], "atr": r["ATR"], "c": r["Close"]}
            if fill == "close":
                buy(t, r["Close"], s, dt)
            else:
                pend[t] = s
        pend = {t: s for t, s in pend.items() if t not in pos}
        eq.append((dt, equity()))
    for t in list(pos):
        sell(t, last[t], dates[-1], "konec")
    return trades, pd.Series([e for _, e in eq], index=[d for d, _ in eq])


# ------------------------------------------------------------------ S3: krypto (Binance Futures)
CRYPTO = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
C_COST = 0.0007
FAPI = "https://fapi.binance.com"


def bn_klines(sym, interval, start_ms):
    import requests
    rows, cur = [], start_ms
    while True:
        r = requests.get(f"{FAPI}/fapi/v1/klines", params={"symbol": sym, "interval": interval,
                                                         "startTime": cur, "limit": 1500}, timeout=20)
        r.raise_for_status()
        k = r.json()
        if not k:
            break
        rows += k
        cur = k[-1][0] + 1
        if len(k) < 1500:
            break
        time.sleep(0.15)
    df = pd.DataFrame(rows, columns=list("tohlcv") + ["ct", "qv", "n", "tb", "tq", "x"])
    df = df[["t", "o", "h", "l", "c"]].astype(float)
    df.index = pd.to_datetime(df["t"], unit="ms")
    df.columns = ["t", "Open", "High", "Low", "Close"]
    return df.drop(columns="t")


def bn_funding(sym, start_ms):
    import requests
    rows, cur = [], start_ms
    while True:
        r = requests.get(f"{FAPI}/fapi/v1/fundingRate", params={"symbol": sym, "startTime": cur, "limit": 1000},
                         timeout=20)
        r.raise_for_status()
        k = r.json()
        if not k:
            break
        rows += k
        cur = k[-1]["fundingTime"] + 1
        if len(k) < 1000:
            break
        time.sleep(0.15)
    s = pd.Series([float(x["fundingRate"]) for x in rows],
                  index=pd.to_datetime([x["fundingTime"] for x in rows], unit="ms"))
    return s[~s.index.duplicated()]


def sim_crypto(D, H, F, variant):
    """variant: 'S3' (výzkum: SMA50 + volatilita + funding, držení až 48 h, trailing)
                'S3-bez-fundingu', 'HIGH20' (dnešní bot: bez filtrů, výstup po 24 h)."""
    prep = {}
    for s, d in D.items():
        x = d.copy()
        x["ATR"] = atr(x)
        x["HH20"] = x["High"].shift(1).rolling(20).max()
        x["SMA50"] = x["Close"].rolling(50).mean()
        ap = x["ATR"] / x["Close"]
        x["VOLOK"] = ap > ap.rolling(60).median()
        prep[s] = x
    days = sorted(set().union(*[set(x.index) for x in prep.values()]))
    days = [d for d in days if d >= pd.Timestamp("2020-06-01")]
    eqv, pos, trades, eq = 10000.0, {}, [], []
    for di, day in enumerate(days):
        # 1) správa otevřených pozic během dne (hodinové svíčky)
        for s in list(pos):
            q = pos[s]
            h = H[s]
            hb = h[(h.index >= day) & (h.index < day + pd.Timedelta(days=1))]
            closed = False
            for ts, b in hb.iterrows():
                if b["Low"] <= q["stop"]:
                    px = min(b["Open"], q["stop"])
                    closed = True
                    break
            fr = F.get(s)
            if fr is not None:  # funding za dobu držení v tomto dni
                f = fr[(fr.index >= day) & (fr.index < day + pd.Timedelta(days=1))]
                if closed:
                    f = f[f.index <= ts]
                q["funding"] += float(f.sum()) * q["qty"] * q["entry"]
            if closed:
                pnl = q["qty"] * (px - q["entry"]) - C_COST * q["qty"] * (px + q["entry"]) - q["funding"]
                eqv += pnl
                trades.append({"t": s, "d0": q["d0"], "pnl": pnl, "ret": pnl / (q["qty"] * q["entry"]),
                               "R": pnl / q["risk"], "why": "stop"})
                del pos[s]
        # 2) na konci dne (00:00 UTC další den) rozhodnutí o výstupu / trailing
        for s in list(pos):
            q = pos[s]
            x = prep[s]
            if day not in x.index:
                continue
            c = x.loc[day, "Close"]
            q["days"] += 1
            exit_now = False
            if variant == "HIGH20":
                exit_now = q["days"] >= 1
            else:
                if q["days"] >= 2 or (q["days"] == 1 and c < q["entry"]):
                    exit_now = True
                else:
                    q["stop"] = max(q["stop"], c - 1.5 * x.loc[day, "ATR"])
            if exit_now:
                px = c
                pnl = q["qty"] * (px - q["entry"]) - C_COST * q["qty"] * (px + q["entry"]) - q["funding"]
                eqv += pnl
                trades.append({"t": s, "d0": q["d0"], "pnl": pnl, "ret": pnl / (q["qty"] * q["entry"]),
                               "R": pnl / q["risk"], "why": "cas"})
                del pos[s]
        # 3) signály ze svíčky 'day' -> vstup na open dalšího dne (00:05 UTC)
        if di + 1 < len(days):
            nxt = days[di + 1]
            for s, x in prep.items():
                if s in pos or day not in x.index or nxt not in x.index or len(pos) >= 5:
                    continue
                r = x.loc[day]
                if not (np.isfinite(r["HH20"]) and r["Close"] >= r["HH20"]):
                    continue
                if variant != "HIGH20":
                    if not (r["Close"] > r["SMA50"] and bool(r["VOLOK"])):
                        continue
                    if variant == "S3":
                        fr = F.get(s)
                        if fr is not None:
                            prev = fr[fr.index <= nxt]
                            if len(prev) and prev.iloc[-1] >= 0.0003:
                                continue
                entry = x.loc[nxt, "Open"]
                stop = entry - 1.5 * r["ATR"]
                if entry <= stop:
                    continue
                qty = RISK * eqv / ((entry - stop) + 2 * C_COST * entry)
                qty = min(qty, 0.2 * eqv * 3 / entry)  # strop nominálu (páka do 3x při 20 % marže)
                pos[s] = {"qty": qty, "entry": entry, "stop": stop, "d0": str(nxt.date()), "days": 0,
                          "funding": 0.0, "risk": qty * (entry - stop) + 2 * C_COST * qty * entry}
        eq.append((day, eqv))
    return trades, pd.Series([e for _, e in eq], index=[d for d, _ in eq])


# ------------------------------------------------------------------ main
def run(name, fn, *args, note="", start_eq=START, bench=None, **kw):
    try:
        tr, eq = fn(*args, **kw)
        report(name, stats(tr, eq, start_eq, bench), note)
        return tr
    except Exception:
        p(f"\n== {name} == CHYBA\n" + traceback.format_exc())
        return []


def main():
    p("Backtest kandidátů z výzkumu –", time.strftime("%Y-%m-%d %H:%M"))
    etf = yf_daily(["SXRV.DE", "SXR8.DE", "EXS1.DE", "EXW1.DE", "IS3N.DE", "4GLD.DE", "XEON.DE"])
    p("ETF data:", {t: f"{d.index[0].date()}..{d.index[-1].date()}" for t, d in etf.items()})
    bench = etf["SXR8.DE"]["Close"] if "SXR8.DE" in etf else None
    s1 = {t: etf[t] for t in ("SXRV.DE", "SXR8.DE", "EXS1.DE", "EXW1.DE") if t in etf}
    s2 = {t: etf[t] for t in ("SXR8.DE", "SXRV.DE", "EXS1.DE") if t in etf}
    run("S1 IBS (výzkum: vstup 17:25, riziko 0,5 %)", sim_etf, s1, "S1", bench=bench)
    run("S1 IBS – vstup až další ráno", sim_etf, s1, "S1", fill="open", bench=bench)
    run("S1 IBS – plná alokace 25 % na ETF", sim_etf, s1, "S1", sizing="plna", bench=bench)
    run("S2 přelom měsíce (výzkum)", sim_etf, s2, "S2", bench=bench)
    run("S2 přelom měsíce – plná alokace 33 % na ETF", sim_etf, s2, "S2", sizing="plna", bench=bench)
    run("S4 dual momentum – měsíčně, 2x50 %", sim_gem, etf, bench=bench, note="(srovnej s benchmarkem)")
    run("S4 dual momentum – každé 2 týdny", sim_gem, etf, every="2W", bench=bench)
    run("S4 dual momentum – riziko 0,5 % + stop 3,5 ATR (výzkum)", sim_gem, etf, sizing="risk", use_stop=True,
        bench=bench)
    # S5
    try:
        import yfinance as yf
        us = yf_daily(US5, period="5y")
        react = {}
        for t in us:
            try:
                ed = yf.Ticker(t).get_earnings_dates(limit=24)
                if ed is not None and len(ed):
                    react[t] = reaction_days(us[t].index, list(ed.index))
            except Exception:
                pass
        p(f"\nS5 data: {len(us)} akcií, výsledky u {len(react)}")
        run("S5 PEAD kostra (vstup na close dne reakce)", sim_pead, us, react)
        run("S5 PEAD kostra – vstup další ráno", sim_pead, us, react, fill="open")
    except Exception:
        p("S5 CHYBA\n" + traceback.format_exc())
    # S3
    try:
        start_ms = int(pd.Timestamp("2020-01-01").timestamp() * 1000)
        D, H, F = {}, {}, {}
        for s in CRYPTO:
            D[s] = bn_klines(s, "1d", start_ms)
            H[s] = bn_klines(s, "1h", start_ms)
            F[s] = bn_funding(s, start_ms)
            p(f"  {s}: {len(D[s])} dní, {len(H[s])} hodin, {len(F[s])} funding")
        btc = D["BTCUSDT"]["Close"]
        run("S3 krypto průraz + filtry (výzkum)", sim_crypto, D, H, F, "S3", start_eq=10000.0, bench=btc)
        run("S3 bez filtru fundingu", sim_crypto, D, H, F, "S3-bez-fundingu", start_eq=10000.0, bench=btc)
        run("HIGH20 jak běží dnes (bez filtrů, 24 h)", sim_crypto, D, H, F, "HIGH20", start_eq=10000.0, bench=btc)
    except Exception:
        p("S3 CHYBA\n" + traceback.format_exc())
    (OUT / "kandidati.txt").write_text("\n".join(lines), encoding="utf-8")
    (OUT / "kandidati.json").write_text(json.dumps(RES, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    p("\nHotovo:", OUT / "kandidati.txt")


if __name__ == "__main__":
    main()
