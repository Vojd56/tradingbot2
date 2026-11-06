# -*- coding: utf-8 -*-
"""Backtest screenu kvalitnich firem (satelit L2) na datech SEC XBRL, point-in-time podle data podani.
Vesmir: soucasne S&P 500 bez financi, nemovitosti a utilit (pozor: survivorship -> kontrola nahodnym vyberem
ze stejneho vesmiru). Rebalance kazdy rok 1. dubna (signal), obchod na close dalsiho dne.
Ceny: yfinance (Close + 85 % dividend = cista dividenda po srazkove dani), prevod do EUR.
Naklady 0,20 %/strana (FX 0,15 + skluz 0,05). Dan CZ 15 % (3lety test, limit 100 000 Kc prodeju/rok).
Vystup: _analyza_kvalita\\kvalita.txt + kvalita.json (+ vybery.csv)
"""
import os, io, json, gzip, time, random
import numpy as np, pandas as pd

UA = 'BotBacktest research-contact@example.com'
COST = 0.0020
TAX = 0.15
LIMIT_EUR = 100000 / 25.0
CAP = 5000.0
N = 15
BUFFER = 45
YEARS_LOCK = 3
SEEDS = 30
SPLIT = pd.Timestamp('2020-01-01')
BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, '_analyza_kvalita')
CACHE = os.path.join(OUT, 'sec_cache')
EXCL = {'Financials', 'Real Estate', 'Utilities'}

T_REV = ['Revenues', 'RevenueFromContractWithCustomerExcludingAssessedTax', 'SalesRevenueNet',
         'RevenueFromContractWithCustomerIncludingAssessedTax', 'SalesRevenueGoodsNet']
T_OPI = ['OperatingIncomeLoss']
T_NI = ['NetIncomeLoss', 'ProfitLoss']
T_CFO = ['NetCashProvidedByUsedInOperatingActivities',
         'NetCashProvidedByUsedInOperatingActivitiesContinuingOperations']
T_CAPEX = ['PaymentsToAcquirePropertyPlantAndEquipment', 'PaymentsToAcquireProductiveAssets']
T_ASSETS = ['Assets']
T_CL = ['LiabilitiesCurrent']
T_CASH = ['CashAndCashEquivalentsAtCarryingValue',
          'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents', 'Cash']
T_DEBT = ['LongTermDebt', 'LongTermDebtNoncurrent']
T_DEBTC = ['LongTermDebtCurrent', 'DebtCurrent']
ALL_TAGS = set(T_REV + T_OPI + T_NI + T_CFO + T_CAPEX + T_ASSETS + T_CL + T_CASH + T_DEBT + T_DEBTC)


# ---------------- data ----------------
def sp500():
    import requests
    html = requests.get('https://en.wikipedia.org/wiki/List_of_S%26P_500_companies',
                        headers={'User-Agent': 'Mozilla/5.0 BotBacktest'}, timeout=30).text
    t = pd.read_html(io.StringIO(html))[0]
    t = t.rename(columns={'Symbol': 'sym', 'GICS Sector': 'sector', 'CIK': 'cik'})
    t = t[~t['sector'].isin(EXCL)]
    t['yf'] = t['sym'].str.replace('.', '-', regex=False)
    t['cik'] = t['cik'].astype(int)
    return t[['sym', 'yf', 'sector', 'cik']].reset_index(drop=True)


def sec_facts(cik):
    os.makedirs(CACHE, exist_ok=True)
    p = os.path.join(CACHE, f'{cik}.json.gz')
    if os.path.exists(p):
        with gzip.open(p, 'rt', encoding='utf-8') as f:
            return json.load(f)
    import requests
    for attempt in range(4):
        r = requests.get(f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json',
                         headers={'User-Agent': UA}, timeout=60)
        time.sleep(0.15)
        if r.status_code == 200:
            break
        if r.status_code == 404:
            return None
        time.sleep(2 ** attempt)
    else:
        raise RuntimeError(f'SEC {cik} status {r.status_code}')
    j = r.json()
    keep = {'us-gaap': {}, 'dei': {}}
    for tag, v in j.get('facts', {}).get('us-gaap', {}).items():
        if tag in ALL_TAGS:
            keep['us-gaap'][tag] = v.get('units', {}).get('USD', [])
    sh = j.get('facts', {}).get('dei', {}).get('EntityCommonStockSharesOutstanding', {})
    keep['dei']['shares'] = sh.get('units', {}).get('shares', [])
    with gzip.open(p, 'wt', encoding='utf-8') as f:
        json.dump(keep, f)
    return keep


