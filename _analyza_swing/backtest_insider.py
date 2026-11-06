# -*- coding: utf-8 -*-
"""R2: nakupy insideru (Form 4, SEC ctvrtletni datove sady), 2. 11. 2026.
Nic nekupuje, nic v botovi nemeni, Gemini nevola. Nikdy prodej v den nakupu.

Spusteni ze slozky bota:  python _analyza_swing\\backtest_insider.py
Potrebuje vedle sebe backtest_drzeni.py (+ cache _analyza_drzeni\\ceny_US.pkl).
Data: https://www.sec.gov/files/structureddata/data/form-345-data-sets/<rok>q<q>_form345.zip (2009q1 - dnes),
po zpracovani se uklada jen maly vytah do _analyza_drzeni\\form345\\.
Vystup: _analyza_drzeni\\insider.txt (+ obchody_R2_*.csv)

Pravidla (podle vyzkumu A3 a studie Cohen-Malloy-Pomorski 2012):
- nakup = Form 4, kod P, nabyti (A), cena*kusy; jen officer nebo director (ne ciste 10% fondy).
- rutinni insider = obchodoval ve stejnem kalendarnim mesici kazdy z predchozich 3 let -> vyrazen (varianta VSICHNI ho bere).
- shluk: za 30 dni >= 2 ruzni insideri s nakupy celkem >= 100 000 USD, NEBO CEO/CFO nakup >= 150 000 USD a navyseni podilu >= 5 %.
- veto: prodej (kod S) insidera teze firmy podany v 15 dnech pred nakupem.
- signal je znamy az den po podani (konzervativne), vstup na open dalsiho dne.
- POTVRZENI: do 10 dni Close > EMA10, Close > vcerejsi High, RSI14 >= 40 (prvni takovy den); HNED: bez potvrzeni.
- stop vstup - 2,5 ATR, od +1R chandelier 3 ATR22, prodej po 35 dnech nebo kdyz insider teze firmy podal prodej.
Kontroly NAHODNE/TREND (30 seminek), naklad 0,20 %/strana, riziko 0,5 %, dan 15 %.
Pozor: vesmir je dnesni S&P 1500 (zkresleni prezivsich) -> rozhoduje srovnani s nahodnymi kontrolami.
"""
import os, io, sys, time, zipfile, random
import numpy as np, pandas as pd
import backtest_drzeni as B

OUT = B.OUT
DIR = os.path.join(OUT, 'form345')
UA = 'BotBacktest research-contact@example.com'
COST = 0.0020
URL = 'https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/{y}q{q}_form345.zip'


def _read(z, name, cols):
    fn = next((n for n in z.namelist() if n.lower().endswith(name.lower())), None)
    if fn is None:
        raise RuntimeError('v zipu chybi ' + name)
    with z.open(fn) as f:
        df = pd.read_csv(f, sep='\t', dtype=str, quoting=3, on_bad_lines='skip', encoding='utf-8',
                         encoding_errors='replace', usecols=lambda c: c in cols)
    return df


