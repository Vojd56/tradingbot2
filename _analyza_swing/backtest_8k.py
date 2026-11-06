# -*- coding: utf-8 -*-
"""Krok 1 z vyzkumu "Akciova strategie rizena zpravami" (2. 11. 2026): kostra BEZ Gemini.
Nic nekupuje, nic v botovi nemeni, Gemini nevola. Nikdy prodej v den nakupu.

Spusteni ze slozky bota:  python _analyza_swing\\backtest_8k.py
Potrebuje vedle sebe backtest_drzeni.py a backtest_a1.py (+ cache cen a vysledku v _analyza_drzeni).
Data 8-K: https://data.sec.gov/submissions/CIK##########.json (polozky + cas prijeti), cache _analyza_drzeni\\sec8k\\.
Vystup: _analyza_drzeni\\8k.txt (+ obchody_8K_*.csv)

Pravidla (US, den zpravy = T+0):
- 8-K s polozkou 1.01, 2.02, 7.01 nebo 8.01 prijaty do 15:30 NY v den T+0 nebo po 15:30 predchoziho obchodniho dne.
- T+0: vynos proti vcerejsimu close +2,5 az +12 %, objem dne >= 2x prumer 20 dni, Close > Open,
  (Close-Low)/(High-Low) >= 0,65, cena >= 5 USD, prumerny denni obrat 20 dni >= 5 mil. USD.
- Nakup na CLOSE dne T+0 (bot by nakupoval 15:42-15:55 NY; close je mirne optimisticka aproximace).
- Stop max(vstup - 2 ATR14, Low T+0 x 0,995), vzdalenost 3-8 % (pod 3 % -> 3 %, nad 8 % -> obchod se nebere).
- Od close >= vstup + 1,5 ATR stop na vstup x 1,0045; od close >= vstup + 2 ATR posuvny stop nejvyssi close - 1,8 ATR.
- Prodej na close: T+3 pod vstup + 0,4 ATR (mrtvy drift), 10. den, den pred dalsimi vysledky (Yahoo cache z A1),
  v patek pokud zisk < +1,5 %. Stop behem dne (gap pod stop = open).
Varianty: 8K-VSE (1.01/2.02/7.01/8.01), 8K-BEZ-VYSLEDKU (bez 2.02), CENA-BEZ-8K (stejna reakce ceny bez podminky 8-K).
Vesmir MID+SMALL (S&P 400+600, odpovida kapitalizaci ~0,15-8 mld.) a LARGE (S&P 500) pro srovnani.
Kontrola NAHODNE: stejne dny, stejny pocet, nahodna akcie ze stejneho kvintilu obratu (30 seminek).
Laťka vyzkumu pro krok 1: PF >= 1,12 v letech 2020-2026 a nad p90 nahodnych. Nase laťka: PF >= 1,3.
EU (EQS-Adhoc) se otestovat neda: historie zprav neni zdarma.
"""
import os, sys, json, time, random
import numpy as np, pandas as pd
import backtest_drzeni as B

OUT = B.OUT
DIR = os.path.join(OUT, 'sec8k')
UA = 'BotBacktest research-contact@example.com'
COST = 0.0020
ITEMS_ALL = {'1.01', '2.02', '7.01', '8.01'}
ITEMS_NOEARN = {'1.01', '7.01', '8.01'}


def ticker_cik():
    import requests
    p = os.path.join(DIR, 'company_tickers.json')
    os.makedirs(DIR, exist_ok=True)
    if not os.path.exists(p):
        r = requests.get('https://www.sec.gov/files/company_tickers.json', headers={'User-Agent': UA}, timeout=60)
        r.raise_for_status()
        open(p, 'w', encoding='utf-8').write(r.text)
    j = json.load(open(p, encoding='utf-8'))
    return {v['ticker'].upper().replace('.', '-'): int(v['cik_str']) for v in j.values()}


def _rows(block):
    out = []
    forms, acc, items = block.get('form', []), block.get('acceptanceDateTime', []), block.get('items', [])
    for f, a, it in zip(forms, acc, items):
        if f in ('8-K', '8-K/A') and f == '8-K':
            out.append((a, it))
    return out


