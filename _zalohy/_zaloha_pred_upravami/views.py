"""Discord UI komponenty: Vizuální Embed karty pro autonomní obchody 6členného týmu.
"""

import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional
import discord

from paper_trading import close_position, get_open_positions_count
from config import GEMINI_MODEL, ENABLE_TRADING_212, TRADING_212_ENV, ENABLE_BINANCE, MAX_OPEN_POSITIONS
import trading212
import binance_client

logger = logging.getLogger("TradingBot.Views")


def build_autonomous_embed(
    analysis: Dict[str, Any],
    executed: bool = False,
    t212_result: Optional[Dict[str, Any]] = None,
    binance_result: Optional[Dict[str, Any]] = None,
    close_info: Optional[Dict[str, Any]] = None
) -> discord.Embed:
    """Vytvoří vizuální Embed kartu pro autonomní rozhodnutí a exekuci týmu."""
    ticker = analysis["ticker"]
    price = analysis["price"]
    move_pct = analysis["price_move_pct"]
    vol_ratio = analysis["volume_ratio"]
    news = analysis["news"]
    chart = analysis.get("chart", {})
    skeptik = analysis.get("skeptik", {})
    sizer = analysis.get("sizer", {})
    executor = analysis.get("executor", {})

    entry = chart.get("entry", price)
    sl = chart.get("stop_loss", round(entry * 0.98, 2))
    tp = chart.get("take_profit", round(entry * 1.04, 2))
    rrr = chart.get("rrr", "1:2.0")

    sl_pct = ((sl - entry) / entry) * 100 if entry > 0 else -2.0
    tp_pct = ((tp - entry) / entry) * 100 if entry > 0 else 4.0

    verdict = skeptik.get("verdict", "PRESKOČIŤ")
    reasons = skeptik.get("reasons", [])

    allocated_usd = sizer.get("allocated_usd", 500.0)
    shares = sizer.get("shares", 0)
    open_pos = sizer.get("open_positions", get_open_positions_count())
    max_pos = sizer.get("max_positions", MAX_OPEN_POSITIONS)

    confidence = executor.get("confidence", 75)
    reasoning = executor.get("reasoning", "")

    if close_info:
        pnl_usd = close_info.get("pnl_usd", 0.0)
        pnl_pct = close_info.get("pnl_pct", 0.0)
        color = discord.Color.green() if pnl_usd >= 0 else discord.Color.red()
        title = f"🏁 POZICE UZAVŘENA: {ticker} (PnL: {pnl_usd:+.2f} USD / {pnl_pct:+.2f}%)"
    elif executed:
        color = discord.Color.green()
        title = f"🤖 AUTONOMNÍ NÁKUP PROVEDEN: {ticker} @ ${entry:.2f}"
    elif verdict == "POČKAŤ":
        color = discord.Color.gold()
        title = f"⏳ AUTONOMNÍ ROZHODNUTÍ: POČKAŤ [{ticker}]"
    else:
        color = discord.Color.dark_grey()
        title = f"🛑 AUTONOMNÍ ROZHODNUTÍ: PRESKOČIŤ [{ticker}]"

    embed = discord.Embed(
        title=title,
        description="Rozhodnutí a exekuce 6členného AI týmu (Scout, News, Chart, Skeptik, Sizer, Executor)",
        color=color,
        timestamp=datetime.now(timezone.utc)
    )

    # 1. SCOUT
    sfp_info = ""
    if analysis.get("sfp_detected"):
        sfp_data = analysis.get("sfp_data", {})
        sw_val = sfp_data.get("swing_low_15m", sfp_data.get("swing_low_4h", 0))
        sfp_info = f"\n• 🎯 **SFP Pattern (15M dno):** Knot prorazil 15M Swing Low `${sw_val:.2f}` a svíčka zavřela nad ním!"

    embed.add_field(
        name="🔭 1. SCOUT (Tržní data)",
        value=(
            f"• **Cena:** `${price:.2f}`\n"
            f"• **1h pohyb:** `{move_pct:+.2f}%`\n"
            f"• **Volume spike:** `{vol_ratio:.2f}x` 20D průměru"
            f"{sfp_info}"
        ),
        inline=False
    )

    # 2. NEWS
    embed.add_field(
        name="📰 2. NEWS ANALYST (Fundament)",
        value=f"*{news}*",
        inline=False
    )

    # 3. CHART
    embed.add_field(
        name="📊 3. CHART ANALYST (Setup)",
        value=(
            f"• **Entry:** `${entry:.2f}`\n"
            f"• **Stop-Loss (max 2%):** `${sl:.2f}` ({sl_pct:.2f}%)\n"
            f"• **Take-Profit (min 1:2):** `${tp:.2f}` ({tp_pct:+.2f}%)\n"
            f"• **Poměr RRR:** `{rrr}`"
        ),
        inline=True
    )

    # 4. SKEPTIK
    skeptik_text = "\n".join(f"• {r}" for r in reasons) if reasons else "• Žádná bezprostřední rizika."
    embed.add_field(
        name=f"🧐 4. SKEPTIK (Verdikt: {verdict})",
        value=f"{skeptik_text}\n• **Doporučení:** **{verdict}**",
        inline=False
    )

    # 5. SIZER
    is_crypto = ticker.upper().endswith("USDT") or ticker.upper() in ["BTC", "ETH", "SOL", "BNB"]
    platform_name = "Binance Futures" if is_crypto else "Trading 212"
    platform_max = 5
    if executed:
        sizer_val = (
            f"• **Alokováno:** `${allocated_usd:.2f}` (5 % z $10,000)\n"
            f"• **Množství:** `{shares}` jednotek\n"
            f"• **Aktivní pozice ({platform_name}):** `{open_pos + 1}/{platform_max}` (Celkem max 10)"
        )
    else:
        sizer_val = (
            f"• **Alokováno:** `$0.00` (Obchod nebyl otevřen)\n"
            f"• **Aktivní pozice ({platform_name}):** `{open_pos}/{platform_max}` (Celkem max 10)"
        )

    embed.add_field(
        name="💰 5. SIZER (Money Management)",
        value=sizer_val,
        inline=False
    )

    # 6. EXECUTOR (Autonomní provedení)
    if close_info:
        executor_val = f"🏁 **Pozice byla uzavřena.**\nVýstupní cena: `${close_info.get('exit_price', 0):.2f}`\nVýsledek: **{close_info.get('pnl_usd', 0):+.2f} USD**."
    elif executed:
        executor_val = (
            f"🚀 **PŘÍKAZ AUTONOMNĚ EXEKUOVÁN BOTEM** (Důvěra AI: **{confidence}%**)\n"
            f"• *Zdůvodnění LLM:* {reasoning}\n"
            f"• *Zápis:* Pozice otevřena v `paper_trades.json`"
        )
        if t212_result:
            if t212_result.get("success"):
                executor_val += f"\n• 🏛️ **Trading 212 ({TRADING_212_ENV.upper()}):** Úspěšně nakoupeno! (Order ID: `#{t212_result.get('order_id')}`)"
            else:
                executor_val += f"\n• ⚠️ **Trading 212:** {t212_result.get('error')} *(Trh je mimo obchodní hodiny, simulováno lokálně)*"
        elif binance_result:
            if binance_result.get("success"):
                executor_val += f"\n• 🟡 **Binance Futures Testnet:** Úspěšně nakoupeno! (Order ID: `#{binance_result.get('order_id')}`)"
            else:
                executor_val += f"\n• ℹ️ **Binance Futures:** {binance_result.get('error')} *(Simulováno lokálně v paper trades)*"
    else:
        executor_val = (
            f"🛑 **OBCHOD NEBYL OTEVŘEN** (Autonomní rozhodnutí)\n"
            f"• *Zdůvodnění LLM:* {reasoning or 'Rizika převyšují potenciální výnos.'}"
        )

    embed.add_field(
        name="⚡ 6. AUTONOMNÍ EXECUTOR",
        value=executor_val,
        inline=False
    )

    model_used = analysis.get("model_used", GEMINI_MODEL)
    footers = [f"Model: {model_used}"]
    if ENABLE_TRADING_212:
        footers.append(f"T212: {TRADING_212_ENV.upper()}")
    if ENABLE_BINANCE:
        footers.append("Binance: TESTNET")

    embed.set_footer(text=f"AIBot 2.0 • Autonomní Mód • {' | '.join(footers)}")
    return embed


