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
MAX_DAILY_ALERTS_TOTAL = 30      # Max 30 alertů denně celkem
SCOUT_INTERVAL_MINUTES = 5.0     # Periodická smyčka každých 5 minut

# Pravidla Sizer & Portfolio (dle Jiřího Přibyla)
FICTITIOUS_CAPITAL = 10000.0     # Fiktivní kapitál 10 000 USD
MAX_ALLOCATION_PCT = 5.0         # Max 5 % z kapitálu (= 500 USD)
MAX_OPEN_POSITIONS_STOCKS = 10   # Max 10 otevřených pozic na Trading 212 (akcie)
MAX_OPEN_POSITIONS_CRYPTO = 10   # Max 10 krypto longů najednou (uživatel zvýšil z 2 na 10 dne 26.10.; pozor: altcoiny se hýbou s BTC, 90denní korelace ETH 0,88, SOL 0,83)
MAX_OPEN_POSITIONS = 20          # Celkem max 20 pozic (10 T212 + 10 Binance)
RISK_PER_TRADE_PCT = 1.0         # 1 % risk z kapitálu na obchod (100 USD)

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

# Datové soubory
PAPER_TRADES_FILE = BASE_DIR / "paper_trades.json"
ALERT_TRACKER_FILE = BASE_DIR / "alert_tracker.json"
