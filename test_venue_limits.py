"""Offline test limitů podle velikosti účtu (risk_guard.venue_heat_block) a záznamu rozhodnutí (decision_log)."""
import json, os, sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
os.environ.update(BINANCE_API_KEY="k", BINANCE_SECRET="s", BINANCE_FUTURES_URL="https://testnet.fake",
                  TRADING_212_API_KEY="k", TRADING_212_SECRET_KEY="s", TRADING_212_ENV="demo")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import paper_trading
paper_trading.PAPER_TRADES_FILE = Path(tempfile.mkdtemp()) / "paper_trades_test.json"

from config import DAILY_LOSS_LIMIT_USD, HEAT_SLIPPAGE_RESERVE
from risk_guard import venue_heat_block, venue_limit_usd
import decision_log

FAILS = []
def ok(cond, name, info=""):
    print(("OK   " if cond else "FAIL ") + name + (f"  ({info})" if info and not cond else ""))
    if not cond:
        FAILS.append(name)

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
def open_t(mt, qty, entry, sl):
    return {"status": "OPEN", "market_type": mt, "bot_qty": qty, "entry": entry, "stop_loss": sl,
            "opened_by_bot": True, "action": "AUTO_BUY"}
def closed_t(mt, pnl, minutes_ago=30):
    return {"status": "CLOSED", "market_type": mt, "pnl_usd": pnl, "opened_by_bot": True, "action": "AUTO_BUY",
            "closed_at": (NOW - timedelta(minutes=minutes_ago)).isoformat()}

ok(abs(venue_limit_usd(5800) - 174.0) < 1e-6, "3 % z 5 800 USD = 174 USD")
ok(venue_limit_usd(50_000) == DAILY_LOSS_LIMIT_USD, "velký účet: strop 300 USD")
ok(venue_heat_block(50.0, "STOCK", None, NOW, []) is None, "neznámý kapitál -> neblokuje")
ok(venue_heat_block(25.0, "STOCK", 5800, NOW, []) is None, "prázdný účet, obchod 25 USD -> povoleno")
room = HEAT_SLIPPAGE_RESERVE * 174.0
ok(venue_heat_block(room + 1, "STOCK", 5800, NOW, []) is not None, "nový obchod nad 0,8 x limit -> blokováno")
r = venue_heat_block(25.0, "STOCK", 5800, NOW, [closed_t("STOCK", -180)])
ok(r is not None and "Denní limit T212" in r, "ztráta akcií 180 >= 174 -> blokováno", r)
ok(venue_heat_block(25.0, "CRYPTO", 5800, NOW, [closed_t("STOCK", -180)]) is None, "ztráta akcií neblokuje krypto")
ok(venue_heat_block(25.0, "STOCK", 5800, NOW, [closed_t("STOCK", -180, 60 * 20)]) is None, "včerejší ztráta se nepočítá")
crypto_open = [open_t("CRYPTO", 1, 100.0, 50.0)]  # ~50 USD otevřeného rizika
ok(venue_heat_block(100.0, "CRYPTO", 5000, NOW, crypto_open) is not None, "krypto: 50 + 100 > 0,8 x 150 -> blokováno")
ok(venue_heat_block(100.0, "STOCK", 5000, NOW, crypto_open) is None, "otevřené krypto se do akcií nepočítá")

p = Path(tempfile.mkdtemp()) / "signals.jsonl"
decision_log.log_decision({"strategy": "TEST", "ticker": "X", "action": "SKIP"}, path=p)
decision_log.log_decision({"strategy": "TEST", "ticker": "Y", "action": "BUY", "obj": object()}, path=p)
rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()]
ok(len(rows) == 2 and rows[0]["ticker"] == "X" and "ts_utc" in rows[0], "záznam rozhodnutí: jeden řádek JSON na signál", rows)
decision_log.log_decision({"a": 1}, path=Path(tempfile.mkdtemp()) / "neexistuje" / "x.jsonl")
ok(True, "chyba zápisu nevyhodí výjimku")

print()
print("VŠECHNO PROŠLO" if not FAILS else f"SELHALO {len(FAILS)}: {FAILS}")
sys.exit(1 if FAILS else 0)
