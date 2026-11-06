import asyncio
import market_hours
import binance_client
import scout
import trading212

async def test():
    print("=== MARKET HOURS ===")
    st = market_hours.get_market_status_dict()
    print("Prague Time:", st["prague_time"])
    print("EU Open:", st["eu_open"], "| US Open:", st["us_open"], "| Crypto Open:", st["crypto_open"])
    active = market_hours.get_active_markets_and_watchlists()
    print("Active watchlists:", [a[0] for a in active])

    print("\n=== BINANCE FUTURES ===")
    b_acc = await binance_client.get_account_info()
    print("Binance connected:", b_acc is not None)
    if b_acc:
        print("Total Wallet:", b_acc.get("totalWalletBalance"), "USDT | Available:", b_acc.get("availableBalance"), "USDT")
    
    print("\n=== PŘIBYL SFP PATTERN (BTCUSDT) ===")
    sfp = await binance_client.detect_sfp_pattern("BTCUSDT")
    print("Pattern detected:", sfp.get("pattern_detected"))
    print("4H Swing Low:", sfp.get("swing_low_4h"))
    print("Entry:", sfp.get("entry"), "| SL:", sfp.get("stop_loss"), "| TP1:", sfp.get("take_profit_1"), "| RRR:", sfp.get("rrr"))

    print("\n=== TRADING 212 ===")
    t_acc = await trading212.get_account_summary()
    print("Trading 212 connected:", t_acc is not None)
    if t_acc:
        print("Total value:", t_acc.get("totalValue"), t_acc.get("currency"))

    print("\n=== SCOUT INSPECT ASSETS ===")
    btc_data = await scout.inspect_ticker("BTCUSDT")
    print("BTCUSDT price:", btc_data["price"] if btc_data else "None")
    
    sap_data = await scout.inspect_ticker("SAP.DE")
    print("SAP.DE price:", sap_data["price"] if sap_data else "None")

    print("\nAll integration tests succeeded!")

if __name__ == "__main__":
    asyncio.run(test())