def quarter(y, q):
    os.makedirs(DIR, exist_ok=True)
    p = os.path.join(DIR, f'{y}q{q}.pkl')
    if os.path.exists(p):
        return pd.read_pickle(p)
    import requests
    r = None
    for att in range(4):
        try:
            r = requests.get(URL.format(y=y, q=q), headers={'User-Agent': UA}, timeout=180)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            break
        except Exception as ex:
            print('  chyba stahovani', y, q, ex)
            time.sleep(5 * (att + 1))
    if r is None or not r.ok:
        return None
    z = zipfile.ZipFile(io.BytesIO(r.content))
    sub = _read(z, 'SUBMISSION.tsv', {'ACCESSION_NUMBER', 'FILING_DATE', 'DOCUMENT_TYPE', 'ISSUERTRADINGSYMBOL', 'ISSUERCIK'})
    own = _read(z, 'REPORTINGOWNER.tsv', {'ACCESSION_NUMBER', 'RPTOWNERCIK', 'RPTOWNER_RELATIONSHIP', 'RPTOWNER_TITLE'})
    nd = _read(z, 'NONDERIV_TRANS.tsv', {'ACCESSION_NUMBER', 'TRANS_DATE', 'TRANS_CODE', 'TRANS_SHARES',
                                         'TRANS_PRICEPERSHARE', 'TRANS_ACQUIRED_DISP_CD', 'SHRS_OWND_FOLWNG_TRANS'})
    nd = nd[nd['TRANS_CODE'].isin(['P', 'S'])]
    own = own.groupby('ACCESSION_NUMBER', as_index=False).first()
    df = nd.merge(sub, on='ACCESSION_NUMBER', how='inner').merge(own, on='ACCESSION_NUMBER', how='left')
    df = df[df['DOCUMENT_TYPE'].astype(str).str.strip() == '4']
    for c in ('FILING_DATE', 'TRANS_DATE'):
        df[c] = pd.to_datetime(df[c], format='%d-%b-%Y', errors='coerce')
    for c in ('TRANS_SHARES', 'TRANS_PRICEPERSHARE', 'SHRS_OWND_FOLWNG_TRANS'):
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df['sym'] = df['ISSUERTRADINGSYMBOL'].astype(str).str.strip().str.upper().str.replace('.', '-', regex=False)
    df = df[['ACCESSION_NUMBER', 'FILING_DATE', 'TRANS_DATE', 'TRANS_CODE', 'TRANS_ACQUIRED_DISP_CD', 'TRANS_SHARES',
             'TRANS_PRICEPERSHARE', 'SHRS_OWND_FOLWNG_TRANS', 'sym', 'RPTOWNERCIK', 'RPTOWNER_RELATIONSHIP', 'RPTOWNER_TITLE']]
    df.to_pickle(p)
    time.sleep(1)
    return df


def load_all():
    parts = []
    now = pd.Timestamp.today()
    for y in range(2009, now.year + 1):
        for q in range(1, 5):
            if pd.Timestamp(year=y, month=3 * q - 2, day=1) > now:
                break
            d = quarter(y, q)
            if d is not None:
                parts.append(d)
                print(f'  form345 {y}q{q}: {len(d)} radku', flush=True)
            else:
                print(f'  form345 {y}q{q}: nedostupne', flush=True)
    return pd.concat(parts, ignore_index=True)


def prepare(df):
    df = df.dropna(subset=['FILING_DATE', 'TRANS_DATE'])
    rel = df['RPTOWNER_RELATIONSHIP'].fillna('').str.lower()
    title = df['RPTOWNER_TITLE'].fillna('').str.lower()
    df = df.assign(insider=rel.str.contains('officer') | rel.str.contains('director'),
                   ceocfo=title.str.contains('ceo|cfo|chief executive|chief financial|president', regex=True),
                   val=df['TRANS_SHARES'] * df['TRANS_PRICEPERSHARE'])
    # rutinni: obchod ve stejnem mesici v kazdem z predchozich 3 let
    ym = set(zip(df['RPTOWNERCIK'], df['TRANS_DATE'].dt.year, df['TRANS_DATE'].dt.month))
    y, m = df['TRANS_DATE'].dt.year.values, df['TRANS_DATE'].dt.month.values
    ck = df['RPTOWNERCIK'].values
    df['routine'] = [all((c, yy - k, mm) in ym for k in (1, 2, 3)) for c, yy, mm in zip(ck, y, m)]
    buys = df[(df['TRANS_CODE'] == 'P') & (df['TRANS_ACQUIRED_DISP_CD'] == 'A') & df['insider'] & (df['val'] > 0)]
    # jeden radek na podani a insidera
    buys = buys.groupby(['ACCESSION_NUMBER', 'sym', 'RPTOWNERCIK'], as_index=False).agg(
        FILING_DATE=('FILING_DATE', 'first'), val=('val', 'sum'), sh=('TRANS_SHARES', 'sum'),
        after=('SHRS_OWND_FOLWNG_TRANS', 'max'), ceocfo=('ceocfo', 'first'), routine=('routine', 'all'))
    before = (buys['after'] - buys['sh']).clip(lower=0)
    buys['stake_up'] = np.where(before > 0, buys['sh'] / before, 1.0)
    sells = df[(df['TRANS_CODE'] == 'S') & df['insider']][['sym', 'FILING_DATE']].drop_duplicates()
    return buys, sells


