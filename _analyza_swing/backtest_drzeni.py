# -*- coding: utf-8 -*-
"""Backtest kandidatu z vyzkumu "Swingove obchodovani akcii s LLM" (2. 11. 2026). Drzeni ~20 dni (5-40),
nikdy nakup a prodej ve stejny den. Nic nekupuje, nic v botovi nemeni, Gemini nevola.

Spusteni ze slozky bota:  python _analyza_swing\\backtest_drzeni.py
Vystup: _analyza_swing\\_analyza_drzeni\\drzeni.txt (+ obchody_*.csv)

A2 EU-HIGH252: prurazy 252denniho maxima po zuzeni volatility (Xetra + Amsterdam), cena 0,10 % tam a zpet.
A4 PULLBACK:   navrat v trendu do zony EMA20-50 s reverzni svickou, cil +2,5R.
Oba na EU (Xetra/AMS, naklad 0,05 %/strana) i US (S&P 500+400+600, naklad 0,20 %/strana: FX 0,15 + skluz 0,05).
Kontroly (30 seminek): NAHODNE = stejny pocet vstupu ve stejne dny, nahodne akcie, stejne stopy a vystupy;
TREND = totez, ale jen z akcii v trendu (close > EMA50 > SMA200) -> ukaze, jestli setup prida neco navic.
Portfolio: start 5 000, riziko 0,5 % uctu vc. nakladu, strop pozice 20 % uctu, max 10 pozic, bez paky.
Dan: 15 % z kladneho realizovaneho zisku za rok (bez osvobozeni; pri obratu nad 100 000 Kc/rok se neuplatni).
Pozor: vesmir je dnesni seznam firem -> zkresleni prezivsich; proto rozhoduje srovnani s nahodnymi kontrolami.
Mena: vysledky v mene trhu (FX pohyb se neuvazuje, jen poplatek za konverzi).
"""
import os, io, sys, json, time, random
import numpy as np, pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, '_analyza_drzeni')
START = '2011-01-01'
TEST_FROM = pd.Timestamp('2013-01-01')
SPLIT = pd.Timestamp('2020-01-01')
EQ0 = 5000.0
RISK = 0.005
POS_CAP = 0.20
MAX_POS = 10
TAX = 0.15
SEEDS = 30
COST = {'EU': 0.0005, 'US': 0.0020}
INDEX = {'EU': '^GDAXI', 'US': 'SPY'}

EU = """SAP.DE SIE.DE ALV.DE DTE.DE BAS.DE BAYN.DE BMW.DE MBG.DE VOW3.DE ADS.DE IFX.DE MUV2.DE DBK.DE RWE.DE EOAN.DE
DHL.DE HEN3.DE MRK.DE FRE.DE FME.DE BEI.DE CON.DE HNR1.DE DTG.DE ZAL.DE RHM.DE MTX.DE SHL.DE ENR.DE AIR.DE DB1.DE
HEI.DE P911.DE PAH3.DE QIA.DE SRT3.DE SY1.DE VNA.DE CBK.DE BNR.DE AIXA.DE AFX.DE BC8.DE BOSS.DE DUE.DE EVK.DE EVD.DE
FNTN.DE FPE3.DE FRA.DE G1A.DE GXI.DE HAG.DE HLE.DE HOT.DE HFG.DE JUN3.DE KGX.DE KBX.DE KRN.DE LEG.DE LXS.DE NDA.DE
NEM.DE PSM.DE PUM.DE RAA.DE RRTL.DE SAX.DE SDF.DE STM.DE TAG.DE TEG.DE TKA.DE TLX.DE UTDI.DE WCH.DE JEN.DE 8TRA.DE
RDC.DE AT1.DE EVT.DE HYQ.DE SZG.DE SZU.DE TMV.DE FIE.DE GBF.DE COK.DE S92.DE VBK.DE SIX2.DE WAF.DE ADN1.DE ELG.DE
DRW3.DE KWS.DE GFT.DE HDD.DE NDX1.DE BYW6.DE CEC.DE 1U1.DE O2D.DE DEZ.DE SMHN.DE NOEJ.DE PBB.DE STO3.DE HAB.DE
SGL.DE VOS.DE WUW.DE MUX.DE DMP.DE INH.DE YOU.DE AOX.DE
ASML.AS ADYEN.AS INGA.AS PRX.AS HEIA.AS AD.AS PHIA.AS WKL.AS ASM.AS BESI.AS ABN.AS AGN.AS AKZA.AS ASRNL.AS
EXO.AS IMCD.AS KPN.AS MT.AS NN.AS RAND.AS UMG.AS URW.AS ALFEN.AS AALB.AS ARCAD.AS BFIT.AS FLOW.AS FUR.AS INPST.AS
JDEP.AS LIGHT.AS NSI.AS OCI.AS SBMO.AS TKWY.AS TOM2.AS VPK.AS AMG.AS BAMNB.AS HEIJM.AS CTPNV.AS TWEKA.AS DSFIR.AS
SIFG.AS KENDR.AS CRBN.AS BASIC.AS ACOMO.AS ECMPA.AS WHA.AS""".split()