class Company:
    def __init__(s, facts):
        s.dur = {}   # tag -> list (end, filed, val)
        s.inst = {}
        for tag, rows in facts['us-gaap'].items():
            for x in rows:
                if x.get('form') not in ('10-K', '10-K/A'):
                    continue
                end = pd.Timestamp(x['end']); filed = pd.Timestamp(x['filed'])
                if 'start' in x:
                    d = (end - pd.Timestamp(x['start'])).days
                    if 330 <= d <= 400:
                        s.dur.setdefault(tag, []).append((end, filed, float(x['val'])))
                else:
                    s.inst.setdefault(tag, []).append((end, filed, float(x['val'])))
        s.shares = [(pd.Timestamp(x['end']), pd.Timestamp(x['filed']), float(x['val']))
                    for x in facts['dei'].get('shares', [])]

    @staticmethod
    def _get(store, tags, end, D):
        for t in tags:
            best = None
            for e, f, v in store.get(t, []):
                if abs((e - end).days) <= 7 and f <= D and (best is None or f > best[0]):
                    best = (f, v)
            if best is not None:
                return best[1]
        return None

    def fy_ends(s, D):
        ends = set()
        for t in T_REV:
            for e, f, v in s.dur.get(t, []):
                if f <= D:
                    ends.add(e)
        ends = sorted(ends)
        out = []
        for e in ends:
            if out and (e - out[-1]).days < 300:
                out[-1] = e
            else:
                out.append(e)
        return out

    def metrics(s, D):
        ends = s.fy_ends(D)
        if len(ends) < 5:
            return None
        ends = ends[-5:]
        if (D - ends[-1]).days > 500:
            return None
        rows = []
        for e in ends:
            g = lambda tags, st: s._get(st, tags, e, D)
            rev = g(T_REV, s.dur); opi = g(T_OPI, s.dur); ni = g(T_NI, s.dur)
            cfo = g(T_CFO, s.dur); capex = g(T_CAPEX, s.dur) or 0.0
            assets = g(T_ASSETS, s.inst); cl = g(T_CL, s.inst); cash = g(T_CASH, s.inst) or 0.0
            debt = g(T_DEBT, s.inst)
            if debt is None:
                debt = 0.0
            debt += (g(T_DEBTC, s.inst) or 0.0) if g(['LongTermDebt'], s.inst) is None else 0.0
            if None in (rev, opi, ni, cfo, assets, cl):
                return None
            ce = assets - cl - cash
            rows.append({'end': e, 'rev': rev, 'opi': opi, 'ni': ni, 'fcf': cfo - capex,
                         'roce': opi / ce if ce > 0 else np.nan, 'netdebt': debt - cash})
        m = pd.DataFrame(rows)
        sh = [(f, e, v) for e, f, v in s.shares if f <= D]
        shares = None
        if sh:
            lf = max(x[0] for x in sh)
            same = [x for x in sh if x[0] == lf]
            le = max(x[1] for x in same)
            shares = sum(x[2] for x in same if x[1] == le)
        return m, shares


def passes(m):
    r = m['roce'].values
    if np.isnan(r).any() or (r < 0.15).any():
        return False
    rev = m['rev'].values
    if rev[0] <= 0 or (rev[-1] / rev[0]) ** (1 / 4) - 1 < 0.05:
        return False
    if (np.diff(rev) > 0).sum() < 3:
        return False
    ni = m['ni'].values
    if (ni <= 0).any() or m['fcf'].sum() / ni.sum() < 0.8:
        return False
    if m['opi'].iloc[-1] <= 0 or m['netdebt'].iloc[-1] / m['opi'].iloc[-1] > 2.5:
        return False
    return True


def load_prices(tickers):
    import yfinance as yf
    raw = yf.download(tickers + ['SPY', 'RSP', 'EURUSD=X'], start='2012-01-01', auto_adjust=False,
                      actions=True, progress=False, threads=True)
    return raw


def build_prices(raw, tickers):
    close = raw['Close']
    idx = close['SPY'].dropna().index
    close = close.reindex(idx)
    div = raw['Dividends'].reindex(idx).fillna(0) if 'Dividends' in raw else pd.DataFrame(0, index=idx, columns=close.columns)
    spl = raw['Stock Splits'].reindex(idx).fillna(0) if 'Stock Splits' in raw else pd.DataFrame(0, index=idx, columns=close.columns)
    fx = raw['Close']['EURUSD=X'].ffill().reindex(idx).ffill().bfill()
    tr = {}
    split_after = {}
    for t in tickers + ['SPY', 'RSP']:
        if t not in close:
            continue
        c = close[t]
        if c.notna().sum() < 50:
            continue
        cf = c.ffill()
        r = (cf + 0.85 * div[t]) / cf.shift(1) - 1
        r = r.where(c.notna() | c.shift(1).notna(), np.nan)
        s = (1 + r.fillna(0)).cumprod()
        s[c.ffill().isna()] = np.nan
        tr[t] = s / fx
        f = spl[t].replace(0, 1.0)
        split_after[t] = f[::-1].cumprod()[::-1].shift(-1).fillna(1.0)
    eur = pd.DataFrame(tr)
    return eur, close, pd.DataFrame(split_after), idx