def filings(cik):
    import requests
    p = os.path.join(DIR, f'{cik}.json')
    if os.path.exists(p):
        return json.load(open(p))
    rows = []
    for att in range(4):
        try:
            r = requests.get(f'https://data.sec.gov/submissions/CIK{cik:010d}.json', headers={'User-Agent': UA}, timeout=60)
            if r.status_code == 404:
                json.dump([], open(p, 'w'))
                return []
            r.raise_for_status()
            j = r.json()
            break
        except Exception as ex:
            print('  chyba', cik, ex)
            time.sleep(3 * (att + 1))
    else:
        return []
    rows += _rows(j.get('filings', {}).get('recent', {}))
    for fx in j.get('filings', {}).get('files', []):
        if fx.get('filingTo', '9999') < '2012-01-01':
            continue
        time.sleep(0.12)
        try:
            r2 = requests.get('https://data.sec.gov/submissions/' + fx['name'], headers={'User-Agent': UA}, timeout=60)
            r2.raise_for_status()
            rows += _rows(r2.json())
        except Exception as ex:
            print('  chyba', cik, fx.get('name'), ex)
    json.dump(rows, open(p, 'w'))
    time.sleep(0.12)
    return rows


def event_matrix(rows_by_t, keep, dates, items):
    """True v den T+0 akcie, kdyz existuje 8-K s danymi polozkami."""
    T, N = len(dates), len(keep)
    ev = np.zeros((T, N), bool)
    dn = dates.tz_localize(None) if dates.tz is not None else dates
    for j, t in enumerate(keep):
        for acc, it in rows_by_t.get(t, []):
            its = {s.strip() for s in str(it).split(',')}
            if not (its & items):
                continue
            # EDGAR uvadi cas prijeti ve vychodnim case (NY), i kdyz retezec konci "Z"
            ts = pd.Timestamp(acc).tz_convert('America/New_York').tz_localize(None)
            day = pd.Timestamp(ts.date())
            i = dn.searchsorted(day)
            if i >= T:
                continue
            if dn[i] == day and (ts.hour * 60 + ts.minute) > 15 * 60 + 30:
                i += 1
            if i < T:
                ev[i, j] = True
    return ev


def simulate(I, dates, cands, ann):
    O, H, L, C = (I[k].values for k in ('O', 'H', 'L', 'C'))
    A14 = I['ATR14'].values
    T = len(dates)
    wd = np.array([d.weekday() for d in dates])
    cash, pos, trades, eq = B.EQ0, {}, [], np.full(T, np.nan)
    year_pnl, cur_year = 0.0, None
    t0 = dates.searchsorted(B.TEST_FROM)

    def close_pos(j, px, d):
        nonlocal cash, year_pnl
        p = pos.pop(j)
        val = p['sh'] * px * (1 - COST)
        cash += val
        pnl = val - p['basis']
        year_pnl += pnl
        trades.append((dates[p['d0']], dates[d], j, pnl, pnl / p['risk'], d - p['d0']))

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
            nxt = p['nxt']
            if (held == 3 and c < p['entry'] + 0.4 * a) or held >= 10 or d + 1 >= nxt or \
                    (wd[d] == 4 and c < p['entry'] * 1.015):
                close_pos(j, c, d)
                continue
            if c >= p['entry'] + 1.5 * a:
                p['stop'] = max(p['stop'], p['entry'] * 1.0045)
            if c >= p['entry'] + 2 * a and not np.isnan(A14[d, j]):
                p['stop'] = max(p['stop'], p['hc'] - 1.8 * A14[d, j])
        equity = cash + sum(p['sh'] * (C[d, j] if not np.isnan(C[d, j]) else p['entry']) for j, p in pos.items())
        for j in cands.get(d, []):
            if len(pos) >= B.MAX_POS:
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
            r = c - st
            per = r + COST * c * 2
            sh = min(equity * B.RISK / per, equity * B.POS_CAP / c)
            val = sh * c * (1 + COST)
            if val > cash:
                sh = cash / (c * (1 + COST))
                val = cash
            if sh * c < 50:
                continue
            cash -= val
            an = ann.get(j, np.array([], dtype=int))
            k = an.searchsorted(d + 1)
            pos[j] = dict(d0=d, entry=c, sh=sh, basis=val, stop=st, risk=sh * per, hc=c, atr=a,
                          nxt=an[k] if k < len(an) else 10 ** 9)
        eq[d] = cash + sum(p['sh'] * (C[d, j] if not np.isnan(C[d, j]) else p['entry']) for j, p in pos.items())
    e = pd.Series(eq, index=dates).ffill().dropna()
    return e, pd.DataFrame(trades, columns=['vstup', 'vystup', 'j', 'pnl', 'R', 'dni'])


