"""Offline test strategie SFP 15M a správy pozice (žádné skutečné API volání).
Spuštění:  python test_strategy.py
Nesahá na paper_trades.json – používá dočasný soubor.
"""
import os, sys, json, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
os.environ.update(BINANCE_API_KEY="k", BINANCE_SECRET="s", BINANCE_FUTURES_URL="https://testnet.fake",
                  TRADING_212_API_KEY="k", TRADING_212_SECRET_KEY="s", TRADING_212_ENV="demo")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import paper_trading
TMP = Path(tempfile.mkdtemp()) / "paper_trades_test.json"
paper_trading.PAPER_TRADES_FILE = TMP

import binance_client
import position_manager
import strategy_rules
from binance_client import analyze_sfp
from strategy_rules import apply_setup_levels, management_action, STRATEGY_SFP

FAILS = []
def check(name, cond, info=""):
    print(("OK   " if cond else "FAIL ") + name + (f"  ({info})" if info and not cond else ""))
    if not cond:
        FAILS.append(name)

BAR_MS = 15 * 60 * 1000
T0 = 1793000000000

def bars(rows):
    """rows = [(open, high, low, close, volume)] -> Binance klines (všechny uzavřené)."""
    out = []
    for i, (o, h, l, c, v) in enumerate(rows):
        ot = T0 + i * BAR_MS
        out.append([ot, str(o), str(h), str(l), str(c), str(v), ot + BAR_MS - 1])
    return out

def base_rows(p=100.0, n=30, vol=1000):
    return [(p, p * 1.002, p * 0.998, p, vol) for _ in range(n)]

NOW = T0 + 40 * BAR_MS  # po všech svíčkách

# ---------- 1) SFP detekce ----------
rows = base_rows()
rows[20] = (100, 100.2, 99.0, 99.8, 1000)       # 6h dno 99.0, potvrzené (dál jsou vyšší low)
rows.append((99.8, 100.0, 98.8, 99.6, 2500))    # signál: knot pod 99.0, zavřeno nad, objem 2.5x
r = analyze_sfp(bars(rows), now_ms=NOW)
check("SFP: sweep 6h dna + zavření nad + objem = signál", r["pattern_detected"], r.get("reason"))
check("SFP: SL pod knotem (98.8 - 0.1 %)", abs(r["stop_loss"] - 98.8 * 0.999) < 1e-6, r["stop_loss"])
check("SFP: TP = 2R", abs(r["take_profit"] - (99.6 + 2 * (99.6 - r["stop_loss"]))) < 1e-3, r["take_profit"])

kl = bars(rows)
kl[-1][6] = NOW + 60_000  # signální svíčka ještě běží
r = analyze_sfp(kl, now_ms=NOW)
check("SFP: rozjetá svíčka se nepočítá", not r["pattern_detected"])

rows_lowvol = list(rows); rows_lowvol[-1] = (99.8, 100.0, 98.8, 99.6, 900)
check("SFP: bez objemu žádný signál", not analyze_sfp(bars(rows_lowvol), now_ms=NOW)["pattern_detected"])

rows_noclose = list(rows); rows_noclose[-1] = (99.8, 100.0, 98.8, 98.9, 2500)
check("SFP: zavření pod dnem není SFP", not analyze_sfp(bars(rows_noclose), now_ms=NOW)["pattern_detected"])

rows_unconf = base_rows(); rows_unconf[-1] = (100, 100.2, 99.0, 99.8, 1000)  # dno je hned předchozí svíčka
rows_unconf.append((99.8, 100.0, 98.8, 99.6, 2500))
check("SFP: nepotvrzené dno (předchozí svíčka) se nebere", not analyze_sfp(bars(rows_unconf), now_ms=NOW)["pattern_detected"])

rows_wide = list(rows); rows_wide[-1] = (99.8, 100.0, 96.0, 99.6, 2500)  # riziko ~3.7 %
check("SFP: stop širší než 2 % = obchod se nebere", not analyze_sfp(bars(rows_wide), now_ms=NOW)["pattern_detected"])

