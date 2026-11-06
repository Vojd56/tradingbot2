# -*- coding: utf-8 -*-
"""R1: prepinac rezimu trhu (2. 11. 2026). Navrh: https://claude.ai/artifact/1PfMni7AGQfAvmyoMNN6nk
Nic nekupuje, nic v botovi nemeni, Gemini nevola. Nikdy prodej v den nakupu.

Spusteni ze slozky bota:  python _analyza_swing\\backtest_rezim.py
Potrebuje vedle sebe backtest_drzeni.py (+ cache _analyza_drzeni\\ceny_US.pkl). Vystup: _analyza_drzeni\\rezim.txt

Rezim (vecer, plati po 2 dnech po sobe): MEDVED = SPY < SMA200 nebo VIX > 30;
NERVOZITA = jinak VIX >= 20 nebo podil akcii nad SMA50 < 40 %; KLID = zbytek.
M  (lidri odvetvi, v KLIDU): top 3 sektory (prumer vynosu 63 a 126 dni, sektor = rovnovazny prumer clenu),
   akcie v hornich 20 % vynosu 126 dni, Close >= 0,9 x max 252, Close > SMA50 > SMA200; spoustec: dotek EMA20 a zaver
   nad EMA20 a nahoru, nebo nove 20denni maximum s objemem >= 1,3x. Stop 2,5 ATR, od +1R chandelier 3 ATR22,
   prodej: zaver pod SMA50, sektor mimo top 5, rezim MEDVED, 40 dni. Riziko 0,5 %.
MR (odraz po propadu, v NERVOZITE, jen S&P 500): Close > SMA200, RSI(2) < 10, max High 5 dni - Close >= 2 ATR.
   Stop 3 ATR, prodej: zaver nad SMA5 nebo RSI(2) > 70, 10 dni. Riziko 0,25 %.
Varianty: R1 (M+MR s rezimy), M-vzdy, MR-vzdy, M-rezim, MR-rezim; kazda i s JADREM (volne penize v SPY).
Kontroly NAHODNE/TREND: stejne dny, stejny typ obchodu, nahodne akcie. Naklad 0,20 %/strana, dan 15 % ze zisku obchodu.
"""
import os, io, sys, time, random
import numpy as np, pandas as pd
import backtest_drzeni as B

OUT = B.OUT
COST = 0.0020
RISKS = {'M': 0.005, 'MR': 0.0025}


def sectors():
    import requests
    res = {}
    for n, page in B.WIKI.items():
        html = requests.get('https://en.wikipedia.org/wiki/' + page, headers={'User-Agent': 'Mozilla/5.0 BotBacktest'}, timeout=30).text
        for t in pd.read_html(io.StringIO(html)):
            cols = {c: str(c).strip().lower() for c in t.columns}
            sym = next((c for c, l in cols.items() if l in ('symbol', 'ticker symbol', 'ticker')), None)
            sec = next((c for c, l in cols.items() if l.startswith('gics sector') or l == 'sector'), None)
            if sym is not None and sec is not None and len(t) > 100:
                for s, g in zip(t[sym].astype(str), t[sec].astype(str)):
                    res[s.strip().replace('.', '-')] = (g.strip(), n)
                break
    return res