def events(buys, sells, keep, dates, opportunistic):
    """Vrati dict sloupec -> seznam (index dne udalosti, hodnota) a dict sloupec -> pole indexu dni prodeju."""
    dn = dates.tz_localize(None) if dates.tz is not None else dates
    col = {t: j for j, t in enumerate(keep)}
    b = buys[buys['sym'].isin(col)]
    if opportunistic:
        b = b[~b['routine']]
    s = sells[sells['sym'].isin(col)]
    sell_days = {col[t]: np.sort(dn.searchsorted(g['FILING_DATE'].values, side='right'))
                 for t, g in s.groupby('sym')}
    sell_dates = {t: np.sort(g['FILING_DATE'].values) for t, g in s.groupby('sym')}
    ev = {}
    for t, g in b.groupby('sym'):
        g = g.sort_values('FILING_DATE')
        fd = g['FILING_DATE'].values
        sd = sell_dates.get(t, np.array([], dtype='datetime64[ns]'))
        for i in range(len(g)):
            f = fd[i]
            w = g[(g['FILING_DATE'] > f - np.timedelta64(30, 'D')) & (g['FILING_DATE'] <= f)]
            cluster = w['RPTOWNERCIK'].nunique() >= 2 and w['val'].sum() >= 100000
            row = g.iloc[i]
            boss = bool(row['ceocfo']) and row['val'] >= 150000 and row['stake_up'] >= 0.05
            if not (cluster or boss):
                continue
            k = sd.searchsorted(f - np.timedelta64(15, 'D'))
            if k < len(sd) and sd[k] <= f:
                continue  # prodej insidera v 15 dnech pred nakupem
            e = dn.searchsorted(f, side='right')  # prvni obchodni den PO dni podani
            if e < len(dn):
                ev.setdefault(col[t], {})[e] = max(ev.get(col[t], {}).get(e, 0), w['val'].sum())
    return ev, sell_days


def signals(ev, I, dates, confirm):
    T, N = len(dates), len(I['C'].columns)
    sig = np.zeros((T, N), bool)
    rank = np.zeros((T, N))
    C, H = I['C'].values, I['H'].values
    E10 = B.ema(I['C'], 10).values
    R14 = I['RSI'].values
    for j, d in ev.items():
        for e, v in d.items():
            if not confirm:
                sig[e, j], rank[e, j] = True, v
                continue
            for k in range(e, min(e + 10, T)):
                if C[k, j] > E10[k, j] and k >= 1 and C[k, j] > H[k - 1, j] and R14[k, j] >= 40:
                    sig[k, j], rank[k, j] = True, v
                    break
    return sig, rank


def simulate(I, dates, cands, sell_days):
    O, H, L, C = (I[k].values for k in ('O', 'H', 'L', 'C'))
    A14, A22 = I['ATR14'].values, I['ATR22'].values
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
                val = p['sh'] * px * (1 - COST)
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
                o, a = O[d, j], A14[d - 1, j]
                if np.isnan(o) or np.isnan(a) or a <= 0:
                    continue
                st = o - 2.5 * a
                r = o - st
                per = r + COST * o * 2
                sh = min(equity * B.RISK / per, equity * B.POS_CAP / o)
                val = sh * o * (1 + COST)
                if val > cash:
                    sh = cash / (o * (1 + COST))
                    val = cash
                if sh * o < 50:
                    continue
                cash -= val
                pos[j] = dict(d0=d, entry=o, sh=sh, basis=val, stop=st, r=r, risk=sh * per, hh=H[d, j], exit_next=False)
        for j, p in pos.items():
            c = C[d, j]
            if np.isnan(c):
                continue
            if not np.isnan(H[d, j]):
                p['hh'] = max(p['hh'], H[d, j])
            if p['hh'] >= p['entry'] + p['r'] and not np.isnan(A22[d, j]):
                p['stop'] = max(p['stop'], p['hh'] - 3 * A22[d, j])
            sd = sell_days.get(j)
            sold = sd is not None and len(sd) and (sd.searchsorted(p['d0'], side='right') < sd.searchsorted(d, side='right'))
            if d - p['d0'] >= 35 or sold:
                p['exit_next'] = True
        eq[d] = cash + sum(p['sh'] * (C[d, j] if not np.isnan(C[d, j]) else p['entry']) for j, p in pos.items())
    e = pd.Series(eq, index=dates).ffill().dropna()
    return e, pd.DataFrame(trades, columns=['vstup', 'vystup', 'j', 'pnl', 'R', 'dni'])


