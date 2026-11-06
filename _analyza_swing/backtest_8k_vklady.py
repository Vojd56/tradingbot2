# -*- coding: utf-8 -*-
"""Varianta kroku 1 (backtest_8k.py) s mesicnimi vklady na ucet T212 (pozadavek 2. 11. 2026).
Nic nekupuje, nic v botovi nemeni, Gemini nevola. Stejne signaly a pravidla vystupu jako backtest_8k.py.

Spusteni ze slozky bota az PO backtest_8k.py (pouzije jeho cache):  python _analyza_swing\\backtest_8k_vklady.py
Vystup: _analyza_drzeni\\8k_vklady.txt

Predpoklady:
- Start 1 000 Kc, vklad 2 000 Kc kazdy mesic (prvni obchodni den mesice), kurz 25 Kc/EUR, USD ~ EUR (FX pohyb ignorovan,
  poplatek za konverzi je v nakladu 0,20 % na stranu).
- Rizeni rizika podle navrhu: ucet < 500 EUR -> riziko 1,5 %, max pozice 30 %, max 3 pozice, min. pokyn 8 EUR;
  od 500 EUR -> riziko 0,5 %, max pozice 20 %, max 10 pozic, min. pokyn 25 EUR.
- Dan 15 % z rocniho zisku jen kdyz hrube prijmy z prodeju v roce > 100 000 Kc (osvobozeni dle ZDP).
- Srovnani: stejne vklady do SPY (nakup na close, bez nakladu a dani) a stejne vklady "pod matraci".
"""
import os, sys, json, time
import numpy as np, pandas as pd
import backtest_drzeni as B
import backtest_8k as K

KURZ = 25.0
START = 1000 / KURZ
DEP = 2000 / KURZ
LIMIT_OSVOB = 100000 / KURZ
COST = K.COST


def rules(equity):
    if equity < 500:
        return 0.015, 0.30, 3, 8.0
    return B.RISK, B.POS_CAP, B.MAX_POS, 25.0


def simulate_dep(I, dates, cands, ann, start):
    O, H, L, C = (I[k].values for k in ('O', 'H', 'L', 'C'))
    A14 = I['ATR14'].values
    T = len(dates)
    wd = np.array([d.weekday() for d in dates])
    cash, pos, trades = START, {}, []
    eq, units = np.full(T, np.nan), np.full(T, np.nan)
    flows = [(dates[0], 0.0)]
    year_pnl, year_gross, cur_year, cur_month = 0.0, 0.0, None, None
    t0 = dates.searchsorted(start)
    nu = 1.0

    def close_pos(j, px, d):
        nonlocal cash, year_pnl, year_gross
        p = pos.pop(j)
        val = p['sh'] * px * (1 - COST)
        cash += val
        year_gross += val
        pnl = val - p['basis']
        year_pnl += pnl
        trades.append((dates[p['d0']], dates[d], j, pnl, pnl / p['risk'], d - p['d0']))

    def value(d):
        return cash + sum(p['sh'] * (C[d, j] if not np.isnan(C[d, j]) else p['entry']) for j, p in pos.items())

    flows = [(dates[t0], -START)]
    for d in range(t0, T):
        y, m = dates[d].year, dates[d].month
        if cur_year is None:
            cur_year, cur_month = y, m
        if y != cur_year:
            if year_pnl > 0 and year_gross > LIMIT_OSVOB:
                cash -= B.TAX * year_pnl
            year_pnl, year_gross, cur_year = 0.0, 0.0, y
        if m != cur_month:
            cur_month = m
            v = value(d - 1)
            px_unit = v / nu
            cash += DEP
            nu += DEP / px_unit
            flows.append((dates[d], -DEP))
        for j in list(pos):
            p = pos[j]
            o, l, c = O[d, j], L[d, j], C[d, j]
            if np.isnan(o) or np.isnan(c):
                continue
            if o <= p['stop']:
                close_pos(j, o, d)
                continue
            if l <= p['stop']:
                close_pos(j, p['stop'], d)
                continue
            held = d - p['d0']
            p['hc'] = max(p['hc'], c)
            a = p['atr']
            if (held == 3 and c < p['entry'] + 0.4 * a) or held >= 10 or d + 1 >= p['nxt'] or \
                    (wd[d] == 4 and c < p['entry'] * 1.015):
                close_pos(j, c, d)
                continue
            if c >= p['entry'] + 1.5 * a:
                p['stop'] = max(p['stop'], p['entry'] * 1.0045)
            if c >= p['entry'] + 2 * a and not np.isnan(A14[d, j]):
                p['stop'] = max(p['stop'], p['hc'] - 1.8 * A14[d, j])
        equity = value(d)
        risk, cap, maxpos, minord = rules(equity)
        for j in cands.get(d, []):
            if len(pos) >= maxpos:
                break
            if j in pos:
                continue
            c, a = C[d, j], A14[d - 1, j]
            if np.isnan(c) or np.isnan(a) or a <= 0:
                continue
            st = max(c - 2 * a, L[d, j] * 0.995)
            dist = (c - st) / c
            if dist < 0.03:
                st, dist = c * 0.97, 0.03
            if dist > 0.08:
                continue
            per = (c - st) + COST * c * 2
            sh = min(equity * risk / per, equity * cap / c)
            val = sh * c * (1 + COST)
            if val > cash:
                sh = cash / (c * (1 + COST))
                val = cash
            if sh * c < minord:
                continue
            cash -= val
            an = ann.get(j, np.array([], dtype=int))
            k = an.searchsorted(d + 1)
            pos[j] = dict(d0=d, entry=c, sh=sh, basis=val, stop=st, risk=sh * per, hc=c, atr=a,
                          nxt=an[k] if k < len(an) else 10 ** 9)
        eq[d] = value(d)
        units[d] = eq[d] / nu
    e = pd.Series(eq, index=dates).dropna()
    u = pd.Series(units, index=dates).dropna()
    return e, u, flows, pd.DataFrame(trades, columns=['vstup', 'vystup', 'j', 'pnl', 'R', 'dni'])


