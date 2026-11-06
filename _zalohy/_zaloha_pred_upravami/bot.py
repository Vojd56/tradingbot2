"""Hlavní Discord Bot pro simulaci 6členného trading týmu v AUTONOMNÍM MÓDU.

Role týmu:
1. SCOUT: Sleduje trhy (US akcie, Evropské akcie, Krypto 24/7).
          Obsahuje OCHRANU PROTI PLÝTVÁNÍ KREDITY: akcie skenuje pouze v obchodních hodinách (EU 09:00-17:30, US 15:30-22:00 CET).
          Automaticky monitoruje otevřené pozice a při zasažení Stop-Lossu nebo Take-Profitu pozici uzavře na burze!
2. NEWS: Vysvětlí důvod pohybu ve 2 větách (nebo "Žádný jasný důvod").
3. CHART: Spočítá Entry, Stop-Loss (max 2% pod entry) a Take-Profit (min RRR 1:2).
          U krypta navíc integruje SFP (Swing Failure Pattern) dle výzkumu Jiřího Přibyla.
4. SKEPTIK: 3 důvody proti. Pokud News nenašel důvod, verdikt MUSÍ BÝT "PRESKOČIŤ". Jinak "KÚPIŤ" / "POČKAŤ".
5. SIZER: Alokuje max 5 % z 10 000 USD kapitálu (max 3 otevřené pozice), zohledňuje velikost rizika (1-2 % kapitálu dle Přibyla).
6. AUTONOMNÍ EXECUTOR: Prodiskutuje s LLM data, rozhodne o vstupu a SAMOSTATNĚ PROVEDE NÁKUP na Trading 212 / Binance Futures Testnet i v evidenci!
"""

import os
import sys
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Union

# Zajištění UTF-8 kódování na Windows konzoli pro emotikony a české znaky
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import (
    DISCORD_BOT_TOKEN,
    DISCORD_CHANNEL_ID,
    DEFAULT_WATCHLIST,
    US_STOCKS_WATCHLIST,
    EU_STOCKS_WATCHLIST,
    CRYPTO_WATCHLIST,
    SCOUT_INTERVAL_MINUTES,
    MAX_DAILY_ALERTS_TOTAL,
    MAX_DAILY_ALERTS_PER_TICKER,
    GEMINI_MODEL,
    ENABLE_TRADING_212,
    TRADING_212_ENV,
    ENABLE_BINANCE,
    BINANCE_FUTURES_URL,
    PRAGUE_TIMEZONE,
    MAX_OPEN_POSITIONS,
    MAX_OPEN_POSITIONS_STOCKS,
    MAX_OPEN_POSITIONS_CRYPTO
)
from scout import inspect_ticker, alert_tracker, scan_active_markets
from gemini_team import analyze_with_gemini
from paper_trading import (
    load_trades,
    get_open_positions,
    get_open_positions_count,
    get_open_positions_by_market,
    get_positions_limit_info,
    close_position,
    record_new_trade,
    can_open_new_position,
    can_open_new_position_for
)
from views import build_autonomous_embed, AutonomousPositionView
import trading212
import binance_client
import market_hours
from model_manager import quota_manager

# Nastavení přehledného logování
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("TradingBot")

# Standardní intenty (nevyžadují Privileged Intents)
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
target_channel_id = DISCORD_CHANNEL_ID


def is_crypto_symbol(symbol: str) -> bool:
    """Rozpozná, zda jde o kryptoměnu."""
    s = symbol.upper().strip()
    return s.endswith("USDT") or s in ["BTC", "ETH", "SOL", "BNB"] or s in CRYPTO_WATCHLIST


def get_crypto_order_qty(symbol: str, allocated_usd: float, price: float) -> Union[int, float]:
    """Vypočte a zaokrouhlí množství kryptoměny dle přesnosti Binance."""
    if price <= 0:
        return 1 if symbol.upper() in ["AVAXUSDT", "NEARUSDT", "DOGEUSDT"] else 0.001
    raw_qty = allocated_usd / price
    return binance_client.format_quantity(symbol, raw_qty)