# DOGE řád ceny (26. 10. bylo SL == entry kvůli zaokrouhlení na 0.08)
d = [(0.0768, 0.0770, 0.0766, 0.0768, 5e6) for _ in range(30)]
d[20] = (0.0768, 0.0769, 0.07598, 0.0763, 5e6)
d.append((0.0764, 0.0766, 0.07590, 0.0764, 1.2e7))
r = analyze_sfp(bars(d), now_ms=NOW)
check("DOGE: signál s cenou 0.0764", r["pattern_detected"], r.get("reason"))
check("DOGE: SL < entry (žádné SL == entry)", r["stop_loss"] < r["entry"], (r["stop_loss"], r["entry"]))

# ---------- 2) vstupní úrovně ----------
setup = {"entry": 99.6, "stop_loss": 98.7, "take_profit": 101.4}
lv = apply_setup_levels(99.7, setup)
check("Vstup: SL ze setupu, ne procentem", lv["stop_loss"] == 98.7 and lv["skip_reason"] is None, lv)
check("Vstup: TP aspoň 2R od aktuální ceny a 1:2 po nákladech", lv["take_profit"] >= 99.7 + 2 * 1.0 - 1e-6 and lv["net_rr"] >= 1.99, lv)
check("Vstup: cena utekla o > 0.5R = nekupovat", apply_setup_levels(100.2, setup)["skip_reason"] is not None)
check("Vstup: cena pod SL = nekupovat", apply_setup_levels(98.6, setup)["skip_reason"] is not None)

# ---------- 3) správa pozice ----------
now = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
tr = {"strategy": STRATEGY_SFP, "entry": 100.0, "stop_loss": 99.0, "initial_stop_loss": 99.0,
      "take_profit": 102.0, "decided_at": (now - timedelta(hours=1)).isoformat()}
check("Správa: pod +1R nic", management_action(tr, 100.9, now) is None)
check("Správa: +1R = PARTIAL", management_action(tr, 101.0, now) == "PARTIAL")
check("Správa: po částečném zisku už ne", management_action({**tr, "partial_taken": True}, 101.5, now) is None)
check("Správa: po 6 h = TIME_STOP", management_action({**tr, "decided_at": (now - timedelta(hours=6, minutes=1)).isoformat()}, 100.2, now) == "TIME_STOP")
check("Správa: staré pozice bez strategie se neřídí", management_action({**tr, "strategy": None}, 105, now) is None)

# ---------- 4) evidence: vlastnictví, re-entry, limit krypto, částečný zisk ----------
today = datetime.now(timezone.utc)
TMP.write_text(json.dumps([
    {"trade_id": "A", "ticker": "TSLA", "status": "OPEN", "market_type": "STOCK", "entry": 396, "shares": 0.05,
     "news_summary": "Pozice synchronizována z Trading 212 portfolia."},
    {"trade_id": "B", "ticker": "DOGEUSDT", "status": "CLOSED", "market_type": "CRYPTO", "pnl_usd": -2.1,
     "closed_by": "TradingBot (STOP-LOSS ZASAŽEN)", "closed_at": today.isoformat()},
    {"trade_id": "C", "ticker": "ETHUSDT", "status": "OPEN", "market_type": "CRYPTO", "entry": 2300, "shares": 0.2},
]))
check("Synchronizovaná pozice z T212 není botova", all(t["ticker"] != "TSLA" for t in paper_trading.get_open_positions()))
check("Po stopu dnes stejný symbol znovu nekoupit", not paper_trading.can_open_new_position_for("DOGEUSDT"))
check("Jiný symbol koupit lze (limit krypto nenaplněn)", paper_trading.can_open_new_position_for("SOLUSDT"))
data = json.loads(TMP.read_text())
# doplnit otevřené krypto pozice až do limitu z configu (ETH už je otevřené)
for i in range(paper_trading.MAX_OPEN_POSITIONS_CRYPTO - 1):
    data.append({"trade_id": f"D{i}", "ticker": f"X{i}USDT", "status": "OPEN", "market_type": "CRYPTO", "entry": 1.0, "shares": 1})
