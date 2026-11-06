"""End-to-End Test pro 6členný Trading Tým a Trading 212 integraci.

1. Ověří spojení s Trading 212 API (zůstatek, portfolio).
2. Spustí Scouta na ticker NVDA (yfinance data, 1h pohyb, volume, zprávy).
3. Spustí Gemini analýzu (News, Chart, Skeptik, Sizer).
4. Odešle reálnou interaktivní Embed kartu s tlačítky do Discord kanálu #trading-bot!
"""

import sys
import asyncio
from datetime import datetime

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import discord
from config import DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID
from scout import inspect_ticker
from gemini_team import analyze_with_gemini
from views import build_trading_embed, TradingApprovalView
import trading212


async def run_test():
    print("=" * 60)
    print("🧪 SPUŠTĚNÍ END-TO-END TESTU TRADING BOTA")
    print("=" * 60)

    # 1. Test Trading 212
    print("\n[1/4] Testování Trading 212 API...")
    if trading212.is_configured():
        summary = await trading212.get_account_summary()
        if summary:
            curr = summary.get("currency")
            cash = summary.get("cash", {}).get("availableToTrade")
            total = summary.get("totalValue")
            print(f"✅ Trading 212 OK: Celkem {total} {curr}, Volná hotovost {cash} {curr}")
        else:
            print("⚠️ Nepodařilo se načíst summary z Trading 212.")
        
        portfolio = await trading212.get_portfolio()
        print(f"✅ Trading 212 Portfolio: {len(portfolio)} otevřených pozic")
    else:
        print("❌ Trading 212 není nakonfigurováno.")

    # 2. Test Scouta
    print("\n[2/4] Testování Scouta pro NVDA...")
    data = await inspect_ticker("NVDA")
    if not data:
        print("❌ Scout selhal: nepodařilo se stáhnout data pro NVDA.")
        return
    print(f"✅ Scout data získána:")
    print(f"   • Cena: ${data['price']:.2f}")
    print(f"   • 1h pohyb: {data['price_move_pct']:+.2f}%")
    print(f"   • Volume ratio: {data['volume_ratio']:.2f}x")
    print(f"   • Počet zpráv: {len(data['headlines'])}")

    # 3. Test Gemini týmu (News, Chart, Skeptik, Sizer)
    print("\n[3/4] Testování Gemini Analytického Týmu...")
    analysis = await analyze_with_gemini(
        ticker="NVDA",
        price=data["price"],
        price_move_pct=data["price_move_pct"],
        volume_ratio=data["volume_ratio"],
        headlines=data["headlines"]
    )
    print("✅ Analýza týmu dokončena:")
    print(f"   • News: {analysis['news']}")
    print(f"   • Chart: Entry=${analysis['chart']['entry']:.2f}, SL=${analysis['chart']['stop_loss']:.2f}, TP=${analysis['chart']['take_profit']:.2f}, RRR={analysis['chart']['rrr']}")
    print(f"   • Skeptik Verdikt: {analysis['skeptik']['verdict']}")
    print(f"   • Skeptik Důvody: {len(analysis['skeptik']['reasons'])} protiargumenty")
    print(f"   • Sizer: Alokace ${analysis['sizer']['allocated_usd']:.2f} ({analysis['sizer']['shares']} ks)")

    # 4. Odeslání do Discordu
    print(f"\n[4/4] Odesílání interaktivní karty do Discordu (#{DISCORD_CHANNEL_ID})...")
    client = discord.Client(intents=discord.Intents.default())

    @client.event
    async def on_ready():
        print(f"✅ Přihlášen jako {client.user}")
        channel = client.get_channel(DISCORD_CHANNEL_ID)
        if not channel:
            print(f"❌ Kanál s ID {DISCORD_CHANNEL_ID} nebyl nalezen.")
            await client.close()
            return

        embed = build_trading_embed(analysis, status="PENDING")
        view = TradingApprovalView(analysis=analysis)

        msg = await channel.send(
            content="🧪 **TESTOVACÍ SIGNÁL 6ČLENNÉHO TÝMU S TRADING 212 INTEGRACÍ**\n*Níže můžete kliknout na [ ✅ Schválit nákup ] pro test reálné exekuce na Trading 212 (Demo), nebo [ ❌ Zamítnout ] pro storno.*",
            embed=embed,
            view=view
        )
        print(f"🎉 Úspěšně odesláno do Discordu! Message ID: {msg.id}")
        await client.close()

    await client.start(DISCORD_BOT_TOKEN)
    print("\n🏁 Testovací skript dokončen!")


if __name__ == "__main__":
    asyncio.run(run_test())
