"""S0 backtest swingové strategie akcií (1. 11. 2026). Nic nekupuje, nic v botovi nemění, Gemini nevolá.

Spuštění ze složky bota:  python _analyza_swing\\backtest_swing.py
Výstup: _analyza_swing\\vysledky.txt a vysledky.json

Pravidla podle plánu https://claude.ai/artifact/9ubt4M46idKSVsEvoWuPxd:
- Vstup A (zpráva / PEAD proxy): den s pohybem nebo gapem >= 4 %, objem >= 2x průměru 20 dní, závěr v horní
  polovině svíčky. Nákup další seanci na open, jen když open >= střed svíčky dne zprávy a neuteklo > 0,75 ATR.
  Stop = max(low dne zprávy, entry - 3 ATR). Gemini se v backtestu simulovat nedá (je to horní mez bez jeho filtru).
- Vstup B (momentum ze studie): 12m výnos bez posl. 5 dní / volatilita, průřezové Z >= 1,75,
  close > SMA20 > SMA60, (close - SMA20)/ATR v [-0,5; 1,5]. Nákup další seanci na open, stop entry - 3 ATR.
- Výstupy: stop (gap pod stop = open), chandelier max high od vstupu - 2,75 ATR (jen nahoru),
  B: close < SMA60 nebo Z < 0,5 -> prodej další open; 20 dní bez nového maxima -> prodej;
  A: 10 dní bez +1R -> prodej. Cooldown 5 seancí po stopu.
- Velikost: 0,5 % účtu riziko vč. nákladů, strop pozice 20 % účtu, max 10 pozic. Start 5 000 EUR.
- Náklady: US 0,15 % FX + 0,05 % skluz za stranu; EU 0,05 % skluz; Paříž +0,3 %, Madrid +0,2 % daň z nákupu.
- Varianty: A / B / A+B, nákup najednou / po třetinách, filtr trhu (index nad SMA200) vyp/zap, Hurst > 0,55 pro B,
  A jen kolem výsledků (pokud se podaří stáhnout data výsledků).
Pozor: vesmír je dnešní seznam firem -> zkreslení přeživších (výsledky spíš nadhodnocené, hlavně u B).
"""
import json
import math
import sys
import time
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parent

US = """AAPL MSFT NVDA AMZN GOOGL META TSLA AVGO AMD NFLX ORCL CRM ADBE INTC CSCO QCOM TXN MU AMAT LRCX KLAC MRVL ARM
SMCI PLTR SNOW CRWD PANW ZS NET DDOG MDB SHOP UBER ABNB DASH COIN HOOD SOFI AFRM PYPL XYZ JPM BAC WFC GS MS C SCHW V
MA AXP BLK UNH LLY JNJ PFE MRK ABBV AMGN GILD VRTX REGN BMY MRNA ISRG DXCM TMO DHR ABT MDT WMT COST HD LOW TGT NKE
SBUX MCD CMG LULU DECK ELF XOM CVX COP OXY SLB HAL CAT DE BA GE GEV HON LMT RTX UPS FDX DAL UAL CCL RCL DIS CMCSA T
VZ TMUS KO PEP PG PM MO NEE DUK LIN FCX NEM TSM BABA PDD RIVN ROKU SPOT TTD APP RBLX U DKNG CVNA ETSY EBAY ANET VRT
CEG VST NRG ONON CELH MSTR INTU NOW WDAY TEAM HUBS OKTA TWLO DOCU ZM F GM NU ENPH FSLR AXON DELL HPE IBM ACN NVO
TJX BKNG MAR HLT EXPE LYFT PINS SNAP CHWY BURL ULTA WSM RH DPZ YUM KR DG DLTR CAH MCK HUM CI CVS ELV MRVL ON MPWR
ADI NXPI WDC STX ALB MP CF NUE STLD URI PWR ETN EMR ROK PH ITW MMM GD NOC HWM TDG""".split()
EU = """SAP.DE SIE.DE ALV.DE DTE.DE BAS.DE BAYN.DE BMW.DE MBG.DE VOW3.DE ADS.DE IFX.DE MUV2.DE DBK.DE RWE.DE EOAN.DE
DHL.DE HEN3.DE MRK.DE FRE.DE BEI.DE CON.DE HNR1.DE DTG.DE AIXA.DE ZAL.DE RHM.DE MTX.DE PUM.DE CBK.DE SHL.DE ENR.DE
MC.PA OR.PA TTE.PA SAN.PA AIR.PA SU.PA BNP.PA AI.PA EL.PA KER.PA RMS.PA SAF.PA DG.PA CS.PA GLE.PA ACA.PA RI.PA
CAP.PA STMPA.PA ORA.PA RNO.PA VIE.PA HO.PA DSY.PA ASML.AS ADYEN.AS INGA.AS PRX.AS HEIA.AS AD.AS PHIA.AS WKL.AS
ASM.AS BESI.AS SAN.MC BBVA.MC ITX.MC IBE.MC TEF.MC REP.MC AMS.MC CABK.MC""".split()
INDEX = {"US": "^GSPC", "EU": "^STOXX"}
BENCH = ["SPY", "EXSA.DE"]