def get_target_channel() -> Optional[discord.TextChannel]:
    """Vyhledá cílový textový kanál pro odesílání alertů."""
    global target_channel_id
    if target_channel_id:
        channel = bot.get_channel(target_channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel

    for guild in bot.guilds:
        for ch in guild.text_channels:
            if ch.name.lower() in ["trading-bot", "trading", "bot"]:
                target_channel_id = ch.id
                return ch
        for ch in guild.text_channels:
            if ch.permissions_for(guild.me).send_messages:
                target_channel_id = ch.id
                return ch
    return None


async def execute_autonomous_trade(analysis: dict, channel: discord.TextChannel) -> None:
    """Provede autonomní exekuci nákupu schváleného LLM týmem (Akcie na T212, Krypto na Binance)."""
    ticker = analysis["ticker"]
    chart = analysis.get("chart", {})
    sizer = analysis.get("sizer", {})
    executor = analysis.get("executor", {})
    news = analysis.get("news", "")
    skeptik = analysis.get("skeptik", {})

    allocated_usd = sizer.get("allocated_usd", 500.0)
    entry = chart.get("entry", analysis["price"])
    is_crypto = is_crypto_symbol(ticker)

    logger.info(f"🚀 AUTONOMNÍ EXEKUCE: Zahajuji nákup {ticker} (Alokace: ${allocated_usd:.2f})...")

    t212_res = None
    t212_order_id = None
    t212_status = None
    binance_res = None
    binance_order_id = None
    binance_status = None

    if is_crypto:
        # Krypto: Exekuce na Binance Futures Testnet
        crypto_qty = get_crypto_order_qty(ticker, allocated_usd, entry)
        shares_val = crypto_qty
        if ENABLE_BINANCE:
            binance_res = await binance_client.place_futures_order(symbol=ticker, side="BUY", quantity=crypto_qty)
            if binance_res and binance_res.get("success"):
                binance_order_id = str(binance_res.get("order_id", ""))
                binance_status = binance_res.get("status", "NEW")
            else:
                err_text = binance_res.get("error") if binance_res else "Neznámá chyba Binance"
                logger.error(f"❌ Nákup {ticker} selhal na Binance Futures: {err_text}")
                embed = build_autonomous_embed(analysis, executed=False, binance_result=binance_res)
                await channel.send(
                    content=f"❌ **EXEKUCE SELHALA NA BINANCE FUTURES: {ticker}** (Chyba: `{err_text}`)",
                    embed=embed
                )
                return
    else:
        # Akcie: Exekuce na Trading 212 Demo
        stock_shares = sizer.get("shares", 1)
        shares_val = stock_shares
        if ENABLE_TRADING_212:
            order_qty = float(stock_shares) if stock_shares > 0 else 1.0
            t212_res = await trading212.place_market_order(symbol=ticker, quantity=order_qty)
            if t212_res and t212_res.get("success"):
                t212_order_id = str(t212_res.get("order_id", ""))
                t212_status = t212_res.get("status", "NEW")

    # Zápis do lokálního paper_trades.json
    record_new_trade(
        ticker=ticker,
        action="AUTO_BUY",
        status="OPEN",
        entry=entry,
        stop_loss=chart.get("stop_loss", 0.0),
        take_profit=chart.get("take_profit", 0.0),
        rrr=chart.get("rrr", "1:2.0"),
        allocated_usd=allocated_usd,
        shares=shares_val,
        news_summary=news,
        skeptik_reasons=skeptik.get("reasons", []),
        user_name="Autonomní AI Executor",
        user_id=bot.user.id if bot.user else 0,
        t212_order_id=t212_order_id,
        t212_status=t212_status,
        binance_order_id=binance_order_id,
        binance_status=binance_status,
        market_type="CRYPTO" if is_crypto else "STOCK"
    )

    # Odeslání vizuální notifikace do Discordu
    embed = build_autonomous_embed(
        analysis,
        executed=True,
        t212_result=t212_res,
        binance_result=binance_res
    )
    view = AutonomousPositionView(ticker=ticker)
    asset_label = "KRYPTO (Binance Futures)" if is_crypto else "AKCIE (Trading 212)"
    await channel.send(
        content=f"🤖 **AUTONOMNÍ NÁKUP PROVEDEN: {ticker} [{asset_label}]** (AI Důvěra: {executor.get('confidence', 80)}%)",
        embed=embed,
        view=view
    )


# =====================================================================
# 1. SCOUT: PERIODICKÁ SMYČKA KAŽDÝCH 5 MINUT PŘES discord.ext.tasks
# =====================================================================

@tasks.loop(minutes=SCOUT_INTERVAL_MINUTES)
async def scout_periodic_loop():
    """Periodická smyčka Scouta:
    1. Monitoruje otevřené pozice a automaticky uzavírá při Stop-Lossu nebo Take-Profitu (SL max 2%, TP min RRR 1:2)!
    2. Ochrana proti plýtvání kredity: Skenuje pouze trhy, které jsou v aktuální hodinu v Praze otevřeny!
       - Víkendy a noci: Skenuje POUZE kryptoměny 24/7.
       - Všední dny 09:00-17:30 CET: Skenuje Evropské akcie + Krypto.
       - Všední dny 15:30-22:00 CET: Skenuje US akcie + Krypto.
    3. Při splnění podmínek (breakout nebo SFP setup dle Přibyla) spustí analýzu Gemini a autonomně nakoupí.
    """
    channel = get_target_channel()
    if not channel:
        return

    m_status = market_hours.get_market_status_dict()
    logger.info(
        f"🔭 SCOUT: Kontrola trhu | Praha: {m_status['prague_time']} | "
        f"EU akcie: {'🟢' if m_status['eu_open'] else '🔴'} | "
        f"US akcie: {'🟢' if m_status['us_open'] else '🔴'} | "
        f"Krypto: 🟢 24/7"
    )

    # A) KONTROLA A AUTOMATICKÉ UZAVÍRÁNÍ OTEVŘENÝCH POZIC (SL / TP)
    open_positions = get_open_positions()
    for pos in open_positions:
        ticker = pos["ticker"]
        try:
            p_data = await inspect_ticker(ticker)
            if not p_data:
                continue

            current_price = p_data["price"]
            sl = float(pos.get("stop_loss", 0.0))
            tp = float(pos.get("take_profit", 999999.0))
            entry = float(pos.get("entry", current_price))

            hit_sl = current_price <= sl
            hit_tp = current_price >= tp

            if hit_sl or hit_tp:
                reason = "STOP-LOSS ZASAŽEN" if hit_sl else "TAKE-PROFIT DOSAŽEN"
                emoji = "🔴" if hit_sl else "🟢"
                logger.info(f"{emoji} {reason} u {ticker}: Cena ${current_price:.2f} (Entry: ${entry:.2f}, SL: ${sl:.2f}, TP: ${tp:.2f})")

                # Uzavření v lokální evidenci
                closed_trade = close_position(ticker=ticker, exit_price=current_price, user_name=f"TradingBot ({reason})")

                # Prodej na příslušné burze
                t212_res = None
                binance_res = None
                is_crypto = is_crypto_symbol(ticker)

                if is_crypto and ENABLE_BINANCE:
                    binance_res = await binance_client.close_futures_position(ticker)
                elif not is_crypto and ENABLE_TRADING_212:
                    t212_res = await trading212.close_position_t212(ticker)

                # Notifikace do Discordu
                pnl_usd = closed_trade.get("pnl_usd", 0.0) if closed_trade else 0.0
                pnl_pct = closed_trade.get("pnl_pct", 0.0) if closed_trade else 0.0

                close_embed = discord.Embed(
                    title=f"{emoji} AUTONOMNÍ VÝSTUP: {ticker} ({reason})",
                    description=f"Pozice byla automaticky uzavřena na základě stanovené strategie (Risk Management).",
                    color=discord.Color.green() if pnl_usd >= 0 else discord.Color.red(),
                    timestamp=datetime.now()
                )
                close_embed.add_field(name="Nákupní cena", value=f"${entry:.2f}", inline=True)
                close_embed.add_field(name="Výstupní cena", value=f"${current_price:.2f}", inline=True)
                close_embed.add_field(name="Výsledek obchodu", value=f"**{pnl_usd:+.2f} USD ({pnl_pct:+.2f}%)**", inline=True)

                if t212_res and t212_res.get("success"):
                    close_embed.add_field(name="Trading 212 Exekuce", value=f"✅ Pozice prodána na burze (Order ID: `#{t212_res.get('order_id')}`)", inline=False)
                elif binance_res and binance_res.get("success"):
                    close_embed.add_field(name="Binance Futures Exekuce", value=f"✅ Pozice uzavřena na testnetu (Order ID: `#{binance_res.get('order_id')}`)", inline=False)

                stock_cnt = len(get_open_positions_by_market("STOCK"))
                crypto_cnt = len(get_open_positions_by_market("CRYPTO"))
                close_embed.set_footer(text=f"Slot uvolněn. Otevřené pozice: Trading 212: {stock_cnt}/{MAX_OPEN_POSITIONS_STOCKS} | Binance: {crypto_cnt}/{MAX_OPEN_POSITIONS_CRYPTO} (Celkem: {stock_cnt + crypto_cnt}/{MAX_OPEN_POSITIONS})")
                await channel.send(embed=close_embed)

        except Exception as pe:
            logger.error(f"Chyba při kontrole otevřené pozice {ticker}: {pe}")

    # B) KONTROLA AKTIVNÍCH TRHŮ (OCHRANA PROTI PLÝTVÁNÍ KREDITY)
    today_status = alert_tracker.get_status()
    if today_status["total_alerts"] >= MAX_DAILY_ALERTS_TOTAL:
        logger.info(f"Dosažen denní limit alertů ({MAX_DAILY_ALERTS_TOTAL}/{MAX_DAILY_ALERTS_TOTAL}).")
        return

    # Pokud již máme otevřeno maximum pozic (10 celkem), nový obchod neotevíráme
    if not can_open_new_position():
        logger.info(f"Globální limit {MAX_OPEN_POSITIONS} otevřených pozic je naplněn. Žádné nové pozice nebudou otevírány.")
        return

    # Získání aktivních trhů
    active_market_lists = market_hours.get_active_markets_and_watchlists()
    stocks_closed = not m_status["eu_open"] and not m_status["us_open"]
    if stocks_closed:
        logger.info("🛡️ OCHRANA KREDITŮ: Akciové burzy jsou zavřené. Skenuji POUZE kryptoměny 24/7 a šetřím Gemini API kredity.")

    for market_name, m_type, tickers in active_market_lists:
        for ticker in tickers:
            # Kontrola kapacity pro danou platformu (5 pro T212, 5 pro Binance)
            if not can_open_new_position_for(ticker):
                continue

            if not alert_tracker.can_alert(ticker):
                continue

            try:
                data = await inspect_ticker(ticker)
                if not data or not data["triggered"]:
                    continue

                logger.info(f"🔥 SCOUT SIGNAL pro {ticker} ({market_name}): {data['trigger_reasons']}")
                alert_tracker.register_alert(ticker)

                # Analýza LLM týmem (Gemini 2.5 Flash / 3.8 Flash)
                analysis = await analyze_with_gemini(
                    ticker=ticker,
                    price=data["price"],
                    price_move_pct=data["price_move_pct"],
                    volume_ratio=data["volume_ratio"],
                    headlines=data["headlines"]
                )

                # Pokud byl zjištěn SFP pattern, připojíme metadata do analýzy
                if data.get("sfp_detected"):
                    analysis["sfp_detected"] = True
                    analysis["sfp_data"] = data.get("sfp_data", {})

                executor = analysis.get("executor", {})
                should_execute = executor.get("should_execute", False)
                verdict = analysis.get("skeptik", {}).get("verdict", "PRESKOČIŤ")

                if should_execute and verdict == "KÚPIŤ" and can_open_new_position_for(ticker):
                    await execute_autonomous_trade(analysis, channel)
                else:
                    logger.info(f"🛑 {ticker}: Autonomní nákup neproveden (Verdikt: {verdict}, ShouldExecute: {should_execute})")
                    embed = build_autonomous_embed(analysis, executed=False)
                    await channel.send(embed=embed)

                if alert_tracker.get_status()["total_alerts"] >= MAX_DAILY_ALERTS_TOTAL:
                    break

            except Exception as e:
                logger.error(f"Chyba při zpracování {ticker} ve Scout smyčce: {e}", exc_info=True)


@scout_periodic_loop.before_loop
async def before_scout_loop():
    await bot.wait_until_ready()
    logger.info("Bot je připraven, spouštím smyčku Scouta...")


# =====================================================================
# SLASH COMMANDS (MODERNÍ DISCORD APP COMMANDS)
# =====================================================================

@bot.tree.command(name="scout", description="Provede analýzu akcie nebo kryptoměny 6členným týmem a autonomně nakoupí.")
@app_commands.describe(ticker="Symbol k analýze (např. NVDA, AAPL, SAP.DE, BTCUSDT, ETHUSDT)")
async def slash_scout(interaction: discord.Interaction, ticker: str = "NVDA"):
    ticker = ticker.upper().strip()
    await interaction.response.defer(thinking=True)

    data = await inspect_ticker(ticker)
    if not data:
        await interaction.followup.send(f"❌ Nepodařilo se získat tržní data pro symbol `{ticker}`.")
        return

    try:
        analysis = await analyze_with_gemini(
            ticker=ticker,
            price=data["price"],
            price_move_pct=data["price_move_pct"],
            volume_ratio=data["volume_ratio"],
            headlines=data["headlines"]
        )

        if data.get("sfp_detected"):
            analysis["sfp_detected"] = True
            analysis["sfp_data"] = data.get("sfp_data", {})

        executor = analysis.get("executor", {})
        should_execute = executor.get("should_execute", False)
        verdict = analysis.get("skeptik", {}).get("verdict", "PRESKOČIŤ")

        channel = interaction.channel or get_target_channel()
        is_crypto = is_crypto_symbol(ticker)

        if should_execute and verdict == "KÚPIŤ":
            if not can_open_new_position_for(ticker):
                lim_info = get_positions_limit_info(ticker)
                embed = build_autonomous_embed(analysis, executed=False)
                if lim_info.get("already_open"):
                    msg = f"⚠️ **Pozice pro {ticker} je již otevřena:** Tento symbol již máte v aktivním portfoliu. Obchod nebyl duplikován."
                else:
                    msg = f"⚠️ **Limit pozic naplněn ({lim_info['platform']}):** Již máte otevřeno {lim_info['count']}/{lim_info['limit']} pozic. Obchod nebyl zadán."
                await interaction.followup.send(
                    content=msg,
                    embed=embed
                )
                return

            allocated_usd = analysis.get("sizer", {}).get("allocated_usd", 500.0)
            entry = analysis.get("chart", {}).get("entry", analysis["price"])

            t212_res = None
            t212_order_id = None
            t212_status = None
            binance_res = None
            binance_order_id = None
            binance_status = None

            if is_crypto:
                crypto_qty = get_crypto_order_qty(ticker, allocated_usd, entry)
                shares_val = crypto_qty
                if ENABLE_BINANCE:
                    binance_res = await binance_client.place_futures_order(symbol=ticker, side="BUY", quantity=crypto_qty)
                    if binance_res and binance_res.get("success"):
                        binance_order_id = str(binance_res.get("order_id", ""))
                        binance_status = binance_res.get("status", "NEW")
                    else:
                        err_text = binance_res.get("error") if binance_res else "Neznámá chyba Binance"
                        logger.error(f"❌ /scout nákup {ticker} selhal na Binance Futures: {err_text}")
                        embed = build_autonomous_embed(analysis, executed=False, binance_result=binance_res)
                        await interaction.followup.send(
                            content=f"❌ **EXEKUCE SELHALA NA BINANCE FUTURES: {ticker}** (Chyba burzy: `{err_text}`)",
                            embed=embed
                        )
                        return
            else:
                stock_shares = analysis.get("sizer", {}).get("shares", 1)
                shares_val = stock_shares
                if ENABLE_TRADING_212:
                    order_qty = float(stock_shares) if stock_shares > 0 else 1.0
                    t212_res = await trading212.place_market_order(symbol=ticker, quantity=order_qty)
                    if t212_res and t212_res.get("success"):
                        t212_order_id = str(t212_res.get("order_id", ""))
                        t212_status = t212_res.get("status", "NEW")

            record_new_trade(
                ticker=ticker,
                action="AUTO_BUY",
                status="OPEN",
                entry=entry,
                stop_loss=analysis.get("chart", {}).get("stop_loss", 0.0),
                take_profit=analysis.get("chart", {}).get("take_profit", 0.0),
                rrr=analysis.get("chart", {}).get("rrr", "1:2.0"),
                allocated_usd=allocated_usd,
                shares=shares_val,
                news_summary=analysis.get("news", ""),
                skeptik_reasons=analysis.get("skeptik", {}).get("reasons", []),
                user_name=f"Autonomní Executor (přes /scout od {interaction.user.display_name})",
                user_id=interaction.user.id,
                t212_order_id=t212_order_id,
                t212_status=t212_status,
                binance_order_id=binance_order_id,
                binance_status=binance_status,
                market_type="CRYPTO" if is_crypto else "STOCK"
            )

            embed = build_autonomous_embed(
                analysis,
                executed=True,
                t212_result=t212_res,
                binance_result=binance_res
            )
            view = AutonomousPositionView(ticker=ticker)
            asset_label = "KRYPTO (Binance)" if is_crypto else "AKCIE (Trading 212)"
            await interaction.followup.send(
                content=f"🤖 **AUTONOMNÍ NÁKUP PROVEDEN: {ticker} [{asset_label}]** (AI Důvěra: {executor.get('confidence', 80)}%)",
                embed=embed,
                view=view
            )
        else:
            embed = build_autonomous_embed(analysis, executed=False)
            await interaction.followup.send(
                content=f"🛑 **Výsledek týmu: {ticker} | Verdikt Skeptika: {verdict}** (Obchod nebyl otevřen)",
                embed=embed
            )

    except Exception as e:
        logger.error(f"Chyba při /scout {ticker}: {e}", exc_info=True)
        await interaction.followup.send(f"❌ Došlo k chybě při analýze přes Gemini API: `{e}`")


@bot.tree.command(name="hours", description="Zobrazí stav obchodních hodin burz (EU, US, Krypto) a ochranu kreditů.")
async def slash_hours(interaction: discord.Interaction):
    """Zobrazí aktuální stav burz a ochranu kreditů."""
    embed = market_hours.build_market_hours_embed()
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="binance", description="Zobrazí stav účtu a otevřené pozice na Binance Futures Testnet.")
async def slash_binance(interaction: discord.Interaction):
    """Zobrazí zůstatek a pozice na Binance Futures Testnet."""
    if not ENABLE_BINANCE:
        await interaction.response.send_message("❌ Binance API klíče nejsou nakonfigurovány v prostředí.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    acc = await binance_client.get_account_info()
    if not acc:
        await interaction.followup.send("❌ Nepodařilo se připojit k Binance Futures Testnet API.")
        return

    total_wallet = float(acc.get("totalWalletBalance", 0.0))
    available = float(acc.get("availableBalance", 0.0))
    unrealized_pnl = float(acc.get("totalUnrealizedProfit", 0.0))
    positions = acc.get("positions", [])
    active_pos = [p for p in positions if float(p.get("positionAmt", 0)) != 0]

    embed = discord.Embed(
        title="🟡 Stav Účtu Binance Futures (Testnet)",
        color=discord.Color.gold(),
        timestamp=datetime.now()
    )
    embed.add_field(name="Celkový zůstatek", value=f"**{total_wallet:.2f} USDT**", inline=True)
    embed.add_field(name="Volná marže", value=f"**{available:.2f} USDT**", inline=True)
    embed.add_field(name="Nerealizovaný PnL", value=f"**{unrealized_pnl:+.2f} USDT**", inline=True)

    if active_pos:
        pos_lines = []
        for p in active_pos:
            sym = p.get("symbol")
            amt = float(p.get("positionAmt", 0))
            entry_p = float(p.get("entryPrice", 0))
            pnl = float(p.get("unrealizedProfit", 0))
            side = "LONG 🟢" if amt > 0 else "SHORT 🔴"
            pos_lines.append(f"• **{sym}** ({side}): `{amt}` ks @ `${entry_p:.2f}` | PnL: `{pnl:+.2f} USDT`")
        embed.add_field(name=f"📦 Otevřené Futures Pozice ({len(active_pos)})", value="\n".join(pos_lines), inline=False)
    else:
        embed.add_field(name="📦 Otevřené Futures Pozice", value="Žádné otevřené pozice na Binance Futures.", inline=False)

    embed.set_footer(text=f"Binance Futures Testnet • {BINANCE_FUTURES_URL}")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="crypto", description="Analyzuje krypto watchlist pomocí strategie SFP Jiřího Přibyla (Swing Failure).")
async def slash_crypto(interaction: discord.Interaction):
    """Zkontroluje krypto watchlist z hlediska SFP a breakoutů dle metodiky Jiřího Přibyla."""
    await interaction.response.defer(thinking=True)

    embed = discord.Embed(
        title="⚡ Krypto Scanner & SFP Analýza (15M dno)",
        description=(
            "Aplikace intradenní strategie **Swing Failure Pattern (SFP)** na Binance Futures.\n"
            "Sleduje průrazy **15minutového dna (15M Swing Low)** knotem s absorpcí objemu a návratem zpět nad dno."
        ),
        color=discord.Color.purple(),
        timestamp=datetime.now()
    )

    found_sfp = 0
    for sym in CRYPTO_WATCHLIST:
        data = await inspect_ticker(sym)
        if not data:
            continue

        sfp_data = data.get("sfp_data", {})
        is_sfp = data.get("sfp_detected", False)
        price = data["price"]
        move = data["price_move_pct"]
        vol_ratio = data["volume_ratio"]
        sw_low = sfp_data.get("swing_low_15m", sfp_data.get("swing_low_4h", 0.0))

        if is_sfp:
            found_sfp += 1
            status_text = (
                f"🎯 **15M SFP FORMACE POTVRZENA!**\n"
                f"• Knot prorazil pod 15M dno (${sw_low:.4f}) a svíčka uzavřela zpět výše!\n"
                f"• Entry: `${sfp_data.get('entry'):.4f}` | SL: `${sfp_data.get('stop_loss'):.4f}` | TP1: `${sfp_data.get('take_profit_1'):.4f}` (RRR 1:2)\n"
                f"• Objem: `{vol_ratio:.2f}x` 15M průměru (absorpce)"
            )
        else:
            status_text = (
                f"• Aktuální cena: `${price:.4f}` (1h: `{move:+.2f}%`)\n"
                f"• 15M dno (Swing Low): `${sw_low:.4f}` (Cena je {'nad dnem' if price >= sw_low else 'pod dnem'})\n"
                f"• SFP stav: *Čeká se na sweep likvidity pod 15min dno*"
            )

        embed.add_field(
            name=f"{'🟢' if is_sfp else '⚪'} {sym}",
            value=status_text,
            inline=False
        )

    embed.set_footer(text=f"Metodika: Intradenní SFP na 15M dnu | Nalezeno aktivních 15M SFP: {found_sfp}")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="models", description="Zobrazí stav multi-tier kaskády Gemini modelů a denní limity volání.")
