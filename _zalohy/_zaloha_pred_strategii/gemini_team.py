"""Modul pro 5 analytických rolí: NEWS, CHART, SKEPTIK, SIZER a AUTONOMNÍ EXECUTOR.

Provádí jeden strukturovaný Gemini API call přes balíček `google-genai`
s MIME typem application/json a aplikuje přísná validační pravidla:
1. News: 2 věty vysvětlení pohybu nebo "Žádný jasný důvod".
2. Chart: Entry, Stop-Loss (max 2 % pod entry), Take-Profit (min RRR 1:2).
3. Skeptik: 3 důvody proti. Pokud News nenašel důvod, verdikt MUSÍ BÝT "PRESKOČIŤ".
4. Sizer: Pokud KÚPIŤ a otevřené pozice < 3, alokace max 5 % z 10 000 USD (500 USD).
5. Executor: Finální vyhodnocení a autonomní rozhodnutí, zda do obchodu vstoupit.
"""

from price_utils import round_price, fmt_price
import json
import asyncio
import logging
from typing import Dict, Any, List, Optional
from google import genai
from google.genai import types
from google.genai.errors import ClientError

from config import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    FICTITIOUS_CAPITAL,
    MAX_ALLOCATION_PCT,
    MAX_OPEN_POSITIONS,
    MAX_OPEN_POSITIONS_STOCKS,
    MAX_OPEN_POSITIONS_CRYPTO,
    CRYPTO_WATCHLIST
)
from paper_trading import (
    get_open_positions_count,
    get_positions_limit_info
)
from model_manager import quota_manager

logger = logging.getLogger("TradingBot.GeminiTeam")

# Inicializace klienta
_client: Optional[genai.Client] = None


def get_gemini_client() -> genai.Client:
    """Získá nebo inicializuje instanci klienta google-genai."""
    global _client
    if _client is None:
        if not GEMINI_API_KEY:
            raise ValueError("GEMINI_API_KEY není nastavena v .env ani v prostředí!")
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