START_EQ = 5000.0
RISK = 0.005
POS_CAP = 0.20
MAX_POS = 10
TEST_DAYS = 504  # ~2 roky


def region(t):
    return "EU" if "." in t else "US"


def side_cost(t):
    return 0.0005 if region(t) == "EU" else 0.0020


def buy_tax(t):
    return 0.003 if t.endswith(".PA") else 0.002 if t.endswith(".MC") else 0.0


lines = []


def p(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    lines.append(s)


# ---------------------------------------------------------------- data
def load_data():
    import yfinance as yf
    tick = US + EU + list(INDEX.values()) + BENCH + ["EURUSD=X"]
    frames = {}
    for i in range(0, len(tick), 60):
        chunk = tick[i:i + 60]
        for attempt in range(3):
            try:
                df = yf.download(chunk, period="4y", interval="1d", auto_adjust=True, group_by="ticker",
                                 threads=True, progress=False)
                break
            except Exception as e:
                p("  stažení selhalo, zkouším znovu:", e)
                time.sleep(5)
        else:
            continue
        for t in chunk:
            try:
                d = df[t].dropna(subset=["Open", "High", "Low", "Close"])
                d = d[(d["Close"] > 0) & (d["High"] >= d["Low"])]
                if len(d) > 300:
                    d.index = pd.to_datetime(d.index).tz_localize(None).normalize()
                    frames[t] = d[["Open", "High", "Low", "Close", "Volume"]].astype(float)
            except Exception:
                pass
    earnings = {}
    for t in US + EU:
        if t not in frames:
            continue
        try:
            ed = yf.Ticker(t).get_earnings_dates(limit=16)
            if ed is not None and len(ed):
                earnings[t] = sorted({pd.Timestamp(x).tz_localize(None).normalize() for x in ed.index})
        except Exception:
            pass
    return frames, earnings


# ---------------------------------------------------------------- indicators
def hurst_rs(r):
    r = np.asarray(r, float)
    if len(r) < 126 or not np.all(np.isfinite(r)):
        return np.nan
    xs, ys = [], []
    for n in (16, 32, 63, 126):
        vals = []
        for k in range(len(r) // n):
            seg = r[len(r) - (k + 1) * n: len(r) - k * n]
            y = np.cumsum(seg - seg.mean())
            s = seg.std() + 1e-12
            vals.append((y.max() - y.min()) / s)
        xs.append(math.log(n))
        ys.append(math.log(np.mean(vals) + 1e-12))
    return float(np.polyfit(xs, ys, 1)[0])


def prep(d, fx_eurusd, is_us):
    d = d.copy()
    pc = d["Close"].shift(1)
    tr = pd.concat([d["High"] - d["Low"], (d["High"] - pc).abs(), (d["Low"] - pc).abs()], axis=1).max(axis=1)
    d["ATR"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    d["SMA20"] = d["Close"].rolling(20).mean()
    d["SMA60"] = d["Close"].rolling(60).mean()
    d["VOL20"] = d["Volume"].rolling(20).mean().shift(1)
    lr = np.log(d["Close"] / pc)
    d["LR"] = lr
    sig = lr.rolling(63).std()
    d["MOMRAW"] = lr.shift(5).rolling(247).sum() / (math.sqrt(247) * sig)
    fx = 1.0 if not is_us else 1.0 / fx_eurusd.reindex(d.index).ffill().bfill()
    d["FX"] = fx  # násobek lokální měna -> EUR (US: USD -> EUR)
    dv = d["Close"] * d["Volume"] * (1.0 if not is_us else 1.0)  # USD nebo EUR, oboje ~ srovnatelné
    d["ADV"] = dv.rolling(20).mean()
    d["PREVC"] = pc
    return d


def build(frames, earnings):
    fx = frames.get("EURUSD=X")
    fx_s = fx["Close"] if fx is not None else pd.Series(1.08, index=frames["SPY"].index)
    data = {}
    for t in US + EU:
        if t in frames:
            data[t] = prep(frames[t], fx_s, region(t) == "US")
    mom = pd.DataFrame({t: d["MOMRAW"] for t, d in data.items()})
    mom = mom.where(mom.notna())
    mu = mom.mean(axis=1)
    sd = mom.std(axis=1)
    z = mom.sub(mu, axis=0).div(sd, axis=0).clip(-3.5, 3.5)
    cnt = mom.count(axis=1)
    z[cnt < 30] = np.nan
    for t, d in data.items():
        d["ZMOM"] = z[t].reindex(d.index)
    idx = {}
    for reg, sym in INDEX.items():
        s = frames.get(sym)
        if s is None and reg == "EU":
            s = frames.get("EXSA.DE")
        if s is not None:
            c = s["Close"]
            idx[reg] = (c > c.rolling(200).mean())
    return data, idx


# ---------------------------------------------------------------- simulation
class Arr(dict):
    """Sloupce DataFrame jako numpy pole + index (rychlý přístup v simulaci)."""
    def __init__(self, d):
        super().__init__({c: d[c].to_numpy(dtype=float) for c in d.columns})
        self.index = d.index


def simulate(data, idx, earnings, cfg):
    dates = sorted(set().union(*[set(d.index) for d in data.values()]))
    dates = dates[-TEST_DAYS:]
    start = dates[0]
    rows = {t: {dt: i for i, dt in enumerate(d.index)} for t, d in data.items()}
    data = {t: Arr(d) for t, d in data.items()}  # numpy pole kvůli rychlosti
    cash = START_EQ
    pos = {}       # t -> dict
    pending = {}   # t -> signal
    cooldown = {}  # t -> datum, od kdy lze znovu
    trades = []
    eq_curve = []
    last_close = {}

    def equity():
        v = cash
        for t, q in pos.items():
            v += q["qty"] * last_close.get(t, q["entry"]) * data[t]["FX"][q["i_last"]] * (1 - side_cost(t))
        return v

    def close_pos(t, price, i, reason):
        nonlocal cash
        q = pos.pop(t)
        fxv = data[t]["FX"][i]
        proceeds = q["qty"] * price * fxv * (1 - side_cost(t))
        cash += proceeds
        pnl = proceeds - q["cost"]
        trades.append({"t": t, "type": q["type"], "reg": region(t), "entry_date": str(q["d0"].date()),
                       "exit_date": str(data[t].index[i].date()), "pnl": pnl, "R": pnl / q["risk_eur"],
                       "reason": reason, "tranches": q["n_tr"], "days": i - q["i0"]})
        if reason == "stop":
            k = min(i + 5, len(data[t].index) - 1)
            cooldown[t] = data[t].index[k]

    def buy(t, i, price, stop, risk_eur):
        nonlocal cash
        fxv = data[t]["FX"][i]
        per_share = (price - stop) * fxv + price * fxv * (2 * side_cost(t) + buy_tax(t))
        if per_share <= 0:
            return 0.0, 0.0
        qty = risk_eur / per_share
        eq = equity()
        cap_cost = POS_CAP * eq - (pos[t]["cost"] if t in pos else 0.0)
        unit = price * fxv * (1 + side_cost(t) + buy_tax(t))
        qty = min(qty, max(cap_cost, 0) / unit, max(cash, 0) / unit)
        if qty * unit < 20:  # méně než 20 EUR nemá smysl
            return 0.0, 0.0
        cash -= qty * unit
        return qty, qty * unit

    for dt in dates:
        todays = [t for t in data if dt in rows[t]]
        # 1) příkazy na open
        for t in todays:
            i = rows[t][dt]
            d = data[t]
            o = d["Open"][i]
            q = pos.get(t)
            if q is not None:
                q["i_last"] = i
                if q.get("exit_next"):
                    close_pos(t, o, i, q["exit_next"])
                    continue
                if q.get("add_next"):
                    budget = q["risk_eur"] / 3
                    if o > q["stop"]:
                        qty, cost = buy(t, i, o, q["stop"], budget)
                        if qty > 0:
                            q["qty"] += qty
                            q["cost"] += cost
                            q["n_tr"] += 1
                    q["add_next"] = False
        # nové vstupy: seřadit podle priority
        cands = [(pending[t]["score"], t) for t in todays if t in pending and t not in pos]
        for _, t in sorted(cands, reverse=True):
            s = pending.pop(t)
            i = rows[t][dt]
            d = data[t]
            o = d["Open"][i]
            if len(pos) >= MAX_POS:
                continue
            if o > s["c"] + 0.75 * s["atr"]:
                continue
            if s["type"] == "A":
                if o < s["mid"]:
                    continue
                stop = max(s["low"], o - 3 * s["atr"])
            else:
                stop = o - 3 * s["atr"]
            if o <= stop:
                continue
            risk_eur = equity() * RISK * s["risk_mult"]
            first = risk_eur / 3 if cfg["thirds"] else risk_eur
            qty, cost = buy(t, i, o, stop, first)
            if qty <= 0:
                continue
            pos[t] = {"type": s["type"], "entry": o, "stop": stop, "stop0": stop, "qty": qty, "cost": cost,
                      "risk_eur": risk_eur, "i0": i, "d0": dt, "hh": o, "hc": o, "last_high_i": i, "n_tr": 1,
                      "i_last": i, "exit_next": None, "add_next": False}
        pending = {t: s for t, s in pending.items() if t not in todays}  # signál platí jen 1 seanci
        # 2) intradenní stop a závěr dne
        for t in todays:
            i = rows[t][dt]
            d = data[t]
            last_close[t] = d["Close"][i]
            q = pos.get(t)
            if q is None:
                continue
            o, h, l, c = d["Open"][i], d["High"][i], d["Low"][i], d["Close"][i]
            if l <= q["stop"]:
                close_pos(t, min(o, q["stop"]), i, "stop")
                continue
            if h > q["hh"]:
                q["hh"] = h
            if c > q["hc"]:
                q["hc"] = c
                q["last_high_i"] = i
            atr = d["ATR"][i]
            q["stop"] = max(q["stop"], q["hh"] - 2.75 * atr)
            r0 = q["entry"] - q["stop0"]
            held = i - q["i0"]
            if q["type"] == "B" and (c < d["SMA60"][i] or (d["ZMOM"][i] < 0.5)):
                q["exit_next"] = "trend"
            elif i - q["last_high_i"] >= 20:
                q["exit_next"] = "cas20"
            elif q["type"] == "A" and held >= 10 and q["hc"] < q["entry"] + r0:
                q["exit_next"] = "cas10"
            elif cfg["thirds"] and held >= 1 and q["n_tr"] < 3:
                if q["n_tr"] == 1 and c > q["entry"] and l <= q["entry"] + 0.5 * atr:
                    q["add_next"] = True
                elif q["n_tr"] == 2 and c >= q["entry"] + r0 and c > d["SMA20"][i] > d["SMA60"][i]:
                    q["add_next"] = True
        # 3) nové signály na close
        for t in todays:
            if t in pos or (t in cooldown and dt < cooldown[t]):
                continue
            i = rows[t][dt]
            if i < 260:
                continue
            d = data[t]
            c, h, l, o = d["Close"][i], d["High"][i], d["Low"][i], d["Open"][i]
            atr, pc = d["ATR"][i], d["PREVC"][i]
            px_eur = c * d["FX"][i]
            if not (px_eur >= 5 and d["ADV"][i] >= 10e6 and 0.008 <= atr / c <= 0.06):
                continue
            reg = region(t)
            up = bool(idx.get(reg, pd.Series(dtype=bool)).get(dt, True)) if cfg["filter"] else True
            sig = None
            if "A" in cfg["types"]:
                move = max(o / pc - 1, c / pc - 1)
                vr = d["Volume"][i] / d["VOL20"][i] if d["VOL20"][i] > 0 else 0
                rng = h - l
                if move >= 0.04 and vr >= 2 and rng > 0 and (c - l) / rng >= 0.5:
                    ok = True
                    if cfg.get("earn_only"):
                        ed = earnings.get(t, [])
                        ok = any(abs((dt - e).days) <= 1 for e in ed)
                    if ok:
                        sig = {"type": "A", "c": c, "atr": atr, "low": l, "mid": (h + l) / 2,
                               "score": 100 + vr * move * 10, "risk_mult": 1.0 if up else 0.5}
            if sig is None and "B" in cfg["types"] and up:
                z = d["ZMOM"][i]
                s20, s60 = d["SMA20"][i], d["SMA60"][i]
                if z >= 1.75 and c > s20 > s60 and -0.5 <= (c - s20) / atr <= 1.5:
                    ok = True
                    if cfg.get("hurst"):
                        hval = hurst_rs(d["LR"][i - 125:i + 1])
                        ok = hval > 0.55
                    ed = earnings.get(t, [])
                    if ok and ed and any(0 <= (e - dt).days <= 7 for e in ed):
                        ok = False  # 5 seancí před výsledky nekupovat
                    if ok:
                        sig = {"type": "B", "c": c, "atr": atr, "score": z, "risk_mult": 1.0}
            if sig:
                pending[t] = sig
        eq_curve.append((dt, equity()))
    # uzavřít zbytek na posledním close
    for t in list(pos):
        i = pos[t]["i_last"]
        close_pos(t, data[t]["Close"][i], i, "konec")
    eq = pd.Series([e for _, e in eq_curve], index=[d for d, _ in eq_curve])
    return trades, eq


def stats(trades, eq):
    n = len(trades)
    if n == 0:
        return {"obchody": 0}
    R = np.array([t["R"] for t in trades])
    pnl = np.array([t["pnl"] for t in trades])
    gw, gl = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    dd = (eq / eq.cummax() - 1).min()
    half = len(eq) // 2
    r1 = eq.iloc[half - 1] / eq.iloc[0] - 1
    r2 = eq.iloc[-1] / eq.iloc[half - 1] - 1
    return {"obchody": n, "vyhry_pct": round(100 * (R > 0).mean(), 1), "prumer_R": round(float(R.mean()), 3),
            "median_R": round(float(np.median(R)), 3), "PF": round(gw / gl, 2) if gl > 0 else None,
            "vynos_pct": round(100 * (eq.iloc[-1] / START_EQ - 1), 1), "max_pokles_pct": round(100 * dd, 1),
            "1_pulka_pct": round(100 * r1, 1), "2_pulka_pct": round(100 * r2, 1),
            "nejlepsi_R": round(R.max(), 2), "nejhorsi_R": round(R.min(), 2),
            "prum_dni": round(np.mean([t["days"] for t in trades]), 1)}


def gate(s):
    return (s.get("obchody", 0) >= 100 and (s.get("PF") or 0) >= 1.3 and s.get("max_pokles_pct", -100) > -15
            and s.get("vynos_pct", -1) > 0)


def main():
    p("S0 backtest swingové strategie –", time.strftime("%Y-%m-%d %H:%M"))
    frames, earnings = load_data()
    p(f"Data: {len([t for t in US + EU if t in frames])}/{len(US + EU)} akcií, výsledky pro {len(earnings)} akcií")
    data, idx = build(frames, earnings)
    res = {"data_akcie": len(data), "vysledky_akcie": len(earnings), "varianty": {}}
    variants = []
    for types in ("A", "B", "AB"):
        for thirds in (False, True):
            for flt in (False, True):
                variants.append({"name": f"{types}{'-tretiny' if thirds else '-najednou'}{'-filtr' if flt else ''}",
                                 "types": types, "thirds": thirds, "filter": flt})
    variants.append({"name": "B-najednou-hurst", "types": "B", "thirds": False, "filter": False, "hurst": True})
    if len(earnings) >= 50:
        variants.append({"name": "A-najednou-jen-vysledky", "types": "A", "thirds": False, "filter": False,
                         "earn_only": True})
    for v in variants:
        try:
            tr, eq = simulate(data, idx, earnings, v)
            s = stats(tr, eq)
            s["brana1"] = gate(s)
            by = {}
            for key in ("type", "reg", "reason"):
                for val in sorted({t[key] for t in tr}):
                    sub = [t for t in tr if t[key] == val]
                    R = np.array([t["R"] for t in sub])
                    by[f"{key}={val}"] = {"n": len(sub), "prumer_R": round(float(R.mean()), 3),
                                          "soucet_R": round(float(R.sum()), 1)}
            s["rozpad"] = by
            res["varianty"][v["name"]] = s
            p(f"\n== {v['name']} ==")
            p("  " + ", ".join(f"{k}={val}" for k, val in s.items() if k != "rozpad"))
            for k, val in by.items():
                p(f"    {k}: {val}")
            if v["name"] in ("AB-najednou", "AB-tretiny"):
                pd.DataFrame(tr).to_csv(OUT / f"obchody_{v['name']}.csv", index=False)
        except Exception:
            p(f"\n== {v['name']} == CHYBA\n" + traceback.format_exc())
    # benchmark
    for b in BENCH:
        if b in frames:
            c = frames[b]["Close"].iloc[-TEST_DAYS:]
            r = c.iloc[-1] / c.iloc[0] - 1
            dd = (c / c.cummax() - 1).min()
            res[f"benchmark_{b}"] = {"vynos_pct": round(100 * r, 1), "max_pokles_pct": round(100 * dd, 1)}
            p(f"\nBenchmark {b} (v místní měně): výnos {100 * r:.1f} %, max pokles {100 * dd:.1f} %")
    (OUT / "vysledky.txt").write_text("\n".join(lines), encoding="utf-8")
    (OUT / "vysledky.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    p("\nHotovo:", OUT / "vysledky.txt")


if __name__ == "__main__":
    main()