async def slash_models(interaction: discord.Interaction):
    """Zobrazí stav kaskády modelů a denních limitů."""
    embed = quota_manager.get_status_embed()
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="scan", description="Proskenuje aktivní trhy (respektuje obchodní hodiny a šetří kredity).")
async def slash_scan(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    channel = interaction.channel or get_target_channel()

    active_lists = market_hours.get_active_markets_and_watchlists()
    found_count = 0
    executed_count = 0
    results_texts = []

    for market_name, m_type, tickers in active_lists:
        for ticker in tickers:
            if not can_open_new_position_for(ticker):
                continue

            data = await inspect_ticker(ticker)
            if data and data["triggered"]:
                found_count += 1
                analysis = await analyze_with_gemini(
                    ticker=ticker,
                    price=data["price"],
                    price_move_pct=data["price_move_pct"],
                    volume_ratio=data["volume_ratio"],
                    headlines=data["headlines"]
                )
                if data.get("sfp_detected"):
                    analysis["sfp_detected"] = True
                    analysis["sfp_data"] = data.get("sfp_data", {})

                executor = analysis.get("executor", {})
                verdict = analysis.get("skeptik", {}).get("verdict", "PRESKOČIŤ")
                if executor.get("should_execute") and verdict == "KÚPIŤ" and can_open_new_position_for(ticker):
                    executed_count += 1
                    if channel:
                        await execute_autonomous_trade(analysis, channel)
                    results_texts.append(f"• **{ticker}** ({market_name}): AUTONOMNĚ NAKOUPENO")
                else:
                    results_texts.append(f"• **{ticker}** ({market_name}): Signál nalezen, AI vyhodnotila jako {verdict}")

    m_status = market_hours.get_market_status_dict()
    if found_count == 0:
        await interaction.followup.send(
            f"🔭 Scout proskenoval aktivní trhy v Praze ({m_status['prague_time']}).\n"
            f"Žádné aktivní aktivum momentálně nesplňuje podmínku breakoutu ani SFP formace.\n"
            f"Pro okamžité otestování autonomního nákupu použij `/scout NVDA` nebo `/scout BTCUSDT`."
        )
    else:
        text = "\n".join(results_texts)
        await interaction.followup.send(
            f"🎯 **Scan dokončen.** Nalezeno {found_count} signálů, autonomně nakoupeno {executed_count} pozic:\n{text}"
        )


@bot.tree.command(name="positions", description="Zobrazí otevřené paper pozice, Trading 212 portfolio i Binance Futures.")
async def slash_positions(interaction: discord.Interaction):
    await interaction.response.defer(thinking=False)
    positions = get_open_positions()
    count = len(positions)
    stock_positions = get_open_positions_by_market("STOCK")
    crypto_positions = get_open_positions_by_market("CRYPTO")
    s_count = len(stock_positions)
    c_count = len(crypto_positions)

    embed = discord.Embed(
        title=f"📊 Otevřené Pozice: Trading 212 ({s_count}/{MAX_OPEN_POSITIONS_STOCKS}) | Binance ({c_count}/{MAX_OPEN_POSITIONS_CRYPTO})",
        description=f"Celková alokace: **{count}/{MAX_OPEN_POSITIONS}** pozic (5 slotů pro akcie na T212 + 5 slotů pro krypto na Binance Futures).",
        color=discord.Color.blue(),
        timestamp=datetime.now()
    )

    # 1. AKCIE (Trading 212)
    if stock_positions:
        s_lines = []
        for p in stock_positions:
            ticker = p["ticker"]
            entry = p["entry"]
            sl = p["stop_loss"]
            tp = p["take_profit"]
            allocated = p["allocated_usd"]
            shares = p["shares"]
            user = p.get("decided_by", "Neznámý")
            date_str = p.get("decided_at", "")[:19].replace("T", " ")
            t212_id = p.get("t212_order_id")
            order_info = f" | T212: `#{t212_id}`" if t212_id else ""

            s_lines.append(
                f"• **{ticker}** ({shares} ks @ `${entry:.2f}`){order_info}\n"
                f"  Alokace: `${allocated:.2f}` | SL: `${sl:.2f}` | TP: `${tp:.2f}` | Otevřel: {user} ({date_str})\n"
                f"  Uzavřít: `/close {ticker}`"
            )
        embed.add_field(
            name=f"🏛️ Akcie – Trading 212 ({s_count}/{MAX_OPEN_POSITIONS_STOCKS})",
            value="\n".join(s_lines),
            inline=False
        )
    else:
        embed.add_field(
            name=f"🏛️ Akcie – Trading 212 (0/{MAX_OPEN_POSITIONS_STOCKS})",
            value="*Žádné otevřené akciové pozice (5 slotů volných pro US/EU akcie).* ",
            inline=False
        )

    # 2. KRYPTOMĚNY (Binance Futures)
    if crypto_positions:
        c_lines = []
        for p in crypto_positions:
            ticker = p["ticker"]
            entry = p["entry"]
            sl = p["stop_loss"]
            tp = p["take_profit"]
            allocated = p["allocated_usd"]
            shares = p["shares"]
            user = p.get("decided_by", "Neznámý")
            date_str = p.get("decided_at", "")[:19].replace("T", " ")
            binance_id = p.get("binance_order_id")
            order_info = f" | Binance: `#{binance_id}`" if binance_id else ""

            c_lines.append(
                f"• **{ticker}** ({shares} ks @ `${entry:.2f}`){order_info}\n"
                f"  Alokace: `${allocated:.2f}` | SL: `${sl:.2f}` | TP: `${tp:.2f}` | Otevřel: {user} ({date_str})\n"
                f"  Uzavřít: `/close {ticker}`"
            )
        embed.add_field(
            name=f"🟡 Kryptoměny – Binance Futures ({c_count}/{MAX_OPEN_POSITIONS_CRYPTO})",
            value="\n".join(c_lines),
            inline=False
        )
    else:
        embed.add_field(
            name=f"🟡 Kryptoměny – Binance Futures (0/{MAX_OPEN_POSITIONS_CRYPTO})",
            value="*Žádné otevřené krypto pozice (5 slotů volných pro Binance).* ",
            inline=False
        )

    # Trading 212 Portfolio
    if ENABLE_TRADING_212:
        try:
            t212_pos = await trading212.get_portfolio()
            if t212_pos:
                pos_lines = []
                for tp in t212_pos:
                    sym = tp.get("ticker", "")
                    qty = tp.get("quantity", 0)
                    avg_p = tp.get("averagePrice", 0)
                    cur_p = tp.get("currentPrice", 0)
                    ppl = tp.get("ppl", 0)
                    pos_lines.append(f"• **{sym}:** `{qty}` ks @ `${avg_p:.2f}` (Nyní: `${cur_p:.2f}` | PnL: `{ppl:+.2f}`) ")
                embed.add_field(
                    name=f"🏛️ Trading 212 Portfolio ({TRADING_212_ENV.upper()})",
                    value="\n".join(pos_lines),
                    inline=False
                )
        except Exception as e:
            logger.debug(f"Chyba při načítání T212 portfolia: {e}")

    # Binance Futures Pozice
    if ENABLE_BINANCE:
        try:
            b_pos = await binance_client.get_open_positions()
            if b_pos:
                b_lines = []
                for bp in b_pos:
                    sym = bp.get("symbol")
                    amt = float(bp.get("positionAmt", 0))
                    entry_p = float(bp.get("entryPrice", 0))
                    pnl = float(bp.get("unrealizedProfit", 0))
                    b_lines.append(f"• **{sym}:** `{amt}` ks @ `${entry_p:.2f}` | PnL: `{pnl:+.2f} USDT`")
                embed.add_field(
                    name=f"🟡 Binance Futures Pozice (Testnet)",
                    value="\n".join(b_lines),
                    inline=False
                )
        except Exception as be:
            logger.debug(f"Chyba při načítání Binance pozic: {be}")

    embed.set_footer(text="AIBot 2.0 • Autonomní Multi-Asset Engine")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="close", description="Manuálně uzavře pozici předčasně (Paper Trading + Burza).")