# ---------------- engine ----------------
class Port:
    def __init__(s, P, dates, tax_on):
        s.P = P; s.dates = dates; s.tax_on = tax_on
        s.cash = CAP; s.lots = {}; s.sh = {}
        s.ys = 0.0; s.yg = 0.0; s.taxes = 0.0; s.trades = 0; s.sales = 0.0

    def px(s, t, i):
        v = s.P[t][i]
        return v if np.isfinite(v) else None

    def val(s, i):
        v = s.cash
        for t, q in s.sh.items():
            if q > 0:
                p = s.px(t, i)
                if p is None:   # delisting / chybi cena -> posledni znama
                    col = s.P[t][:i + 1]
                    ok = np.where(np.isfinite(col))[0]
                    p = col[ok[-1]] if len(ok) else 0.0
                v += q * p
        return v

    def buy(s, t, amt, i):
        p = s.px(t, i)
        if p is None or amt <= 1:
            return False
        q = amt * (1 - COST) / p
        s.lots.setdefault(t, []).append([s.dates[i], q, amt / q])
        s.sh[t] = s.sh.get(t, 0) + q
        s.cash -= amt; s.trades += 1
        return True

    def sell_all(s, t, i):
        p = s.px(t, i)
        if p is None:
            col = s.P[t][:i + 1]; ok = np.where(np.isfinite(col))[0]
            p = col[ok[-1]] if len(ok) else 0.0
        lim = s.dates[i] - pd.DateOffset(years=YEARS_LOCK)
        for d, q, b in s.lots.get(t, []):
            pr = q * p * (1 - COST)
            s.sales += pr
            if not (d < lim):
                s.ys += pr; s.yg += pr - q * b
            s.cash += pr
        s.lots[t] = []; s.sh[t] = 0.0; s.trades += 1

    def held_since(s, t):
        l = s.lots.get(t)
        return min(x[0] for x in l) if l else None

    def settle(s, i):
        if s.tax_on and s.ys > LIMIT_EUR and s.yg > 0:
            tax = TAX * s.yg
            V = s.val(i); f = max(0.0, (V - tax) / V)
            s.cash *= f
            for t in s.sh:
                s.sh[t] *= f
                for l in s.lots.get(t, []):
                    l[1] *= f
            s.taxes += tax
        s.ys = 0.0; s.yg = 0.0

    def holdings(s):
        return [t for t, q in s.sh.items() if q > 1e-12]


def simulate(kind, ctx, tax_on, seed=None):
    eur, dates, rebal, sel, universe = ctx['eur'], ctx['dates'], ctx['rebal'], ctx['sel'], ctx['univ']
    P = {t: eur[t].values for t in eur.columns}
    pf = Port(P, dates, tax_on)
    rng = random.Random(seed)
    i0 = rebal[0][1]
    eq = []
    year = dates[i0].year
    rebal_at = {ei: k for k, (D, ei) in enumerate(rebal)}
    for i in range(i0, len(dates)):
        if dates[i].year != year:
            pf.settle(i); year = dates[i].year
        if i in rebal_at:
            k = rebal_at[i]
            D = rebal[k][0]
            avail = [t for t in universe[k] if pf.px(t, i) is not None]
            ranked = [t for t in sel[k]['roce' if 'roce' in kind else 'fcf'] if pf.px(t, i) is not None]
            if kind.startswith('NAHODNE'):
                pool = avail[:]; rng.shuffle(pool); ranked = pool
                keepset = set(ranked[:BUFFER])
            else:
                keepset = set(ranked[:BUFFER])
            hold = [t for t in pf.holdings() if t != 'SPY']
            sells = []
            for t in hold:
                if pf.px(t, i) is None:
                    sells.append(t); continue
                age_ok = (D - pf.held_since(t)).days >= 365 * YEARS_LOCK
                broken = t in sel[k]['broken'] and not kind.startswith('NAHODNE')
                if 'rocne' in kind:
                    if t not in ranked[:N]:
                        sells.append(t)
                elif broken or (age_ok and t not in keepset):
                    sells.append(t)
            for t in sells:
                pf.sell_all(t, i)
            # filler SPY prodat, pokud jsou nove firmy
            hold = [t for t in pf.holdings() if t != 'SPY']
            new = [t for t in ranked if t not in hold][:max(0, N - len(hold))]
            if new and pf.sh.get('SPY', 0) > 0:
                pf.sell_all('SPY', i)
            slots = N - len(hold)
            if pf.cash > 1 and slots > 0:
                per = pf.cash / slots
                bought = 0
                for t in new:
                    if pf.buy(t, per, i):
                        bought += 1
                if slots - bought > 0 and pf.cash > 1:
                    pf.buy('SPY', pf.cash, i)
            elif pf.cash > 1:
                pf.buy('SPY', pf.cash, i)
        eq.append(pf.val(i))
    last = len(dates) - 1
    pre = pf.val(last)
    for t in pf.holdings():
        pf.sell_all(t, last)
    pf.settle(last)
    return {'eq': pd.Series(eq, index=dates[i0:]), 'pre': pre, 'net': pf.cash, 'taxes': pf.taxes,
            'trades': pf.trades, 'sales': pf.sales}


