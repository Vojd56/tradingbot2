# -*- coding: utf-8 -*-
"""Backtest A1 (PEAD po vysledcich firem) z vyzkumu "Swingove obchodovani akcii s LLM" (2. 11. 2026).
Drzeni do 35 dni, nikdy nakup a prodej ve stejny den. Nic nekupuje, nic v botovi nemeni, Gemini nevola.

Spusteni ze slozky bota:  python _analyza_swing\\backtest_a1.py
Potrebuje vedle sebe backtest_drzeni.py a jeho cache cen (_analyza_drzeni\\ceny_US.pkl, jinak stahne).
Vystup: _analyza_swing\\_analyza_drzeni\\a1.txt (+ obchody_A1_*.csv, vysledky_cache.pkl)

Data vysledku: Yahoo pres yfinance get_earnings_dates (datum+cas, odhad EPS, skutecne EPS, prekvapeni %).
Den reakce: zverejneni pred 16:00 NY -> tentyz den, po 16:00 -> dalsi obchodni den.
A1-EPS (podle vyzkumu, bez Gemini a bez trzeb, ty zdarma nejsou):
  prekvapeni EPS >= 5 % a (skutecne - odhad)/cena >= 0,005; v den reakce vynos +2,5 az +12 %,
  zaver v horni casti svicky (>= 0,65), objem >= 1,8x prumer 20 dni, Close > SMA200 a > EMA50;
  trh: SPY > SMA200 a VIX < 28. Vstup na open dalsiho dne.
A1-CENA: stejna reakce ceny po vysledcich, ale bez podminky na prekvapeni EPS (ukaze, co pridavaji data o EPS).
Stop max(Low reakce - 0,2 ATR, vstup - 2,5 ATR); od +1R chandelier 3x ATR22; konec po 35 dnech,
v 10. den pod +0,3R prodej; prodej den pred dalsimi vysledky. Kontroly: NAHODNE a TREND (30 seminek).
Gemini veto (guidance) se offline otestovat neda -> vysledek je bez nej.
"""
import os, sys, time, pickle
import numpy as np, pandas as pd
import backtest_drzeni as B

OUT = B.OUT
CACHE = os.path.join(OUT, 'vysledky_cache.pkl')


def earnings(tickers):
    import yfinance as yf
    db = {}
    if os.path.exists(CACHE):
        with open(CACHE, 'rb') as f:
            db = pickle.load(f)
    todo = [t for t in tickers if t not in db]
    for k, t in enumerate(todo):
        for att in range(3):
            try:
                df = yf.Ticker(t).get_earnings_dates(limit=100)
                db[t] = df if df is not None else pd.DataFrame()
                break
            except Exception as ex:
                if att == 2:
                    db[t] = pd.DataFrame()
                    print('  bez vysledku', t, str(ex)[:80])
                time.sleep(2 + 3 * att)
        if k % 50 == 49 or k == len(todo) - 1:
            with open(CACHE, 'wb') as f:
                pickle.dump(db, f)
            print(f'  vysledky {k + 1}/{len(todo)}', flush=True)
        time.sleep(0.3)
    return db


def event_frames(db, keep, dates):
    """Vrati matice (dny x akcie): surprise %, surprise/cena (pocita se pozdeji), odhad, skutecnost a seznam dni vysledku."""
    T, N = len(dates), len(keep)
    sp = np.full((T, N), np.nan)
    est = np.full((T, N), np.nan)
    act = np.full((T, N), np.nan)
    isev = np.zeros((T, N), bool)
    ann = {}
    dn = dates.tz_localize(None) if dates.tz is not None else dates
    first_seen = None
    for j, t in enumerate(keep):
        df = db.get(t)
        if df is None or len(df) == 0:
            continue
        lst = []
        for ts, row in df.iterrows():
            ts = pd.Timestamp(ts)
            if ts.tzinfo is not None:
                ts = ts.tz_convert('America/New_York')
            day = pd.Timestamp(ts.date())
            after = ts.hour >= 16
            i = dn.searchsorted(day)
            if i >= T:
                continue
            if dn[i] == day and after:
                i += 1
            if i >= T:
                continue
            lst.append(dn.searchsorted(day))
            e, a, s = row.get('EPS Estimate', np.nan), row.get('Reported EPS', np.nan), row.get('Surprise(%)', np.nan)
            if pd.isna(a):
                continue  # budouci vysledky
            isev[i, j] = True
            est[i, j], act[i, j], sp[i, j] = e, a, s
            first_seen = dn[i] if first_seen is None or dn[i] < first_seen else first_seen
        ann[j] = np.array(sorted(set(lst)))
    return isev, sp, est, act, ann, first_seen