@app_commands.describe(ticker="Symbol k uzavření (např. NVDA nebo BTCUSDT)")
async def slash_close(interaction: discord.Interaction, ticker: str):
    ticker = ticker.upper().strip()
    await interaction.response.defer(thinking=True)

    data = await inspect_ticker(ticker)
    if not data:
        await interaction.followup.send(f"❌ Nelze získat aktuální tržní cenu pro `{ticker}` k uzavření pozice.")
        return

    current_price = data["price"]
    user_name = interaction.user.display_name

    closed_trade = close_position(ticker=ticker, exit_price=current_price, user_name=user_name)
    if not closed_trade:
        await interaction.followup.send(f"❌ Žádná otevřená pozice pro `{ticker}` nebyla nalezena v `paper_trades.json`.")
        return

    pnl_usd = closed_trade.get("pnl_usd", 0.0)
    pnl_pct = closed_trade.get("pnl_pct", 0.0)
    pnl_color = discord.Color.green() if pnl_usd >= 0 else discord.Color.red()
    emoji = "🟢 ZISK" if pnl_usd >= 0 else "🔴 ZTRÁTA"

    embed = discord.Embed(
        title=f"🏁 Pozice {ticker} byla UZAVŘENA",
        color=pnl_color,
        timestamp=datetime.now()
    )
    embed.add_field(name="Nákupní cena (Entry)", value=f"${closed_trade['entry']:.2f}", inline=True)
    embed.add_field(name="Výstupní cena (Exit)", value=f"${current_price:.2f}", inline=True)
    embed.add_field(name="Výsledek (PnL)", value=f"**{emoji}: {pnl_usd:+.2f} USD ({pnl_pct:+.2f}%)**", inline=True)
    embed.add_field(name="Množství", value=f"{closed_trade['shares']} jednotek", inline=True)
    embed.add_field(name="Uvolněný kapitál", value=f"${closed_trade['allocated_usd']:.2f}", inline=True)
    embed.add_field(name="Uzavřel", value=user_name, inline=True)

    # Exekuce na burze
    is_crypto = is_crypto_symbol(ticker)
    if is_crypto and ENABLE_BINANCE:
        try:
            b_res = await binance_client.close_futures_position(ticker)
            if b_res.get("success"):
                embed.add_field(name="🟡 Binance Futures Exekuce", value=f"✅ Pozice uzavřena (Order ID: `#{b_res.get('order_id')}`)", inline=False)
            else:
                embed.add_field(name="ℹ️ Binance Futures", value=f"Pozice nebyla na burze nalezena: `{b_res.get('error')}`", inline=False)
        except Exception as e:
            embed.add_field(name="⚠️ Binance Chyba", value=str(e), inline=False)
    elif not is_crypto and ENABLE_TRADING_212:
        try:
            t212_res = await trading212.close_position_t212(ticker)
            if t212_res.get("success"):
                embed.add_field(name="🚀 Trading 212 Exekuce", value=f"✅ Prodejní příkaz zadán (Order ID: `#{t212_res.get('order_id')}`)", inline=False)
            else:
                embed.add_field(name="ℹ️ Trading 212", value=f"Pozice nebyla na burze nalezena nebo je trh zavřen: `{t212_res.get('error')}`", inline=False)
        except Exception as e:
            embed.add_field(name="⚠️ Trading 212 Chyba", value=str(e), inline=False)

    stock_cnt = len(get_open_positions_by_market("STOCK"))
    crypto_cnt = len(get_open_positions_by_market("CRYPTO"))
    embed.set_footer(text=f"Slot byl uvolněn. Otevřené pozice: Trading 212: {stock_cnt}/{MAX_OPEN_POSITIONS_STOCKS} | Binance: {crypto_cnt}/{MAX_OPEN_POSITIONS_CRYPTO} (Celkem: {stock_cnt + crypto_cnt}/{MAX_OPEN_POSITIONS})")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="t212", description="Zobrazí stav účtu a otevřené pozice na Trading 212.")
