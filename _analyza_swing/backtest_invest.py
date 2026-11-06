# -*- coding: utf-8 -*-
"""Backtest investicnich strategii z pruzkumu (UCITS ETF, CZ dane).
Signaly z US ETF (USD, total return), drzeni v EUR (cena / EURUSD), hotovost = XEON.DE.
Signal na konci mesice, obchod na close dalsiho dne. Naklad 0,10 % za stranu.
Dan CZ: 15 % ze zisku lotu drzenych <= 3 roky, ztraty jen v tomtez roce,
osvobozeno pokud prodeje (bez casove osvobozenych) v roce <= 100 000 Kc (kurz 25).
Vystup: _analyza_swing\\invest.txt a invest.json
"""
import sys, os, json
import numpy as np, pandas as pd

TICK = ['SPY', 'EFA', 'EEM', 'IWM', 'VNQ', 'DBC', 'IEF', 'TLT', 'TIP', 'AGG', 'SHY', 'GLD', 'VBR']
COST = 0.0010
TAX = 0.15
LIMIT_EUR = 100000 / 25.0
START_CAP = 5000.0
CONTRIB = 200.0
BAND = 0.05
SPLIT = pd.Timestamp('2017-01-01')
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_analyza_swing')


def load():
    import yfinance as yf
    raw = yf.download(TICK + ['^IRX', 'EURUSD=X', 'XEON.DE'], start='2002-01-01',
                      auto_adjust=True, progress=False)['Close']
    return raw


def prep(raw):
    raw = raw.sort_index()
    idx = raw['SPY'].dropna().index
    df = raw.ffill().reindex(idx)
    usd = df[TICK].copy()
    fx = df['EURUSD=X']
    irx = df['^IRX'].ffill().fillna(0)
    usd['BIL'] = ((1 + irx / 100.0) ** (1 / 252.0)).cumprod()
    wr = 0.7 * usd['SPY'].pct_change() + 0.3 * usd['EFA'].pct_change()
    usd['WORLD'] = (1 + wr.fillna(0)).cumprod()
    eur = usd.div(fx, axis=0)
    xe = df['XEON.DE']
    syn = pd.Series((1.035) ** (1 / 252.0), index=idx).cumprod()
    first = xe.first_valid_index()
    if first is None:
        print('VAROVANI: XEON.DE chybi, hotovost = 3,5 % p.a. synteticky')
        xe = syn
    else:
        xe = xe.copy()
        m = idx < first
        xe[m] = syn[m] * (xe[first] / syn[first])
        xe = xe.ffill()
    eur['XEON'] = xe
    return usd, eur, fx


class Sig:
    def __init__(s, usd):
        idx = usd.index
        me = idx.to_series().groupby(idx.to_period('M')).max()
        s.me = list(me.values)
        s.m = usd.loc[me.values]
        # prvni mesic, kde maji vsechna aktiva 13 mesicu historie
        ok = s.m[TICK + ['BIL']].notna().all(axis=1).values
        k0 = None
        for k in range(13, len(ok)):
            if ok[k - 13:k + 1].all():
                k0 = k
                break
        s.k0 = k0

    def r(s, t, k, n):
        a = s.m[t].iloc[k]; b = s.m[t].iloc[k - n]
        return a / b - 1

    def mom(s, t, k):
        return np.mean([s.r(t, k, n) for n in (1, 3, 6, 12)])

    def sma10(s, t, k):
        return s.m[t].iloc[k - 9:k + 1].mean()

    def targets(s, name, k):
        mon = pd.Timestamp(s.me[k]).month
        if name in ('BH-SPY',):
            return {'SPY': 1.0}, False
        if name == 'BH-SVET':
            return {'WORLD': 1.0}, False
        if name in ('GEM', 'GEM-ensemble'):
            ns = [12] if name == 'GEM' else [3, 6, 9, 12]
            w = {}
            for n in ns:
                a = 'SPY' if s.r('SPY', k, n) >= s.r('EFA', k, n) else 'EFA'
                t = a if s.r(a, k, n) > s.r('BIL', k, n) else 'AGG'
                w[t] = w.get(t, 0) + 1.0 / len(ns)
            return w, True
        if name == 'GTAA5':
            w = {}
            for t in ('SPY', 'EFA', 'IEF', 'DBC', 'VNQ'):
                d = t if s.m[t].iloc[k] > s.sma10(t, k) else 'XEON'
                w[d] = w.get(d, 0) + 0.2
            return w, True
        if name.startswith('HAA') or name == 'CF-HAA':
            dfn = 'IEF' if s.mom('IEF', k) > s.mom('BIL', k) else 'XEON'
            if name == 'HAA-simple':
                if s.mom('TIP', k) > 0 and s.mom('SPY', k) > 0:
                    return {'SPY': 1.0}, True
                return {dfn: 1.0}, True
            if s.mom('TIP', k) < 0:
                w = {dfn: 1.0}
            else:
                off = ['SPY', 'IWM', 'EFA', 'EEM', 'VNQ', 'DBC', 'IEF', 'TLT']
                top = sorted(off, key=lambda t: s.mom(t, k), reverse=True)[:4]
                w = {}
                for t in top:
                    d = t if s.mom(t, k) > 0 else dfn
                    w[d] = w.get(d, 0) + 0.25
            return w, (name != 'CF-HAA')
        if name.startswith('GB'):
            w = {t: 0.2 for t in ('SPY', 'VBR', 'TLT', 'SHY', 'GLD')}
            return w, (name == 'GB-rocne' and mon == 12)
        raise ValueError(name)