def _build_prompt(
    ticker: str,
    price: float,
    price_move_pct: float,
    volume_ratio: float,
    headlines: List[str],
    open_positions: int,
    max_positions: int,
    platform_name: str,
    is_crypto: bool
) -> str:
    """Sestaví prompt pro jeden strukturovaný Gemini API call s autonomním Executorem."""
    headlines_text = "\n".join(f"- {h}" for h in headlines) if headlines else "Žádné aktuální zprávy nejsou k dispozici."

    if is_crypto:
        skeptik_rule = (
            "   - PRAVIDLO PRO KRYPTOMĚNY (Binance Futures & 15M Daytrading):\n"
            "     Pohyby na kryptu jsou často poháněny čistě technickou likviditou a odrazem od 15M dna (SFP). "
            "Pokud News Analyst nenašel zprávu a uvedl 'Žádný jasný důvod', ale Chart má příznivý poměr RRR >= 1:2 "
            "a svíčka potvrdila odraz od dna/supportu, MŮŽEŠ doporučit 'KÚPIŤ'. 'PRESKOČIŤ' zvol pouze při vysokém technickém riziku (např. nákup pod rezistencí)."
        )
    else:
        skeptik_rule = (
            "   - PRAVIDLO PRO AKCIE (Trading 212):\n"
            "     Pokud News Analyst nenašel jasný fundament a uvedl 'Žádný jasný důvod', verdikt MUSÍ BÝT STRIKTNĚ 'PRESKOČIŤ'."
        )

    return f"""Jsi profesionální autonomní trading tým složený z těchto 5 rolí:
1. NEWS ANALYST:
   - Analyzuje dostupné titulky zpráv a vysvětlí důvod pohybu ceny PŘESNĚ VE 2 VĚTÁCH.
   - Pokud ze zpráv není jasný fundamentální katalyzátor pro pohyb ceny, napiš: "Žádný jasný důvod".

2. CHART ANALYST:
   - Vypočte nákupní Entry cenu (kolem aktuální ceny {fmt_price(price)} USD).
   - Vypočte Stop-Loss (SL): PŘÍSNÉ PRAVIDLO: Stop-Loss smí být MAXIMÁLNĚ 2 % pod Entry (tedy Entry * 0.98 nebo těsnější).
   - Vypočte Take-Profit (TP): PŘÍSNÉ PRAVIDLO: Minimální poměr Risk to Reward (RRR) musí být 1:2 (tedy zisk TP - Entry musí být alespoň dvojnásobek risku Entry - SL).
   - Uvede vypočtený poměr RRR jako text (např. "1:2.2").

3. SKEPTIK:
   - Najde přesně 3 věcné důvody PROTI tomuto obchodu (rizika, resistence, překoupenost, makro, sektorová rizika).
   - Rozhodne verdikt: "KÚPIŤ", "POČKAŤ", nebo "PRESKOČIŤ".
{skeptik_rule}

4. SIZER:
   - Celkový fiktivní kapitál je {FICTITIOUS_CAPITAL:.0f} USD.
   - Maximální alokace na 1 obchod je {MAX_ALLOCATION_PCT:.0f} % z kapitálu (= {FICTITIOUS_CAPITAL * (MAX_ALLOCATION_PCT / 100.0):.0f} USD).
   - Platforma: {platform_name}. Limit otevřených pozic pro tuto platformu je {max_positions} (aktuálně otevřeno: {open_positions}/{max_positions}).
   - Pokud je verdikt "KÚPIŤ" a počet otevřených pozic na této platformě je méně než {max_positions}: alokuje max {FICTITIOUS_CAPITAL * (MAX_ALLOCATION_PCT / 100.0):.0f} USD.
   - Pokud je limit {max_positions} otevřených pozic již dosažen NEBO verdikt není "KÚPIŤ", alokace je 0 USD.

5. AUTONOMNÍ EXECUTOR:
   - Prodiskutuje všechny argumenty (fundament od News, technický poměr RRR od Chartu, výhrady od Skeptika a limity Sizeru).
   - Zhodnotí, zda je v tuto chvíli dobrý nápad obchod samostatně provést bez schvalování člověkem.
   - Určí:
     - "should_execute": bool (true POUZE pokud Skeptik doporučil KÚPIŤ a riziko je přijatelné)
     - "confidence": int (1-100 %, míra jistoty týmu)
     - "reasoning": "Shrnutí v 1-2 větách, proč tým obchod provádí nebo vynechává."

VSTUPNÍ DATA:
- Ticker: {ticker}
- Platforma: {platform_name}
- Aktuální tržní cena: {fmt_price(price)} USD
- 1hodinový pohyb ceny: {price_move_pct:+.2f}%
- Volume spike: {volume_ratio:.2f}x 20denního průměrného objemu
- Počet otevřených pozic pro {platform_name}: {open_positions} / {max_positions}
- Dostupné zprávy z trhu:
{headlines_text}

Odpověz POUZE validním JSON objektem v tomto přesném formátu:
{{
  "news": "Vysvětlení ve 2 větách NEBO přesně 'Žádný jasný důvod'",
  "chart": {{
    "entry": float,
    "stop_loss": float,
    "take_profit": float,
    "rrr": "1:X.X"
  }},
  "skeptik": {{
    "reasons": [
      "První důvod proti obchodu",
      "Druhý důvod proti obchodu",
      "Třetí důvod proti obchodu"
    ],
    "verdict": "KÚPIŤ"
  }},
  "sizer": {{
    "capital": {FICTITIOUS_CAPITAL:.0f},
    "allocation_pct": float,
    "allocated_usd": float,
    "shares": int,
    "open_positions": {open_positions},
    "max_positions": {max_positions},
    "can_open": bool,
    "note": "Komentář k pozici a velikosti"
  }},
  "executor": {{
    "should_execute": bool,
    "confidence": int,
    "reasoning": "Důvod finálního rozhodnutí"
  }}
}}
"""