async def slash_t212(interaction: discord.Interaction):
    if not ENABLE_TRADING_212:
        await interaction.response.send_message("❌ Trading 212 API není nakonfigurováno v `.env`.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    summary = await trading212.get_account_summary()
    portfolio = await trading212.get_portfolio()

    if not summary:
        await interaction.followup.send("❌ Nepodařilo se načíst souhrn účtu z Trading 212.")
        return

    currency = summary.get("currency", "CZK")
    total_val = summary.get("totalValue", 0.0)
    cash = summary.get("cash", {})
    investments = summary.get("investments", {})
    available = cash.get("availableToTrade", 0.0)
    inv_val = investments.get("currentValue", 0.0)
    unrealized_pnl = investments.get("unrealizedProfitLoss", 0.0)

    embed = discord.Embed(
        title=f"🏛️ Stav Účtu Trading 212 ({TRADING_212_ENV.upper()})",
        color=discord.Color.teal(),
        timestamp=datetime.now()
    )
    embed.add_field(name="Celková hodnota", value=f"**{total_val:.2f} {currency}**", inline=True)
    embed.add_field(name="Volná hotovost", value=f"**{available:.2f} {currency}**", inline=True)
    embed.add_field(name="Hodnota investic", value=f"**{inv_val:.2f} {currency}**", inline=True)
    embed.add_field(name="Nerealizovaný zisk/ztráta", value=f"**{unrealized_pnl:+.2f} {currency}**", inline=True)

    if portfolio:
        pos_lines = []
        for p in portfolio:
            sym = p.get("ticker", "")
            qty = p.get("quantity", 0)
            avg_p = p.get("averagePrice", 0)
            cur_p = p.get("currentPrice", 0)
            ppl = p.get("ppl", 0)
            pos_lines.append(f"• **{sym}:** `{qty}` ks @ `${avg_p:.2f}` (Nyní: `${cur_p:.2f}` | PnL: `{ppl:+.2f}`) ")
        embed.add_field(name=f"📦 Otevřené pozice ({len(portfolio)})", value="\n".join(pos_lines), inline=False)
    else:
        embed.add_field(name="📦 Otevřené pozice", value="Žádné otevřené pozice na Trading 212.", inline=False)

    embed.set_footer(text=f"Trading 212 API • Prostředí: {TRADING_212_ENV.upper()}")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="trades", description="Zobrazí historii schválených a zamítnutých paper obchodů.")