def simulate_a1(I, dates, cands, ST, cost, ann):
    O, H, L, C = (I[k].values for k in ('O', 'H', 'L', 'C'))
    A22 = I['ATR22'].values
    T = len(dates)
    cash, pos, trades, eq = B.EQ0, {}, [], np.full(T, np.nan)
    year_pnl, cur_year = 0.0, None
    t0 = dates.searchsorted(B.TEST_FROM)
    for d in range(t0, T):
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
                val = p['sh'] * px * (1 - cost)
                cash += val
                pnl = val - p['basis']
                year_pnl += pnl
                trades.append((dates[p['d0']], dates[d], j, pnl, pnl / p['risk'], d - p['d0']))
                del pos[j]
        if d - 1 in cands:
            equity = cash + sum(p['sh'] * (C[d - 1, j] if not np.isnan(C[d - 1, j]) else p['entry']) for j, p in pos.items())
            for j in cands[d - 1]:
                if len(pos) >= B.MAX_POS:
                    break
                if j in pos:
                    continue
                o, st = O[d, j], ST[d - 1, j]
                if np.isnan(o) or np.isnan(st) or o <= st:
                    continue
                r = o - st
                per = r + cost * o * 2
                sh = min(equity * B.RISK / per, equity * B.POS_CAP / o)
                val = sh * o * (1 + cost)
                if val > cash:
                    sh = cash / (o * (1 + cost))
                    val = cash
                if sh * o < 50:
                    continue
                cash -= val
                stp = max(st, o - 2.5 * I['ATR14'].values[d - 1, j])
                a = ann.get(j, np.array([], dtype=int))
                k = a.searchsorted(d + 1)
                nxt = a[k] if k < len(a) else 10 ** 9
                pos[j] = dict(d0=d, entry=o, sh=sh, basis=val, stop=stp, r=o - stp, risk=sh * per, hh=H[d, j],
                              nxt=nxt, exit_next=False)
        for j, p in pos.items():
            c = C[d, j]
            if np.isnan(c):
                continue
            if not np.isnan(H[d, j]):
                p['hh'] = max(p['hh'], H[d, j])
            held = d - p['d0']
            if p['hh'] >= p['entry'] + p['r'] and not np.isnan(A22[d, j]):
                p['stop'] = max(p['stop'], p['hh'] - 3 * A22[d, j])
            if held >= 35 or (held == 10 and c < p['entry'] + 0.3 * p['r']) or d + 2 >= p['nxt']:
                p['exit_next'] = True
        eq[d] = cash + sum(p['sh'] * (C[d, j] if not np.isnan(C[d, j]) else p['entry']) for j, p in pos.items())
    e = pd.Series(eq, index=dates).ffill().dropna()
    return e, pd.DataFrame(trades, columns=['vstup', 'vystup', 'j', 'pnl', 'R', 'dni'])