def rand_cands(cands, pool, quint, seed):
    rng = random.Random(seed)
    out = {}
    for d, js in cands.items():
        picks = []
        for j in js:
            q = quint[d, j]
            avail = np.where(pool[d] & (quint[d] == q))[0]
            if len(avail):
                picks.append(int(avail[rng.randrange(len(avail))]))
        if picks:
            out[d] = picks
    return out


def run(name, raw, tick, rows_by_t, db, vix, w):
    import backtest_a1 as A
    I, keep, dates, ix = B.prep(raw, tick, 'SPY')
    ann = A.event_frames(db, keep, dates)[4] if db else {}
    C, O, H, L, V = (I[k] for k in ('C', 'O', 'H', 'L', 'V'))
    ret = (C / C.shift(1) - 1).values
    cl = ((C - L) / (H - L).replace(0, np.nan)).values
    adv = (C * V).shift(1).rolling(20).mean()
    base = ((ret >= 0.025) & (ret <= 0.12) & (V >= 2 * I['V20']).values & (C > O).values & (cl >= 0.65) &
            (C >= 5).values & (adv >= 5e6).values & I['ATR14'].notna().values)
    valid = (C.notna() & I['ATR14'].notna() & (C >= 5) & (adv >= 5e6)).values
    quint = adv.rank(axis=1, pct=True).fillna(0).values
    quint = np.minimum((quint * 5).astype(int), 4)
    end = dates[-1] + pd.Timedelta(days=1)
    w('')
    w(f'===== {name}: {len(keep)} akcii, s daty 8-K {sum(1 for t in keep if rows_by_t.get(t))} =====')
    for (a, b, lab) in ((B.TEST_FROM, end, 'cele obdobi'), (B.TEST_FROM, B.SPLIT, '2013-2019'), (B.SPLIT, end, '2020-dnes')):
        bm = B.bench(ix, a, b)
        w(f'  SPY {lab}: CAGR {bm["cagr"]:.1%}, propad {bm["dd"]:.0%}')
    for var, items in (('8K-VSE', ITEMS_ALL), ('8K-BEZ-VYSLEDKU', ITEMS_NOEARN), ('CENA-BEZ-8K', None)):
        sig = base & (event_matrix(rows_by_t, keep, dates, items) if items else True)
        rank = pd.DataFrame(ret, index=dates)
        cands = B.build_cands(pd.DataFrame(sig, index=dates), rank)
        e, tr = simulate(I, dates, cands, ann)
        t2 = tr.copy()
        t2['akcie'] = [keep[j] for j in t2.j]
        t2.drop(columns='j').to_csv(os.path.join(OUT, f'obchody_8K_{var}_{name.split()[0]}.csv'), index=False)
        ctl = [simulate(I, dates, rand_cands(cands, valid, quint, 5000 + s), ann) for s in range(B.SEEDS)]
        w('')
        w(f'--- {var} {name}: signalu {int(sig.sum())} ve {len(cands)} dnech ---')
        for (a, b, lab) in ((None, None, 'cele obdobi'), (B.TEST_FROM, B.SPLIT, '2013-2019'), (B.SPLIT, end, '2020-dnes')):
            st = B.stats(e, tr, a, b)
            if not st or st['n'] == 0:
                w(f'  {lab}: zadne obchody')
                continue
            ss = [x for x in (B.stats(er, trr, a, b) for er, trr in ctl) if x and x['n']]
            w(f'  {lab}: obchodu {st["n"]}, vyhernich {st["win"]:.0%}, PF {st["pf"]:.2f}, prumer {st["avgR"]:+.2f}R, '
              f'drzeni {st["dni"]:.1f} dni, CAGR po dani {st["cagr"]:.1%}, propad {st["dd"]:.0%}')
            w(f'     NAHODNE (stejny kvintil obratu) PF p10/p50/p90: {B.pct([x["pf"] for x in ss], 10):.2f} / '
              f'{B.pct([x["pf"] for x in ss], 50):.2f} / {B.pct([x["pf"] for x in ss], 90):.2f}, '
              f'CAGR p50/p90 {B.pct([x["cagr"] for x in ss], 50):.1%} / {B.pct([x["cagr"] for x in ss], 90):.1%}')
        st, st2 = B.stats(e, tr), B.stats(e, tr, B.SPLIT, end)
        if not st or not st['n']:
            continue
        p90_2 = B.pct([x['pf'] for x in (B.stats(er, trr, B.SPLIT, end) for er, trr in ctl) if x and x['n']], 90)
        k1 = bool(st2) and st2['n'] > 0 and st2['pf'] >= 1.12 and st2['pf'] > p90_2
        k2 = st['n'] >= 100 and st['pf'] >= 1.3 and st['dd'] > -0.15 and bool(st2) and st2.get('pf', 0) >= 1.2
        w(f'  LAŤKA VYZKUMU (2020+ PF >= 1,12 a nad p90 nahodnych PF {p90_2:.2f}): {"PROSLO" if k1 else "NEPROSLO"}'
          f' | NASE LAŤKA (PF >= 1,3, propad < 15 %, po 2020 PF >= 1,2): {"PROSLO" if k2 else "NEPROSLO"}')


