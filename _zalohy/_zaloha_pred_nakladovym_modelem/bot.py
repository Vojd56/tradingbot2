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

from price_utils import fmt_price
import os
import sys
import asyncio
import logging
from datetime import datetime, timezone, time as dtime
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
    MAX_OPEN_POSITIONS_CRYPTO,
    T212_USE_REAL_BALANCE,
    T212_ALLOCATION_PCT,
    T212_CASH_BUFFER_PCT,
    SFP_LIVE_ENABLED,
    RISK_PER_TRADE_PCT_LIVE,
    CRYPTO_MAX_NOTIONAL_PCT,
    HIGH20_ENABLED,
)
from scout import inspect_ticker, alert_tracker, scan_active_markets, with_extra_tickers
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
import position_manager
import risk_guard
import news_briefing
import momentum_scanner
from strategy_rules import STRATEGY_SFP, risk_sized_qty, floor_qty, loss_per_unit
from model_manager import quota_manager
import turtle_strategy
import daily_breakout

# Nastavení přehledného logování
# Log jde do konzole i do souboru bot.log (UTF-8, rotace po 5 MB, 3 zálohy), aby šel vždy dohledat zpětně
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FILE = Path(__file__).resolve().parent / "bot.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
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


def crypto_risk_qty(symbol: str, equity_usd: float, entry: float, sl: float, max_notional_usd: float) -> float:
    """Krypto množství podle rizika (RISK_PER_TRADE_PCT_LIVE % kapitálu), zaokrouhlené DOLŮ na přesnost Binance.

    format_quantity zaokrouhluje na nejbližší a zvedá na min_qty, což by riziko zvětšilo; tady se proto
    zaokrouhluje dolů a pod min_qty se obchod nebere (vrací 0).
    """
    qty = risk_sized_qty(equity_usd, entry, sl, RISK_PER_TRADE_PCT_LIVE, max_notional_usd)
    spec = binance_client.BINANCE_LOT_SPECS.get(symbol.upper().strip(), {"precision": 2, "min_qty": 0.01})
    qty = floor_qty(qty, 10 ** -spec["precision"])
    return round(qty, spec["precision"]) if qty >= spec["min_qty"] else 0.0


async def binance_equity_usd() -> Optional[float]:
    """Kapitál futures účtu v USD (totalMarginBalance = zůstatek + nerealizované P&L), None při chybě."""
    info = await asyncio.to_thread(binance_client.get_account_info_sync)
    try:
        return float(info["totalMarginBalance"]) if info else None
    except (KeyError, TypeError, ValueError):
        return None


# Časové limity, aby jedno zaseknuté stažení dat (Yahoo/Binance) nebo odpověď Gemini nezastavily celou smyčku
TICKER_TIMEOUT_S = 45
GEMINI_TIMEOUT_S = 180