def run(name, raw, tick, db, vix, w, cost):
    I, keep, dates, ix = B.prep(raw, tick, 'SPY')
    isev, sp, est, act, ann, first = event_frames(db, keep, dates)
    C, O, H, L, V = (I[k] for k in ('C', 'O', 'H', 'L', 'V'))
    ret = (C / C.shift(1) - 1).values
    cl = ((C - L) / (H - L).replace(0, np.nan)).values
    volok = (V >= 1.8 * I['V20']).values
    trend = ((C > I['SMA200']) & (C > I['EMA50'])).values
    sma = ix.rolling(200).mean()
    rg = ((ix > sma) & (vix.reindex(ix.index).ffill() < 28)).reindex(dates).fillna(False).values[:, None]
    valid = (I['SMA200'].notna() & I['ATR22'].notna() & C.notna() & O.shift(-1).notna()).values
    react = isev & (ret >= 0.025) & (ret <= 0.12) & (cl >= 0.65) & volok & trend & rg & valid
    with np.errstate(invalid='ignore', divide='ignore'):
        sup = (act - est) / C.shift(1).values
    epsok = (sp >= 5) & (sup >= 0.005)
    ST = (L - 0.2 * I['ATR14']).values
    w('')
    w(f'===== {name}: {len(keep)} akcii, s daty vysledku {sum(1 for j in ann if len(ann[j]))}, '
      f'nejstarsi vysledek {first.date() if first is not None else "?"}, naklad {cost:.2%}/strana =====')
    if first is not None and first > B.TEST_FROM:
        w(f'  POZOR: data vysledku zacinaji az {first.date()}, obdobi pred tim je bez signalu.')
    for (a, b, lab) in ((B.TEST_FROM, dates[-1] + pd.Timedelta(days=1), 'cele obdobi'),
                        (B.TEST_FROM, B.SPLIT, '2013-2019'), (B.SPLIT, dates[-1] + pd.Timedelta(days=1), '2020-dnes')):
        bm = B.bench(ix, a, b)
        w(f'  SPY {lab}: CAGR {bm["cagr"]:.1%}, propad {bm["dd"]:.0%}, CAGR/propad {bm["cagr"] / abs(bm["dd"]):.2f}')
    trendpool = valid & ((C > I['EMA50']) & (I['EMA50'] > I['SMA200'])).values
    for var, sig, rank in (('A1-EPS', react & epsok, np.nan_to_num(sp, nan=-9)), ('A1-CENA', react, ret)):
        cands = B.build_cands(pd.DataFrame(sig, index=dates), pd.DataFrame(rank, index=dates))
        e, tr = simulate_a1(I, dates, cands, ST, cost, ann)
        t2 = tr.copy()
        t2['akcie'] = [keep[j] for j in t2.j]
        t2.drop(columns='j').to_csv(os.path.join(OUT, f'obchody_{var}_{name.split()[0]}.csv'), index=False)
        w('')
        w(f'--- {var} {name}: signalu {int(sig.sum())} ve {len(cands)} dnech ---')
        ctl = {nm: [simulate_a1(I, dates, B.random_cands(cands, pool, 2000 + s), ST, cost, ann) for s in range(B.SEEDS)]
               for nm, pool in (('NAHODNE', valid), ('TREND', trendpool))}
        for (a, b, lab) in ((None, None, 'cele obdobi'), (B.TEST_FROM, B.SPLIT, '2013-2019'),
                            (B.SPLIT, dates[-1] + pd.Timedelta(days=1), '2020-dnes')):
            st = B.stats(e, tr, a, b)
            if not st or st['n'] == 0:
                w(f'  {lab}: zadne obchody')
                continue
            w(f'  {lab}: obchodu {st["n"]}, vyhernich {st["win"]:.0%}, PF {st["pf"]:.2f}, prumer {st["avgR"]:+.2f}R, '
              f'drzeni {st["dni"]:.0f} dni, CAGR po dani {st["cagr"]:.1%}, propad {st["dd"]:.0%}, CAGR/propad {st["mar"]:.2f}')
            for nm in ctl:
                ss = [x for x in (B.stats(er, trr, a, b) for er, trr in ctl[nm]) if x]
                w(f'     {nm:8s} p10/p50/p90: CAGR {B.pct([x["cagr"] for x in ss], 10):.1%} / {B.pct([x["cagr"] for x in ss], 50):.1%} / '
                  f'{B.pct([x["cagr"] for x in ss], 90):.1%}, PF {B.pct([x["pf"] for x in ss], 50):.2f} (p90 {B.pct([x["pf"] for x in ss], 90):.2f}), '
                  f'prumer R p50 {B.pct([x["avgR"] for x in ss], 50):+.2f}')
        st = B.stats(e, tr)
        if not st or st['n'] == 0:
            continue
        bm = B.bench(ix, B.TEST_FROM, dates[-1] + pd.Timedelta(days=1))
        p90 = max(B.pct([B.stats(er, trr)['cagr'] for er, trr in ctl[nm]], 90) for nm in ctl)
        checks = {'>=100 obchodu': st['n'] >= 100, 'PF>=1,3': st['pf'] >= 1.3, 'propad<15 %': st['dd'] > -0.15,
                  'nad p90 kontrol': st['cagr'] > p90, 'CAGR/propad > index': st['mar'] > bm['cagr'] / abs(bm['dd'])}
        w('  LAŤKA: ' + ', '.join(f'{k} {"ANO" if v else "ne"}' for k, v in checks.items()) +
          f'  => {"PROSLO" if all(checks.values()) else "NEPROSLO"}')


