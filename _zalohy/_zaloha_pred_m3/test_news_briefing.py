"""Offline test ranního přehledu zpráv (news_briefing). Nic nestahuje, Gemini nevolá.
Spuštění:  python test_news_briefing.py
"""
import sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import news_briefing as nb
from gemini_team import _build_prompt

ok = lambda c, m: print(("PASS " if c else "FAIL ") + m) or (c or sys.exit(1))

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>X</title>
<item><title>Nvidia to report earnings  Wednesday</title><pubDate>Tue, 27 Oct 2026 06:00:00 GMT</pubDate></item>
<item><title>Fed holds rates</title><pubDate>Mon, 19 Oct 2026 06:00:00 GMT</pubDate></item>
<item><title></title></item>
</channel></rss>"""
ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Bitcoin jumps above 100k</title><updated>2026-10-27T05:00:00Z</updated></entry></feed>"""

items = nb.parse_feed(RSS, "Test")
ok(len(items) == 2 and items[0]["title"] == "Nvidia to report earnings Wednesday", "RSS: titulky, prázdný vynechán, mezery srovnány")
ok(items[0]["published"].startswith("2026-10-27T06:00"), "RSS: datum převedeno na UTC ISO")
a = nb.parse_feed(ATOM, "Atom")
ok(len(a) == 1 and a[0]["title"].startswith("Bitcoin"), "Atom feed se čte taky")
now = datetime(2026, 10, 27, 8, 0, tzinfo=timezone.utc)
ok(nb._is_fresh(items[0], now) and not nb._is_fresh(items[1], now), "titulek starší 36 h se zahodí")

ok(nb._mentions("Nvidia beats estimates", "NVDA") and nb._mentions("NVDA falls", "NVDA"), "zmínka firmy podle jména i tickeru")
ok(nb._mentions("Bitcoin rallies", "BTCUSDT") and not nb._mentions("Apple event", "NVDA"), "zmínka coinu, cizí firma ne")

today = nb._today_str()
raw = {
    "headlines": {"trhy": [{"title": "Nvidia unveils new chip", "source": "CNBC", "published": None}],
                  "krypto": [{"title": "Bitcoin ETF inflows", "source": "CoinDesk", "published": None}]},
    "company_news": {"NVDA": ["Nvidia press conference at GTC"]},
    "earnings": {today: [{"symbol": "MSFT", "name": "Microsoft", "time": "po zavření", "eps_forecast": "$3.10"}]},
    "economic": [{"when": "28.10. 19:00", "country": "USD", "title": "FOMC Statement", "forecast": "", "previous": ""}],
    "watchlist_earnings": {"MSFT": today},
    "sources_failed": [],
}
prompt = nb.build_summary_prompt(raw, today)
ok("MSFT Microsoft" in prompt and "FOMC Statement" in prompt and "press conference" in prompt, "prompt shrnutí obsahuje výsledky, makro i zprávy firem")

briefing = {"date": today, "summary": {
    "market_mood": "RISK-OFF", "summary": "Trhy čekají na Fed.",
    "events_today": ["MSFT výsledky po zavření", "FOMC 19:00"],
    "tickers": {"MSFT": "Výsledky dnes po zavření, vysoká volatilita."},
    "crypto": "Bitcoin táhnou přílivy do ETF.", "risks": ["Fed"]}, "raw": raw}
ctx = nb.get_context_for_ticker("MSFT", briefing)
ok("RISK-OFF" in ctx and "zveřejní výsledky" in ctx and "po zavření" in ctx, "kontext pro MSFT: nálada, výsledky, poznámka")
ctx_btc = nb.get_context_for_ticker("BTCUSDT", briefing)
ok("ETF" in ctx_btc and "zveřejní výsledky" not in ctx_btc, "kontext pro krypto: krypto shrnutí, bez earnings")
ok(nb.get_context_for_ticker("NVDA", {"date": today, "summary": None, "raw": raw}).count("Nvidia") >= 1,
   "bez Gemini shrnutí aspoň surové titulky")
ok(len(nb.get_context_for_ticker("MSFT", briefing, max_chars=100)) <= 100, "kontext oříznut na limit")

msg = nb.format_discord_message(briefing)
ok("Ranní přehled" in msg and "RISK-OFF" in msg and len(msg) <= 1900, "zpráva do Discordu, max 1900 znaků")
long_b = dict(briefing, summary=dict(briefing["summary"], summary="x" * 5000))
ok(len(nb.format_discord_message(long_b)) <= 1900, "dlouhá zpráva se ořízne")

