"""Modul SCOUT: Sleduje trhy, detekuje breakouty a aplikuje strategii SFP Jiřího Přibyla.

Podporuje:
- US akcie (NYSE, NASDAQ) v obchodních hodinách 15:30 - 22:00 CET
- Evropské akcie (XETRA, Euronext) v obchodních hodinách 09:00 - 17:30 CET
- Kryptoměny (Binance Futures) 24/7/365
- Ochrana proti plýtvání kredity: mimo obchodní hodiny a o víkendech akcie neskenuje.
"""

from price_utils import round_price, fmt_price
import json
import logging
from datetime import date, datetime
from typing import Optional, Dict, Any, List
import asyncio
import yfinance as yf

from config import (
    PRICE_MOVE_THRESHOLD_PCT,
    VOLUME_SPIKE_RATIO,
    MAX_DAILY_ALERTS_PER_TICKER,
    MAX_DAILY_ALERTS_TOTAL,
    ALERT_TRACKER_FILE,
    DEFAULT_WATCHLIST,
    CRYPTO_WATCHLIST,
)
import binance_client
import market_hours

logger = logging.getLogger("TradingBot.Scout")


class AlertTracker:
    """Sleduje denní limity alertů a ukládá je do alert_tracker.json."""

    def __init__(self, filepath=ALERT_TRACKER_FILE):
        self.filepath = filepath
        self.data = self._load()

    def _get_today_str(self) -> str:
        return date.today().isoformat()

    def _load(self) -> Dict[str, Any]:
        today_str = self._get_today_str()
        if self.filepath.exists():
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    content = json.load(f)
                    if content.get("date") == today_str:
                        return content
            except Exception as e:
                logger.error(f"Chyba při čtení alert trackeru: {e}")

        return {
            "date": today_str,
            "total_alerts": 0,
            "tickers_alerted": {}
        }

    def _save(self) -> None:
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Chyba při ukládání alert trackeru: {e}")

    def _check_and_reset_if_new_day(self) -> None:
        today_str = self._get_today_str()
        if self.data.get("date") != today_str:
            self.data = {
                "date": today_str,
                "total_alerts": 0,
                "tickers_alerted": {}
            }
            self._save()

    def can_alert(self, ticker: str) -> bool:
        """Zjistí, zda ticker může dnes dostat alert."""
        self._check_and_reset_if_new_day()
        if self.data["total_alerts"] >= MAX_DAILY_ALERTS_TOTAL:
            return False
        ticker_upper = ticker.upper()
        ticker_count = self.data["tickers_alerted"].get(ticker_upper, 0)
        return ticker_count < MAX_DAILY_ALERTS_PER_TICKER

    def register_alert(self, ticker: str) -> None:
        """Zaznamená odeslání alertu."""
        self._check_and_reset_if_new_day()
        ticker_upper = ticker.upper()
        self.data["total_alerts"] = self.data.get("total_alerts", 0) + 1
        self.data["tickers_alerted"][ticker_upper] = self.data["tickers_alerted"].get(ticker_upper, 0) + 1
        self._save()

    def get_status(self) -> Dict[str, Any]:
        """Vrátí aktuální denní statistiku."""
        self._check_and_reset_if_new_day()
        return {
            "date": self.data["date"],
            "total_alerts": self.data["total_alerts"],
            "max_total": MAX_DAILY_ALERTS_TOTAL,
            "tickers_alerted": list(self.data["tickers_alerted"].keys()),
            "remaining_alerts": max(0, MAX_DAILY_ALERTS_TOTAL - self.data["total_alerts"])
        }


alert_tracker = AlertTracker()


def fetch_crypto_data_sync(symbol: str) -> Optional[Dict[str, Any]]:
    """Stažení dat o kryptoměně z Binance Futures a aplikace SFP strategie Jiřího Přibyla."""
    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol = f"{symbol}USDT"

    try:
        # 1. Stažení 1h svíček z Binance
        klines = binance_client.get_klines_sync(symbol, interval="1h", limit=25)
        if not klines or len(klines) < 2:
            # Fallback na yfinance např. BTC-USD
            yf_sym = symbol.replace("USDT", "-USD")
            t = yf.Ticker(yf_sym)
            h = t.history(period="3d", interval="1h")
            if h.empty:
                return None
            price_now = float(h["Close"].iloc[-1])
            price_prev = float(h["Close"].iloc[-2])
            vol_now = float(h["Volume"].iloc[-1])
            vol_avg = float(h["Volume"].mean())
        else:
            # Průměr objemu z DOKONČENÝCH hodinových svíček
            vols = [float(k[5]) for k in klines[-21:-1]]
            vol_avg = sum(vols) / len(vols) if vols else float(klines[-1][5])
            # Pohyb a objem za skutečných posledních 60 minut (1min svíčky). Dřív se bral objem
            # rozjeté hodinové svíčky (např. jen 13 minut) proti celým hodinám -> objem vycházel ~4x nižší.
            klines_1m = binance_client.get_klines_sync(symbol, interval="1m", limit=61)
            if klines_1m and len(klines_1m) >= 61:
                price_now = float(klines_1m[-1][4])
                price_prev = float(klines_1m[0][4])
                vol_now = sum(float(k[5]) for k in klines_1m[1:])
            else:
                price_now = float(klines[-1][4])
                price_prev = float(klines[-2][4])
                vol_now = float(klines[-1][5])

        price_move_pct = ((price_now - price_prev) / price_prev) * 100 if price_prev > 0 else 0.0
        volume_ratio = vol_now / vol_avg if vol_avg > 0 else 1.0

        # 2. Detekce SFP (Swing Failure Pattern) dle Přibyla
        sfp_data = binance_client.detect_sfp_pattern_sync(symbol)
        sfp_detected = sfp_data.get("pattern_detected", False)

        # 3. Zprávy pro krypto
        headlines = []
        try:
            yf_crypto_sym = symbol.replace("USDT", "-USD")
            s = yf.Search(yf_crypto_sym, news_count=5)
            if hasattr(s, "news") and s.news:
                for item in s.news[:5]:
                    if item.get("title") and item.get("title") not in headlines:
                        headlines.append(item.get("title"))
        except Exception:
            pass

        # Podmínky spuštění: SFP formace dle Přibyla NEBO 1h pohyb > 3% NEBO volume > 2x
        price_cond = abs(price_move_pct) >= PRICE_MOVE_THRESHOLD_PCT
        vol_cond = volume_ratio >= VOLUME_SPIKE_RATIO
        triggered = sfp_detected or price_cond or vol_cond

        trigger_reasons = []
        if sfp_detected:
            sw_val = sfp_data.get('swing_low_15m', sfp_data.get('swing_low_4h', 0.0))
            trigger_reasons.append(f"🎯 SFP Bullish Reversal: knot pod 15M Swing Low (${fmt_price(sw_val)}) s absorpcí objemu")
        if price_cond:
            trigger_reasons.append(f"1h pohyb ceny: {price_move_pct:+.2f}%")
        if vol_cond:
            trigger_reasons.append(f"Volume spike: {volume_ratio:.2f}x 20D průměru")

        return {
            "ticker": symbol,
            "market_type": "CRYPTO",
            "price": round_price(price_now),
            "price_1h_ago": round_price(price_prev),
            "price_move_pct": round(price_move_pct, 2),
            "current_volume": int(vol_now),
            "avg_20d_volume": int(vol_avg),
            "volume_ratio": round(volume_ratio, 2),
            "headlines": headlines[:5],
            "sfp_detected": sfp_detected,
            "sfp_data": sfp_data,
            "triggered": triggered,
            "trigger_reasons": trigger_reasons,
            "timestamp": datetime.now().isoformat()
        }

    except Exception as e:
        logger.error(f"Chyba při stahování krypto dat pro {symbol}: {e}")
        return None


def _elapsed_session_fraction(ticker: str, last_bar_ts) -> float:
    """Jaká část dnešní seance už uplynula (0.25 - 1.0). Mimo seanci nebo pro starší data vrací 1.0.

    Spodní mez 0.25: objem je po otevření nadprůměrně vysoký, lineární přepočet by ho v první hodině nafukoval.
    """
    market = market_hours.market_for_ticker(ticker)
    if not market:
        return 1.0
    bounds = market_hours.session_bounds(market)
    if not bounds:
        return 1.0
    open_dt, close_dt = bounds
    try:
        if last_bar_ts.date() != open_dt.date():
            return 1.0  # poslední denní svíčka není dnešní
    except Exception:
        return 1.0
    now = datetime.now(open_dt.tzinfo)
    if now <= open_dt or now >= close_dt:
        return 1.0
    fraction = (now - open_dt).total_seconds() / (close_dt - open_dt).total_seconds()
    return min(1.0, max(0.25, fraction))