def wilder(x, n):
    return x.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def build(raw, secmap, vix, vix3m):
    tick = sorted(t for t in secmap if t in raw['Close'])
    I, keep, dates, ix = B.prep(raw, tick, 'SPY')
    C, H, L, V = I['C'], I['H'], I['L'], I['V']
    d = C.diff()
    up, dn = wilder(d.clip(lower=0), 2), wilder((-d).clip(lower=0), 2)
    I['RSI2'] = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    I['SMA5'], I['SMA50'] = C.rolling(5).mean(), C.rolling(50).mean()
    # rezim
    v = vix.reindex(dates).ffill()
    breadth = (C > I['SMA50']).sum(axis=1) / C.notna().sum(axis=1).replace(0, np.nan)
    sma200 = ix.rolling(200).mean()
    raw_reg = np.where((ix < sma200) | (v > 30), 2, np.where((v >= 20) | (breadth < 0.40), 1, 0))
    reg = np.zeros(len(dates), int)
    cur = 0
    for k in range(len(dates)):
        if k >= 1 and raw_reg[k] == raw_reg[k - 1]:
            cur = raw_reg[k]
        reg[k] = cur
    # sektory
    secs = pd.Series({t: secmap[t][0] for t in keep})
    r63, r126 = C / C.shift(63) - 1, C / C.shift(126) - 1
    sm = ((r63 + r126) / 2).T.groupby(secs).mean().T  # dny x sektory
    rank = sm.rank(axis=1, ascending=False)
    sec_idx = {s: i for i, s in enumerate(sm.columns)}
    col_sec = np.array([sec_idx[secs[t]] for t in keep])
    srank = rank.values[:, col_sec]  # dny x akcie: poradi sektoru
    top20 = r126.ge(r126.quantile(0.8, axis=1), axis=0)
    trig = ((L <= I['EMA20']) & (C > I['EMA20']) & (C > C.shift(1))) | \
           ((C > H.shift(1).rolling(20).max()) & (V >= 1.3 * I['V20']))
    sigM = (pd.DataFrame(srank <= 3, index=dates, columns=keep) & top20 & (C >= 0.9 * H.rolling(252).max()) &
            (C > I['SMA50']) & (I['SMA50'] > I['SMA200']) & trig)
    is500 = np.array([secmap[t][1] == 'S&P 500' for t in keep])
    sigMR = ((C > I['SMA200']) & (I['RSI2'] < 10) & ((H.rolling(5).max() - C) >= 2 * I['ATR14'])) & is500[None, :]
    valid = (I['SMA200'].notna() & I['ATR22'].notna() & C.notna() & I['O'].shift(-1).notna()).values
    trendpool = valid & ((C > I['EMA50']) & (I['EMA50'] > I['SMA200'])).values
    spyret = (ix / ix.shift(1) - 1).reindex(dates).fillna(0).values
    return dict(I=I, keep=keep, dates=dates, ix=ix, reg=reg, srank=srank, sigM=sigM.fillna(False).values & valid,
                sigM_rank=r126.values, sigMR=sigMR.fillna(False).values & valid, sigMR_rank=-I['RSI2'].values,
                valid=valid, trendpool=trendpool, spyret=spyret, breadth=breadth)


def make_cands(D, use_M, use_MR, gate):
    out = {}
    reg = D['reg']
    for kind, use, sig, rk, want in (('M', use_M, D['sigM'], D['sigM_rank'], 0), ('MR', use_MR, D['sigMR'], D['sigMR_rank'], 1)):
        if not use:
            continue
        for d in np.where(sig.any(axis=1))[0]:
            if gate and reg[d] != want:
                continue
            js = np.where(sig[d])[0]
            js = js[np.argsort(-np.nan_to_num(rk[d, js], nan=-1e9))]
            out.setdefault(d, []).extend((int(j), kind) for j in js)
    return out


def rand_cands(cands, D, pool, seed):
    rng = random.Random(seed)
    out = {}
    p500 = None
    for d, lst in cands.items():
        avail = list(np.where(pool[d])[0])
        if not avail:
            continue
        k = min(len(lst), len(avail))
        picks = rng.sample(avail, k)
        out[d] = [(int(j), kind) for j, (_, kind) in zip(picks, lst)]
    return out


