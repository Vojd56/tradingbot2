"""Jednorázová kontrola: odpovídá to, co vidí Scout, realitě? POUZE ČTENÍ – nezadává žádné příkazy.

Spuštění ve složce bota:  python kontrola_reality.py
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests
import yfinance as yf

import scout
import binance_client
import trading212
from config import US_STOCKS_WATCHLIST, CRYPTO_WATCHLIST

print(f"Čas kontroly (UTC): {datetime.now(timezone.utc):%Y-%m-%d %H:%M}")
print("\n=== KRYPTO: co vidí bot vs. skutečný pohyb za posledních 60 minut ===")
for s in CRYPTO_WATCHLIST:
    try:
        bot = scout.fetch_crypto_data_sync(s) or {}
        m = requests.get("https://fapi.binance.com/fapi/v1/klines",
                         params={"symbol": s, "interval": "1m", "limit": 61}, timeout=10).json()
        real_move = (float(m[-1][4]) / float(m[0][4]) - 1) * 100
        vol_60 = sum(float(x[5]) for x in m[1:])
        h = requests.get("https://fapi.binance.com/fapi/v1/klines",
                         params={"symbol": s, "interval": "1h", "limit": 21}, timeout=10).json()
        avg_h = sum(float(x[5]) for x in h[:-1]) / len(h[:-1])
        candle_min = (datetime.now(timezone.utc).timestamp() * 1000 - h[-1][0]) / 60000
        sfp = (bot.get("sfp_data") or {}).get("pattern_detected")
        print(f"{s:9} cena {bot.get('price')} | bot 1h {bot.get('price_move_pct'):+.2f}% / objem {bot.get('volume_ratio')}x"
              f" | realita 60 min {real_move:+.2f}% / objem {vol_60 / avg_h:.2f}x | SFP {sfp} | 1h svíčka běží {candle_min:.0f} min")
    except Exception as e:
        print(f"{s:9} chyba: {e}")

print("\n=== US AKCIE: co vidí bot vs. realita (1min data Yahoo) ===")
for t in US_STOCKS_WATCHLIST:
    try:
        bot = scout.fetch_stock_data_sync(t) or {}
        m = yf.Ticker(t).history(period="1d", interval="1m")
        real_move = (float(m["Close"].iloc[-1]) / float(m["Close"].iloc[-61]) - 1) * 100 if len(m) > 61 else float("nan")
        d = yf.Ticker(t).history(period="1mo", interval="1d")
        avg20 = float(d["Volume"].iloc[-21:-1].mean())
        today = float(d["Volume"].iloc[-1])
        # podíl uplynulé seance (390 minut)
        elapsed = min(max(len(m), 1), 390) / 390
        print(f"{t:6} cena {bot.get('price')} | bot 1h {bot.get('price_move_pct'):+.2f}% / objem {bot.get('volume_ratio')}x"
              f" | realita 60 min {real_move:+.2f}% | dnešní objem {today / avg20:.2f}x průměru, přepočteno na uplynulý čas {today / avg20 / elapsed:.2f}x"
              f" | poslední 1min data {m.index[-1]:%H:%M %Z}")
    except Exception as e:
        print(f"{t:6} chyba: {e}")

print("\n=== BINANCE TESTNET: pozice a čekající ochranné příkazy ===")
print("Pozice:", binance_client.get_all_position_amts_sync())
ok, algo = binance_client._signed_request("GET", "/fapi/v1/openAlgoOrders", {})
if ok:
    for o in algo if isinstance(algo, list) else algo.get("orders", algo):
        print(f"  algo {o.get('symbol')} {o.get('orderType')} {o.get('side')} qty {o.get('quantity')} trigger {o.get('triggerPrice')} stav {o.get('algoStatus')}")
else:
    print("  openAlgoOrders:", algo)
ok, orders = binance_client._signed_request("GET", "/fapi/v1/openOrders", {})
if ok:
    for o in orders:
        print(f"  order {o.get('symbol')} {o.get('type')} {o.get('side')} qty {o.get('origQty')} stop {o.get('stopPrice')}")
else:
    print("  openOrders:", orders)

print("\n=== TRADING 212 DEMO ===")
pf = trading212.get_portfolio_or_none_sync()
print("Portfolio:", [(p.get("ticker"), p.get("quantity")) for p in (pf or [])])
po = trading212.get_pending_orders_sync()
print("Čekající příkazy:", [(o.get("ticker"), o.get("type"), o.get("quantity"), o.get("stopPrice")) for o in (po or [])])
