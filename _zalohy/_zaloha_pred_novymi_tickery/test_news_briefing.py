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
print("ALL OK")