def main():
    os.makedirs(OUT, exist_ok=True)
    lines = []

    def w(s=''):
        print(s, flush=True)
        lines.append(s)

    w('Backtest A1 PEAD (drzeni do 35 dni) - ' + time.strftime('%Y-%m-%d %H:%M'))
    w('Laťka: >=100 obchodu, PF >=1,3, propad <15 %, lepsi nez 90. percentil nahodnych kontrol a lepsi CAGR/propad nez SPY.')
    if '--test' in sys.argv:
        raw, groups, db, vix = fake()
    else:
        groups = {}
        for n in B.WIKI:
            try:
                groups[n] = B.index_list(n)
            except Exception as ex:
                print('wiki chyba', n, ex)
        allt = sorted(set(sum(groups.values(), [])))
        raw = B.download(allt + ['SPY', '^VIX'], os.path.join(OUT, 'ceny_US.pkl'))
        vix = raw['Close']['^VIX'].dropna()
        db = earnings(allt)
    mid = sorted(set(groups.get('S&P 400', []) + groups.get('S&P 600', [])))
    run('MID+SMALL (S&P 400+600)', raw, mid, db, vix, w, B.COST['US'])
    run('LARGE (S&P 500)', raw, groups.get('S&P 500', []), db, vix, w, B.COST['US'])
    with open(os.path.join(OUT, 'a1.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')


def fake():
    rng = np.random.default_rng(2)
    dates = pd.bdate_range('2011-01-03', '2026-10-30')
    tick = [f'T{i}' for i in range(40)]
    cols, db = {}, {}
    for t in tick + ['SPY', '^VIX']:
        r = rng.normal(0.0004, 0.015, len(dates))
        evd = dates[60::63]
        if t in tick:
            for dd in evd:
                r[dates.get_loc(dd)] += rng.normal(0.02, 0.05)
        c = 50 * np.exp(np.cumsum(r))
        o = np.r_[c[0], c[:-1]] * (1 + rng.normal(0, 0.002, len(dates)))
        h = np.maximum(o, c) * (1 + abs(rng.normal(0, 0.004, len(dates))))
        l = np.minimum(o, c) * (1 - abs(rng.normal(0, 0.004, len(dates))))
        v = rng.lognormal(12, 0.3, len(dates))
        if t in tick:
            for dd in evd:
                v[dates.get_loc(dd)] *= 3
            idx = pd.DatetimeIndex([pd.Timestamp(x).tz_localize('America/New_York') + pd.Timedelta(hours=8) for x in evd])
            db[t] = pd.DataFrame({'EPS Estimate': 1.0, 'Reported EPS': 1.0 + rng.normal(0.05, 0.1, len(evd)) * 10,
                                  'Surprise(%)': rng.normal(5, 10, len(evd))}, index=idx)
        if t == '^VIX':
            c = o = h = l = np.full(len(dates), 18.0)
        for k, x in zip(('Open', 'High', 'Low', 'Close', 'Volume'), (o, h, l, c, v)):
            cols[(k, t)] = x
    raw = pd.DataFrame(cols, index=dates)
    return raw, {'S&P 400': tick[:20], 'S&P 600': tick[20:30], 'S&P 500': tick[30:]}, db, raw['Close']['^VIX']


if __name__ == '__main__':
    main()