class AutonomousPositionView(discord.ui.View):
    """View s tlačítkem pro předčasné manuální uzavření pozice, pokud si to uživatel přeje."""

    def __init__(self, ticker: str):
        super().__init__(timeout=None)
        self.ticker = ticker.upper()

    @discord.ui.button(
        label="Uzavřít pozici předčasně",
        style=discord.ButtonStyle.secondary,
        emoji="🏁",
        custom_id="btn_manual_close_pos"
    )
    async def manual_close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        user_name = interaction.user.display_name

        # Stažení aktuální ceny
        import scout
        data = await scout.inspect_ticker(self.ticker)
        current_price = data["price"] if data else 0.0

        closed_trade = close_position(ticker=self.ticker, exit_price=current_price, user_name=user_name)
        if not closed_trade:
            await interaction.followup.send(f"⚠️ Žádná aktivní otevřená pozice pro `{self.ticker}` k uzavření nebyla nalezena.", ephemeral=True)
            return

        # Prodej na burzách (Trading 212 nebo Binance)
        t212_res = None
        binance_res = None
        is_crypto = self.ticker.endswith("USDT") or self.ticker in ["BTC", "ETH", "SOL", "BNB"]

        if is_crypto and ENABLE_BINANCE:
            binance_res = await binance_client.close_futures_position(self.ticker)
        elif not is_crypto and ENABLE_TRADING_212:
            t212_res = await trading212.close_position_t212(self.ticker)

        button.disabled = True
        pnl = closed_trade.get("pnl_usd", 0.0)
        pnl_pct = closed_trade.get("pnl_pct", 0.0)

        broker_msg = ""
        if t212_res and t212_res.get("success"):
            broker_msg = f" | T212 Order: #{t212_res.get('order_id')}"
        elif binance_res and binance_res.get("success"):
            broker_msg = f" | Binance Order: #{binance_res.get('order_id')}"

        await interaction.message.reply(
            content=f"🏁 Pozice **{self.ticker}** byla manuálně uzavřena uživatelem {user_name}.\nVýsledek: **{pnl:+.2f} USD ({pnl_pct:+.2f}%)** za cenu `${current_price:.2f}`{broker_msg}."
        )
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass
