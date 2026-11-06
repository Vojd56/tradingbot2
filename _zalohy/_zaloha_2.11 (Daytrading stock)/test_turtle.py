"""Offline test strategie TURTLE 4h (žádné skutečné API volání, žádné příkazy na burzu).
Spuštění:  python test_turtle.py
Nesahá na paper_trades.json ani paper_trades_turtle.json – používá dočasné soubory.
"""
import os, sys, json, tempfile
from datetime import datetime, timezone
from pathlib import Path
os.environ.update(BINANCE_API_KEY="k", BINANCE_SECRET="s", BINANCE_FUTURES_URL="https://testnet.fake",
                  TRADING_212_API_KEY="k", TRADING_212_SECRET_KEY="s", TRADING_212_ENV="demo")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import paper_trading
TMPDIR = Path(tempfile.mkdtemp())
paper_trading.PAPER_TRADES_FILE = TMPDIR / "paper_trades_test.json"
LEDGER = TMPDIR / "paper_trades_turtle_test.json"
NO_STOP = TMPDIR / "STOP_neexistuje"

import turtle_strategy as ts
ts.TURTLE_ENABLED = True  # test logiky běží i když je turtle v configu vypnuté

FAILS = []
def check(name, cond, info=""):
    print(("OK   " if cond else "FAIL ") + name + (f"  ({info})" if info and not cond else ""))
    if not cond:
        FAILS.append(name)

BAR = ts.BAR_MS
T0 = 1793000000000 - (1793000000000 % BAR)

def bars(closes, highs=None, lows=None, start=T0):
    out = []
    for i, c in enumerate(closes):
        h = highs[i] if highs else c + 1
        l = lows[i] if lows else c - 1
        o = start + i * BAR
        out.append([o, c, h, l, c, 100.0, o + BAR - 1])
    return out

flat = [100.0] * 30
# ---------- 1) vstupní signál ----------
kl = bars(flat + [105.0])                       # poslední svíčka zavře 105 > high 101
now_ms = kl[-1][6] + 2 * 60 * 1000               # 2 min po zavření
sig = ts.entry_signal(kl, now_ms)
check("Průraz nad 20svíčkové high = signál", sig is not None and sig["level"] == 101.0)
check("ATR z uzavřených svíček > 0", sig is not None and sig["atr"] > 0)
check("SL vzdálenost = 2x ATR", sig is not None and abs(sig["stop_distance"] - 2 * sig["atr"]) < 1e-9)
check("Bez průrazu žádný signál", ts.entry_signal(bars(flat + [100.5]), now_ms) is None)
# rozjetá (neuzavřená) svíčka se nepočítá
kl_open = bars(flat + [100.0, 110.0])
check("Neuzavřená svíčka se nepočítá", ts.entry_signal(kl_open, kl_open[-2][6] + 60_000) is None)
check("Starý signál (po restartu) se nebere", ts.entry_signal(kl, kl[-1][6] + 3 * 3600 * 1000) is None)

# ATR na známých datech: TR = 2 každou svíčku -> ATR 2
check("ATR výpočet", abs(ts.atr(bars([100.0] * 20)) - 2.0) < 1e-9)

# ---------- 2) velikost pozice a P&L s poplatky ----------
qty = ts.position_qty(100.0, 95.0, 2.2)
check("1R = 2,2 USD", abs(qty * 5.0 - 2.2) < 1e-9)
p = ts.trade_pnl(100.0, 95.0, qty, 95.0, fee_pct=0.05, slip_pct=0.02)
exp_buy, exp_sell = 100.02, 95.0 * 0.9998
exp = qty * (exp_sell - exp_buy) - qty * (exp_buy + exp_sell) * 0.0005
check("P&L na SL po poplatcích a skluzu", abs(p["pnl_usd"] - round(exp, 4)) < 1e-4, f"{p['pnl_usd']} vs {exp}")
check("Ztráta na SL je o poplatky horší než -1R", p["r"] < -1.0)
p0 = ts.trade_pnl(100.0, 100.0, qty, 95.0)
check("Výstup za vstupní cenu = jen poplatky (minus)", p0["pnl_usd"] < 0 and p0["fees_usd"] > 0)

# ---------- 3) výstup pod 10svíčkové low ----------
opened_ms = kl[-1][6] + 1
kl_exit = bars(flat + [105.0, 104.0, 98.0])      # 98 < low (99) předchozích 10
ex = ts.exit_signal(kl_exit, kl_exit[-1][6] + 60_000, opened_ms)
check("Zavření pod 10svíčkové low = výstup", ex is not None and ex["level"] == 99.0)
check("Bez propadu žádný výstup", ts.exit_signal(bars(flat + [105.0, 104.0, 103.0]), kl_exit[-1][6] + 60_000, opened_ms) is None)
check("Svíčka před otevřením pozice výstup nespouští", ts.exit_signal(kl_exit, kl_exit[-1][6] + 60_000, kl_exit[-1][6] + 5) is None)

# ---------- 4) Stop-Loss ----------
tr = ts.new_trade("SOLUSDT", 105.0, sig, datetime.now(timezone.utc))
check("SL = entry - 2x ATR", abs(tr["stop_loss"] - (105.0 - sig["stop_distance"])) < 1e-9)
check("Nad SL se nezavírá", not ts.stop_hit(tr, tr["stop_loss"] + 0.01))
check("Na SL se zavírá", ts.stop_hit(tr, tr["stop_loss"]))

# ---------- 5) evidence, limity, oddělení od paper_trades.json ----------
now = datetime.fromtimestamp(now_ms / 1000, timezone.utc)
syms = ["S1USDT", "S2USDT", "S3USDT", "S4USDT", "S5USDT", "S6USDT", "S7USDT"]
events = ts.run_4h_evaluation(syms, now=now, get_klines=lambda s: kl, get_price=lambda s: 105.0,
                              path=LEDGER, kill_switch=NO_STOP)
ledger = ts.load_ledger(LEDGER)
check("Max 5 turtle pozic najednou", len(ts.open_positions(ledger)) == ts.TURTLE_MAX_POSITIONS, len(ts.open_positions(ledger)))
check("Události pro Discord", sum(e["type"] == "open" for e in events) == 5)
check("Zpráva má prefix [TURTLE DEMO]", "[TURTLE DEMO]" in ts.format_event(events[0]))
events2 = ts.run_4h_evaluation(syms, now=now, get_klines=lambda s: kl, get_price=lambda s: 105.0,
                               path=LEDGER, kill_switch=NO_STOP)
check("Stejný signál podruhé neotevře nic (1 pozice na symbol)", not any(e["type"] == "open" for e in events2))
check("Turtle se nezapisuje do paper_trades.json", not paper_trading.PAPER_TRADES_FILE.exists() or paper_trading.load_trades() == [])
check("Turtle se nepočítá do limitů SFP", paper_trading.get_open_positions_count() == 0)

# vypínač STOP blokuje nové vstupy
LEDGER2 = TMPDIR / "turtle2.json"
STOPF = TMPDIR / "STOP"; STOPF.write_text("")
ev_stop = ts.run_4h_evaluation(["S1USDT"], now=now, get_klines=lambda s: kl, get_price=lambda s: 105.0,
                               path=LEDGER2, kill_switch=STOPF)
check("Vypínač STOP: žádný nový vstup", ev_stop == [])

# SL kontrola zavře pozice a zapíše P&L
sl_events = ts.check_stops(get_price=lambda s: 1.0, path=LEDGER, now=now)
ledger = ts.load_ledger(LEDGER)
check("check_stops zavře všechny pod SL", len(sl_events) == 5 and not ts.open_positions(ledger))
check("Uzavřený obchod má P&L a důvod", all(t.get("exit_reason") == "Stop-Loss" and "pnl_usd" in t for t in ledger))
s = ts.summary(ledger)
check("Souhrn: 5 uzavřených, celkem v minusu", s["closed"] == 5 and s["total_usd"] < 0)

# chyba při stahování dat nesmí shodit vyhodnocení
def boom(sym): raise RuntimeError("síť nejde")
try:
    ts.run_4h_evaluation(["S1USDT"], now=now, get_klines=boom, get_price=boom, path=TMPDIR / "t3.json", kill_switch=NO_STOP)
    check("Chyba sítě nespadne", True)
except Exception as e:
    check("Chyba sítě nespadne", False, str(e))

print()
print("VŠE OK" if not FAILS else f"SELHALO {len(FAILS)}: {FAILS}")
sys.exit(1 if FAILS else 0)