WIKI = {'S&P 500': 'List_of_S%26P_500_companies', 'S&P 400': 'List_of_S%26P_400_companies',
        'S&P 600': 'List_of_S%26P_600_companies'}


def index_list(name):
    import requests
    html = requests.get('https://en.wikipedia.org/wiki/' + WIKI[name],
                        headers={'User-Agent': 'Mozilla/5.0 BotBacktest'}, timeout=30).text
    for t in pd.read_html(io.StringIO(html)):
        cols = {c: str(c).strip().lower() for c in t.columns}
        sym = next((c for c, l in cols.items() if l in ('symbol', 'ticker symbol', 'ticker')), None)
        if sym is not None and len(t) > 100:
            return [s.strip().replace('.', '-') for s in t[sym].astype(str)]
    raise RuntimeError('tabulka nenalezena: ' + name)


def download(tickers, cache):
    if os.path.exists(cache):
        return pd.read_pickle(cache)
    import yfinance as yf
    parts = []
    for k in range(0, len(tickers), 200):
        ch = tickers[k:k + 200]
        parts.append(yf.download(ch, start=START, auto_adjust=True, progress=False, threads=True))
        print(f'  ceny {min(k + 200, len(tickers))}/{len(tickers)}', flush=True)
    raw = pd.concat(parts, axis=1)
    raw = raw.loc[:, ~raw.columns.duplicated()]
    raw.to_pickle(cache)
    return raw


# ---------------- indikatory ----------------
def ema(x, n):
    return x.ewm(span=n, adjust=False, min_periods=n).mean()


def wilder(x, n):
    return x.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def prep(raw, tickers, index_t):
    idx_c = raw['Close'][index_t].dropna()
    dates = idx_c.index
    f = {k: raw[k].reindex(index=dates) for k in ('Open', 'High', 'Low', 'Close', 'Volume')}
    keep = [t for t in tickers if t in f['Close'] and f['Close'][t].notna().sum() > 300]
    O, H, L, C, V = (f[k][keep].astype(float) for k in ('Open', 'High', 'Low', 'Close', 'Volume'))
    pc = C.shift(1)
    tr = pd.concat([(H - L), (H - pc).abs(), (L - pc).abs()]).groupby(level=0).max().reindex(dates)
    I = dict(O=O, H=H, L=L, C=C, V=V)
    I['ATR10'], I['ATR14'], I['ATR22'], I['ATR50'] = (wilder(tr, n) for n in (10, 14, 22, 50))
    I['EMA20'], I['EMA50'] = ema(C, 20), ema(C, 50)
    I['SMA200'] = C.rolling(200).mean()
    d = C.diff()
    up, dn = wilder(d.clip(lower=0), 14), wilder((-d).clip(lower=0), 14)
    I['RSI'] = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    I['V20'] = V.shift(1).rolling(20).mean()
    ix = idx_c
    return I, keep, dates, ix