p = _build_prompt("MSFT", 400.0, 1.0, 2.0, [], 0, 10, "Trading 212", False, market_context=ctx)
ok("RANNÍ PŘEHLED TRHU" in p and "zveřejní výsledky" in p, "Gemini analýza obchodu dostane ranní přehled")
p0 = _build_prompt("MSFT", 400.0, 1.0, 2.0, [], 0, 10, "Trading 212", False)
ok("RANNÍ PŘEHLED TRHU" not in p0, "bez přehledu se sekce nepřidá")
# Nové firmy z ranních zpráv (max 5 US akcií mimo watchlist, jen co jde koupit na T212)
import trading212
trading212.to_t212_ticker = lambda sym: None if sym == "NOPE" else f"{sym}_US_EQ"
sugg = {"new_tickers": [
    {"ticker": "MRNA", "reason": "Moderna představila nový lék", "ipo": False},
    {"ticker": "$ANTH", "reason": "Anthropic vstupuje na burzu", "ipo": True},
    {"ticker": "NVDA", "reason": "už ve watchlistu"}, {"ticker": "NOPE"}, {"ticker": "SAP.DE"},
    {"ticker": "MRNA"}, {"ticker": "A1"}, {"ticker": "AAA"}, {"ticker": "BBB"}, {"ticker": "CCC"}, {"ticker": "DDD"}]}
ex = nb.validate_extra_tickers(sugg)
ok([x["ticker"] for x in ex] == ["MRNA", "ANTH", "AAA", "BBB", "CCC"], "nové tickery: max 5, bez watchlistu, duplicit, EU a mimo T212")
ok(ex[1]["ipo"] is True and nb.validate_extra_tickers(None) == [] and nb.validate_extra_tickers({"new_tickers": "x"}) == [], "IPO příznak, prázdné/vadné návrhy")
b2 = dict(briefing, extra_tickers=ex)
c2 = nb.get_context_for_ticker("ANTH", b2)
ok("sledován navíc" in c2 and "IPO" in c2, "kontext pro nový ticker: důvod a varování IPO")
ok("Dnes navíc sleduji" in nb.format_discord_message(b2) and "ANTH** (IPO)" in nb.format_discord_message(b2), "Discord ukáže nové tickery")
ok("new_tickers" in nb.build_summary_prompt(raw, today), "ranní prompt žádá nové tickery (pořád jedno volání)")

# Správné vstupy pro Gemini (29. 10.): jméno firmy, relevantní titulky, pořadí kontextu, prompt
ok(nb.clean_company_name("Aixtron SE") == "Aixtron" and nb.clean_company_name("Renault S.A.") == "Renault"
   and nb.clean_company_name("adidas AG") == "adidas", "jméno firmy bez právní formy")
ok(nb._google_news_query("AIXA.DE", "Aixtron SE") == '"Aixtron" when:1d' and "Nvidia" in nb._google_news_query("NVDA"),
   "Google News hledá podle jména firmy, ne podle zkratky")
ok(not nb._mentions("ASM International NV (ASM.AS) stock price", "AIXA.DE", "Aixtron")
   and nb._mentions("Aixtron shares jump on orders", "AIXA.DE", "Aixtron"), "titulek o cizí firmě se zahodí")
long_mood = dict(briefing["summary"], summary="y" * 1400)
ctx_cut = nb.get_context_for_ticker("NVDA", {"date": today, "summary": long_mood, "raw": raw})
ok("press conference" in ctx_cut, "zprávy k firmě přežijí oříznutí (jsou před náladou trhu)")
p2 = _build_prompt("AIR.PA", 198.44, 3.39, 0.19, ["Airbus beats Q3 estimates"], 0, 10, "Trading 212", False, None, "",
                   {"company_name": "Airbus", "currency": "EUR", "day_change_pct": 3.2, "gap_pct": 3.0, "from_high_pct": -0.4})
ok("AIR.PA (Airbus)" in p2 and "198.440 EUR" in p2 and "gap při otevření +3.00%" in p2 and "reports Q3 results" in p2,
   "prompt: jméno firmy, měna burzy, denní změna a gap, výsledky jako katalyzátor")
p3 = _build_prompt("NVDA", 100.0, 1.0, 2.0, [], 0, 10, "Trading 212", False)
ok("100.000 USD" in p3 and "Dnes proti včerejšímu" not in p3, "prompt bez denních dat pořád funguje (USD)")
print("ALL OK")