def main():
    os.makedirs(OUT, exist_ok=True)
    lines = []

    def w(s=''):
        print(s, flush=True)
        lines.append(s)

    w('R2 nakupy insideru - ' + time.strftime('%Y-%m-%d %H:%M'))
    w('Laťka: >=100 obchodu, PF >=1,3, propad <15 %, nad p90 kontrol, CAGR/propad > SPY a PF po roce 2020 >= 1,2.')
    if '--test' in sys.argv:
        raw, tick, df = fake()
    else:
        tick = []
        for n in B.WIKI:
            try:
                tick += B.index_list(n)
            except Exception as ex:
                print('wiki chyba', n, ex)
        tick = sorted(set(tick))
        raw = B.download(tick + ['SPY', '^VIX'], os.path.join(OUT, 'ceny_US.pkl'))
        df = load_all()
    buys, sells = prepare(df)
    I, keep, dates, ix = B.prep(raw, tick, 'SPY')
    bk = buys[buys['sym'].isin(set(keep))]
    w(f'Akcii {len(keep)}. Nakupu insideru (officer/director) ve vesmiru: {len(bk)}, z toho oportunistickych {int((~bk["routine"]).sum())}; '
      f'od {bk["FILING_DATE"].min().date() if len(bk) else "?"}.')
    end = dates[-1] + pd.Timedelta(days=1)
    for (a, b, lab) in ((B.TEST_FROM, end, 'cele obdobi'), (B.TEST_FROM, B.SPLIT, '2013-2019'), (B.SPLIT, end, '2020-dnes')):
        bm = B.bench(ix, a, b)
        w(f'  SPY {lab}: CAGR {bm["cagr"]:.1%}, propad {bm["dd"]:.0%}, CAGR/propad {bm["cagr"] / abs(bm["dd"]):.2f}')
    valid = (I['SMA200'].notna() & I['ATR22'].notna() & I['C'].notna() & I['O'].shift(-1).notna()).values
    trendpool = valid & ((I['C'] > I['EMA50']) & (I['EMA50'] > I['SMA200'])).values
    for name, opp, conf in (('OPORTUNISTICTI-POTVRZENI', True, True), ('OPORTUNISTICTI-HNED', True, False),
                            ('VSICHNI-POTVRZENI', False, True)):
        ev, sell_days = events(buys, sells, keep, dates, opp)
        sig, rank = signals(ev, I, dates, conf)
        sig &= valid
        cands = B.build_cands(pd.DataFrame(sig, index=dates), pd.DataFrame(rank, index=dates))
        e, tr = simulate(I, dates, cands, sell_days)
        t2 = tr.copy()
        t2['akcie'] = [keep[j] for j in t2.j]
        t2.drop(columns='j').to_csv(os.path.join(OUT, f'obchody_R2_{name}.csv'), index=False)
        w('')
        w(f'--- {name}: udalosti {sum(len(v) for v in ev.values())}, signalu {int(sig.sum())} ve {len(cands)} dnech ---')
        ctl = {nm: [simulate(I, dates, B.random_cands(cands, pool, 4000 + s), sell_days) for s in range(B.SEEDS)]
               for nm, pool in (('NAHODNE', valid), ('TREND', trendpool))}
        for (a, b, lab) in ((None, None, 'cele obdobi'), (B.TEST_FROM, B.SPLIT, '2013-2019'), (B.SPLIT, end, '2020-dnes')):
            st = B.stats(e, tr, a, b)
            if not st or st['n'] == 0:
                w(f'  {lab}: zadne obchody')
                continue
            w(f'  {lab}: obchodu {st["n"]}, vyhernich {st["win"]:.0%}, PF {st["pf"]:.2f}, prumer {st["avgR"]:+.2f}R, '
              f'drzeni {st["dni"]:.0f} dni, CAGR po dani {st["cagr"]:.1%}, propad {st["dd"]:.0%}, CAGR/propad {st["mar"]:.2f}')
            for nm in ctl:
                ss = [x for x in (B.stats(er, trr, a, b) for er, trr in ctl[nm]) if x]
                w(f'     {nm:8s} p10/p50/p90: CAGR {B.pct([x["cagr"] for x in ss], 10):.1%} / {B.pct([x["cagr"] for x in ss], 50):.1%} / '
                  f'{B.pct([x["cagr"] for x in ss], 90):.1%}, PF p50 {B.pct([x["pf"] for x in ss], 50):.2f} (p90 {B.pct([x["pf"] for x in ss], 90):.2f})')
        st = B.stats(e, tr)
        if not st or st['n'] == 0:
            continue
        st2 = B.stats(e, tr, B.SPLIT, end)
        bm = B.bench(ix, B.TEST_FROM, end)
        p90 = max(B.pct([B.stats(er, trr)['cagr'] for er, trr in ctl[nm]], 90) for nm in ctl)
        checks = {'>=100 obchodu': st['n'] >= 100, 'PF>=1,3': st['pf'] >= 1.3, 'propad<15 %': st['dd'] > -0.15,
                  'nad p90 kontrol': st['cagr'] > p90, 'CAGR/propad > SPY': st['mar'] > bm['cagr'] / abs(bm['dd']),
                  'PF po 2020 >=1,2': bool(st2) and st2.get('pf', 0) >= 1.2}
        w('  LAŤKA: ' + ', '.join(f'{k} {"ANO" if v else "ne"}' for k, v in checks.items()) +
          f'  => {"PROSLO" if all(checks.values()) else "NEPROSLO"}')
    with open(os.path.join(OUT, 'insider.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')


def fake():
    rng = np.random.default_rng(4)
    dates = pd.bdate_range('2011-01-03', '2026-10-30')
    tick = [f'T{i}' for i in range(40)]
    cols, rows = {}, []
    for t in tick + ['SPY', '^VIX']:
        r = rng.normal(0.0004, 0.015, len(dates))
        if t in tick:
            for k in rng.choice(np.arange(300, len(dates) - 50), 12, replace=False):
                d0 = dates[k]
                for who in ('A', 'B'):
                    rows.append(dict(ACCESSION_NUMBER=f'{t}{k}{who}', FILING_DATE=d0, TRANS_DATE=d0 - pd.Timedelta(days=2),
                                     TRANS_CODE='P', TRANS_ACQUIRED_DISP_CD='A', TRANS_SHARES=5000.0, TRANS_PRICEPERSHARE=30.0,
                                     SHRS_OWND_FOLWNG_TRANS=50000.0, sym=t, RPTOWNERCIK=t + who,
                                     RPTOWNER_RELATIONSHIP='Officer', RPTOWNER_TITLE='CEO' if who == 'A' else 'VP'))
                r[k + 1:k + 20] += 0.003
        c = 50 * np.exp(np.cumsum(r))
        o = np.r_[c[0], c[:-1]] * (1 + rng.normal(0, 0.002, len(dates)))
        h = np.maximum(o, c) * (1 + abs(rng.normal(0, 0.006, len(dates))))
        l = np.minimum(o, c) * (1 - abs(rng.normal(0, 0.006, len(dates))))
        v = rng.lognormal(12, 0.4, len(dates))
        if t == '^VIX':
            c = o = h = l = np.full(len(dates), 18.0)
        for k2, x in zip(('Open', 'High', 'Low', 'Close', 'Volume'), (o, h, l, c, v)):
            cols[(k2, t)] = x
    raw = pd.DataFrame(cols, index=dates)
    df = pd.DataFrame(rows)
    df = pd.concat([df, pd.DataFrame([dict(ACCESSION_NUMBER='s1', FILING_DATE=dates[500], TRANS_DATE=dates[499], TRANS_CODE='S',
                                           TRANS_ACQUIRED_DISP_CD='D', TRANS_SHARES=100.0, TRANS_PRICEPERSHARE=30.0,
                                           SHRS_OWND_FOLWNG_TRANS=1.0, sym='T1', RPTOWNERCIK='x', RPTOWNER_RELATIONSHIP='Director',
                                           RPTOWNER_TITLE='')])], ignore_index=True)
    return raw, tick, df


if __name__ == '__main__':
    main()