def regime(ix, kind, vix):
    sma = ix.rolling(200).mean()
    if kind == 'A2':
        r252, r20 = ix / ix.shift(252) - 1, ix / ix.shift(20) - 1
        return (ix > sma) & ~((r252 < 0) & (r20 > 0))
    v = vix.reindex(ix.index).ffill()
    return (ix > sma) & (v < 22)


def signals_A2(I, ix):
    H, L, C, V = I['H'], I['L'], I['C'], I['V']
    h252p = H.shift(1).rolling(252).max()
    q = C.shift(1) / h252p
    near = (q >= 0.94) & (q <= 1.0)
    squeeze = I['ATR10'].shift(1) < 0.85 * I['ATR50'].shift(1)
    nonew = H.shift(1).rolling(5).max() < H.shift(6).rolling(247).max()
    brk = C > C.shift(1).rolling(252).max()
    vol = V >= 1.5 * I['V20']
    rs = (C / C.shift(60)).sub(ix / ix.shift(60), axis=0)
    trend = (C > I['EMA50']) & (I['EMA50'] > I['SMA200'])
    sig = near & squeeze & nonew & brk & vol & (rs > 0) & trend
    return sig, rs


def stops_A2(I):
    return np.maximum(I['L'].rolling(10).min(), I['C'] - 2.5 * I['ATR14'])


def signals_A4(I):
    H, L, C, V = I['H'], I['L'], I['C'], I['V']
    trend = (I['EMA20'] > I['EMA50']) & (I['EMA50'] > I['SMA200'])
    hi = H.rolling(10).max() / H.rolling(252).max() >= 0.90
    down = (C < C.shift(1)).astype(float)
    downs = down.shift(1).rolling(5).sum() >= 3
    lmin = L.rolling(4).min()
    zone = (lmin <= I['EMA20']) & (lmin >= I['EMA50'] - 0.5 * I['ATR14'])
    rp = I['RSI'].shift(1)
    rsi = (rp >= 36) & (rp <= 48)
    dv = (V * down).shift(1).rolling(5).sum() / down.shift(1).rolling(5).sum()
    dvol = dv < 0.85 * I['V20']
    rng = (H - L).replace(0, np.nan)
    rev = ((C - L) / rng >= 0.55) & (C > I['EMA20']) & (C > C.shift(1))
    sig = trend & hi & downs & zone & rsi & dvol & rev
    return sig, H.rolling(10).max() / H.rolling(252).max()


def stops_A4(I):
    return np.minimum(I['L'].rolling(5).min(), I['EMA50']) - 0.5 * I['ATR14']