async def inspect_ticker_safe(ticker: str) -> Optional[Dict[str, Any]]:
    """inspect_ticker s časovým limitem. Při vypršení vrátí None a zaloguje varování."""
    try:
        return await asyncio.wait_for(inspect_ticker(ticker), timeout=TICKER_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning(f"⏱️ {ticker}: stažení dat trvalo déle než {TICKER_TIMEOUT_S} s, přeskakuji")
        return None


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


async def size_from_t212_balance(ticker: str, entry: float, sl: float) -> Dict[str, Any]:
    """Počet akcí podle rizika: ztráta na stopu = RISK_PER_TRADE_PCT_LIVE % hodnoty účtu T212.

    Strop nominálu zůstává T212_ALLOCATION_PCT % hodnoty účtu a nikdy víc než volná hotovost.

    Vrací {"shares", "alloc_account_ccy", "account_currency", "fx_rate", "instrument_currency"} nebo {"error": str}.
    """
    summary = await asyncio.to_thread(trading212.get_account_summary_sync)
    if not summary:
        return {"error": "Nepodařilo se načíst stav účtu T212"}
    acct_ccy = summary.get("currency") or "CZK"
    total = float(summary.get("totalValue") or 0.0)
    free = float((summary.get("cash") or {}).get("availableToTrade") or 0.0)

    instr_ccy = trading212.instrument_currency(ticker)
    if not instr_ccy:
        return {"error": f"Neznámá měna instrumentu T212 pro {ticker}"}
    fx = await asyncio.to_thread(trading212.get_fx_rate_sync, acct_ccy, instr_ccy)
    if not fx:
        return {"error": f"Nepodařilo se načíst kurz {acct_ccy}/{instr_ccy}, nákup {ticker} se neprovede"}

    calc = trading212.compute_stock_qty(total, free, entry, fx, T212_ALLOCATION_PCT, T212_CASH_BUFFER_PCT)
    # Risk sizing v měně instrumentu; alokační strop z compute_stock_qty funguje jako max. nominál
    calc["shares"] = floor_qty(risk_sized_qty(total * fx, entry, sl, RISK_PER_TRADE_PCT_LIVE,
                                              calc["alloc_acct"] * fx), 0.01)
    if calc["shares"] <= 0:
        return {"error": f"Na účtu je málo peněz: volných {free:.2f} {acct_ccy} nestačí ani na 0,01 akcie {ticker} "
                         f"(alokace {calc['alloc_acct']:.2f} {acct_ccy} ≈ {calc['alloc_acct'] * fx:.2f} {instr_ccy}, "
                         f"cena {fmt_price(entry)} {instr_ccy})"}
    logger.info(f"💰 {ticker}: účet T212 {total:.2f} {acct_ccy} (volných {free:.2f}), alokace {calc['alloc_acct']:.2f} "
                f"{acct_ccy} × kurz {fx:.5f} → {calc['shares']} ks po {fmt_price(entry)} {instr_ccy}")
    # USD za 1 jednotku měny instrumentu, pro součet rizika v risk_guard (None = neznámé, počítá se 1)
    if instr_ccy == "USD":
        usd_rate = 1.0
    else:
        # GBX (pence) i EUR/CHF: kolik instrumentové měny za 1 USD -> USD za 1 jednotku
        per_usd = await asyncio.to_thread(trading212.get_fx_rate_sync, "USD", instr_ccy)
        usd_rate = 1.0 / per_usd if per_usd else None
    return {"shares": calc["shares"], "alloc_account_ccy": round(calc["alloc_acct"], 2), "account_currency": acct_ccy,
            "fx_rate": fx, "instrument_currency": instr_ccy, "usd_per_unit_ccy": usd_rate}


async def open_and_record_trade(analysis: dict, user_name: str, user_id: int) -> Dict[str, Any]:
    """Nakoupí na burze (s okamžitým Stop-Lossem na burze) a teprve po úspěchu zapíše pozici jako OPEN.

    Vrací {"ok": bool, "error": str|None, "result": dict|None, "is_crypto": bool}.
    """
    ticker = analysis["ticker"]
    chart = analysis.get("chart", {})
    sizer = analysis.get("sizer", {})
    allocated_usd = sizer.get("allocated_usd", 500.0)
    entry = chart.get("entry", analysis["price"])
    sl = float(chart.get("stop_loss", 0.0))
    tp = float(chart.get("take_profit", 0.0))
    is_crypto = is_crypto_symbol(ticker)
    real_sizing: Dict[str, Any] = {}

    # Pojistka proti sérii ztrát platí pro každý nákup (smyčka, /scout, /scan)
    blocked = risk_guard.entries_blocked()
    if blocked:
        return {"ok": False, "error": f"Pojistka: {blocked}", "result": None, "is_crypto": is_crypto}

    # SFP 15M nemá v backtestu edge (28. 10.: hrubě −27R / 377 signálů) -> žádné nové živé vstupy
    if is_crypto and chart.get("source") == "SFP setup" and not SFP_LIVE_ENABLED:
        return {"ok": False, "error": "SFP živé vstupy jsou vypnuté (SFP_LIVE_ENABLED=False)",
                "result": None, "is_crypto": is_crypto}

    # Bez platného stopu pod vstupem nejde spočítat riziko ani velikost pozice
    if sl <= 0 or sl >= float(entry):
        return {"ok": False, "error": f"Stop-Loss {sl} neleží pod vstupem {entry}", "result": None, "is_crypto": is_crypto}

    if not is_crypto:
        market = market_hours.market_for_ticker(ticker)
        if market and not market_hours.entries_allowed(market):
            return {"ok": False, "error": f"Mimo vstupní okno trhu {market} – nové akciové pozice teď bot neotevírá",
                    "result": None, "is_crypto": is_crypto}

    usd_rate = 1.0
    if is_crypto:
        enabled = ENABLE_BINANCE
        equity = await binance_equity_usd() if enabled else None
        if enabled and equity is None:
            return {"ok": False, "error": "Nepodařilo se načíst kapitál Binance účtu – nákup zrušen",
                    "result": None, "is_crypto": is_crypto}
        # Paper režim bez Binance: kapitál = fiktivní z Sizeru (FICTITIOUS_CAPITAL)
        if equity is None:
            equity = float(sizer.get("capital") or 10000.0)
        qty = crypto_risk_qty(ticker, equity, float(entry), sl, equity * CRYPTO_MAX_NOTIONAL_PCT / 100.0)
        if qty <= 0:
            return {"ok": False, "error": "Riziková velikost pozice je pod minimem burzy", "result": None,
                    "is_crypto": is_crypto}
        allocated_usd = round(qty * float(entry), 2)
        sizer.update({"shares": qty, "allocated_usd": allocated_usd, "risk_pct": RISK_PER_TRADE_PCT_LIVE})
    else:
        qty = float(sizer.get("shares", 0) or 0)
        enabled = ENABLE_TRADING_212
        if enabled and T212_USE_REAL_BALANCE:
            real = await size_from_t212_balance(ticker, float(entry), sl)
            if real.get("error"):
                logger.warning(f"🛑 {ticker}: {real['error']}")
                return {"ok": False, "error": real["error"], "result": None, "is_crypto": is_crypto}
            qty = real["shares"]
            allocated_usd = round(qty * float(entry), 2)  # v měně instrumentu (u US akcií USD)
            real_sizing = {k: real[k] for k in ("alloc_account_ccy", "account_currency", "fx_rate", "instrument_currency",
                                                "usd_per_unit_ccy")}
            usd_rate = real["usd_per_unit_ccy"] or 1.0
            # Embed na Discordu ukáže skutečnou alokaci místo fiktivních 5 % z $10,000
            sizer.update({"shares": qty, "allocated_usd": allocated_usd, **real_sizing})
        if qty <= 0:
            return {"ok": False, "error": "Alokace nestačí ani na 0,01 akcie", "result": None, "is_crypto": is_crypto}

    # Portfolio heat: nový obchod nesmí s dnešní ztrátou a otevřeným rizikem přetáhnout denní limit
    new_risk_usd = qty * loss_per_unit(float(entry), sl) * usd_rate
    heat = risk_guard.would_exceed_heat(new_risk_usd)
    if heat:
        return {"ok": False, "error": f"Pojistka: {heat}", "result": None, "is_crypto": is_crypto}

    res = None
    extra: Dict[str, Any] = {"opened_by_bot": True, "bot_qty": qty}
    if enabled:
        res = await position_manager.open_bot_position(ticker, is_crypto, qty, sl, tp)
        if not res.get("success"):
            logger.error(f"❌ Nákup {ticker} neproveden: {res.get('error')}")
            return {"ok": False, "error": res.get("error"), "result": res, "is_crypto": is_crypto}
        extra = res["extra"]
        qty = res["fill_qty"]
        entry = res.get("fill_price") or entry
        sl = res["stop_loss"]
        tp = res["take_profit"]
        for w in res.get("warnings", []):
            logger.warning(f"⚠️ {ticker}: {w}")

    if real_sizing:
        extra = {**extra, **real_sizing}

    if is_crypto and chart.get("source") == "SFP setup":
        # Pozici řídí pravidla SFP: polovina při +1R, stop na nulu, časový stop (position_manager.manage_open_trade)
        extra = {**extra, "strategy": STRATEGY_SFP, "initial_stop_loss": sl}
    elif is_crypto and chart.get("source") == "HIGH20":
        # Pozice HIGH20 se drží do konce dne vstupu (UTC), pak ji zavře manage_open_trade
        extra = {**extra, "strategy": daily_breakout.STRATEGY_HIGH20, "initial_stop_loss": sl,
                 "exit_at": daily_breakout.exit_at(datetime.now(timezone.utc)).isoformat()}

    record_new_trade(
        ticker=ticker,
        action="AUTO_BUY",
        status="OPEN",
        entry=entry,
        stop_loss=sl,
        take_profit=tp,
        rrr=chart.get("rrr", "1:2.0"),
        allocated_usd=allocated_usd,
        shares=qty,
        news_summary=analysis.get("news", ""),
        skeptik_reasons=analysis.get("skeptik", {}).get("reasons", []),
        user_name=user_name,
        user_id=user_id,
        t212_order_id=res.get("order_id") if (res and not is_crypto) else None,
        t212_status=res.get("status") if (res and not is_crypto) else None,
        binance_order_id=res.get("order_id") if (res and is_crypto) else None,
        binance_status=res.get("status") if (res and is_crypto) else None,
        market_type="CRYPTO" if is_crypto else "STOCK",
        extra=extra
    )
    return {"ok": True, "error": None, "result": res, "is_crypto": is_crypto}


def _protection_text(result: Optional[Dict[str, Any]]) -> str:
    """Krátký text o ochranných příkazech na burze pro Discord."""
    if not result:
        return "\n🛡️ Stop-Loss hlídá bot softwarově (burza není připojena)."
    extra = result.get("extra", {})
    parts = []
    parts.append("🛡️ Stop-Loss leží na burze" if extra.get("sl_order") else "⚠️ Stop-Loss NENÍ na burze – hlídá ho bot softwarově")
    if extra.get("tp_order"):
        parts.append("Take-Profit leží na burze")
    text = "\n" + " | ".join(parts)
    for w in result.get("warnings", []):
        text += f"\n⚠️ {w}"
    return text


async def execute_autonomous_trade(analysis: dict, channel: discord.TextChannel) -> Dict[str, Any]:
    """Provede autonomní exekuci nákupu schváleného LLM týmem (Akcie na T212, Krypto na Binance).

    Vrací {"ok": bool, "error": str|None}, aby Scout počítal jen skutečně provedené nákupy.
    """
    ticker = analysis["ticker"]
    executor = analysis.get("executor", {})
    allocated_usd = analysis.get("sizer", {}).get("allocated_usd", 500.0)

    if is_crypto_symbol(ticker) or not (ENABLE_TRADING_212 and T212_USE_REAL_BALANCE):
        logger.info(f"🚀 AUTONOMNÍ EXEKUCE: Zahajuji nákup {ticker} (Alokace: ${allocated_usd:.2f})...")
    else:
        logger.info(f"🚀 AUTONOMNÍ EXEKUCE: Zahajuji nákup {ticker} (alokace ze skutečného zůstatku T212)...")

    outcome = await open_and_record_trade(
        analysis,
        user_name="Autonomní AI Executor",
        user_id=bot.user.id if bot.user else 0
    )
    res = outcome["result"]
    is_crypto = outcome["is_crypto"]
    platform = "BINANCE FUTURES" if is_crypto else "TRADING 212"

    if not outcome["ok"]:
        embed = build_autonomous_embed(
            analysis, executed=False,
            binance_result=res if is_crypto else None,
            t212_result=None if is_crypto else res
        )
        await channel.send(
            content=f"❌ **EXEKUCE SELHALA NA {platform}: {ticker}** (Chyba: `{outcome['error']}`)\nPozice NEBYLA zapsána jako otevřená.",
            embed=embed
        )
        return {"ok": False, "error": outcome["error"]}

    embed = build_autonomous_embed(
        analysis,
        executed=True,
        t212_result=None if is_crypto else res,
        binance_result=res if is_crypto else None
    )
    view = AutonomousPositionView(ticker=ticker)
    asset_label = "KRYPTO (Binance Futures)" if is_crypto else "AKCIE (Trading 212)"
    await channel.send(
        content=f"🤖 **AUTONOMNÍ NÁKUP PROVEDEN: {ticker} [{asset_label}]** (AI Důvěra: {executor.get('confidence', 80)}%)" + _protection_text(res),
        embed=embed,
        view=view
    )
    return {"ok": True, "error": None}


# =====================================================================
# 1. SCOUT: PERIODICKÁ SMYČKA KAŽDÝCH 5 MINUT PŘES discord.ext.tasks
# =====================================================================

async def send_close_embed(channel, ticker: str, entry: float, closed_trade: Optional[Dict[str, Any]],
                           reason: str, hit_sl: bool, exchange_res: Optional[Dict[str, Any]]) -> None:
    """Odešle do Discordu kartu o uzavření pozice (až po potvrzeném uzavření)."""
    closed_trade = closed_trade or {}
    pnl_usd = closed_trade.get("pnl_usd", 0.0) or 0.0
    pnl_pct = closed_trade.get("pnl_pct", 0.0) or 0.0
    exit_price = closed_trade.get("exit_price", 0.0) or 0.0
    emoji = "🔴" if hit_sl else "🟢"
    close_embed = discord.Embed(
        title=f"{emoji} AUTONOMNÍ VÝSTUP: {ticker} ({reason})",
        description="Pozice byla uzavřena na burze a teprve poté v evidenci.",
        color=discord.Color.green() if pnl_usd >= 0 else discord.Color.red(),
        timestamp=datetime.now()
    )
    approx = " (přibližně)" if closed_trade.get("exit_price_approx") else ""
    close_embed.add_field(name="Nákupní cena", value=f"${entry}", inline=True)
    close_embed.add_field(name="Výstupní cena", value=f"${exit_price}{approx}", inline=True)
    close_embed.add_field(name="Výsledek obchodu", value=f"**{pnl_usd:+.2f} USD ({pnl_pct:+.2f}%)**", inline=True)
    if closed_trade.get("exchange_close"):
        close_embed.add_field(name="Burza", value=f"✅ {closed_trade['exchange_close']}", inline=False)

    stock_cnt = len(get_open_positions_by_market("STOCK"))
    crypto_cnt = len(get_open_positions_by_market("CRYPTO"))
    close_embed.set_footer(text=f"Slot uvolněn. Otevřené pozice: Trading 212: {stock_cnt}/{MAX_OPEN_POSITIONS_STOCKS} | Binance: {crypto_cnt}/{MAX_OPEN_POSITIONS_CRYPTO} (Celkem: {stock_cnt + crypto_cnt}/{MAX_OPEN_POSITIONS})")
    await channel.send(embed=close_embed)


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

    # A) KONTROLA OTEVŘENÝCH POZIC: synchronizace s burzou + softwarová záloha SL/TP
    open_positions = get_open_positions()
    snapshot = await position_manager.fetch_snapshot(open_positions) if open_positions else {}
    for pos in open_positions:
        ticker = pos["ticker"]
        try:
            p_data = await inspect_ticker_safe(ticker)
            if not p_data:
                continue
            current_price = p_data["price"]
            entry = float(pos.get("entry", current_price))

            # 1) Nezavřel už pozici Stop-Loss / Take-Profit příkaz přímo na burze?
            event = await position_manager.sync_trade(pos, current_price, snapshot)
            if event and event.get("type") == "closed":
                await send_close_embed(channel, ticker, entry, event["trade"], event["reason"], event.get("hit_sl", False), None)
                continue

            # 2) Konec seance u akcií: bot své pozice nedrží přes noc (gap bez dozoru)
            reason = None
            if not is_crypto_symbol(ticker):
                reason = market_hours.should_flatten(ticker, pos.get("decided_at"))
            else:
                # SFP pozice: časový stop a polovina při +1R se stopem zbytku na nule
                m_event = await position_manager.manage_open_trade(pos, current_price)
                if m_event and m_event.get("type") == "exit":
                    reason = m_event["reason"]
                elif m_event and m_event.get("type") == "partial":
                    upd = m_event["result"].get("updates", {})
                    await channel.send(
                        f"🟢 **{ticker}: +1R – prodána část pozice** ({upd.get('partial_qty')} ks za ${fmt_price(upd.get('partial_exit_price', 0))}, "
                        f"zisk ${upd.get('partial_pnl_usd', 0):.2f}). Stop-Loss zbytku posunut na ${fmt_price(upd.get('stop_loss', 0))} (nula + poplatky)."
                    )
                    continue
                elif m_event and m_event.get("type") == "partial_failed" and m_event["result"].get("emergency_close"):
                    await channel.send(f"🚨 **{ticker}:** {m_event['result'].get('error')}")
                    continue

            # 3) Softwarová záloha: jen když odpovídající příkaz na burze neleží
            if not reason:
                reason = position_manager.needs_software_exit(pos, current_price)
            if not reason:
                continue
            logger.info(f"{reason} u {ticker}: Cena {current_price} (Entry: {entry}, SL: {pos.get('stop_loss')}, TP: {pos.get('take_profit')})")

            close_res = await position_manager.close_bot_trade(pos, current_price, f"TradingBot ({reason})")
            if close_res.get("success"):
                await send_close_embed(channel, ticker, entry, close_res["trade"], reason, (close_res["trade"] or {}).get("pnl_usd", 0) < 0, close_res.get("exchange"))
            else:
                await channel.send(
                    f"🚨 **NEPODAŘILO SE UZAVŘÍT {ticker}** ({reason}): `{close_res.get('error')}`\n"
                    f"Pozice zůstává v evidenci OTEVŘENÁ, bot to zkusí znovu v dalším průchodu."
                )

        except Exception as pe:
            logger.error(f"Chyba při kontrole otevřené pozice {ticker}: {pe}", exc_info=True)

    # B) KONTROLA AKTIVNÍCH TRHŮ (OCHRANA PROTI PLÝTVÁNÍ KREDITY)
    today_status = alert_tracker.get_status()
    if today_status["total_alerts"] >= MAX_DAILY_ALERTS_TOTAL:
        logger.info(f"Dosažen denní limit alertů ({MAX_DAILY_ALERTS_TOTAL}/{MAX_DAILY_ALERTS_TOTAL}).")
        return

    # Pokud již máme otevřeno maximum pozic (10 celkem), nový obchod neotevíráme
    if not can_open_new_position():
        logger.info(f"Globální limit {MAX_OPEN_POSITIONS} otevřených pozic je naplněn. Žádné nové pozice nebudou otevírány.")
        return

    # Pojistka proti sérii ztrát: nové vstupy stojí, otevřené pozice se výše hlídají dál (a Gemini se zbytečně nevolá)
    blocked = risk_guard.entries_blocked()
    if blocked:
        logger.info(f"🛑 POJISTKA: {blocked}. Nové pozice se neotevírají.")
        return

    # Získání aktivních trhů + dnešní tickery z ranního přehledu a momentum skeneru (US i EU)
    active_market_lists = with_extra_tickers(market_hours.get_active_markets_and_watchlists())
    stocks_closed = not m_status["eu_open"] and not m_status["us_open"]
    if stocks_closed:
        logger.info("🛡️ OCHRANA KREDITŮ: Akciové burzy jsou zavřené. Skenuji POUZE kryptoměny 24/7 a šetřím Gemini API kredity.")

    # Souhrn průchodu: kolik tickerů prošel, kolik signálů a proč nenakoupil
    summary = {"prověřeno": 0, "bez_signálu": 0, "obsazeno": 0, "limit_alertů": 0,
               "timeout": 0, "chyba": 0, "signály": 0, "nakoupeno": 0, "nákup_selhal": 0, "bez_t212": 0}
    rejected = []
    pass_started = datetime.now()

    for market_name, m_type, tickers in active_market_lists:
        for ticker in tickers:
            # Kontrola kapacity pro danou platformu (5 pro T212, 5 pro Binance) a už otevřených tickerů
            if not can_open_new_position_for(ticker):
                summary["obsazeno"] += 1
                continue

            if not alert_tracker.can_alert(ticker):
                summary["limit_alertů"] += 1
                continue

            # Akcie bez instrumentu na T212 by stejně nešla koupit: přeskočit před Gemini, ať se neplýtvají kredity
            if ENABLE_TRADING_212 and not is_crypto_symbol(ticker) and trading212.to_t212_ticker(ticker) is None:
                summary["bez_t212"] += 1
                continue

            try:
                summary["prověřeno"] += 1
                data = await inspect_ticker_safe(ticker)
                if data is None:
                    summary["timeout"] += 1
                    continue
                if not data["triggered"]:
                    summary["bez_signálu"] += 1
                    continue
                # Krypto spouští jen SFP; když je vypnuté, Gemini se zbytečně nevolá (kupuje jen HIGH20 smyčka)
                if is_crypto_symbol(ticker) and not SFP_LIVE_ENABLED:
                    summary["bez_signálu"] += 1
                    continue

                summary["signály"] += 1
                logger.info(f"🔥 SCOUT SIGNAL pro {ticker} ({market_name}): {data['trigger_reasons']}")
                alert_tracker.register_alert(ticker)

                # Analýza LLM týmem (Gemini) s časovým limitem
                try:
                    analysis = await asyncio.wait_for(analyze_with_gemini(
                        ticker=ticker,
                        price=data["price"],
                        price_move_pct=data["price_move_pct"],
                        volume_ratio=data["volume_ratio"],
                        headlines=data["headlines"],
                        setup=data.get("sfp_data") if data.get("sfp_detected") else None
                    ), timeout=GEMINI_TIMEOUT_S)
                except asyncio.TimeoutError:
                    logger.warning(f"⏱️ {ticker}: Gemini neodpověděl do {GEMINI_TIMEOUT_S} s, přeskakuji")
                    summary["timeout"] += 1
                    rejected.append(f"{ticker}: Gemini timeout")
                    continue

                # Pokud byl zjištěn SFP pattern, připojíme metadata do analýzy
                if data.get("sfp_detected"):
                    analysis["sfp_detected"] = True
                    analysis["sfp_data"] = data.get("sfp_data", {})

                executor = analysis.get("executor", {})
                should_execute = executor.get("should_execute", False)
                verdict = analysis.get("skeptik", {}).get("verdict", "PRESKOČIŤ")

                if should_execute and verdict == "KÚPIŤ" and can_open_new_position_for(ticker):
                    trade = await execute_autonomous_trade(analysis, channel)
                    if trade["ok"]:
                        summary["nakoupeno"] += 1
                    else:
                        summary["nákup_selhal"] += 1
                        rejected.append(f"{ticker}: nákup selhal ({trade['error']})")
                else:
                    logger.info(f"🛑 {ticker}: Autonomní nákup neproveden (Verdikt: {verdict}, ShouldExecute: {should_execute})")
                    rejected.append(f"{ticker}: verdikt {verdict}, executor {'ANO' if should_execute else 'NE'}")
                    embed = build_autonomous_embed(analysis, executed=False)
                    await channel.send(embed=embed)

                if alert_tracker.get_status()["total_alerts"] >= MAX_DAILY_ALERTS_TOTAL:
                    break

            except Exception as e:
                summary["chyba"] += 1
                logger.error(f"Chyba při zpracování {ticker} ve Scout smyčce: {e}", exc_info=True)

    duration_s = (datetime.now() - pass_started).total_seconds()
    markets_txt = ", ".join(m_type for _, m_type, _ in active_market_lists)
    logger.info(
        f"📋 SCOUT SOUHRN ({duration_s:.0f} s, trhy: {markets_txt}): "
        + " | ".join(f"{k}: {v}" for k, v in summary.items())
        + (f" | zamítnuto: {'; '.join(rejected)}" if rejected else "")
    )


@scout_periodic_loop.before_loop
async def before_scout_loop():
    await bot.wait_until_ready()
    logger.info("Bot je připraven, spouštím smyčku Scouta...")


# Ranní přehled zpráv: v 07:30 Praha (před otevřením Evropy) nebo při prvním spuštění dne
MORNING_BRIEFING_TIME = dtime(7, 30, tzinfo=market_hours.PRAGUE_TZ)


async def run_morning_briefing(reason: str) -> None:
    """Jednou denně posbírá zprávy a kalendář událostí. Do Discordu pošle jen nově vytvořený přehled."""
    try:
        briefing, created = await news_briefing.ensure_today_briefing()
    except Exception as e:
        logger.error(f"🗞️ Ranní přehled selhal ({reason}): {e}", exc_info=True)
        return
    if not created:
        logger.info(f"🗞️ Ranní přehled pro {briefing.get('date')} už existuje, znovu se nestahuje ({reason}).")
        return
    model = (briefing.get("summary") or {}).get("model_used", "bez Gemini")
    logger.info(f"🗞️ Ranní přehled hotový ({reason}, model: {model}).")
    channel = get_target_channel()
    if channel:
        try:
            await channel.send(news_briefing.format_discord_message(briefing))
        except Exception as e:
            logger.error(f"🗞️ Ranní přehled nejde poslat do Discordu: {e}")


@tasks.loop(time=MORNING_BRIEFING_TIME)
async def morning_briefing_loop():
    await run_morning_briefing("ranní plán 07:30")


@morning_briefing_loop.before_loop
async def before_morning_briefing_loop():
    await bot.wait_until_ready()


# Strategie TURTLE 4h: JEN DEMO (papír), vlastní evidence, žádné příkazy na burzu ani Gemini.
# Vyhodnocení 2 min po zavření každé 4h svíčky (00:02, 04:02 ... UTC), Stop-Loss se hlídá každých 5 minut.
TURTLE_EVAL_TIMES = [dtime(h, 2, tzinfo=timezone.utc) for h in range(0, 24, 4)]


async def send_turtle_events(events) -> None:
    channel = get_target_channel()
    if not channel:
        return
    for ev in events:
        try:
            await channel.send(turtle_strategy.format_event(ev))
        except Exception as e:
            logger.error(f"🐢 Turtle zprávu nejde poslat do Discordu: {e}")


@tasks.loop(time=TURTLE_EVAL_TIMES)
async def turtle_4h_loop():
    try:
        events = await asyncio.to_thread(turtle_strategy.run_4h_evaluation)
        await send_turtle_events(events)
    except Exception as e:
        logger.error(f"🐢 Turtle vyhodnocení selhalo: {e}", exc_info=True)


# Momentum skener: každých 30 min v obchodní den USA (premarket i seance) hledá akcie s pohybem,
# objemem a výsledky a zařadí je ke sledování. Nově zařazené ohlásí do Discordu i s důvodem.
@tasks.loop(minutes=30)
async def momentum_scan_loop():
    if not momentum_scanner.is_scan_time():
        return
    try:
        added = await asyncio.to_thread(momentum_scanner.run_scan)
    except Exception as e:
        logger.error(f"🚀 Momentum skener selhal: {e}", exc_info=True)
        return
    channel = get_target_channel()
    if added and channel:
        try:
            await channel.send(momentum_scanner.format_added_message(added))
        except Exception as e:
            logger.error(f"🚀 Zprávu skeneru nejde poslat do Discordu: {e}")


@momentum_scan_loop.before_loop
async def before_momentum_scan_loop():
    await bot.wait_until_ready()


@tasks.loop(minutes=5)
async def high20_loop():
    """Strategie HIGH20: jednou denně po zavření denní svíčky (00:00 UTC) koupí páry na 20denním maximu.

    Běží každých 5 min, ale nakupuje jen v okně HIGH20_ENTRY_WINDOW_HOURS po půlnoci UTC a jen jednou za den.
    Výstup (konec dne) a Stop-Loss řeší Scout smyčka přes manage_open_trade a příkazy na burze.
    """
    now = datetime.now(timezone.utc)
    state = daily_breakout.load_state()
    if not daily_breakout.in_entry_window(now) or daily_breakout.already_done(state, now):
        return
    # Včerejší HIGH20 pozice musí Scout nejdřív zavřít, jinak by symbol vypadal obsazený a dnešní signál by propadl
    if any(daily_breakout.expired(p, now) for p in get_open_positions()):
        logger.info("📈 HIGH20: čekám, až Scout zavře včerejší pozice")
        return
    channel = get_target_channel()
    if not channel:
        return
    now_ms = int(now.timestamp() * 1000)
    results: Dict[str, str] = {}
    for sym in CRYPTO_WATCHLIST:
        try:
            klines = await asyncio.to_thread(binance_client.get_klines_sync, sym, "1d", 40)
            sig = daily_breakout.signal(klines, now_ms)
            if not sig:
                results[sym] = "bez signálu"
                continue
            if not can_open_new_position_for(sym):
                results[sym] = "signál, ale symbol je obsazený / plné sloty"
                continue
            price = await asyncio.to_thread(turtle_strategy.fetch_price, sym)
            analysis = daily_breakout.build_analysis(sym, price, sig)
            outcome = await open_and_record_trade(analysis, user_name="HIGH20", user_id=bot.user.id if bot.user else 0)
            if outcome["ok"]:
                ch = analysis["chart"]
                results[sym] = "koupeno"
                await channel.send(
                    f"📈 **HIGH20: {sym} koupeno** – denní close na {daily_breakout.HIGH20_LOOKBACK}denním maximu. "
                    f"Vstup ~${fmt_price(price)}, Stop-Loss ${fmt_price(ch['stop_loss'])} (1,5× ATR), "
                    f"výstup na konci dne (00:00 UTC)."
                )
            else:
                results[sym] = f"nákup neproveden: {outcome['error']}"
        except Exception as e:
            results[sym] = f"chyba: {e}"
            logger.error(f"📈 HIGH20 {sym}: {e}", exc_info=True)
    daily_breakout.save_state(daily_breakout.done_state(now, results))
    logger.info("📈 HIGH20 SOUHRN: " + " | ".join(f"{s}: {r}" for s, r in results.items()))


@high20_loop.before_loop
async def before_high20_loop():
    await bot.wait_until_ready()


@tasks.loop(minutes=5)
async def turtle_stop_loop():
    try:
        events = await asyncio.to_thread(turtle_strategy.check_stops)
        await send_turtle_events(events)
    except Exception as e:
        logger.error(f"🐢 Turtle kontrola Stop-Lossu selhala: {e}", exc_info=True)


@turtle_4h_loop.before_loop
async def before_turtle_4h_loop():
    await bot.wait_until_ready()


@turtle_stop_loop.before_loop
async def before_turtle_stop_loop():
    await bot.wait_until_ready()


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
            headlines=data["headlines"],
            setup=data.get("sfp_data") if data.get("sfp_detected") else None
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

            outcome = await open_and_record_trade(
                analysis,
                user_name=f"Autonomní Executor (přes /scout od {interaction.user.display_name})",
                user_id=interaction.user.id
            )
            res = outcome["result"]
            if not outcome["ok"]:
                embed = build_autonomous_embed(
                    analysis, executed=False,
                    binance_result=res if is_crypto else None,
                    t212_result=None if is_crypto else res
                )
                await interaction.followup.send(
                    content=f"❌ **EXEKUCE SELHALA: {ticker}** (Chyba burzy: `{outcome['error']}`)\nPozice NEBYLA zapsána jako otevřená.",
                    embed=embed
                )
                return

            embed = build_autonomous_embed(
                analysis,
                executed=True,
                t212_result=None if is_crypto else res,
                binance_result=res if is_crypto else None
            )
            view = AutonomousPositionView(ticker=ticker)
            asset_label = "KRYPTO (Binance)" if is_crypto else "AKCIE (Trading 212)"
            await interaction.followup.send(
                content=f"🤖 **AUTONOMNÍ NÁKUP PROVEDEN: {ticker} [{asset_label}]** (AI Důvěra: {executor.get('confidence', 80)}%)" + _protection_text(res),
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


@bot.tree.command(name="turtle", description="Zobrazí demo strategii TURTLE 4h: otevřené pozice a celkový výsledek.")
async def slash_turtle(interaction: discord.Interaction):
    trades = turtle_strategy.load_ledger()
    s = turtle_strategy.summary(trades)
    lines = [f"🐢 **[TURTLE DEMO]** jen papír, 1R = ${turtle_strategy.TURTLE_RISK_USD:.2f}, "
             f"max {turtle_strategy.TURTLE_MAX_POSITIONS} pozic",
             f"Uzavřeno: {s['closed']} (v zisku {s['wins']}) | celkem **{s['total_r']:+.2f}R / ${s['total_usd']:+.2f}** po poplatcích",
             f"Otevřeno: {s['open']}"]
    for t in turtle_strategy.open_positions(trades):
        lines.append(f"• {t['ticker']}: vstup ${t['entry']:.6g}, SL ${t['stop_loss']:.6g}, od {t['opened_at'][:16].replace('T', ' ')} UTC")
    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="news", description="Ukáže dnešní ranní přehled zpráv, výsledků firem a makro událostí.")
async def slash_news(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        briefing, _ = await news_briefing.ensure_today_briefing()
        await interaction.followup.send(news_briefing.format_discord_message(briefing))
    except Exception as e:
        logger.error(f"Chyba při /news: {e}", exc_info=True)
        await interaction.followup.send(f"❌ Ranní přehled se nepovedl: {e}")


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
                    headlines=data["headlines"],
                    setup=data.get("sfp_data") if data.get("sfp_detected") else None
                )
                if data.get("sfp_detected"):
                    analysis["sfp_detected"] = True
                    analysis["sfp_data"] = data.get("sfp_data", {})

                executor = analysis.get("executor", {})
                verdict = analysis.get("skeptik", {}).get("verdict", "PRESKOČIŤ")
                if executor.get("should_execute") and verdict == "KÚPIŤ" and can_open_new_position_for(ticker):
                    trade = await execute_autonomous_trade(analysis, channel) if channel else {"ok": False, "error": "není kanál"}
                    if trade["ok"]:
                        executed_count += 1
                        results_texts.append(f"• **{ticker}** ({market_name}): AUTONOMNĚ NAKOUPENO")
                    else:
                        results_texts.append(f"• **{ticker}** ({market_name}): nákup selhal ({trade['error']})")
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
                f"• **{ticker}** ({shares} ks @ `${fmt_price(entry)}`){order_info}\n"
                f"  Alokace: `${allocated:.2f}` | SL: `${fmt_price(sl)}` | TP: `${fmt_price(tp)}` | Otevřel: {user} ({date_str})\n"
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
                f"• **{ticker}** ({shares} ks @ `${fmt_price(entry)}`){order_info}\n"
                f"  Alokace: `${allocated:.2f}` | SL: `${fmt_price(sl)}` | TP: `${fmt_price(tp)}` | Otevřel: {user} ({date_str})\n"
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

    trade = next((t for t in get_open_positions() if t.get("ticker") == ticker), None)
    if not trade:
        await interaction.followup.send(f"❌ Žádná otevřená pozice bota pro `{ticker}` nebyla nalezena v `paper_trades.json`. Ruční pozice bot nezavírá.")
        return

    close_res = await position_manager.close_bot_trade(trade, current_price, user_name)
    if not close_res.get("success"):
        await interaction.followup.send(
            f"🚨 Pozici `{ticker}` se na burze NEPODAŘILO uzavřít: `{close_res.get('error')}`\nV evidenci zůstává otevřená."
        )
        return

    closed_trade = close_res["trade"] or {}
    pnl_usd = closed_trade.get("pnl_usd", 0.0) or 0.0
    pnl_pct = closed_trade.get("pnl_pct", 0.0) or 0.0
    pnl_color = discord.Color.green() if pnl_usd >= 0 else discord.Color.red()
    emoji = "🟢 ZISK" if pnl_usd >= 0 else "🔴 ZTRÁTA"

    embed = discord.Embed(
        title=f"🏁 Pozice {ticker} byla UZAVŘENA",
        color=pnl_color,
        timestamp=datetime.now()
    )
    embed.add_field(name="Nákupní cena (Entry)", value=f"${closed_trade.get('entry')}", inline=True)
    embed.add_field(name="Výstupní cena (Exit)", value=f"${closed_trade.get('exit_price')}", inline=True)
    embed.add_field(name="Výsledek (PnL)", value=f"**{emoji}: {pnl_usd:+.2f} USD ({pnl_pct:+.2f}%)**", inline=True)
    embed.add_field(name="Množství (jen kusy bota)", value=f"{closed_trade.get('bot_qty', closed_trade.get('shares'))} jednotek", inline=True)
    embed.add_field(name="Uvolněný kapitál", value=f"${closed_trade.get('allocated_usd', 0):.2f}", inline=True)
    embed.add_field(name="Uzavřel", value=user_name, inline=True)
    if closed_trade.get("exchange_close"):
        embed.add_field(name="Burza", value=f"✅ {closed_trade['exchange_close']}", inline=False)

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
            value=f"• Akce: `{action}` | Entry: `${fmt_price(entry)}`\n• Kdo: `{user}`",
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
            "`/news` - Zobrazí dnešní ranní přehled zpráv, výsledků firem a makro událostí.\n"
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

    # Ranní přehled zpráv: denní plán + hned teď, pokud dnešní ještě není (první spuštění dne)
    if not morning_briefing_loop.is_running():
        morning_briefing_loop.start()
        asyncio.create_task(run_morning_briefing("první spuštění dne"))

    if not momentum_scan_loop.is_running():
        momentum_scan_loop.start()
        logger.info("🚀 Momentum skener spuštěn: každých 30 min v obchodní den USA 07:00-16:00 New York.")

    # Strategie HIGH20: krypto long po denním close na 20denním maximu (Binance)
    if HIGH20_ENABLED and not high20_loop.is_running():
        high20_loop.start()
        logger.info("📈 Smyčka HIGH20 spuštěna: nákupy 00:00-02:00 UTC po denním close na 20denním maximu, výstup na konci dne.")

    # Strategie TURTLE 4h (jen demo)
    if turtle_strategy.TURTLE_ENABLED:
        if not turtle_4h_loop.is_running():
            turtle_4h_loop.start()
        if not turtle_stop_loop.is_running():
            turtle_stop_loop.start()
        logger.info("🐢 Smyčka TURTLE 4h (demo) spuštěna: vyhodnocení v 00:02, 04:02 ... UTC, kontrola SL každých 5 min.")

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
