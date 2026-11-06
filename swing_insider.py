"""Stopa E (papírová): oportunistické nákupy insiderů z SEC Form 4 (pravidlo V3 z výzkumu 3. 11. 2026).

Backtest 2013-2026 (S&P 1500, 500 náhodných kontrol): 102 obchodů, PF 1,99, +0,28R, v 2013-19 i od 2020 PF ~2,0;
náhodné akcie ve stejné korekci PF 1,29 (p = 0,04). Jen ~7 signálů ročně.

Signál pro den `day` (vstup dnes) se počítá z podání Form 4 zveřejněných v předchozí obchodní den t:
- nákup na trhu (kód P, A), insider není "rutinní" (Cohen-Malloy-Pomorski: obchodoval ve stejném kalendářním měsíci
  v aspoň 2 ze 3 předchozích let), není plán 10b5-1 (příznak od 2023),
- součet takových nákupů za 5 obchodních dní do t >= 100 000 USD, aspoň jeden kupující je officer, nové podání v den t,
- žádný prodej (kód S) insiderem firmy za posledních 15 obchodních dní,
- akcie S&P 1500, cena >= 5 USD, obrat 20 dní >= 10 mil. USD, kapitalizace <= 25 mld. USD, close > SMA200,
  close 4-22 % pod 60denním maximem a odráží se (close > min(low 5 dní) + 0,5 x ATR14),
- žádné výsledky v příštích 5 obchodních dnech (řeší swing_shadow stejně jako u ostatních stop).
Plán: stop = min(min low 10 dní, close - 2 x ATR14), cíl ½ = +2R po nákladech, časový stop 15 dní bez +0,5R, max 40 dní.

Data: SEC EDGAR denní index (form.YYYYMMDD.idx) + XML podání; uložené v insider_trades.jsonl (append-only).
Historie insiderů pro "rutinní": insider_history.json (z SEC Form 345 datasetů v _analyza_swing + nová podání).
První spuštění:  python swing_insider.py --backfill 30   (dotáhne posledních 30 obchodních dní, ~10 minut).
"""

import json
import logging
import math
import re
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from config import BASE_DIR

logger = logging.getLogger("TradingBot.SwingInsider")

UA = "BotBacktest research-contact@example.com"
TRADES_FILE = BASE_DIR / "insider_trades.jsonl"
HISTORY_FILE = BASE_DIR / "insider_history.json"
SEEN_FILE = BASE_DIR / "insider_seen_days.json"
CIK_FILE = BASE_DIR / "sec_company_tickers.json"
FORM345_DIR = BASE_DIR / "_analyza_swing" / "_analyza_drzeni" / "form345"

MIN_USD = 100_000.0
MAX_MCAP = 25e9
MIN_PRICE = 5.0
MIN_ADV = 10e6
BUY_WINDOW = 5          # obchodních dní
SELL_WINDOW = 15
DIST_MIN, DIST_MAX = -0.22, -0.04
TIME_STOP_DAYS = 15
MAX_HOLD_DAYS = 40
REQ_PAUSE_S = 0.13      # SEC: max 10 dotazů za sekundu
MAX_FILINGS_PER_DAY = 600

_PLAN_RE = re.compile(r"10b5-?1|pre-?arranged", re.I)


# ---------------------------------------------------------------- SEC

def _get(url: str, tries: int = 3) -> Optional[bytes]:
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            time.sleep(REQ_PAUSE_S)
            return data
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                return None
            time.sleep(2 ** k)
        except Exception:
            time.sleep(2 ** k)
    return None