# ---------------- simulace portfolia ----------------
def simulate(kind, I, dates, cands, stopdf, cost):
    """cands: dict den_index -> seznam sloupcu (serazeny podle priority). Vstup na open dalsiho dne."""
    O, H, L, C = (I[k].values for k in ('O', 'H', 'L', 'C'))
    A22, E50 = I['ATR22'].values, I['EMA50'].values
    ST = stopdf.values
    T = len(dates)
    cash, pos, trades, eq = EQ0, {}, [], np.full(T, np.nan)
    year_pnl, cur_year = 0.0, None
    t0 = dates.searchsorted(TEST_FROM)
    for d in range(t0, T):
        y = dates[d].year
        if cur_year is None:
            cur_year = y
        if y != cur_year:  # dan za predchozi rok
            if year_pnl > 0:
                cash -= TAX * year_pnl
            year_pnl, cur_year = 0.0, y
        # 1) vystupy (nikdy v den vstupu)
        for j in list(pos):
            p = pos[j]
            o, h, l = O[d, j], H[d, j], L[d, j]
            if np.isnan(o):
                continue
            px = None
            if p['exit_next']:
                px = o
            elif o <= p['stop']:
                px = o
            elif l <= p['stop']:
                px = p['stop']
            elif p['tgt'] is not None and o >= p['tgt']:
                px = o
            elif p['tgt'] is not None and h >= p['tgt']:
                px = p['tgt']
            if px is not None:
                val = p['sh'] * px * (1 - cost)
                cash += val
                pnl = val - p['basis']
                year_pnl += pnl
                trades.append((dates[p['d0']], dates[d], j, pnl, pnl / p['risk'], d - p['d0']))
                del pos[j]
        # 2) vstupy na open ze signalu predchoziho dne
        if d - 1 in cands:
            equity = cash + sum(p['sh'] * (C[d - 1, j] if not np.isnan(C[d - 1, j]) else p['entry']) for j, p in pos.items())
            for j in cands[d - 1]:
                if len(pos) >= MAX_POS:
                    break
                if j in pos:
                    continue
                o, st = O[d, j], ST[d - 1, j]
                if np.isnan(o) or np.isnan(st) or o <= st:
                    continue
                r = o - st
                per = r + cost * o * 2
                sh = min(equity * RISK / per, equity * POS_CAP / o)
                val = sh * o * (1 + cost)
                if val > cash:
                    sh = cash / (o * (1 + cost))
                    val = cash
                if sh * o < 50:
                    continue
                cash -= val
                pos[j] = dict(d0=d, entry=o, sh=sh, basis=val, stop=st, r=r, risk=sh * per, hh=H[d, j],
                              tgt=(o + 2.5 * r) if kind == 'A4' else None, exit_next=False, onr=False)
        # 3) uzaviraci aktualizace
        for j, p in pos.items():
            c = C[d, j]
            if np.isnan(c):
                continue
            p['hh'] = max(p['hh'], H[d, j]) if not np.isnan(H[d, j]) else p['hh']
            held = d - p['d0']
            if kind == 'A2':
                if p['hh'] >= p['entry'] + p['r'] and not np.isnan(A22[d, j]):
                    p['stop'] = max(p['stop'], p['hh'] - 3 * A22[d, j])
                if held >= 40 or (held == 12 and c < p['entry'] + 0.2 * p['r']) or \
                        (held >= 5 and not np.isnan(E50[d, j]) and c < E50[d, j]):
                    p['exit_next'] = True
            else:
                if held >= 25 or (held == 7 and c < p['entry']):
                    p['exit_next'] = True
        eq[d] = cash + sum(p['sh'] * (C[d, j] if not np.isnan(C[d, j]) else p['entry']) for j, p in pos.items())
    e = pd.Series(eq, index=dates).ffill().dropna()
    tr = pd.DataFrame(trades, columns=['vstup', 'vystup', 'j', 'pnl', 'R', 'dni'])
    return e, tr


def stats(e, tr, a=None, b=None):
    if a is not None:
        e = e[(e.index >= a) & (e.index < b)]
        tr = tr[(tr['vystup'] >= a) & (tr['vystup'] < b)]
    if len(e) < 50:
        return {}
    yrs = (e.index[-1] - e.index[0]).days / 365.25
    cagr = (e.iloc[-1] / e.iloc[0]) ** (1 / yrs) - 1
    dd = (e / e.cummax() - 1).min()
    g, l = tr.loc[tr.pnl > 0, 'pnl'].sum(), -tr.loc[tr.pnl < 0, 'pnl'].sum()
    return dict(n=len(tr), win=(tr.pnl > 0).mean() if len(tr) else np.nan, pf=g / l if l > 0 else np.nan,
                avgR=tr.R.mean() if len(tr) else np.nan, dni=tr.dni.mean() if len(tr) else np.nan,
                cagr=cagr, dd=dd, mar=cagr / abs(dd) if dd < 0 else np.nan)