def xirr(flows, final_date, final_value):
    fl = flows + [(final_date, final_value)]
    t0 = fl[0][0]
    ts = np.array([(d - t0).days / 365.25 for d, _ in fl])
    cf = np.array([v for _, v in fl])
    lo, hi = -0.99, 5.0
    f = lambda r: (cf / (1 + r) ** ts).sum()
    if f(lo) * f(hi) > 0:
        return np.nan
    for _ in range(200):
        mid = (lo + hi) / 2
        if f(lo) * f(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def spy_dep(ix, dates, flows):
    """Stejne vklady do SPY na close dne vkladu."""
    s = ix.reindex(dates).ffill()
    sh = 0.0
    fd = {d: -v for d, v in flows}
    vals = []
    for d in s.index[s.index >= flows[0][0]]:
        if d in fd:
            sh += fd[d] / s[d]
        vals.append(sh * s[d])
    v = pd.Series(vals, index=s.index[s.index >= flows[0][0]])
    return v.iloc[-1], (v / v.cummax() - 1).min()


def main():
    lines = []

    def w(s=''):
        print(s, flush=True)
        lines.append(s)

    w('Krok 1 s mesicnimi vklady (start 1 000 Kc + 2 000 Kc/mesic, kurz 25) - ' + time.strftime('%Y-%m-%d %H:%M'))
    import pickle
    groups = {n: B.index_list(n) for n in B.WIKI}
    allt = sorted(set(sum(groups.values(), [])))
    raw = B.download(allt + ['SPY', '^VIX'], os.path.join(B.OUT, 'ceny_US.pkl'))
    cmap = K.ticker_cik()
    rows_by_t = {t: K.filings(cmap[t]) for t in allt if t in cmap}
    cp = os.path.join(B.OUT, 'vysledky_cache.pkl')
    db = pickle.load(open(cp, 'rb')) if os.path.exists(cp) else {}
    mid = sorted(set(groups.get('S&P 400', []) + groups.get('S&P 600', [])))
    import backtest_a1 as A
    for name, tick in (('MID+SMALL (S&P 400+600)', mid), ('LARGE (S&P 500)', groups.get('S&P 500', []))):
        I, keep, dates, ix = B.prep(raw, tick, 'SPY')
        ann = A.event_frames(db, keep, dates)[4] if db else {}
        C, O, H, L, V = (I[k] for k in ('C', 'O', 'H', 'L', 'V'))
        ret = (C / C.shift(1) - 1).values
        cl = ((C - L) / (H - L).replace(0, np.nan)).values
        adv = (C * V).shift(1).rolling(20).mean()
        base = ((ret >= 0.025) & (ret <= 0.12) & (V >= 2 * I['V20']).values & (C > O).values & (cl >= 0.65) &
                (C >= 5).values & (adv >= 5e6).values & I['ATR14'].notna().values)
        w('')
        w(f'===== {name} =====')
        for var, items in (('8K-VSE', K.ITEMS_ALL), ('8K-BEZ-VYSLEDKU', K.ITEMS_NOEARN), ('CENA-BEZ-8K', None)):
            sig = base & (K.event_matrix(rows_by_t, keep, dates, items) if items else True)
            cands = B.build_cands(pd.DataFrame(sig, index=dates), pd.DataFrame(ret, index=dates))
            w(f'--- {var} ---')
            for start, lab in ((B.TEST_FROM, 'od 2013'), (B.SPLIT, 'od 2020')):
                e, u, flows, tr = simulate_dep(I, dates, cands, ann, start)
                dep = -sum(v for _, v in flows)
                fin = e.iloc[-1]
                g, l = tr.loc[tr.pnl > 0, 'pnl'].sum(), -tr.loc[tr.pnl < 0, 'pnl'].sum()
                pf = g / l if l > 0 else np.nan
                dd = (u / u.cummax() - 1).min()
                sv, sdd = spy_dep(ix, dates, flows)
                w(f'  {lab}: vlozeno {dep * KURZ:,.0f} Kc, konec {fin * KURZ:,.0f} Kc (zisk {(fin - dep) * KURZ:+,.0f} Kc), '
                  f'rocni vynos vkladu (XIRR) {xirr(flows, e.index[-1], fin):.1%}, propad {dd:.0%}, '
                  f'obchodu {len(tr)}, PF {pf:.2f}')
                w(f'     SPY se stejnymi vklady: konec {sv * KURZ:,.0f} Kc, XIRR {xirr(flows, e.index[-1], sv):.1%}, '
                  f'propad {sdd:.0%} | pod matraci {dep * KURZ:,.0f} Kc')
    with open(os.path.join(B.OUT, '8k_vklady.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')


if __name__ == '__main__':
    main()