def simulate(D, cands, gate, park):
    I, dates = D['I'], D['dates']
    O, H, L, C = (I[k].values for k in ('O', 'H', 'L', 'C'))
    A14, A22, S50, S5, R2 = (I[k].values for k in ('ATR14', 'ATR22', 'SMA50', 'SMA5', 'RSI2'))
    srank, reg, spyret = D['srank'], D['reg'], D['spyret']
    T = len(dates)
    cash, pos, trades, eq = B.EQ0, {}, [], np.full(T, np.nan)
    year_pnl, cur_year = 0.0, None
    t0 = dates.searchsorted(B.TEST_FROM)
    for d in range(t0, T):
        if park:
            cash *= 1 + spyret[d]
        y = dates[d].year
        if cur_year is None:
            cur_year = y
        if y != cur_year:
            if year_pnl > 0:
                cash -= B.TAX * year_pnl
            year_pnl, cur_year = 0.0, y
        for j in list(pos):
            p = pos[j]
            o, l = O[d, j], L[d, j]
            if np.isnan(o):
                continue
            px = None
            if p['exit_next'] or o <= p['stop']:
                px = o
            elif l <= p['stop']:
                px = p['stop']
            if px is not None:
                val = p['sh'] * px * (1 - COST)
                cash += val
                pnl = val - p['basis']
                year_pnl += pnl
                trades.append((dates[p['d0']], dates[d], j, p['kind'], pnl, pnl / p['risk'], d - p['d0']))
                del pos[j]
        if d - 1 in cands:
            equity = cash + sum(p['sh'] * (C[d - 1, j] if not np.isnan(C[d - 1, j]) else p['entry']) for j, p in pos.items())
            for j, kind in cands[d - 1]:
                if len(pos) >= B.MAX_POS:
                    break
                if j in pos:
                    continue
                o, a = O[d, j], A14[d - 1, j]
                if np.isnan(o) or np.isnan(a) or a <= 0:
                    continue
                st = C[d - 1, j] - (2.5 if kind == 'M' else 3.0) * a
                if o <= st:
                    continue
                r = o - st
                per = r + COST * o * 2
                sh = min(equity * RISKS[kind] / per, equity * B.POS_CAP / o)
                val = sh * o * (1 + COST)
                if val > cash:
                    sh = cash / (o * (1 + COST))
                    val = cash
                if sh * o < 50:
                    continue
                cash -= val
                pos[j] = dict(d0=d, kind=kind, entry=o, sh=sh, basis=val, stop=st, r=r, risk=sh * per, hh=H[d, j], exit_next=False)
        for j, p in pos.items():
            c = C[d, j]
            if np.isnan(c):
                continue
            held = d - p['d0']
            if p['kind'] == 'M':
                if not np.isnan(H[d, j]):
                    p['hh'] = max(p['hh'], H[d, j])
                if p['hh'] >= p['entry'] + p['r'] and not np.isnan(A22[d, j]):
                    p['stop'] = max(p['stop'], p['hh'] - 3 * A22[d, j])
                if held >= 40 or (not np.isnan(S50[d, j]) and c < S50[d, j]) or srank[d, j] > 5 or (gate and reg[d] == 2):
                    p['exit_next'] = True
            else:
                if held >= 10 or (not np.isnan(S5[d, j]) and c > S5[d, j]) or (not np.isnan(R2[d, j]) and R2[d, j] > 70):
                    p['exit_next'] = True
        eq[d] = cash + sum(p['sh'] * (C[d, j] if not np.isnan(C[d, j]) else p['entry']) for j, p in pos.items())
    e = pd.Series(eq, index=dates).ffill().dropna()
    return e, pd.DataFrame(trades, columns=['vstup', 'vystup', 'j', 'typ', 'pnl', 'R', 'dni'])