def fetch_stock_data_sync(ticker: str) -> Optional[Dict[str, Any]]:
    """Synchronní stažení dat z yfinance pro US a evropské akcie."""
    try:
        t = yf.Ticker(ticker)

        # 1. Hodinová historie pro 1h pohyb
        hist_1h = t.history(period="5d", interval="1h")
        if hist_1h.empty or len(hist_1h) < 2:
            return None

        price_now = float(hist_1h["Close"].iloc[-1])
        price_1h_ago = float(hist_1h["Close"].iloc[-2])
        price_move_pct = ((price_now - price_1h_ago) / price_1h_ago) * 100

        # 2. Denní historie pro 20denní průměrný objem
        hist_daily = t.history(period="1mo", interval="1d")
        if hist_daily.empty:
            return None

        if len(hist_daily) >= 21:
            avg_20d_volume = float(hist_daily["Volume"].iloc[-21:-1].mean())
        elif len(hist_daily) > 1:
            avg_20d_volume = float(hist_daily["Volume"].iloc[:-1].mean())
        else:
            avg_20d_volume = float(hist_daily["Volume"].iloc[0])

        current_volume = float(hist_daily["Volume"].iloc[-1])
        # Během seance je dnešní objem jen částečný -> porovnáme ho s průměrem přepočteným na uplynulý
        # čas seance (dřív se půlden porovnával s celým dnem a objemový spouštěč skoro nemohl zabrat).
        session_fraction = _elapsed_session_fraction(ticker, hist_daily.index[-1])
        expected_volume = avg_20d_volume * session_fraction
        volume_ratio = current_volume / expected_volume if expected_volume > 0 else 1.0

        # 3. Zprávy (headlines)
        headlines = []
        try:
            if hasattr(t, "news") and t.news:
                for item in t.news[:5]:
                    title = item.get("title") or (item.get("content", {}).get("title") if isinstance(item.get("content"), dict) else None)
                    if title and title not in headlines:
                        headlines.append(title)
        except Exception:
            pass

        if len(headlines) < 3:
            try:
                search_res = yf.Search(ticker, news_count=5)
                if hasattr(search_res, "news") and search_res.news:
                    for item in search_res.news:
                        title = item.get("title")
                        if title and title not in headlines:
                            headlines.append(title)
            except Exception:
                pass

        # 4. Scout podmínky
        price_condition = abs(price_move_pct) >= PRICE_MOVE_THRESHOLD_PCT
        volume_condition = volume_ratio >= VOLUME_SPIKE_RATIO
        triggered = price_condition or volume_condition

        trigger_reasons = []
        if price_condition:
            trigger_reasons.append(f"1h pohyb ceny: {price_move_pct:+.2f}%")
        if volume_condition:
            trigger_reasons.append(f"Volume spike: {volume_ratio:.2f}x 20D průměru")

        market_type = "STOCK_EU" if ".DE" in ticker or ".AS" in ticker or ".PA" in ticker else "STOCK_US"

        return {
            "ticker": ticker.upper(),
            "market_type": market_type,
            "price": round_price(price_now),
            "price_1h_ago": round_price(price_1h_ago),
            "price_move_pct": round(price_move_pct, 2),
            "current_volume": int(current_volume),
            "avg_20d_volume": int(avg_20d_volume),
            "volume_ratio": round(volume_ratio, 2),
            "headlines": headlines[:5],
            "price_condition": price_condition,
            "volume_condition": volume_condition,
            "triggered": triggered,
            "trigger_reasons": trigger_reasons,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"Chyba při stahování dat pro akcii {ticker}: {e}")
        return None


async def inspect_ticker(ticker: str) -> Optional[Dict[str, Any]]:
    """Univerzální asynchronní obal pro kontrolu akcie nebo kryptoměny."""
    t_clean = ticker.upper().strip()
    is_crypto = t_clean.endswith("USDT") or t_clean in ["BTC", "ETH", "SOL", "BNB"] or t_clean in CRYPTO_WATCHLIST
    if is_crypto:
        return await asyncio.to_thread(fetch_crypto_data_sync, t_clean)
    else:
        return await asyncio.to_thread(fetch_stock_data_sync, t_clean)


async def scan_active_markets() -> List[Dict[str, Any]]:
    """Projde POUZE ty trhy, které jsou v aktuální čas v Praze otevřeny.

    Chrání API kredity Gemini před plýtváním mimo obchodní hodiny.
    """
    active_market_lists = market_hours.get_active_markets_and_watchlists()
    triggered_assets = []

    for market_name, market_type, tickers in active_market_lists:
        logger.info(f"🔍 Skenuji aktivní trh: {market_name} ({len(tickers)} instrumentů)...")
        for sym in tickers:
            if not alert_tracker.can_alert(sym):
                continue
            data = await inspect_ticker(sym)
            if data and data.get("triggered"):
                triggered_assets.append(data)

    return triggered_assets