def bench(ix, a, b):
    s = ix[(ix.index >= a) & (ix.index < b)]
    yrs = (s.index[-1] - s.index[0]).days / 365.25
    return dict(cagr=(s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1, dd=(s / s.cummax() - 1).min())


def build_cands(sig, rank):
    s, r = sig.values, rank.values
    out = {}
    for d in np.where(s.any(axis=1))[0]:
        js = np.where(s[d])[0]
        js = js[np.argsort(-np.nan_to_num(r[d, js], nan=-9))]
        out[d] = list(js)
    return out


def random_cands(cands, pool, seed):
    rng = random.Random(seed)
    out = {}
    for d, js in cands.items():
        avail = np.where(pool[d])[0]
        if len(avail):
            out[d] = list(rng.sample(list(avail), min(len(js), len(avail))))
    return out


def pct(x, q):
    x = [v for v in x if v == v]
    return np.percentile(x, q) if x else np.nan


def main():
    os.makedirs(OUT, exist_ok=True)
    lines = []

    def w(s=''):
        print(s, flush=True)
        lines.append(s)

    w('Backtest drzeni ~20 dni (A2 EU-HIGH252, A4 PULLBACK) - ' + time.strftime('%Y-%m-%d %H:%M'))
    w(f'Test od {TEST_FROM.date()}, rozdeleni {SPLIT.date()}. Start {EQ0:.0f}, riziko {RISK:.1%}, max {MAX_POS} pozic.')
    w('Laťka: >=100 obchodu, PF >=1,3, propad <15 %, lepsi nez 90. percentil nahodnych kontrol a lepsi CAGR/propad nez index.')
    if '--test' in sys.argv:
        regions = {'TEST': None}
    else:
        us = []
        for n in WIKI:
            try:
                us += index_list(n)
            except Exception as ex:
                print('wiki chyba', n, ex)
        us = sorted(set(us))
        regions = {'EU': EU, 'US': us}
    vix_raw = None
    for reg, tick in regions.items():
        if reg == 'TEST':
            raw, reg, tick, vix = fake_data()
        else:
            raw = download(tick + [INDEX[reg], '^VIX'], os.path.join(OUT, f'ceny_{reg}.pkl'))
            vix = raw['Close']['^VIX'].dropna()
        I, keep, dates, ix = prep(raw, tick, INDEX[reg])
        cost = COST[reg]
        w('')
        w(f'===== {reg}: {len(keep)} akcii s daty, naklad {cost:.2%}/strana, index {INDEX[reg]} =====')
        for (a, b, lab) in ((TEST_FROM, dates[-1] + pd.Timedelta(days=1), 'cele obdobi'),
                            (TEST_FROM, SPLIT, '2013-2019'), (SPLIT, dates[-1] + pd.Timedelta(days=1), '2020-dnes')):
            bm = bench(ix, a, b)
            w(f'  index {lab}: CAGR {bm["cagr"]:.1%}, propad {bm["dd"]:.0%}, CAGR/propad {bm["cagr"] / abs(bm["dd"]):.2f}')
        valid = (I['SMA200'].notna() & I['ATR22'].notna() & I['C'].notna() & I['O'].shift(-1).notna()).values
        trendpool = valid & ((I['C'] > I['EMA50']) & (I['EMA50'] > I['SMA200'])).values
        for kind in ('A2', 'A4'):
            if kind == 'A2':
                sig, rank = signals_A2(I, ix)
                stp = stops_A2(I)
            else:
                sig, rank = signals_A4(I)
                stp = stops_A4(I)
            rg = regime(ix, kind, vix).reindex(dates).fillna(False)
            sig = sig.fillna(False) & np.asarray(rg.values)[:, None] & valid
            sig = pd.DataFrame(sig, index=dates, columns=keep)
            cands = build_cands(sig, rank)
            e, tr = simulate(kind, I, dates, cands, stp, cost)
            tr2 = tr.copy()
            tr2['akcie'] = [keep[j] for j in tr2.j]
            tr2.drop(columns='j').to_csv(os.path.join(OUT, f'obchody_{reg}_{kind}.csv'), index=False)
            w('')
            w(f'--- {kind} {reg}: signalu {int(sig.values.sum())} ve {len(cands)} dnech ---')
            ctl = {}
            for nm, pool in (('NAHODNE', valid), ('TREND', trendpool)):
                ctl[nm] = []
                for s in range(SEEDS):
                    er, trr = simulate(kind, I, dates, random_cands(cands, pool, 1000 + s), stp, cost)
                    ctl[nm].append((er, trr))
            for (a, b, lab) in ((None, None, 'cele obdobi'), (TEST_FROM, SPLIT, '2013-2019'),
                                (SPLIT, dates[-1] + pd.Timedelta(days=1), '2020-dnes')):
                st = stats(e, tr, a, b)
                if not st:
                    continue
                w(f'  {lab}: obchodu {st["n"]}, vyhernich {st["win"]:.0%}, PF {st["pf"]:.2f}, prumer {st["avgR"]:+.2f}R, '
                  f'drzeni {st["dni"]:.0f} dni, CAGR po dani {st["cagr"]:.1%}, propad {st["dd"]:.0%}, CAGR/propad {st["mar"]:.2f}')
                for nm in ctl:
                    ss = [stats(er, trr, a, b) for er, trr in ctl[nm]]
                    ss = [x for x in ss if x]
                    w(f'     {nm:8s} p10/p50/p90: CAGR {pct([x["cagr"] for x in ss], 10):.1%} / {pct([x["cagr"] for x in ss], 50):.1%} / '
                      f'{pct([x["cagr"] for x in ss], 90):.1%}, PF {pct([x["pf"] for x in ss], 50):.2f} (p90 {pct([x["pf"] for x in ss], 90):.2f}), '
                      f'propad p50 {pct([x["dd"] for x in ss], 50):.0%}')
            st = stats(e, tr)
            bm = bench(ix, TEST_FROM, dates[-1] + pd.Timedelta(days=1))
            p90 = max(pct([stats(er, trr)['cagr'] for er, trr in ctl[nm]], 90) for nm in ctl)
            checks = {'>=100 obchodu': st['n'] >= 100, 'PF>=1,3': st['pf'] >= 1.3, 'propad<15 %': st['dd'] > -0.15,
                      'nad p90 kontrol': st['cagr'] > p90, 'CAGR/propad > index': st['mar'] > bm['cagr'] / abs(bm['dd'])}
            w('  LAŤKA: ' + ', '.join(f'{k} {"ANO" if v else "ne"}' for k, v in checks.items()) +
              f'  => {"PROSLO" if all(checks.values()) else "NEPROSLO"}')
    with open(os.path.join(OUT, 'drzeni.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')


def fake_data():
    """Synteticka data pro kontrolu, ze skript bezi (--test)."""
    rng = np.random.default_rng(1)
    dates = pd.bdate_range('2011-01-03', '2026-10-30')
    tick = [f'T{i}' for i in range(60)] + ['IDX', '^VIX']
    cols = {}
    for t in tick:
        r = rng.normal(0.0004, 0.015, len(dates))
        c = 50 * np.exp(np.cumsum(r))
        o = c * (1 + rng.normal(0, 0.004, len(dates)))
        h = np.maximum(o, c) * (1 + abs(rng.normal(0, 0.008, len(dates))))
        l = np.minimum(o, c) * (1 - abs(rng.normal(0, 0.008, len(dates))))
        v = rng.lognormal(12, 0.5, len(dates))
        if t == '^VIX':
            c = o = h = l = np.full(len(dates), 18.0)
        for k, x in zip(('Open', 'High', 'Low', 'Close', 'Volume'), (o, h, l, c, v)):
            cols[(k, t)] = x
    raw = pd.DataFrame(cols, index=dates)
    INDEX['TEST'] = 'IDX'
    COST['TEST'] = 0.0005
    return raw, 'TEST', tick[:60], raw['Close']['^VIX']


if __name__ == '__main__':
    main()
