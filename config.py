"""Konfigurace pro Discord Trading Bot (6členný tým).

Načítá proměnné prostředí z .env souboru s fallbackem na systémové proměnné.
Obsahuje nastavení pro US akcie, Evropské akcie, Kryptoměny (Binance) a ochranu neobchodních hodin.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(dotenv_path=ENV_PATH)

# Citlivé klíče Discord a Gemini
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
_raw_channel_id = os.getenv("DISCORD_CHANNEL_ID", "0").strip()
try:
    DISCORD_CHANNEL_ID = int(_raw_channel_id) if _raw_channel_id else 0
except ValueError:
    DISCORD_CHANNEL_ID = 0

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()

# Trading 212 API konfigurace
TRADING_212_API_KEY = os.getenv("TRADING_212_API_KEY", "").strip()
TRADING_212_SECRET_KEY = os.getenv("TRADING_212_SECRET_KEY", "").strip()
TRADING_212_ENV = os.getenv("TRADING_212_ENV", "demo").strip().lower()
TRADING_212_BASE_URL = f"https://{TRADING_212_ENV}.trading212.com/api/v0"
ENABLE_TRADING_212 = bool(TRADING_212_API_KEY and TRADING_212_SECRET_KEY)

# Binance Futures Testnet API konfigurace
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
BINANCE_SECRET = os.getenv("BINANCE_SECRET", "").strip()
BINANCE_FUTURES_URL = os.getenv("BINANCE_FUTURES_URL", "https://testnet.binancefuture.com").strip()
ENABLE_BINANCE = bool(BINANCE_API_KEY and BINANCE_SECRET)

# =====================================================================
# WATCHLISTY PODLE TRHŮ A REGIONŮ (US, EVROPA, KRYPTO)
# =====================================================================

# 1. US akcie (obchodní hodiny: 09:30 - 16:00 New York = 15:30 - 22:00 Praha, v týdnech posunu času o hodinu jinak)
US_STOCKS_WATCHLIST = [
    "NVDA", "AAPL", "TSLA", "AMD", "MSFT", 
    "AMZN", "META", "GOOGL"
]

# 2. Evropské akcie (obchodní hodiny: 09:00 - 17:30 Berlín/Praha)
EU_STOCKS_WATCHLIST = [
    "SAP.DE", "ASML.AS", "SIE.DE", "AIR.PA", 
    "MC.PA", "TTE.PA", "ALV.DE", "BMW.DE"
]

# 3. Kryptoměny (obchodní hodiny: 24/7/365 bez přerušení)
# Top 10 nejvíce obchodovaných a volatilních párů na Binance Futures dle komunity daytraderů
CRYPTO_WATCHLIST = [
    "BTCUSDT",      # Bitcoin - #1 likvidita, určuje směr trhu ($7.5B denní objem)
    "ETHUSDT",      # Ethereum - #2 likvidita, DeFi a beta k BTC ($5.0B denní objem)
    "SOLUSDT",      # Solana - #1 volba daytraderů pro momentum a volatilitu ($1.0B denní objem)
    "XRPUSDT",      # Ripple - vysoký objem ($450M) a časté likviditní knoty
    "DOGEUSDT",     # Dogecoin - nejlikvidnější momentum/meme token ($280M denní objem)
    "BNBUSDT",      # BNB - ekosystém Binance s vysokou hloubkou trhu
    "NEARUSDT",     # Near Protocol - silný AI/L1 narativ s vysokou volatilitou ($600M objem)
    "SUIUSDT",      # Sui - oblíbený momentum coin pro intradenní breakouty
    "AVAXUSDT",     # Avalanche - respektuje čisté support/resistance a SFP úrovně
    "LINKUSDT"      # Chainlink - stabilní technické formace a hluboká likvidita
]

# Tickery Trading 212 nejde vždy složit ze symbolu: Meta má na T212 starý ticker FB, TotalEnergies je FP
# a holé AIR/ALV/MC na americkém seznamu jsou jiné firmy. Ověřeno proti /equity/metadata/instruments 2026-10-27.
T212_TICKER_MAP = {
    "META": "FB_US_EQ",
    "SAP.DE": "SAPd_EQ",
    "ASML.AS": "ASMLa_EQ",
    "SIE.DE": "SIEd_EQ",
    "AIR.PA": "AIRp_EQ",
    "MC.PA": "MCp_EQ",
    "TTE.PA": "FPp_EQ",
    "ALV.DE": "ALVd_EQ",
    "BMW.DE": "BMWd_EQ",
    "HAG.DE": "HAGd_EQ",
}
# Uložený seznam instrumentů T212 (z /equity/metadata/instruments), kontrola tickerů bez volání API
T212_INSTRUMENTS_FILE = BASE_DIR / "t212_instruments.json"

# Výchozí kombinovaný watchlist pro zpětnou kompatibilitu
DEFAULT_WATCHLIST = US_STOCKS_WATCHLIST + EU_STOCKS_WATCHLIST

# =====================================================================
# OCHRANA PROTI PLÝTVÁNÍ KREDITY V NEOBCHODNÍ HODINY (ČASOVÉ PÁSMO PRAHA)
# =====================================================================
PRAGUE_TIMEZONE = "Europe/Prague"

# Časy jsou v ČASOVÉM PÁSMU BURZY (New York / Berlín), přepočet na Prahu dělá market_hours.py.
# Seance: US 09:30-16:00 New York, EU 09:00-17:30 Berlín (+ svátky a zkrácené dny v market_hours.py)

# Vstupní okno = kdy smí bot otevírat NOVÉ pozice (trader-risk: žádné vstupy před 09:45 New York)
US_NO_ENTRIES_BEFORE = "09:45"
US_NO_ENTRIES_AFTER = "15:30"
EU_NO_ENTRIES_BEFORE = "09:15"
EU_NO_ENTRIES_AFTER = "17:00"

# Flat-by = kdy bot uzavře své akciové pozice, aby je nedržel přes noc (trader-exit: --flat-by 15:55)
US_FLAT_BY = "15:50"
EU_FLAT_BY = "17:20"

# Pravidla Scouta
PRICE_MOVE_THRESHOLD_PCT = 3.0   # Pohyb za poslední hodinu o více než 3 %
VOLUME_SPIKE_RATIO = 2.0         # Objem > 2x 20denní průměr
MAX_DAILY_ALERTS_PER_TICKER = 15 # Max 15x za den pro každý ticker dle zadání uživatele
MAX_DAILY_ALERTS_TOTAL = 80      # Max 80 analýz Gemini denně (28. 10.: 30 nestačilo; free tier ~100 Flash + 1000 Flash Lite volání/den)
ALERT_TICKER_COOLDOWN_MIN = 60   # Po analýze stejný ticker 60 min znovu neanalyzovat (dřív BA/HOOD 4x za den, pokaždé PŘESKOČIT)
SCOUT_INTERVAL_MINUTES = 5.0     # Periodická smyčka každých 5 minut

# Pravidla Sizer & Portfolio (dle Jiřího Přibyla)
FICTITIOUS_CAPITAL = 10000.0     # Fiktivní kapitál 10 000 USD
MAX_ALLOCATION_PCT = 5.0         # Max 5 % z kapitálu (= 500 USD)
MAX_OPEN_POSITIONS_STOCKS = 10   # Max 10 otevřených pozic na Trading 212 (akcie)
MAX_OPEN_POSITIONS_CRYPTO = 10   # Max 10 krypto longů najednou (uživatel zvýšil z 2 na 10 dne 26.10.; pozor: altcoiny se hýbou s BTC, 90denní korelace ETH 0,88, SOL 0,83)
MAX_OPEN_POSITIONS = 20          # Celkem max 20 pozic (10 T212 + 10 Binance)
RISK_PER_TRADE_PCT = 1.0         # 1 % risk z kapitálu na obchod (100 USD)

# Velikost akciových obchodů podle skutečných peněz na účtu Trading 212 (místo fiktivních 10 000 USD)
T212_USE_REAL_BALANCE = True     # True = akcie se kupují za skutečný zůstatek T212; False = staré chování (Sizer z FICTITIOUS_CAPITAL)
T212_ALLOCATION_PCT = 20.0       # Na jeden obchod 20 % celkové hodnoty účtu, ale nikdy víc než volná hotovost
T212_CASH_BUFFER_PCT = 5.0       # 5 % volné hotovosti zůstává nevyužité (poplatek za směnu, rezerva T212 u tržního příkazu). 2 % nestačila: NCLH 29. 10. odmítnut "Insufficient funds"

# Strategie SFP 15M pro krypto (binance_client.analyze_sfp)
SFP_LOOKBACK_BARS = 24           # Úroveň = nejnižší low za 24 svíček (6 h), ne každé malé 15M dno
SFP_MIN_VOLUME_RATIO = 1.15      # Objem signální svíčky vs. průměr 20 předchozích
SFP_SL_BUFFER_PCT = 0.1          # Stop-Loss 0,1 % pod knotem sweepu (spread + poplatky)
SFP_MIN_RISK_PCT = 0.15          # Užší stop by vyhodil běžný šum -> obchod se nebere
SFP_MAX_RISK_PCT = 2.0           # Širší stop -> obchod se nebere (stop se procentem nepřepisuje)
SFP_RR = 2.0                     # Take-Profit = 2R
SFP_MAX_CHASE_R = 0.5            # Když cena od signální svíčky utekla o víc než 0,5R, nenakupovat

# Správa otevřené SFP pozice (position_manager.manage_open_trade)
PARTIAL_TAKE_AT_R = 1.0          # Při +1R prodat polovinu ...
PARTIAL_TAKE_FRACTION = 0.5
BREAKEVEN_FEE_BUFFER_PCT = 0.1   # ... a Stop-Loss zbytku posunout na entry + poplatky
SFP_TIME_STOP_HOURS = 6.0        # SFP obchod, který do 6 h nedojde na TP, se zavře
NO_REENTRY_AFTER_STOP = True     # Po Stop-Lossu stejný symbol ten den (UTC) znovu nekupovat

# Pojistka proti sérii ztrát (risk_guard.py): blokuje jen NOVÉ vstupy, otevřené pozice bot dál hlídá
DAILY_LOSS_LIMIT_USD = 300.0     # Realizovaná ztráta za den (UTC) >= 300 USD (3 % kapitálu) -> žádné nové vstupy do půlnoci UTC
MAX_CONSECUTIVE_LOSSES = 5       # 5 ztrátových obchodů za sebou (do 4. 11. 2026 to byly 3) ...
LOSS_STREAK_PAUSE_HOURS = 2.0    # ... -> 2 h bez nových vstupů od poslední ztráty
KILL_SWITCH_FILE = BASE_DIR / "STOP"  # Vypínač: existuje soubor STOP ve složce bota -> žádné nové vstupy

# Strategie TURTLE 4h (turtle_strategy.py): JEN DEMO / PAPER, žádné příkazy na burzu, vlastní evidence
TURTLE_ENABLED = False           # False = smyčka turtle nic nedělá (vypnuto 28. 10. na přání uživatele)
TURTLE_ENTRY_BARS = 20           # Vstup: uzavřená 4h svíčka zavře nad nejvyšším high předchozích 20 svíček
TURTLE_EXIT_BARS = 10            # Výstup: uzavřená 4h svíčka zavře pod nejnižším low předchozích 10 svíček
TURTLE_ATR_MULT = 2.0            # Počáteční Stop-Loss = entry - 2x ATR(14)
TURTLE_MAX_POSITIONS = 5         # Max 5 otevřených turtle pozic (1 na symbol)
TURTLE_RISK_USD = 2.2            # Velikost pozice tak, aby 1R ~ 2,2 USD (jako v backtestu)
TURTLE_FEE_PCT = 0.05            # Poplatek za stranu (taker) v P&L
TURTLE_SLIPPAGE_PCT = 0.02       # Skluz za stranu v P&L

# Datové soubory
PAPER_TRADES_FILE = BASE_DIR / "paper_trades.json"
ALERT_TRACKER_FILE = BASE_DIR / "alert_tracker.json"
TURTLE_TRADES_FILE = BASE_DIR / "paper_trades_turtle.json"  # Evidence turtle demo obchodů (oddělená od paper_trades.json)

# =====================================================================
# RISK MANAGEMENT 2.0 (revize 28. 10. 2026)
# =====================================================================
# SFP 15M: backtest 30 dní, 377 signálů = hrubě −27R, s poplatky −123R -> živé vstupy vypnuté.
# Správa už otevřených SFP pozic (polovina při +1R, časový stop) běží dál.
SFP_LIVE_ENABLED = False

# Poplatky a skluz se počítají do rizika i do RRR (Binance Futures taker 0,05 %, skluz odhad 0,02 %)
FEE_PCT_PER_SIDE = 0.05
SLIPPAGE_PCT_PER_SIDE = 0.02
MAX_COST_SHARE_OF_R = 0.15       # Round-trip náklady > 15 % rizika -> obchod se nebere (úzký stop = platíš burze, ne trhu)
MIN_NET_RR = 2.0                 # RRR po odečtení nákladů musí být aspoň 1:2

# Náklady akcií na Trading 212 (cost_model.py, 28. 10. 2026)
T212_FX_FEE_PCT_PER_SIDE = 0.15  # poplatek T212 za převod měny, když se měna akcie liší od měny účtu (nákup i prodej)
STOCK_SLIPPAGE_PCT_PER_SIDE = 0.05  # odhad skluzu tržního příkazu na akciích
# True = akcie, kde náklady sní víc než MAX_COST_SHARE_OF_R rizika, se nekoupí; False = jen se zapíše do logu
STOCK_COST_GATE_ENFORCE = False

# Velikost pozice podle rizika: ztráta na stopu (vč. nákladů) = RISK_PER_TRADE_PCT_LIVE % kapitálu
# 0,5 %, dokud žádná strategie nemá ověřený kladný výsledek; strop 2 % je tvrdý
RISK_PER_TRADE_PCT_LIVE = 0.5
RISK_PER_TRADE_PCT_MAX = 2.0
CRYPTO_MAX_NOTIONAL_PCT = 20.0   # Strop nominálu krypto pozice = 20 % kapitálu futures účtu (jako T212_ALLOCATION_PCT u akcií)

# Portfolio heat: dnešní realizovaná ztráta + riziko všech otevřených pozic bota (kdyby všechny dojely na SL)
# nesmí přesáhnout denní limit. Korelované krypto longy tak nemůžou limit přeskočit najednou.
MAX_OPEN_RISK_USD = DAILY_LOSS_LIMIT_USD

# Limity podle velikosti účtu (29. 10. 2026, krok M1.5): pro T212 a Binance zvlášť z jejich kapitálu.
# Denní limit trhu = min(DAILY_LOSS_LIMIT_USD, DAILY_LOSS_LIMIT_PCT % kapitálu účtu). Otevřené riziko + nový obchod
# smí být nejvýš HEAT_SLIPPAGE_RESERVE x zbytek limitu (20 % rezerva na skluz tržních příkazů při propadu).
DAILY_LOSS_LIMIT_PCT = 3.0
HEAT_SLIPPAGE_RESERVE = 0.8

# Strategie HIGH20 (daily_breakout.py): krypto long po denním close na 20denním maximu, výstup na konci dne.
# Backtest 28. 10. 2026: out-of-sample rok +17 %, Sharpe 1,07, max. propad 6,9 % (viz dokument Porovnání krypto strategií)
HIGH20_ENABLED = True
HIGH20_LOOKBACK = 20             # Denní close >= nejvyšší close posledních 20 dní (parametr ze zdroje, neladěný)
HIGH20_STOP_ATR = 1.5            # Stop-Loss = entry − 1,5x ATR(14) denních svíček
HIGH20_TP_ATR = 4.0              # Pojistný Take-Profit = entry + 4x ATR (strategie jinak drží do konce dne)
HIGH20_ENTRY_WINDOW_HOURS = 2    # Vstupy jen do 02:00 UTC (backtest vstupuje za open dne)

# Strategie ORB15 pro US akcie (orb_strategy.py) zatím jen ve STÍNU: zapisuje do paper_orb.json, co by koupila,
# žádné příkazy. Přepnutí na živé obchody (M5) až po bráně: >= 60 obchodů, čistý PF >= 1,35, propad < 12 %.
ORB_SHADOW_ENABLED = False       # vypnuto 6. 11. 2026: 31 stínových obchodů, průměr -0,77 R, nákup průrazu nemá výhodu
# Stín vstupu po poklesu (pullback_shadow.py): po zavření trhu přehraje signály Scouta, žádné příkazy ani volání Gemini navíc
PULLBACK_SHADOW_ENABLED = True

# =====================================================================
# SWING F0: vícedenní držení US akcií, JEN STÍN (2. 11. 2026)
# =====================================================================
# Zapisuje do swing_shadow.jsonl / swing_shadow_state.json, co by koupil a jak by pozici vedl. Žádné příkazy.
# Tři stopy se stejnou matematikou: A = skener + Gemini analytik, B = skener bez Gemini (nejvyšší skóre),
# C = skener + náhodný výběr místo Gemini. Současné intradenní akcie i krypto HIGH20 beze změny.
SWING_SHADOW_ENABLED = True
SWING_TRACK_D_ENABLED = False    # Lite tým vypnut 6. 11. 2026: 21 posouzení, 0 nákupů, přitom nejvíc volání Gemini ze stínů
SWING_LIVE_ENABLED = True        # stopa A skutečně nakupuje na T212 DEMO (2. 11. 2026); False = všechny stopy jen stín
SWING_LIVE_LIMIT_SLIPPAGE_PCT = 0.3  # limitní nákup = cena v 09:45 New York + 0,3 % (DAY, nevyplněný se po zavření ruší)
SWING_MIN_PRICE = 5.0
SWING_MIN_DOLLAR_VOLUME = 10e6   # průměrný denní obrat (20 dní) v USD
SWING_MIN_SCORE = 0.6
SWING_EARNINGS_BLACKOUT_DAYS = 5 # výsledky v příštích 5 obchodních dnech -> nekupovat
SWING_MAX_PICKS = 5              # Gemini vybere nejvýš 5 kandidátů
SWING_MAX_ANALYZE = 3            # analytik (3 hlasy) jen u 3 nejlepších
SWING_ANALYST_VOTES = 3
SWING_MIN_BUY_VOTES = 2
SWING_MAX_NEW_PER_DAY = 3        # stopy B a C berou nejvýš tolik nových pozic za den jako A
SWING_ATR_STOP_MULT = 2.0        # SL = nižší z (support, entry − 2×ATR14)
SWING_MIN_NET_RR = 2.0           # cíl = nejbližší rezistence, RRR po nákladech >= 2
SWING_RISK_PCT = 0.5             # ztráta na stopu vč. nákladů = 0,5 % účtu
SWING_MAX_ALLOC_PCT = 20.0
SWING_MAX_POSITIONS = 10            # uživatel zvýšil z 5 na 10 dne 9. 11. 2026 (stropy rizika 3 % a 1/sektor beze změny)
SWING_MAX_PER_SECTOR = 1
SWING_SECTOR_LIMIT_SCOPE = "per_market"   # "per_market" = 1 na sektor zvlášť v US a v EU (uživatel 3. 11.), "global" = dohromady
SWING_MAX_OPEN_RISK_PCT = 5.0       # uživatel zvýšil z 3 na 5 % dne 9. 11. 2026, aby se vešlo 10 pozic po 0,5 %
SWING_LOSS_STREAK = 3            # 3 ztráty za sebou ...
SWING_LOSS_PAUSE_DAYS = 5        # ... -> 5 obchodních dní bez nových vstupů
SWING_MAX_DD_PCT = 10.0          # propad 10 % od vrcholu -> stopa končí s novými vstupy
SWING_BE_AT_R = 1.0
SWING_PARTIAL_AT_R = 2.0
SWING_CHANDELIER_DAYS = 22
SWING_CHANDELIER_ATR = 3.0
SWING_TIME_STOP_DAYS = 10
SWING_TIME_STOP_MIN_R = 0.5
SWING_REVIEW_EVERY_DAYS = 20     # povinná revize Gemini (jen stopa A)
SWING_TIGHTEN_ATR = 2.0
SWING_GEMINI_MAX_CALLS_PER_DAY = 20   # US + EU dohromady (2. 11.: EU přidána), vždy i v mezích kaskády a free tieru
# Plán modelů Gemini (2. 11. 2026): silné Flash modely (4 x 20/den) se dělí pevně, Flash Lite (2 x 500/den) na rutinu.
# Swing rozhoduje jen silným modelem; když silná kvóta dojde, stopa A pro ten trh ten den vynechá.
SWING_GEMINI_BUDGET = {"swing_EU": 10, "swing_US": 10, "evening": 2, "reserve": 6, "swing_D": 60}   # swing_D = Flash Lite
SWING_PICK_MODELS = ["gemini-3-flash-preview"]
SWING_VOTE_MODELS = ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3-flash-preview"]   # 3 hlasy = 3 různé modely
SWING_CHECK_MODELS = ["gemini-3.6-flash"]
SWING_START_EQUITY_FALLBACK_EUR = 1000.0
SWING_MORNING_PRAGUE = "09:10"   # po ranním přehledu a po resetu kvót Gemini (09:00 Praha)
SWING_EVENING_AFTER_CLOSE_MIN = 30  # večerní kontrola 30 min po zavření US (22:30 Praha)

# Trhy swingu (2. 11. 2026: přidána Evropa). Limity pozic, sektorů a rizika jsou společné pro US i EU.
# morning = ranní sken (Praha), lze ho pustit až do 30 min před zavřením (pozdní start); vstup 15 min po otevření
# nebo hned po pozdním skenu. evening_after_min = večerní správa pozic po zavření trhu.
SWING_MARKETS = {
    "US": {"enabled": True, "morning": "15:00", "evening_after_min": 30, "regime": "SPY",
           "min_adv": 10e6, "min_adv_fallback": None},
    "EU": {"enabled": True, "morning": "09:02", "evening_after_min": 15, "regime": "^STOXX",
           "min_adv": 10e6, "min_adv_fallback": 5e6},   # obrat v EUR; když projde málo akcií, stačí 5 mil.
}
SWING_EU_FALLBACK_MIN_PASS = 30     # pod tolik akcií nad 10 mil. EUR -> použije se 5 mil. EUR
SWING_EU_T212_LETTERS = {"d": "DE", "p": "PA", "a": "AS", "e": "MC", "s": "SW"}  # Xetra, Paříž, Amsterdam, Madrid, Curych (bez Londýna)