async def analyze_with_gemini(
    ticker: str,
    price: float,
    price_move_pct: float,
    volume_ratio: float,
    headlines: List[str]
) -> Dict[str, Any]:
    """Zavolá Gemini API a aplikuje striktní validační pravidla zadaná v promptu."""
    client = get_gemini_client()
    limit_info = get_positions_limit_info(ticker)
    open_positions = limit_info["count"]
    max_positions = limit_info["limit"]
    platform_name = limit_info["platform"]
    is_crypto = limit_info["market_type"] == "CRYPTO"

    prompt = _build_prompt(
        ticker=ticker,
        price=price,
        price_move_pct=price_move_pct,
        volume_ratio=volume_ratio,
        headlines=headlines,
        open_positions=open_positions,
        max_positions=max_positions,
        platform_name=platform_name,
        is_crypto=is_crypto
    )

    candidate_models = quota_manager.get_candidate_models()
    raw_response_text = ""
    used_model_name = ""
    last_error = None

    for m_info in candidate_models:
        model_name = m_info["id"]
        try:
            logger.info(f"Volám model {model_name} pro {ticker}...")
            config = types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.2,
                thinking_config=types.ThinkingConfig(thinking_budget=0)
            )

            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=config
                ),
                timeout=14.0
            )
            raw_response_text = response.text or ""
            if raw_response_text:
                used_model_name = model_name
                quota_manager.record_call_success(model_name)
                logger.info(f"✅ Model {model_name} úspěšně odpověděl pro {ticker}!")
                break
        except asyncio.TimeoutError:
            logger.warning(f"⚠️ Model {model_name} neodpověděl do 14s (rate limit / timeout). Přepínám na další model v kaskádě...")
            quota_manager.record_rate_limit(model_name, cooldown_minutes=15)
            continue
        except ClientError as ce:
            err_str = str(ce)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                logger.warning(f"⚠️ Model {model_name} vrátil 429 Rate Limit. Zaznamenávám dočasný cooldown a přepínám na další model...")
                quota_manager.record_rate_limit(model_name, cooldown_minutes=15)
            else:
                logger.warning(f"⚠️ Model {model_name} vrátil ClientError: {ce}. Zkouším další model...")
            last_error = ce
            continue
        except Exception as e:
            logger.warning(f"Chyba při volání modelu {model_name}: {e}. Zkouším další model...")
            last_error = e
            continue

    if not raw_response_text:
        raise RuntimeError(f"Nepodařilo se získat odpověď z žádného modelu Gemini v kaskádě: {last_error}")

    try:
        data = json.loads(raw_response_text)
    except json.JSONDecodeError as jde:
        logger.error(f"Nevalidní JSON z Gemini ({used_model_name}): {raw_response_text}")
        raise RuntimeError(f"Gemini ({used_model_name}) nevrátil platný JSON: {jde}")

    data["model_used"] = used_model_name

    # ==========================================
    # PROGRAMOVÁ KONTROLA A VYNUCOVÁNÍ PRAVIDEL
    # ==========================================
    news_text = str(data.get("news", "")).strip()
    chart = data.get("chart", {})
    skeptik = data.get("skeptik", {})
    sizer = data.get("sizer", {})
    executor = data.get("executor", {})

    entry = float(chart.get("entry", price))
    if entry <= 0:
        entry = price

    # Pravidlo Chart: Stop-Loss max 2 % pod entry
    max_sl = round_price(entry * 0.98)
    sl = float(chart.get("stop_loss", max_sl))
    if sl < max_sl or sl >= entry:
        sl = max_sl
    chart["stop_loss"] = sl

    # Pravidlo Chart: Take-Profit minimálně RRR 1:2
    risk = max(entry - sl, entry * 0.001)  # relativní minimum, ne pevný 1 cent (u DOGE by to bylo 5 %)
    min_tp = round_price(entry + (risk * 2.0))
    tp = float(chart.get("take_profit", min_tp))
    if tp < min_tp:
        tp = min_tp
    chart["take_profit"] = tp
    reward = tp - entry
    actual_rrr = f"1:{reward / risk:.1f}"
    chart["rrr"] = actual_rrr

    # Pravidlo Skeptik: U akcií vyžadujeme fundament, u krypta povolujeme technické SFP/daytrading setupy (Varianta B)
    no_reason_markers = ["žádný jasný důvod", "zadny jasny duvod", "no clear reason", "žádný zjevný důvod"]
    news_lower = news_text.lower()
    is_no_reason = any(m in news_lower for m in no_reason_markers) or not news_text

    raw_verdict = str(skeptik.get("verdict", "PRESKOČIŤ")).upper()
    if is_no_reason and not is_crypto:
        verdikt = "PRESKOČIŤ"
        skeptik["note"] = "Verdikt u akcie vynucen na PRESKOČIŤ, protože News nenašel jasný fundament."
    elif raw_verdict in ["KÚPIŤ", "KUPIT", "BUY"]:
        verdikt = "KÚPIŤ"
    elif raw_verdict in ["POČKAŤ", "POCKAT", "WAIT", "HOLD"]:
        verdikt = "POČKAŤ"
    else:
        verdikt = "PRESKOČIŤ"
    skeptik["verdict"] = verdikt

    reasons = skeptik.get("reasons", [])
    if not isinstance(reasons, list) or len(reasons) < 3:
        reasons = [
            "Zvýšená volatilita po prudkém pohybu může vyvolat výběr zisků.",
            "Možné vyčerpání nákupního tlaku u technických rezistencí.",
            "Riziko obecného obratu nálady na trhu a indexech."
        ]
    skeptik["reasons"] = reasons[:3]

    # Pravidlo Sizer: Alokace max 5 % z 10 000 USD (500 USD), limit 5 pozic pro danou platformu (5 T212, 5 Binance)
    max_capital_allocation = FICTITIOUS_CAPITAL * (MAX_ALLOCATION_PCT / 100.0)  # 500 USD
    can_open = limit_info["can_open"]

    if not can_open:
        allocated_usd = 0.0
        shares = 0
        sizer_note = f"Limit {max_positions} otevřených pozic pro {platform_name} byl naplněn. Nelze otevřít další obchod."
    elif verdikt == "KÚPIŤ":
        allocated_usd = max_capital_allocation
        shares = int(allocated_usd / entry) if entry > 0 else 0
        sizer_note = f"Alokováno 5 % kapitálu ({allocated_usd:.0f} USD) na {platform_name} ({open_positions + 1}/{max_positions})."
    else:
        allocated_usd = 0.0
        shares = 0
        sizer_note = f"Verdikt je {verdikt}. Žádný kapitál nebyl alokován."

    sizer["capital"] = FICTITIOUS_CAPITAL
    sizer["allocation_pct"] = MAX_ALLOCATION_PCT if can_open and verdikt == "KÚPIŤ" else 0.0
    sizer["allocated_usd"] = allocated_usd
    sizer["shares"] = shares
    sizer["open_positions"] = open_positions
    sizer["max_positions"] = max_positions
    sizer["platform"] = platform_name
    sizer["can_open"] = can_open
    sizer["note"] = sizer_note

    # Pravidlo Autonomní Executor:
    # Obchod se provede pouze tehdy, když verdikt je KÚPIŤ a slot pro platformu je volný (< 5)
    raw_should_exec = bool(executor.get("should_execute", False))
    confidence = int(executor.get("confidence", 75))
    reasoning = str(executor.get("reasoning", "")).strip()

    if verdikt != "KÚPIŤ" or not can_open:
        should_execute = False
        if not can_open:
            reasoning = f"Nákup neproveden: Dosažen limit {max_positions} otevřených pozic pro {platform_name}."
        elif verdikt != "KÚPIŤ":
            reasoning = f"Nákup neproveden: Skeptik doporučil {verdikt}."
    else:
        should_execute = raw_should_exec or (verdikt == "KÚPIŤ" and can_open)
        if not reasoning:
            reasoning = f"LLM tým zhodnotil příznivý poměr RRR a technický setup pro nákup na {platform_name}."

    executor["should_execute"] = should_execute
    executor["confidence"] = confidence
    executor["reasoning"] = reasoning

    return {
        "ticker": ticker.upper(),
        "price": price,
        "price_move_pct": price_move_pct,
        "volume_ratio": volume_ratio,
        "news": news_text,
        "chart": chart,
        "skeptik": skeptik,
        "sizer": sizer,
        "executor": executor,
        "model_used": used_model_name
    }