def ticker_ciks(tickers: Set[str]) -> Dict[str, str]:
    """CIK (10 číslic) -> ticker pro tickery z vesmíru. Seznam SEC se obnovuje jednou za 7 dní."""
    data = None
    try:
        if CIK_FILE.exists() and time.time() - CIK_FILE.stat().st_mtime < 7 * 86400:
            data = json.loads(CIK_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = None
    if data is None:
        raw = _get("https://www.sec.gov/files/company_tickers.json")
        if raw:
            data = json.loads(raw)
            CIK_FILE.write_text(json.dumps(data), encoding="utf-8")
        elif CIK_FILE.exists():
            data = json.loads(CIK_FILE.read_text(encoding="utf-8"))
        else:
            return {}
    norm = {t.upper().replace(".", "-"): t for t in tickers}
    out = {}
    for x in data.values():
        t = str(x["ticker"]).upper().replace(".", "-")
        if t in norm:
            out[f"{int(x['cik_str']):010d}"] = norm[t]
    return out


def daily_index(d: date) -> Optional[List[Tuple[str, str]]]:
    """[(cik, cesta)] všech Form 4 v denním indexu; None = index pro den neexistuje (svátek / ještě nevyšel)."""
    q = (d.month - 1) // 3 + 1
    raw = _get(f"https://www.sec.gov/Archives/edgar/daily-index/{d.year}/QTR{q}/form.{d.strftime('%Y%m%d')}.idx")
    if raw is None:
        return None
    out = []
    for line in raw.decode("latin-1").splitlines():
        if not line.startswith("4 "):
            continue
        m = re.match(r"^4\s+(.*?)\s+(\d{1,10})\s+(\d{8}|\d{4}-\d{2}-\d{2})\s+(edgar/\S+)\s*$", line)
        if m:
            out.append((f"{int(m.group(2)):010d}", m.group(4)))
    return out


def _txt(el, path: str) -> str:
    x = el.find(path)
    return (x.text or "").strip() if x is not None and x.text else ""


def parse_form4(raw: bytes) -> Optional[Dict[str, Any]]:
    """XML z úplného podání (.txt) -> vydavatel, vlastníci a nederivátové transakce."""
    import xml.etree.ElementTree as ET
    s = raw.decode("utf-8", errors="replace")
    m = re.search(r"<XML>\s*(.*?)\s*</XML>", s, re.S | re.I)
    if not m:
        return None
    try:
        root = ET.fromstring(m.group(1).strip())
    except ET.ParseError:
        return None
    owners = []
    for o in root.findall("reportingOwner"):
        rel = o.find("reportingOwnerRelationship")
        flag = lambda n: _txt(rel, n).lower() in ("1", "true") if rel is not None else False
        owners.append({"cik": _txt(o, "reportingOwnerId/rptOwnerCik").zfill(10), "officer": flag("isOfficer"),
                       "director": flag("isDirector"), "title": _txt(rel, "officerTitle") if rel is not None else ""})
    plan = _txt(root, "aff10b5One").lower() in ("1", "true")
    foot = " ".join((f.text or "") for f in root.iter("footnote"))
    tx = []
    for t in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        try:
            sh = float(_txt(t, "transactionAmounts/transactionShares/value") or 0)
            px = float(_txt(t, "transactionAmounts/transactionPricePerShare/value") or 0)
        except ValueError:
            continue
        tx.append({"code": _txt(t, "transactionCoding/transactionCode"),
                   "ad": _txt(t, "transactionAmounts/transactionAcquiredDisposedCode/value"),
                   "date": _txt(t, "transactionDate/value")[:10], "shares": sh, "price": px})
    return {"issuer_cik": _txt(root, "issuer/issuerCik").zfill(10), "symbol": _txt(root, "issuer/issuerTradingSymbol").upper(),
            "owners": owners, "plan": plan or bool(_PLAN_RE.search(foot)), "tx": tx}


def fetch_day(d: date, cik_map: Dict[str, str]) -> Optional[int]:
    """Stáhne Form 4 vydavatelů z vesmíru zveřejněné v den d a připíše je do insider_trades.jsonl. None = index chybí."""
    idx = daily_index(d)
    if idx is None:
        return None
    paths = sorted({p for cik, p in idx if cik in cik_map})[:MAX_FILINGS_PER_DAY]
    n = 0
    with open(TRADES_FILE, "a", encoding="utf-8") as f:
        for p in paths:
            raw = _get("https://www.sec.gov/Archives/" + p)
            if not raw:
                continue
            x = parse_form4(raw)
            if not x or x["issuer_cik"] not in cik_map:
                continue
            for o in x["owners"]:
                for t in x["tx"]:
                    if t["code"] not in ("P", "S"):
                        continue
                    f.write(json.dumps({"filed": d.isoformat(), "acc": p.rsplit("/", 1)[-1], "ticker": cik_map[x["issuer_cik"]],
                                        "owner": o["cik"], "officer": o["officer"], "title": o["title"], "plan": x["plan"],
                                        **t}) + "\n")
                    n += 1
    return n


# ---------------------------------------------------------------- stav

def _seen() -> Dict[str, Any]:
    try:
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_trades() -> List[Dict[str, Any]]:
    out, keys = [], set()
    try:
        with open(TRADES_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                k = (r["acc"], r["owner"], r["date"], r["code"], r["shares"], r["price"])
                if k not in keys:
                    keys.add(k)
                    out.append(r)
    except FileNotFoundError:
        pass
    return out


def build_history() -> Dict[str, List[str]]:
    """Insider CIK -> měsíce obchodů 'YYYY-MM' (P/S) z SEC Form 345 datasetů (2021+), jednou při prvním spuštění."""
    hist: Dict[str, Set[str]] = {}
    try:
        import pandas as pd
        for p in sorted(FORM345_DIR.glob("*.pkl")):
            if int(p.stem[:4]) < 2021:
                continue
            df = pd.read_pickle(p)
            df = df[df["TRANS_CODE"].isin(["P", "S"])]
            for cik, d in zip(df["RPTOWNERCIK"].astype(str), df["TRANS_DATE"]):
                if pd.notna(d):
                    hist.setdefault(cik.zfill(10), set()).add(f"{d.year:04d}-{d.month:02d}")
    except Exception as e:
        logger.warning(f"🕵️ Insideři: historie z Form 345 nejde načíst: {e}")
    out = {k: sorted(v) for k, v in hist.items()}
    HISTORY_FILE.write_text(json.dumps(out), encoding="utf-8")
    return out


def load_history() -> Dict[str, Set[str]]:
    try:
        h = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        h = build_history()
    return {k: set(v) for k, v in h.items()}


def is_routine(owner: str, tdate: str, hist: Dict[str, Set[str]]) -> bool:
    y, m = int(tdate[:4]), int(tdate[5:7])
    h = hist.get(owner, set())
    return sum(f"{y - k:04d}-{m:02d}" in h for k in (1, 2, 3)) >= 2


def update(days: List[date], universe: Set[str], force: bool = False) -> Dict[str, Any]:
    """Doplní chybějící dny (každý den se stahuje jen jednou). Vrací souhrn pro log."""
    seen = _seen()
    todo = [d for d in days if force or d.isoformat() not in seen.get("done", [])]
    if not todo:
        return {"fetched": 0, "days": []}
    cik_map = ticker_ciks(universe)
    if not cik_map:
        return {"fetched": 0, "days": [], "error": "chybí mapa CIK"}
    got = []
    for d in todo:
        n = fetch_day(d, cik_map)
        if n is not None:
            seen.setdefault("done", []).append(d.isoformat())
            got.append((d.isoformat(), n))
    seen["done"] = sorted(set(seen.get("done", [])))[-400:]
    SEEN_FILE.write_text(json.dumps(seen), encoding="utf-8")
    return {"fetched": sum(n for _, n in got), "days": got}


# ---------------------------------------------------------------- signál

def flags(trades: List[Dict[str, Any]], t_day: date, buy_days: List[date], sell_days: List[date],
          hist: Dict[str, Set[str]]) -> Dict[str, Dict[str, Any]]:
    """Ticker -> souhrn oportunistických nákupů (čistá funkce, testovatelná offline)."""
    bset, sset = {d.isoformat() for d in buy_days}, {d.isoformat() for d in sell_days}
    s_lo = min(sset) if sset else t_day.isoformat()
    sold = {r["ticker"] for r in trades if r["code"] == "S" and s_lo <= r["date"] <= t_day.isoformat()}
    out: Dict[str, Dict[str, Any]] = {}
    for r in trades:
        if r["code"] != "P" or r.get("ad") not in ("A", "") or r["filed"] not in bset or r.get("plan"):
            continue
        if r["price"] <= 0 or r["shares"] <= 0 or is_routine(r["owner"], r["date"] or r["filed"], hist):
            continue
        x = out.setdefault(r["ticker"], {"usd": 0.0, "owners": set(), "officer": False, "new": False, "titles": set()})
        x["usd"] += r["shares"] * r["price"]
        x["owners"].add(r["owner"])
        x["officer"] |= bool(r.get("officer"))
        x["new"] |= r["filed"] == t_day.isoformat()
        if r.get("title"):
            x["titles"].add(r["title"])
    res = {}
    for t, x in out.items():
        ok = x["usd"] >= MIN_USD and x["officer"] and x["new"] and t not in sold
        res[t] = {"usd": round(x["usd"]), "owners": len(x["owners"]), "officer": x["officer"], "new": x["new"],
                  "sold": t in sold, "titles": sorted(x["titles"])[:3], "ok": ok}
    return res


def price_check(df, side_rate: float) -> Dict[str, Any]:
    """Cenové podmínky V3 nad denními svíčkami do dne t (včetně). Vrací plán nebo důvod zamítnutí."""
    import swing_rules as R
    if df is None or len(df) < 210:
        return {"ok": False, "reason": "málo dat"}
    c, h, l, v = df["Close"], df["High"], df["Low"], df["Volume"]
    close = float(c.iloc[-1])
    a14 = float(R.atr(df, 14).iloc[-1])
    sma200 = float(c.tail(200).mean())
    adv = float((c * v).tail(20).mean())
    dist = close / float(h.tail(61).max()) - 1
    low5 = float(l.tail(6).min())
    why = None
    if close < MIN_PRICE:
        why = f"cena {close:.2f} < {MIN_PRICE}"
    elif adv < MIN_ADV:
        why = f"obrat {adv / 1e6:.1f} mil. < 10 mil."
    elif close <= sma200:
        why = "pod SMA200"
    elif not (DIST_MIN <= dist <= DIST_MAX):
        why = f"{dist * 100:.1f} % od 60denního maxima (mimo -22 až -4 %)"
    elif not close > low5 + 0.5 * a14:
        why = "bez odrazu od dna korekce"
    if why:
        return {"ok": False, "reason": why, "dist": round(dist, 4)}
    stop = min(float(l.tail(10).min()), close - 2 * a14)
    if not (0 < stop < close):
        return {"ok": False, "reason": "neplatný stop"}
    target = R.target_2r(close, stop, side_rate)
    return {"ok": True, "reason": "", "close": close, "atr14": a14, "dist": round(dist, 4),
            "plan": {"ok": True, "entry_ref": round(close, 4), "stop": round(stop, 4), "target": round(target, 4),
                     "risk_ps": round(close - stop, 4), "rr_net": 2.0}}


def market_cap(ticker: str) -> Optional[float]:
    try:
        import yfinance as yf
        mc = yf.Ticker(ticker).fast_info.get("market_cap")
        return float(mc) if mc and not math.isnan(float(mc)) else None
    except Exception:
        return None


def candidates(day: date, t_day: date, buy_days: List[date], sell_days: List[date], universe: Dict[str, str],
               side_rate: float, fetch_daily, upto) -> Dict[str, Any]:
    """Kandidáti stopy E na dnešek (`day`) ze signálu dne t. fetch_daily/upto z swing_shadow (testy je podstrčí)."""
    upd = update(sorted(set(buy_days) | set(sell_days)), set(universe))
    trades = load_trades()
    hist = load_history()
    fl = flags(trades, t_day, buy_days, sell_days, hist)
    hits = {t: x for t, x in fl.items() if x["ok"]}
    daily = fetch_daily(sorted(hits)) if hits else {}
    out, rejected = [], []
    for t, x in sorted(hits.items(), key=lambda kv: (-math.log(max(kv[1]["usd"], 1)), -kv[1]["owners"])):
        pc = price_check(upto(daily[t], t_day) if t in daily else None, side_rate)
        if not pc["ok"]:
            rejected.append({"ticker": t, "reason": pc["reason"], **x})
            continue
        mc = market_cap(t)
        if mc and mc > MAX_MCAP:
            rejected.append({"ticker": t, "reason": f"kapitalizace {mc / 1e9:.0f} mld. > 25 mld.", **x})
            continue
        score = round(math.log10(max(x["usd"], 1)) + 0.5 * x["owners"], 3)
        out.append({"ticker": t, "ccy": "USD", "sector": universe.get(t, "Unknown"), "score": score, "parts": {},
                    "headlines": [], "plan": pc["plan"], "ind": {"atr14": round(pc["atr14"], 4), "close": pc["close"]},
                    "insider": {**x, "dist": pc["dist"], "mcap": mc},
                    "time_stop_days": TIME_STOP_DAYS, "max_hold_days": MAX_HOLD_DAYS})
    return {"candidates": out, "rejected": rejected, "flags": len(fl), "update": upd}


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--backfill":
        import swing_scanner
        n = int(sys.argv[2])
        uni = swing_scanner.load_universe()
        if not HISTORY_FILE.exists():
            h = build_history()
            print(f"historie insiderů: {len(h)} osob")
        days, d = [], date.today() - timedelta(days=1)
        while len(days) < n:
            if d.weekday() < 5:
                days.append(d)
            d -= timedelta(days=1)
        print(update(sorted(days), set(uni)))
        print(f"záznamů v {TRADES_FILE.name}: {len(load_trades())}")