def run(name, eur, sig, tax_on, contrib, scale=1.0):
    dates = eur.index
    P = {t: eur[t].values for t in eur.columns}
    pos = {}   # t -> list [date, qty, basis/share]
    sh = {}
    st = {'cash': START_CAP * scale, 'taxes': 0.0, 'sales_all': 0.0, 'trades': 0,
          'ys': 0.0, 'yg': 0.0}
    flows = []

    def val(i):
        return st['cash'] + sum(q * P[t][i] for t, q in sh.items() if q > 0)

    def buy(t, amt, i):
        if amt <= 1.0:
            return
        q = amt * (1 - COST) / P[t][i]
        pos.setdefault(t, []).append([dates[i], q, amt / q])
        sh[t] = sh.get(t, 0) + q
        st['cash'] -= amt
        st['trades'] += 1

    def sell(t, amt, i):
        px = P[t][i]
        need = min(amt / px, sh.get(t, 0))
        if need * px <= 1.0:
            return
        st['trades'] += 1
        lots = pos[t]
        lim = dates[i] - pd.DateOffset(years=3)
        while need > 1e-12 and lots:
            lot = lots[0]
            q = min(need, lot[1])
            proceeds = q * px * (1 - COST)
            gain = proceeds - q * lot[2]
            st['sales_all'] += proceeds
            if not (lot[0] < lim):
                st['ys'] += proceeds
                st['yg'] += gain
            st['cash'] += proceeds
            lot[1] -= q; sh[t] -= q; need -= q
            if lot[1] <= 1e-12:
                lots.pop(0)

    def settle(i):
        if tax_on and st['ys'] > LIMIT_EUR and st['yg'] > 0:
            tax = TAX * st['yg']
            V = val(i)
            f = max(0.0, (V - tax) / V)
            st['cash'] *= f
            for t in sh:
                sh[t] *= f
                for lot in pos.get(t, []):
                    lot[1] *= f
            st['taxes'] += tax
        st['ys'] = 0.0; st['yg'] = 0.0

    def rebalance(i, tg, allow):
        V = val(i)
        if allow:
            for t in list(sh.keys()):
                if sh[t] <= 1e-12:
                    continue
                cw = sh[t] * P[t][i] / V
                tw = tg.get(t, 0.0)
                if tw == 0 or cw > tw + BAND:
                    sell(t, (cw - tw) * V, i)
        V = val(i)
        cash = st['cash']
        if cash <= 1.0:
            return
        needs = {t: max(0.0, w * V - sh.get(t, 0) * P[t][i]) for t, w in tg.items()}
        tot = sum(needs.values())
        if tot > 1.0:
            spend = min(cash, tot)
            for t, n in needs.items():
                buy(t, spend * n / tot, i)
        rest = st['cash']
        if rest > 1.0:
            tw = sum(tg.values())
            for t, w in tg.items():
                buy(t, rest * w / tw, i)

    pos_of = {d: j for j, d in enumerate(dates)}
    execs = {}
    for k in range(sig.k0, len(sig.me) - 1):
        j = pos_of[pd.Timestamp(sig.me[k])] + 1
        execs[j] = k
    i0 = min(execs)
    eq = []
    cur_year = dates[i0].year
    flows.append((dates[i0], -START_CAP * scale))
    for i in range(i0, len(dates)):
        if dates[i].year != cur_year:
            settle(i)
            cur_year = dates[i].year
        if i in execs:
            if contrib and i != i0:
                st['cash'] += CONTRIB * scale
                flows.append((dates[i], -CONTRIB * scale))
            tg, allow = sig.targets(name, execs[i])
            rebalance(i, tg, allow)
        eq.append(val(i))
    last = len(dates) - 1
    pre = val(last)
    for t in list(sh.keys()):
        if sh[t] > 1e-12:
            sell(t, sh[t] * P[t][last] * 2, last)
    settle(last)
    net = val(last)
    flows.append((dates[last], net))
    return {'eq': pd.Series(eq, index=dates[i0:]), 'pre': pre, 'net': net,
            'taxes': st['taxes'], 'trades': st['trades'], 'sales_all': st['sales_all'],
            'flows': flows}