async def slash_trades(interaction: discord.Interaction):
    trades = load_trades()
    if not trades:
        await interaction.response.send_message("V historii `paper_trades.json` zatím nejsou žádné záznamy.")
        return

    embed = discord.Embed(
        title="📜 Historie Obchodů (Poslední záznamy)",
        color=discord.Color.dark_grey(),
        timestamp=datetime.now()
    )

    for t in trades[-10:]:
        ticker = t.get("ticker", "???")
        action = t.get("action", "???")
        status = t.get("status", "???")
        entry = t.get("entry", 0.0)
        pnl = t.get("pnl_usd")
        user = t.get("decided_by", "Neznámý")
        m_type = t.get("market_type", "STOCK")

        if status == "OPEN":
            status_str = "🟢 OTEVŘENO"
        elif status == "CLOSED":
            status_str = f"🏁 UZAVŘENO ({pnl:+.2f} USD)" if pnl is not None else "🏁 UZAVŘENO"
        else:
            status_str = "❌ ZAMÍTNUTO"

        embed.add_field(
            name=f"{ticker} [{m_type}] | {status_str}",
            value=f"• Akce: `{action}` | Entry: `${entry:.2f}`\n• Kdo: `{user}`",
            inline=True
        )

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="status", description="Zobrazí aktuální stav autonomního bota, burz a watchlistů.")
async def slash_status(interaction: discord.Interaction):
    ch = get_target_channel()
    st = alert_tracker.get_status()
    open_count = get_open_positions_count()
    m_status = market_hours.get_market_status_dict()

    t212_str = f"Aktivní ({TRADING_212_ENV.upper()})" if ENABLE_TRADING_212 else "Neaktivní"
    binance_str = "Aktivní (Futures Testnet)" if ENABLE_BINANCE else "Neaktivní"

    stock_cnt = len(get_open_positions_by_market("STOCK"))
    crypto_cnt = len(get_open_positions_by_market("CRYPTO"))

    embed = discord.Embed(
        title="🤖 Stav Autonomního Trading Bota (6členný tým)",
        color=discord.Color.blue(),
        timestamp=datetime.now()
    )
    embed.add_field(
        name="🎯 Dnešní alerty Scouta",
        value=f"• Odesláno: **{st['total_alerts']} / {st['max_total']}**\n• Zbývá dnes: **{st['remaining_alerts']}**\n• Tickéry s alertem: `{', '.join(st['tickers_alerted']) or 'Žádné'}`",
        inline=False
    )
    embed.add_field(
        name="⏰ Burzy & Ochrana Kreditů (Praha)",
        value=(
            f"• Čas: **{m_status['prague_time']}**\n"
            f"• Evropské akcie (09:00-17:30): **{'🟢 OTEVŘENO' if m_status['eu_open'] else '🔴 ZAVŘENO (kredity chráněny)'}**\n"
            f"• US akcie (15:30-22:00): **{'🟢 OTEVŘENO' if m_status['us_open'] else '🔴 ZAVŘENO (kredity chráněny)'}**\n"
            f"• Kryptoměny (Binance): **🟢 24/7/365 AKTIVNÍ**"
        ),
        inline=False
    )
    embed.add_field(
        name="💼 Napojené Burzy & Účty",
        value=(
            f"• **Trading 212:** `{t212_str}` (Pozice: **{stock_cnt} / {MAX_OPEN_POSITIONS_STOCKS}**)\n"
            f"• **Binance Futures:** `{binance_str}` (Pozice: **{crypto_cnt} / {MAX_OPEN_POSITIONS_CRYPTO}**)\n"
            f"• **Celkem otevřených pozic:** **{open_count} / {MAX_OPEN_POSITIONS}**\n"
            f"• **Mód:** `100% Autonomní (AI rozhoduje a provádí nákupy)`"
        ),
        inline=False
    )
    embed.add_field(
        name="⚙️ Sledované Watchlisty",
        value=(
            f"• **US Akcie:** `{', '.join(US_STOCKS_WATCHLIST)}`\n"
            f"• **EU Akcie:** `{', '.join(EU_STOCKS_WATCHLIST)}`\n"
            f"• **Kryptoměny:** `{', '.join(CRYPTO_WATCHLIST)}`\n"
            f"• Cílový kanál: {ch.mention if ch else 'Nenalezen'}"
        ),
        inline=False
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="help", description="Zobrazí nápovědu, role týmu, strategii Přibyla a burzovní hodiny.")
async def slash_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 Nápověda: 6členný Autonomní Trading Tým",
        description="Bot funguje plně autonomně – analyzuje trhy, aplikuje strategii Jiřího Přibyla (SFP), nakupuje na Trading 212 / Binance a hlídá SL/TP.",
        color=discord.Color.purple()
    )
    embed.add_field(
        name="👥 Role autonomního týmu",
        value=(
            "1. **Scout:** Sleduje trhy každých 5 min + hlídá otevřené pozice na Stop-Loss / Take-Profit.\n"
            "2. **News:** Vysvětlí důvod ve 2 větách (nebo 'Žádný jasný důvod').\n"
            "3. **Chart:** Vypočte Entry, Stop-Loss (max 2 % pod entry) a Take-Profit (min RRR 1:2).\n"
            "4. **Skeptik:** 3 důvody proti. Pokud News nenašel důvod, verdikt = PRESKOČIŤ. Jinak KÚPIŤ / POČKAŤ.\n"
            f"5. **Sizer:** Alokuje max 5 % z $10,000 kapitálu (1-2 % riziko na obchod dle Přibyla, max {MAX_OPEN_POSITIONS} pozic).\n"
            "6. **Autonomní Executor:** Prodiskutuje setup s LLM a SAMOSTATNĚ PROVEDE NÁKUP na Trading 212 / Binance."
        ),
        inline=False
    )
    embed.add_field(
        name="🛡️ Ochrana proti plýtvání kredity",
        value=(
            "Mimo obchodní hodiny (víkendy a noci v Praze) bot **neskenuje akcie**, aby neplýtval API voláními Gemini.\n"
            "Kryptoměny (Binance) běží nepřetržitě 24/7/365."
        ),
        inline=False
    )
    embed.add_field(
        name="🎮 Dostupné příkazy",
        value=(
            "`/scout <ticker>` - Provede analýzu instrumentu (US akcie, EU akcie, Krypto) a autonomně nakoupí.\n"
            "`/scan` - Ručně proskenuje aktuálně otevřené trhy.\n"
            "`/crypto` - Analyzuje krypto watchlist pomocí strategie SFP Jiřího Přibyla.\n"
            "`/hours` - Zobrazí stav obchodních hodin v Praze a ochranu kreditů.\n"
            "`/binance` - Zobrazí zůstatek a otevřené pozice na Binance Futures Testnet.\n"
            "`/t212` - Zobrazí stav účtu a volnou hotovost na Trading 212 Demo.\n"
            "`/positions` - Zobrazí otevřené pozice ze všech napojených burz.\n"
            "`/close <ticker>` - Manuálně předčasně uzavře pozici na burze i v evidenci.\n"
            "`/trades` - Zobrazí historii obchodů.\n"
            "`/status` - Zobrazí dnešní statistiku alertů a stav trhů.\n"
            "`/models` - Zobrazí stav multi-tier kaskády Gemini modelů a denní limity.\n"
            "`/help` - Zobrazí tuto nápovědu."
        ),
        inline=False
    )
    embed.set_footer(text="AIBot 2.0 • Autonomní Multi-Asset Trading Desk")
    await interaction.response.send_message(embed=embed)


