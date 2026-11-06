import sys
base = sys.argv[1]
p = base + '/news_briefing.py'
s = open(p, encoding='utf-8').read()


def rep(a, b):
    global s
    assert s.count(a) == 1, a
    s = s.replace(a, b)


rep('''MAX_EARNINGS_ROWS = 30        # z kalendáře výsledků jen největší firmy podle tržní kapitalizace
''', '''MAX_EARNINGS_ROWS = 30        # z kalendáře výsledků jen největší firmy podle tržní kapitalizace
MAX_EXTRA_TICKERS = 5         # kolik firem mimo watchlist smí ranní přehled na den přidat (uživatel 27.10., včetně IPO)
SUMMARY_TIMEOUT_S = 120.0     # jediné volání za den, nejlepší model potřebuje na dlouhý prompt víc času
''')

# --- obnova seznamu instrumentů T212 (nové IPO) ---
rep('''def collect_raw_briefing() -> Dict[str, Any]:''', '''def refresh_t212_instruments() -> Optional[int]:
    """Stáhne aktuální seznam instrumentů T212, aby šly ověřit i čerstvé IPO. Vrací počet, None = beze změny."""
    import trading212
    if not trading212.is_configured():
        return None
    status, data = trading212._request("GET", "/equity/metadata/instruments", timeout=60)
    if status != 200 or not isinstance(data, list) or len(data) < 1000:
        logger.warning(f"📰 Seznam instrumentů T212 se neobnovil ({status}), zůstává uložený")
        return None
    path = trading212.T212_INSTRUMENTS_FILE
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    tmp.replace(path)
    trading212._instruments_loaded = False  # další dotaz načte nový soubor
    trading212._instrument_currencies = None
    return len(data)


def collect_raw_briefing() -> Dict[str, Any]:''')
rep('''    n_head = sum(len(v) for v in raw["headlines"].values())''', '''    try:
        n = refresh_t212_instruments()
        if n:
            logger.info(f"📰 Seznam instrumentů T212 obnoven ({n} instrumentů)")
    except Exception as e:
        logger.warning(f"📰 Obnova seznamu instrumentů T212 selhala: {e}")

    n_head = sum(len(v) for v in raw["headlines"].values())''')

# --- prompt ---
rep('''  "risks": ["Hlavní rizika dne pro obchodování"]
}''', '''  "risks": ["Hlavní rizika dne pro obchodování"],
  "new_tickers": [{"ticker": "SYMBOL", "reason": "1 věta: jaká konkrétní zpráva dnes s firmou hýbe", "ipo": false}]
}''')
rep('''Do "tickers" dej jen tickery z watchlistu, ke kterým v datech opravdu něco je (používej přesné tickery jako NVDA, SAP.DE, BTCUSDT).""")''',
    '''Do "tickers" dej jen tickery z watchlistu, ke kterým v datech opravdu něco je (používej přesné tickery jako NVDA, SAP.DE, BTCUSDT).
Do "new_tickers" dej nejvýš 5 US akcií MIMO watchlist, kolem kterých je dnes podle dat velká konkrétní zpráva
(vstup na burzu/IPO, schválení nebo představení léku, akvizice, výsledky, velký kontrakt). Jen burzovní symbol US akcie
(např. MRNA), "ipo": true u firmy, která právě vstupuje nebo vstoupila na burzu. Když nic takového v datech není, dej [].""")''')

# --- volání: timeout ---
rep('''                timeout=60.0,''', '''                timeout=SUMMARY_TIMEOUT_S,''')
rep('''            last_error = e
            logger.warning(f"📰 Ranní přehled: {model_name} selhal ({e}), zkouším další model...")''', '''            last_error = e
            logger.warning(f"📰 Ranní přehled: {model_name} selhal ({type(e).__name__}: {e}), zkouším další model...")''')