def xirr(flows):
    t0 = flows[0][0]
    ts = np.array([(d - t0).days / 365.25 for d, _ in flows]); cf = np.array([c for _, c in flows])
    f = lambda r: np.sum(cf / (1 + r) ** ts)
    lo, hi = -0.9, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if f(lo) * f(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def seg(eq, cash):
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1
    dd = float((eq / eq.cummax() - 1).min())
    r = eq.pct_change().dropna()
    c = cash.reindex(eq.index).pct_change().dropna()
    ex = r - c
    sh = float(ex.mean() / ex.std() * np.sqrt(252)) if ex.std() > 0 else 0.0
    return float(cagr), dd, sh, yrs


def combine(a, b):
    out = dict(a)
    out['eq'] = a['eq'] + b['eq']
    for k in ('pre', 'net', 'taxes', 'trades', 'sales_all'):
        out[k] = a[k] + b[k]
    fl = {}
    for d, c in a['flows'] + b['flows']:
        fl[d] = fl.get(d, 0) + c
    out['flows'] = sorted(fl.items())
    return out


NAMES = ['BH-SPY', 'BH-SVET', 'GEM', 'GEM-ensemble', 'GTAA5', 'HAA', 'HAA-simple',
         'GB-rocne', 'GB-vklady', 'CF-HAA', 'JADRO85+GEM15']
POPIS = {
    'BH-SPY': 'drzet S&P 500 (SXR8)', 'BH-SVET': 'drzet svet 70 % US / 30 % EAFE (~VWCE/IWDA)',
    'GEM': 'dual momentum 12m, 100 % jedno ETF', 'GEM-ensemble': 'GEM 3/6/9/12m po 25 %',
    'GTAA5': 'Faber 5 x 20 %, cena > SMA10m jinak XEON', 'HAA': 'Keller HAA Balanced (kanarek TIP, top 4 z 8)',
    'HAA-simple': 'HAA jen S&P 500 nebo IEF/XEON', 'GB-rocne': 'Golden Butterfly 5 x 20 %, rebalanc rocne',
    'GB-vklady': 'Golden Butterfly, bez prodeju (jen vklady)', 'CF-HAA': 'HAA bez prodeju, vklady podle signalu',
    'JADRO85+GEM15': '85 % drzet S&P 500 + 15 % GEM satelit'}


def run_name(n, eur, sig, tax_on, contrib):
    if n == 'JADRO85+GEM15':
        return combine(run('BH-SPY', eur, sig, tax_on, contrib, 0.85), run('GEM', eur, sig, tax_on, contrib, 0.15))
    return run(n, eur, sig, tax_on, contrib)


def main(raw=None):
    if raw is None:
        raw = load()
    usd, eur, fx = prep(raw)
    sig = Sig(usd)
    cash = eur['XEON']
    res = {}
    L = []
    p = lambda s='': (L.append(s), print(s))
    start = pd.Timestamp(sig.me[sig.k0]).date()
    p(f'Backtest investicnich strategii, signaly od {start} do {eur.index[-1].date()}')
    p(f'Kapital {START_CAP:.0f} EUR, naklad {COST*100:.2f} %/strana, dan {TAX*100:.0f} % (3lety test, limit {LIMIT_EUR:.0f} EUR prodeju/rok), pasmo {BAND*100:.0f} p.b.')
    p('')
    p('A) Jednorazovy vklad 5 000 EUR')
    hdr = f"{'strategie':<15}{'CAGR':>7}{'MaxDD':>8}{'Sharpe':>7}{'CAGR po dani':>13}{'dane+prod':>10}{'obch/rok':>9}{'obrat/rok':>10}  {'1.pol CAGR/DD':>15}  {'2.pol CAGR/DD':>15}"
    p(hdr)
    for n in NAMES:
        if n == 'CF-HAA':
            continue
        a = run_name(n, eur, sig, False, False)
        b = run_name(n, eur, sig, True, False)
        cg, dd, shp, yrs = seg(a['eq'], cash)
        h1 = seg(a['eq'][a['eq'].index < SPLIT], cash)
        h2 = seg(a['eq'][a['eq'].index >= SPLIT], cash)
        cnet = (b['net'] / START_CAP) ** (1 / yrs) - 1
        avgv = float(a['eq'].mean())
        res[n] = {'popis': POPIS[n], 'cagr': cg, 'maxdd': dd, 'sharpe': shp, 'cagr_po_dani': cnet,
                  'dane': b['taxes'] + (b['pre'] - b['net']), 'obchodu_rok': a['trades'] / yrs,
                  'obrat_rok': a['sales_all'] / avgv / yrs, 'h1': h1[:3], 'h2': h2[:3]}
        r = res[n]
        p(f"{n:<15}{cg*100:6.1f}%{dd*100:7.1f}%{shp:7.2f}{cnet*100:12.1f}%{r['dane']:10.0f}{r['obchodu_rok']:9.1f}{r['obrat_rok']*100:9.0f}%  {h1[0]*100:6.1f}%/{h1[1]*100:6.1f}%  {h2[0]*100:6.1f}%/{h2[1]*100:6.1f}%")
    bm = res['BH-SPY']
    p('')
    p('Brana: Sharpe > drzeni S&P 500 a MaxDD aspon o tretinu mensi (nebo vyssi CAGR pri MaxDD <= index),')
    p('       a v obou polovinach MAR (CAGR/|DD|) >= index; navic CAGR po dani >= 80 % indexu po dani.')
    for n, r in res.items():
        if n == 'BH-SPY':
            continue
        c1 = (r['sharpe'] > bm['sharpe'] and r['maxdd'] >= bm['maxdd'] * 2 / 3) or (r['cagr'] > bm['cagr'] and r['maxdd'] >= bm['maxdd'])
        mar = lambda h: h[0] / abs(h[1]) if h[1] < 0 else 9
        c2 = mar(r['h1']) >= mar(bm['h1']) and mar(r['h2']) >= mar(bm['h2'])
        c3 = r['cagr_po_dani'] >= 0.8 * bm['cagr_po_dani']
        r['brana'] = bool(c1 and c2 and c3)
        p(f"  {n:<15} riziko {'ANO' if c1 else 'ne'}, obe poloviny {'ANO' if c2 else 'ne'}, vynos po dani {'ANO' if c3 else 'ne'} -> {'PROSLA' if r['brana'] else 'neprosla'}   ({POPIS[n]})")
    p('')
    p(f'B) 5 000 EUR + {CONTRIB:.0f} EUR mesicne (po danich vcetne prodeje na konci)')
    p(f"{'strategie':<15}{'vlozeno':>9}{'konec pred dani':>16}{'konec po dani':>14}{'IRR po dani':>12}{'dane':>8}")
    res_b = {}
    for n in NAMES:
        a = run_name(n, eur, sig, True, True)
        inv = -sum(c for _, c in a['flows'][:-1])
        irr = xirr(a['flows'])
        res_b[n] = {'vlozeno': inv, 'pred': a['pre'], 'po': a['net'], 'irr': irr, 'dane': a['taxes'] + a['pre'] - a['net']}
        p(f"{n:<15}{inv:9.0f}{a['pre']:16.0f}{a['net']:14.0f}{irr*100:11.1f}%{res_b[n]['dane']:8.0f}")
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, 'invest.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(L))
    with open(os.path.join(OUT, 'invest.json'), 'w', encoding='utf-8') as f:
        json.dump({'jednorazove': res, 'vklady': res_b}, f, ensure_ascii=False, indent=1, default=float)
    return res, res_b


if __name__ == '__main__':
    main()