# =====================================================================
# EVENTY BOTŮ
# =====================================================================

@bot.event
async def on_ready():
    logger.info(f"✅ Přihlášen jako {bot.user} (ID: {bot.user.id})")
    channel = get_target_channel()
    if channel:
        logger.info(f"📢 Cílový alert kanál nastaven na: #{channel.name} (ID: {channel.id})")
    else:
        logger.warning("⚠️ Cílový textový kanál nebyl nalezen!")

    # Registrace view pro manuální zavření pozice
    bot.add_view(AutonomousPositionView(ticker="DEFAULT"))

    # 1. Spuštění periodické smyčky Scouta ihned
    if not scout_periodic_loop.is_running():
        scout_periodic_loop.start()
        logger.info("🚀 Periodická smyčka Scouta (5 minut) byla úspěšně spuštěna.")

    # 2. Synchronizace příkazů na pozadí
    async def sync_commands():
        for guild in bot.guilds:
            try:
                bot.tree.copy_global_to(guild=guild)
                synced_guild = await bot.tree.sync(guild=guild)
                logger.info(f"⚡ Synchronizováno {len(synced_guild)} příkazů do serveru {guild.name}!")
            except Exception as ge:
                logger.debug(f"Guild sync pro {guild.name}: {ge}")
        try:
            synced = await bot.tree.sync()
            logger.info(f"⚡ Globálně synchronizováno {len(synced)} Slash Příkazů!")
        except Exception as e:
            logger.error(f"Chyba při globální synchronizaci Slash příkazů: {e}")

    asyncio.create_task(sync_commands())


def main():
    if not DISCORD_BOT_TOKEN:
        logger.critical("❌ DISCORD_BOT_TOKEN nebyl zadán v .env ani v prostředí! Ukončuji běh.")
        sys.exit(1)

    logger.info("Spouštím Autonomního Discord Bota...")
    bot.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