# --- ověření nových tickerů ---
rep('''def _today_str() -> str:''', '''def validate_extra_tickers(summary: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Návrhy Gemini -> max MAX_EXTRA_TICKERS US akcií mimo watchlist, které jdou koupit na T212."""
    import trading212
    if not summary or not isinstance(summary.get("new_tickers"), list):
        return []
    watch = set(US_STOCKS_WATCHLIST + EU_STOCKS_WATCHLIST + CRYPTO_WATCHLIST)
    out, rejected = [], []
    for item in summary["new_tickers"]:
        if not isinstance(item, dict):
            continue
        sym = str(item.get("ticker", "")).upper().strip().lstrip("$")
        if not re.fullmatch(r"[A-Z]{1,5}", sym) or sym in watch or any(x["ticker"] == sym for x in out):
            rejected.append(sym or "?")
            continue
        if not trading212.to_t212_ticker(sym):
            rejected.append(f"{sym} (není na T212)")
            continue
        out.append({"ticker": sym, "reason": str(item.get("reason", "")).strip(), "ipo": bool(item.get("ipo"))})
        if len(out) >= MAX_EXTRA_TICKERS:
            break
    if rejected:
        logger.info(f"📰 Nové tickery zamítnuté: {', '.join(rejected)}")
    if out:
        logger.info(f"📰 Dnes navíc sleduji: {', '.join(x['ticker'] for x in out)}")
    return out


def get_extra_tickers() -> List[str]:
    """US akcie, které dnešní ranní přehled přidal ke sledování (Scout je skenuje s US watchlistem)."""
    b = load_briefing()
    return [x["ticker"] for x in (b or {}).get("extra_tickers", [])]


def _today_str() -> str:''')
rep('''            summary = None
        data = {
            "date": today,''', '''            summary = None
        try:
            extra = validate_extra_tickers(summary)
        except Exception as e:
            logger.error(f"📰 Ověření nových tickerů selhalo: {e}")
            extra = []
        data = {
            "date": today,
            "extra_tickers": extra,''')

# --- kontext pro analýzu ---
rep('''    note = (s.get("tickers") or {}).get(ticker)
    if note:
        parts.append(f"K {ticker}: {note}")''', '''    note = (s.get("tickers") or {}).get(ticker)
    if note:
        parts.append(f"K {ticker}: {note}")
    extra = next((x for x in b.get("extra_tickers") or [] if x.get("ticker") == ticker), None)
    if extra:
        ipo = " Čerstvé IPO: krátká cenová historie, vysoká volatilita." if extra.get("ipo") else ""
        parts.append(f"{ticker} je dnes sledován navíc kvůli ranní zprávě: {extra.get('reason', '')}{ipo}")''')

# --- Discord ---
rep('''        if s.get("crypto"):
            lines.append(f"\\n🪙 **Krypto:** {s['crypto']}")''', '''        if b.get("extra_tickers"):
            lines.append("\\n🆕 **Dnes navíc sleduji:**")
            lines += [f"• **{x['ticker']}**{' (IPO)' if x.get('ipo') else ''}: {x.get('reason', '')}" for x in b["extra_tickers"]]
        if s.get("crypto"):
            lines.append(f"\\n🪙 **Krypto:** {s['crypto']}")''')
open(p, 'w', encoding='utf-8').write(s)

# --- scout ---
p2 = base + '/scout.py'
s2 = open(p2, encoding='utf-8', newline='').read()
crlf = '\r\n' in s2
s2 = s2.replace('\r\n', '\n')
a = '''    for market_name, market_type, tickers in active_market_lists:
        logger.info('''
b = '''    for market_name, market_type, tickers in active_market_lists:
        if market_type == "STOCK_US":
            # Firmy, které dnes ráno přidal přehled zpráv (velká zpráva, IPO), max 5
            try:
                extra = [t for t in news_briefing.get_extra_tickers() if t not in tickers]
            except Exception as e:
                logger.warning(f"Nové tickery z ranního přehledu nejde načíst: {e}")
                extra = []
            tickers = list(tickers) + extra
        logger.info('''
assert s2.count(a) == 1
s2 = s2.replace(a, b)
a = "import market_hours\n"
assert s2.count(a) == 1
s2 = s2.replace(a, "import market_hours\nimport news_briefing\n")
if crlf:
    s2 = s2.replace('\n', '\r\n')
open(p2, 'w', encoding='utf-8', newline='').write(s2)
print("patched")