def main():
    os.makedirs(OUT, exist_ok=True)
    lines = []

    def w(s=''):
        print(s, flush=True)
        lines.append(s)

    w('R1 prepinac rezimu - ' + time.strftime('%Y-%m-%d %H:%M'))
    w('Laťka: >=100 obchodu, PF >=1,3, propad <15 % (bez jadra), nad p90 kontrol, a NOVE i PF >= 1,2 po roce 2020.')
    if '--test' in sys.argv:
        raw, secmap, vix = fake()
        vix3m = None
    else:
        secmap = sectors()
        raw = B.download(sorted(secmap) + ['SPY', '^VIX'], os.path.join(OUT, 'ceny_US.pkl'))
        vix = raw['Close']['^VIX'].dropna()
        vix3m = None
    D = build(raw, secmap, vix, vix3m)
    dates, ix = D['dates'], D['ix']
    reg = pd.Series(D['reg'], index=dates)
    rr = reg[reg.index >= B.TEST_FROM]
    w(f'Akcii {len(D["keep"])}. Rezim od 2013: KLID {np.mean(rr == 0):.0%}, NERVOZITA {np.mean(rr == 1):.0%}, MEDVED {np.mean(rr == 2):.0%} dni.')
    end = dates[-1] + pd.Timedelta(days=1)
    for (a, b, lab) in ((B.TEST_FROM, end, 'cele obdobi'), (B.TEST_FROM, B.SPLIT, '2013-2019'), (B.SPLIT, end, '2020-dnes')):
        bm = B.bench(ix, a, b)
        w(f'  SPY {lab}: CAGR {bm["cagr"]:.1%}, propad {bm["dd"]:.0%}, CAGR/propad {bm["cagr"] / abs(bm["dd"]):.2f}')
    variants = [('R1 (M+MR podle rezimu)', True, True, True), ('M-rezim (jen v klidu)', True, False, True),
                ('MR-rezim (jen v nervozite)', False, True, True), ('M-vzdy', True, False, False), ('MR-vzdy', False, True, False)]
    for name, um, umr, gate in variants:
        cands = make_cands(D, um, umr, gate)
        e, tr = simulate(D, cands, gate, False)
        ej, trj = simulate(D, cands, gate, True)
        tr2 = tr.copy()
        tr2['akcie'] = [D['keep'][j] for j in tr2.j]
        tr2.drop(columns='j').to_csv(os.path.join(OUT, f'obchody_R1_{name.split()[0]}.csv'), index=False)
        w('')
        w(f'--- {name}: signalnich dni {len(cands)} ---')
        ctl = {nm: [simulate(D, rand_cands(cands, D, pool, 3000 + s), gate, False) for s in range(B.SEEDS)]
               for nm, pool in (('NAHODNE', D['valid']), ('TREND', D['trendpool']))}
        for (a, b, lab) in ((None, None, 'cele obdobi'), (B.TEST_FROM, B.SPLIT, '2013-2019'), (B.SPLIT, end, '2020-dnes')):
            st = B.stats(e, tr, a, b)
            if not st or st['n'] == 0:
                w(f'  {lab}: zadne obchody')
                continue
            sj = B.stats(ej, trj, a, b)
            w(f'  {lab}: obchodu {st["n"]}, vyhernich {st["win"]:.0%}, PF {st["pf"]:.2f}, prumer {st["avgR"]:+.2f}R, drzeni {st["dni"]:.0f} dni, '
              f'CAGR {st["cagr"]:.1%}, propad {st["dd"]:.0%} | s jadrem v SPY: CAGR {sj["cagr"]:.1%}, propad {sj["dd"]:.0%}, CAGR/propad {sj["mar"]:.2f}')
            if a is None:
                for typ in ('M', 'MR'):
                    t = tr[tr.typ == typ]
                    if len(t):
                        g, l_ = t.pnl[t.pnl > 0].sum(), -t.pnl[t.pnl < 0].sum()
                        w(f'     typ {typ}: {len(t)} obchodu, PF {g / l_ if l_ > 0 else float("nan"):.2f}, prumer {t.R.mean():+.2f}R')
            for nm in ctl:
                ss = [x for x in (B.stats(er, trr, a, b) for er, trr in ctl[nm]) if x]
                w(f'     {nm:8s} p10/p50/p90: CAGR {B.pct([x["cagr"] for x in ss], 10):.1%} / {B.pct([x["cagr"] for x in ss], 50):.1%} / '
                  f'{B.pct([x["cagr"] for x in ss], 90):.1%}, PF p50 {B.pct([x["pf"] for x in ss], 50):.2f} (p90 {B.pct([x["pf"] for x in ss], 90):.2f})')
        st = B.stats(e, tr)
        if not st or st['n'] == 0:
            continue
        st2 = B.stats(e, tr, B.SPLIT, end)
        sj = B.stats(ej, trj)
        bm = B.bench(ix, B.TEST_FROM, end)
        p90 = max(B.pct([B.stats(er, trr)['cagr'] for er, trr in ctl[nm]], 90) for nm in ctl)
        checks = {'>=100 obchodu': st['n'] >= 100, 'PF>=1,3': st['pf'] >= 1.3, 'propad<15 %': st['dd'] > -0.15,
                  'nad p90 kontrol': st['cagr'] > p90, 'PF po 2020 >=1,2': bool(st2) and st2.get('pf', 0) >= 1.2,
                  's jadrem CAGR/propad > SPY': sj['mar'] > bm['cagr'] / abs(bm['dd'])}
        w('  LAŤKA: ' + ', '.join(f'{k} {"ANO" if v else "ne"}' for k, v in checks.items()) +
          f'  => {"PROSLO" if all(checks.values()) else "NEPROSLO"}')
    with open(os.path.join(OUT, 'rezim.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')


def fake():
    rng = np.random.default_rng(3)
    dates = pd.bdate_range('2011-01-03', '2026-10-30')
    tick = [f'T{i}' for i in range(60)]
    cols = {}
    mkt = rng.normal(0.0004, 0.01, len(dates))
    for t in tick + ['SPY', '^VIX']:
        r = mkt + rng.normal(0.0002, 0.013, len(dates)) if t != 'SPY' else mkt
        c = 50 * np.exp(np.cumsum(r))
        o = np.r_[c[0], c[:-1]] * (1 + rng.normal(0, 0.002, len(dates)))
        h = np.maximum(o, c) * (1 + abs(rng.normal(0, 0.006, len(dates))))
        l = np.minimum(o, c) * (1 - abs(rng.normal(0, 0.006, len(dates))))
        v = rng.lognormal(12, 0.4, len(dates))
        if t == '^VIX':
            c = o = h = l = 15 + 10 * np.abs(np.sin(np.arange(len(dates)) / 150))
        for k, x in zip(('Open', 'High', 'Low', 'Close', 'Volume'), (o, h, l, c, v)):
            cols[(k, t)] = x
    raw = pd.DataFrame(cols, index=dates)
    secmap = {t: (f'S{i % 6}', 'S&P 500' if i < 30 else 'S&P 400') for i, t in enumerate(tick)}
    return raw, secmap, raw['Close']['^VIX']


if __name__ == '__main__':
    main()