TMP.write_text(json.dumps(data))
check("Limit krypto pozic", not paper_trading.can_open_new_position_for("SOLUSDT"))

# částečný zisk v evidenci (paper) a výsledné P&L
TMP.write_text(json.dumps([{"trade_id": "P", "ticker": "SOLUSDT", "status": "OPEN", "market_type": "CRYPTO",
                            "entry": 100.0, "stop_loss": 99.0, "take_profit": 102.0, "shares": 10, "bot_qty": 10,
                            "strategy": STRATEGY_SFP, "decided_at": today.isoformat()}]))
tr = paper_trading.get_open_positions()[0]
ev = position_manager.manage_open_trade_sync(tr, 101.0)
t = paper_trading.get_open_positions()[0]
check("Paper: +1R prodá polovinu", ev and ev["type"] == "partial" and abs(t["bot_qty"] - 5) < 1e-9, ev)
check("Paper: stop zbytku na entry + poplatky", abs(t["stop_loss"] - 100.1) < 1e-9, t["stop_loss"])
closed = paper_trading.close_position("SOLUSDT", 100.1, "TradingBot (STOP-LOSS ZASAŽEN)", trade_id="P")
check("Paper: P&L sečte částečný zisk", abs(closed["pnl_usd"] - (5 * 1.0 + 5 * 0.1)) < 0.01, closed["pnl_usd"])
check("Stop na nule po zisku neblokuje další nákup", not paper_trading.stopped_out_today("SOLUSDT"))

# částečný zisk na burze (nasimulovaná burza)
calls = []
position_manager.ENABLE_BINANCE = True
binance_client.format_quantity = lambda s, q: round(q, 3)
binance_client.close_bot_position_sync = lambda s, q: (calls.append(("close", q)), {"success": True, "quantity": q, "exit_price": 101.05})[1]
binance_client.cancel_protective_order_sync = lambda s, ref: (calls.append(("cancel", ref)), True)[1]
binance_client.place_protective_order_sync = lambda s, k, q, p: (calls.append((k, q, p)), {"success": True, "ref": {"id": k, "trigger": p}})[1]
TMP.write_text(json.dumps([{"trade_id": "X", "ticker": "SOLUSDT", "status": "OPEN", "market_type": "CRYPTO",
                            "entry": 100.0, "stop_loss": 99.0, "take_profit": 102.0, "shares": 4.96, "bot_qty": 4.96,
                            "binance_order_id": "1", "sl_order": {"id": "s"}, "tp_order": {"id": "t"},
                            "strategy": STRATEGY_SFP, "decided_at": today.isoformat()}]))
tr = paper_trading.get_open_positions()[0]
ev = position_manager.manage_open_trade_sync(tr, 101.1)
t = paper_trading.get_open_positions()[0]
check("Burza: prodána polovina reduceOnly", ("close", 2.48) in calls, calls)
check("Burza: nový SL na zbytek na nule", ("SL", 2.48, 100.1) in calls, calls)
check("Burza: TP znovu na zbytek", ("TP", 2.48, 102.0) in calls, calls)
check("Burza: evidence má zbytek 2.48 ks", abs(t["bot_qty"] - 2.48) < 1e-9, t.get("bot_qty"))

calls.clear()
binance_client.place_protective_order_sync = lambda s, k, q, p: {"success": False, "error": "x"}
TMP.write_text(json.dumps([{**tr, "partial_taken": False, "bot_qty": 4.96, "shares": 4.96, "stop_loss": 99.0}]))
ev = position_manager.manage_open_trade_sync(paper_trading.get_open_positions()[0], 101.1)
check("Burza: když nový SL selže, zbytek se hned zavře", ev["type"] == "partial_failed" and ("close", 2.48) in calls and len([c for c in calls if c[0] == "close"]) == 2, calls)

print()
print("VŠE OK" if not FAILS else f"SELHALO {len(FAILS)}: {FAILS}")
sys.exit(1 if FAILS else 0)