def main():
    os.makedirs(OUT, exist_ok=True)
    lines = []

    def w(s=''):
        print(s, flush=True)
        lines.append(s)

    w('Krok 1: nakup na konci dne po 8-K (bez Gemini) - ' + time.strftime('%Y-%m-%d %H:%M'))
    if '--test' in sys.argv:
        raw, groups, rows_by_t, db, vix = fake()
    else:
        import pickle
        groups = {}
        for n in B.WIKI:
            try:
                groups[n] = B.index_list(n)
            except Exception as ex:
                print('wiki chyba', n, ex)
        allt = sorted(set(sum(groups.values(), [])))
        raw = B.download(allt + ['SPY', '^VIX'], os.path.join(OUT, 'ceny_US.pkl'))
        vix = raw['Close']['^VIX'].dropna()
        cmap = ticker_cik()
        rows_by_t = {}
        miss = 0
        for k, t in enumerate(allt):
            cik = cmap.get(t)
            if cik is None:
                miss += 1
                continue
            rows_by_t[t] = filings(cik)
            if k % 100 == 99:
                print(f'  8-K {k + 1}/{len(allt)}', flush=True)
        w(f'Firem bez CIK: {miss}. Hlaseni 8-K celkem: {sum(len(v) for v in rows_by_t.values())}.')
        cp = os.path.join(OUT, 'vysledky_cache.pkl')
        db = pickle.load(open(cp, 'rb')) if os.path.exists(cp) else {}
        if not db:
            w('POZOR: chybi cache vysledku z A1, pravidlo "prodat pred vysledky" se neuplatni.')
    mid = sorted(set(groups.get('S&P 400', []) + groups.get('S&P 600', [])))
    run('MID+SMALL (S&P 400+600)', raw, mid, rows_by_t, db, vix, w)
    run('LARGE (S&P 500)', raw, groups.get('S&P 500', []), rows_by_t, db, vix, w)
    with open(os.path.join(OUT, '8k.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')


def fake():
    rng = np.random.default_rng(5)
    dates = pd.bdate_range('2011-01-03', '2026-10-30')
    tick = [f'T{i}' for i in range(40)]
    cols, rows = {}, {}
    for t in tick + ['SPY', '^VIX']:
        r = rng.normal(0.0004, 0.015, len(dates))
        v = rng.lognormal(14, 0.3, len(dates))
        if t in tick:
            rows[t] = []
            for k in rng.choice(np.arange(300, len(dates) - 30), 25, replace=False):
                r[k] += 0.05
                v[k] *= 3
                r[k + 1:k + 6] += 0.004
                rows[t].append((dates[k].strftime('%Y-%m-%d') + 'T12:00:00.000Z', '8.01,9.01'))
        c = 50 * np.exp(np.cumsum(r))
        o = np.r_[c[0], c[:-1]] * (1 + rng.normal(0, 0.002, len(dates)))
        h = np.maximum(o, c) * (1 + abs(rng.normal(0, 0.003, len(dates))))
        l = np.minimum(o, c) * (1 - abs(rng.normal(0, 0.003, len(dates))))
        if t == '^VIX':
            c = o = h = l = np.full(len(dates), 18.0)
        for k2, x in zip(('Open', 'High', 'Low', 'Close', 'Volume'), (o, h, l, c, v)):
            cols[(k2, t)] = x
    raw = pd.DataFrame(cols, index=dates)
    return raw, {'S&P 400': tick[:20], 'S&P 600': tick[20:30], 'S&P 500': tick[30:]}, rows, {}, raw['Close']['^VIX']


if __name__ == '__main__':
    main()