def bh(t, ctx):
    s = ctx['eur'][t]; i0 = ctx['rebal'][0][1]
    e = s.iloc[i0:] / s.iloc[i0] * CAP * (1 - COST)
    return {'eq': e, 'pre': e.iloc[-1], 'net': e.iloc[-1] * (1 - COST), 'taxes': 0.0, 'trades': 2, 'sales': e.iloc[-1]}


def stats(r):
    eq = r['eq']
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1
    dd = float((eq / eq.cummax() - 1).min())
    d = eq.pct_change().dropna()
    sh = float(d.mean() / d.std() * np.sqrt(252))
    net = (r['net'] / CAP) ** (1 / yrs) - 1
    h = []
    for part in (eq[eq.index < SPLIT], eq[eq.index >= SPLIT]):
        y = (part.index[-1] - part.index[0]).days / 365.25
        h.append(((part.iloc[-1] / part.iloc[0]) ** (1 / y) - 1, float((part / part.cummax() - 1).min())))
    return {'cagr': cagr, 'dd': dd, 'sharpe': sh, 'cagr_po_dani': net, 'dane': r['taxes'],
            'obch_rok': r['trades'] / yrs, 'h1': h[0], 'h2': h[1], 'konec': r['net']}


def main():
    os.makedirs(OUT, exist_ok=True)
    L = []
    p = lambda x='': (L.append(x), print(x, flush=True))
    uni = sp500()
    p(f'Vesmir: {len(uni)} firem ze soucasneho S&P 500 (bez financi, nemovitosti, utilit)')
    comps = {}
    for n, row in enumerate(uni.itertuples()):
        try:
            f = sec_facts(row.cik)
            if f:
                comps[row.yf] = Company(f)
        except Exception as e:
            print('SEC chyba', row.yf, e)
        if n % 50 == 0:
            print(f'  SEC {n}/{len(uni)}', flush=True)
    tick = sorted(comps)
    raw = load_prices(tick)
    eur, close, split_after, dates = build_prices(raw, tick)
    tick = [t for t in tick if t in eur.columns]
    years = range(2013, 2027)
    rebal, sel, univ, log = [], [], [], []
    for y in years:
        D = pd.Timestamp(f'{y}-04-01')
        j = dates.searchsorted(D)
        if j + 1 >= len(dates):
            break
        Dd = dates[j]
        cands, broken, u = [], set(), []
        for t in tick:
            if not np.isfinite(eur[t].iloc[j]):
                continue
            res = comps[t].metrics(Dd)
            if res is None:
                continue
            m, shares = res
            u.append(t)
            if np.isfinite(m['roce'].iloc[-1]) and m['roce'].iloc[-1] < 0.10:
                broken.add(t)
            if not passes(m):
                continue
            price = close[t].iloc[j] * split_after[t].iloc[j] if t in split_after else np.nan
            mcap = price * shares if shares and np.isfinite(price) else np.nan
            fy = m['fcf'].iloc[-1] / mcap if mcap and np.isfinite(mcap) and mcap > 0 else -1
            cands.append((t, fy, m['roce'].mean()))
        by_fcf = [c[0] for c in sorted(cands, key=lambda c: -c[1])]
        by_roce = [c[0] for c in sorted(cands, key=lambda c: -c[2])]
        rebal.append((Dd, j + 1)); univ.append(u)
        sel.append({'fcf': by_fcf, 'roce': by_roce, 'broken': broken})
        log.append({'datum': str(Dd.date()), 'vesmir_s_daty': len(u), 'proslo_screenem': len(cands),
                    'top15_fcf': ' '.join(by_fcf[:N]), 'top15_roce': ' '.join(by_roce[:N])})
        p(f'{Dd.date()}: firem s daty {len(u)}, proslo screenem {len(cands)}; top FCF yield: {" ".join(by_fcf[:N])}')
    pd.DataFrame(log).to_csv(os.path.join(OUT, 'vybery.csv'), index=False)
    ctx = {'eur': eur, 'dates': dates, 'rebal': rebal, 'sel': sel, 'univ': univ}
    p('')
    p(f'Jednorazove {CAP:.0f} EUR od {rebal[0][0].date()} do {dates[-1].date()}, {N} firem, naklad {COST*100:.2f} %/strana, dividendy -15 %')
    p(f"{'strategie':<20}{'CAGR':>7}{'MaxDD':>8}{'Sharpe':>7}{'CAGR po dani':>13}{'dane':>7}{'obch/rok':>9}{'konec EUR':>10}  {'do 2019 CAGR/DD':>16}  {'od 2020 CAGR/DD':>16}")
    res = {}
    def row(name, r):
        st = stats(r); res[name] = st
        p(f"{name:<20}{st['cagr']*100:6.1f}%{st['dd']*100:7.1f}%{st['sharpe']:7.2f}{st['cagr_po_dani']*100:12.1f}%{st['dane']:7.0f}{st['obch_rok']:9.1f}{st['konec']:10.0f}  {st['h1'][0]*100:6.1f}%/{st['h1'][1]*100:6.1f}%  {st['h2'][0]*100:6.1f}%/{st['h2'][1]*100:6.1f}%")
    row('BH-SPY (S&P 500)', bh('SPY', ctx))
    if 'RSP' in eur:
        row('BH-RSP (stejne vahy)', bh('RSP', ctx))
    for k in ('KVALITA-FCF-zamek', 'KVALITA-roce-zamek', 'KVALITA-FCF-rocne'):
        row(k, simulate(k, ctx, True))
    rr = [stats(simulate('NAHODNE-zamek', ctx, True, seed=s)) for s in range(SEEDS)]
    cg = sorted(x['cagr_po_dani'] for x in rr)
    med = rr[int(np.argsort([x['cagr_po_dani'] for x in rr])[len(rr) // 2])]
    res['NAHODNE-median'] = med
    p(f"{'NAHODNE-zamek med.':<20}{med['cagr']*100:6.1f}%{med['dd']*100:7.1f}%{med['sharpe']:7.2f}{med['cagr_po_dani']*100:12.1f}%{med['dane']:7.0f}{med['obch_rok']:9.1f}{med['konec']:10.0f}  {med['h1'][0]*100:6.1f}%/{med['h1'][1]*100:6.1f}%  {med['h2'][0]*100:6.1f}%/{med['h2'][1]*100:6.1f}%")
    p(f'  nahodny vyber {N} firem ze stejneho vesmiru, {SEEDS} behu: CAGR po dani 10. percentil {cg[len(cg)//10]*100:.1f} %, median {cg[len(cg)//2]*100:.1f} %, 90. percentil {cg[len(cg)*9//10]*100:.1f} %')
    p('')
    p('Brana: CAGR po dani > S&P 500 a > 90. percentil nahodneho vyberu (survivorship kontrola), MaxDD ne horsi nez S&P 500 o vic nez 5 p.b.,')
    p('       a v obou obdobich (do 2019 / od 2020) CAGR >= S&P 500.')
    b = res['BH-SPY (S&P 500)']
    for k in ('KVALITA-FCF-zamek', 'KVALITA-roce-zamek', 'KVALITA-FCF-rocne'):
        r = res[k]
        ok = (r['cagr_po_dani'] > b['cagr_po_dani'] and r['cagr_po_dani'] > cg[len(cg) * 9 // 10]
              and r['dd'] >= b['dd'] - 0.05 and r['h1'][0] >= b['h1'][0] and r['h2'][0] >= b['h2'][0])
        r['brana'] = bool(ok)
        p(f'  {k:<20} -> {"PROSLA" if ok else "neprosla"}')
    with open(os.path.join(OUT, 'kvalita.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(L))
    with open(os.path.join(OUT, 'kvalita.json'), 'w', encoding='utf-8') as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=float)


if __name__ == '__main__':
    main()
