"""Stopa „Stará metoda" pro krypto – POUZE PAPER, srovnání s živým HIGH20.

Jak vybírala obchody původní krypto metoda (24.–29. 10. 2026, živě na Binance testnetu):
- Spouštěč (Scout, každých 5 min): 1h pohyb ceny |≥ PRICE_MOVE_THRESHOLD_PCT| NEBO objem ≥ VOLUME_SPIKE_RATIO
  NEBO potvrzené SFP na 15M.
- Rozhodnutí: jeden Gemini prompt s 5 rolemi (News, Chart, Skeptik, Sizer, Executor). Nakupuje se jen při
  verdiktu Skeptika KÚPIŤ a souhlasu Executora. Entry = tržní cena, SL max 2 % pod entry, TP 2–3R
  (u SFP platí SL/TP ze setupu). Pevná alokace 500 USD na obchod.
- Výstup: Stop-Loss nebo Take-Profit (žádný časový výstup, jen pojistka CRYPTO_OLD_MAX_HOLD_HOURS).

Prompt je beze změny převzatý ze zálohy _zalohy/_zaloha_pred_gemini_vstupy/gemini_team.py, aby šlo srovnání
férově. Jediné rozdíly: volá jen modely Flash Lite (silné modely zůstávají swingu a Scoutovi) a má denní strop volání.

Žádné příkazy na burzu. Vlastní evidence CRYPTO_OLD_TRADES_FILE, nepočítá se do limitů živých pozic ani risk_guard.
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from price_utils import round_price, fmt_price
from config import (
    BASE_DIR,
    CRYPTO_WATCHLIST,
    KILL_SWITCH_FILE,
    PRICE_MOVE_THRESHOLD_PCT,
    VOLUME_SPIKE_RATIO,
)

logger = logging.getLogger("TradingBot.CryptoOld")

STRATEGY = "STARA_METODA"
ENABLED = True
TRADES_FILE = BASE_DIR / "paper_crypto_old.json"
STATE_FILE = BASE_DIR / "crypto_old_state.json"
ALLOC_USD = 500.0              # Jako dřív: 5 % z fiktivních 10 000 USD
MAX_POSITIONS = 10             # Jako dřív MAX_OPEN_POSITIONS_CRYPTO
MAX_GEMINI_CALLS_PER_DAY = 60  # Strop, ať stopa nesní kvótu Flash Lite ostatním
TICKER_COOLDOWN_MIN = 60       # Jako ALERT_TICKER_COOLDOWN_MIN: stejný ticker k Gemini nejdřív po hodině
MAX_HOLD_HOURS = 7 * 24        # Pojistka, ať pozice neblokuje slot donekonečna
FEE_PCT = 0.05                 # Binance futures taker za stranu
SLIPPAGE_PCT = 0.02            # Skluz za stranu
MAX_SL_PCT = 2.0               # Původní pravidlo Chartu: SL max 2 % pod entry
MAX_TP_R = 3.0
GEMINI_COMPONENT = "crypto_old"


# =====================================================================
# ČISTÉ FUNKCE (bez API, kvůli testům)
# =====================================================================

def old_trigger(data: Dict[str, Any]) -> List[str]:
    """Původní spouštěč: SFP NEBO |1h pohyb| ≥ 3 % NEBO objem ≥ 2×. Vrací důvody (prázdné = bez signálu)."""
    reasons = []
    if data.get("sfp_detected"):
        reasons.append("SFP 15M")
    move = float(data.get("price_move_pct") or 0.0)
    if abs(move) >= PRICE_MOVE_THRESHOLD_PCT:
        reasons.append(f"1h pohyb {move:+.2f} %")
    vol = float(data.get("volume_ratio") or 0.0)
    if vol >= VOLUME_SPIKE_RATIO:
        reasons.append(f"objem {vol:.2f}x")
    return reasons


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def enforce_chart_levels(price: float, chart: Dict[str, Any]) -> Dict[str, Any]:
    """Stejná pravidla jako původní metoda: entry = trh, SL max 2 % pod entry, TP 2R až 3R."""
    chart = dict(chart) if isinstance(chart, dict) else {}
    entry = round_price(price)
    chart["entry"] = entry
    max_sl = round_price(entry * (1 - MAX_SL_PCT / 100.0))
    sl = _to_float(chart.get("stop_loss"), max_sl)
    if sl < max_sl or sl >= entry:
        sl = max_sl
    chart["stop_loss"] = sl
    risk = max(entry - sl, entry * 0.001)
    min_tp = round_price(entry + risk * 2.0)
    max_tp = round_price(entry + risk * MAX_TP_R)
    tp = min(max(_to_float(chart.get("take_profit"), min_tp), min_tp), max_tp)
    chart["take_profit"] = tp
    chart["rrr"] = f"1:{(tp - entry) / risk:.1f}"
    return chart


def decide(data: Dict[str, Any], price: float, setup: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Původní programová kontrola odpovědi Gemini: koupit jen při KÚPIŤ + souhlasu Executora."""
    chart = enforce_chart_levels(price, data.get("chart") or {})
    if setup:
        try:
            from strategy_rules import apply_setup_levels
            lv = apply_setup_levels(price, setup)
            chart.update(entry=lv["entry"], stop_loss=lv["stop_loss"], take_profit=lv["take_profit"],
                         rrr=lv["rrr"], source="SFP setup")
            if lv.get("skip_reason"):
                return {"buy": False, "reason": f"SFP: {lv['skip_reason']}", "chart": chart, "verdict": "PRESKOČIŤ"}
        except Exception as e:
            logger.warning(f"Stará metoda: SFP úrovně nejdou spočítat ({e}), beru úrovně z Chartu")
    raw = str((data.get("skeptik") or {}).get("verdict", "PRESKOČIŤ")).upper()
    if raw in ("KÚPIŤ", "KUPIT", "BUY"):
        verdict = "KÚPIŤ"
    elif raw in ("POČKAŤ", "POCKAT", "WAIT", "HOLD"):
        verdict = "POČKAŤ"
    else:
        verdict = "PRESKOČIŤ"
    executor = data.get("executor") or {}
    if verdict != "KÚPIŤ":
        return {"buy": False, "reason": f"Skeptik: {verdict}", "chart": chart, "verdict": verdict}
    if not bool(executor.get("should_execute", False)):
        return {"buy": False, "reason": f"Executor zamítl: {str(executor.get('reasoning', ''))[:160]}",
                "chart": chart, "verdict": verdict}
    return {"buy": True, "reason": str(executor.get("reasoning", ""))[:300], "chart": chart, "verdict": verdict}


def trade_pnl(entry: float, exit_price: float, qty: float, stop_loss: float) -> Dict[str, float]:
    """P&L long obchodu po poplatcích a skluzu (obojí za stranu). R z původního rizika entry − SL."""
    fee, slip = FEE_PCT / 100.0, SLIPPAGE_PCT / 100.0
    buy_fill = entry * (1 + slip)
    sell_fill = exit_price * (1 - slip)
    fees = qty * (buy_fill + sell_fill) * fee
    pnl = qty * (sell_fill - buy_fill) - fees
    risk_usd = qty * (entry - stop_loss)
    return {"fees_usd": round(fees, 4), "pnl_usd": round(pnl, 4),
            "pnl_pct": round(pnl / (qty * entry) * 100, 3) if qty * entry > 0 else 0.0,
            "r": round(pnl / risk_usd, 3) if risk_usd > 0 else 0.0}


def open_positions(trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [t for t in trades if t.get("status") == "OPEN"]


def can_open(trades: List[Dict[str, Any]], symbol: str) -> Optional[str]:
    opened = open_positions(trades)
    if any(t["ticker"] == symbol for t in opened):
        return "pozice na symbolu už je otevřená"
    if len(opened) >= MAX_POSITIONS:
        return f"limit {MAX_POSITIONS} pozic"
    return None


def new_trade(symbol: str, chart: Dict[str, Any], now: datetime, triggers: List[str],
              data: Dict[str, Any], model: str) -> Optional[Dict[str, Any]]:
    entry, sl, tp = float(chart["entry"]), float(chart["stop_loss"]), float(chart["take_profit"])
    if not (0 < sl < entry < tp):
        return None
    qty = ALLOC_USD / entry
    return {
        "trade_id": f"OLD-{now.strftime('%Y%m%d%H%M%S')}-{symbol}-{uuid.uuid4().hex[:4]}",
        "strategy": STRATEGY,
        "ticker": symbol,
        "status": "OPEN",
        "paper_only": True,
        "entry": entry,
        "stop_loss": sl,
        "take_profit": tp,
        "rrr": chart.get("rrr"),
        "qty": qty,
        "allocated_usd": ALLOC_USD,
        "risk_usd": round(qty * (entry - sl), 4),
        "triggers": triggers,
        "news": data.get("news"),
        "skeptik_reasons": (data.get("skeptik") or {}).get("reasons"),
        "reasoning": (data.get("executor") or {}).get("reasoning"),
        "confidence": (data.get("executor") or {}).get("confidence"),
        "model": model,
        "opened_at": now.isoformat(),
        "opened_ms": int(now.timestamp() * 1000),
    }


def close_trade(trade: Dict[str, Any], price: float, reason: str, now: datetime) -> Dict[str, Any]:
    trade.update({"status": "CLOSED", "exit_price": price, "exit_reason": reason, "closed_at": now.isoformat(),
                  **trade_pnl(trade["entry"], price, trade["qty"], trade["stop_loss"])})
    return trade


def exit_for_bars(trade: Dict[str, Any], bars: List[List[Any]], now: datetime) -> Optional[tuple]:
    """(cena, důvod) podle 1m svíček od otevření. Když v jedné svíčce padne SL i TP, počítá se SL (konzervativně)."""
    opened_ms = int(trade.get("opened_ms", 0))
    sl, tp = float(trade["stop_loss"]), float(trade["take_profit"])
    for k in bars:
        if int(k[6]) <= opened_ms:
            continue
        low, high = float(k[3]), float(k[2])
        if low <= sl:
            return sl, "Stop-Loss"
        if high >= tp:
            return tp, "Take-Profit"
    opened = datetime.fromisoformat(trade["opened_at"])
    if now - opened >= timedelta(hours=MAX_HOLD_HOURS) and bars:
        return float(bars[-1][4]), f"pojistka {MAX_HOLD_HOURS // 24} dní"
    return None


def summary(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    closed = [t for t in trades if t.get("status") == "CLOSED"]
    wins = sum(1 for t in closed if t.get("pnl_usd", 0) > 0)
    return {"open": len(open_positions(trades)), "closed": len(closed), "wins": wins,
            "win_rate_pct": round(wins / len(closed) * 100, 1) if closed else None,
            "total_r": round(sum(t.get("r", 0) for t in closed), 2),
            "total_usd": round(sum(t.get("pnl_usd", 0) for t in closed), 2)}


# =====================================================================
# EVIDENCE A STAV
# =====================================================================

def _load(path: Path, default: Any) -> Any:
    if not Path(path).exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Chyba při čtení {Path(path).name}: {e}")
        return default


def _save(path: Path, data: Any) -> None:
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


def load_ledger(path: Path = None) -> List[Dict[str, Any]]:
    data = _load(path or TRADES_FILE, [])
    return data if isinstance(data, list) else []


def save_ledger(trades: List[Dict[str, Any]], path: Path = None) -> None:
    _save(path or TRADES_FILE, trades)


def load_state(now: datetime, path: Path = None) -> Dict[str, Any]:
    st = _load(path or STATE_FILE, {})
    if not isinstance(st, dict):
        st = {}
    today = now.date().isoformat()
    if st.get("date") != today:
        st = {"date": today, "gemini_calls": 0, "last_call": st.get("last_call", {}), "signals": 0}
    st.setdefault("last_call", {})
    return st


def save_state(st: Dict[str, Any], path: Path = None) -> None:
    _save(path or STATE_FILE, st)


def cooldown_ok(st: Dict[str, Any], symbol: str, now: datetime) -> bool:
    last = st.get("last_call", {}).get(symbol)
    if not last:
        return True
    try:
        return now - datetime.fromisoformat(last) >= timedelta(minutes=TICKER_COOLDOWN_MIN)
    except ValueError:
        return True


def snapshot_block() -> Dict[str, Any]:
    """Pro web dashboard: souhrn + posledních 50 obchodů."""
    trades = load_ledger()
    return {"enabled": ENABLED, "summary": summary(trades), "trades": trades[-50:],
            "state": _load(STATE_FILE, {})}


# =====================================================================
# GEMINI (původní 5rolový prompt, jen modely Flash Lite)
# =====================================================================

def build_prompt(ticker: str, price: float, price_move_pct: float, volume_ratio: float, headlines: List[str],
                 open_positions_cnt: int, setup: Optional[Dict[str, Any]] = None, market_context: str = "",
                 data_as_of: str = "") -> str:
    """Původní prompt (_zaloha_pred_gemini_vstupy/gemini_team._build_prompt), krypto větev."""
    from config import FICTITIOUS_CAPITAL, MAX_ALLOCATION_PCT
    platform_name = "Binance Futures"
    max_positions = MAX_POSITIONS
    headlines_text = "\n".join(f"- {h}" for h in headlines) if headlines else "Žádné aktuální zprávy nejsou k dispozici."
    skeptik_rule = (
        "   - PRAVIDLO PRO KRYPTOMĚNY (Binance Futures & 15M Daytrading):\n"
        "     Pohyby na kryptu jsou často poháněny čistě technickou likviditou a odrazem od 15M dna (SFP). "
        "Pokud News Analyst nenašel zprávu a uvedl 'Žádný jasný důvod', ale Chart má příznivý poměr RRR >= 1:2 "
        "a svíčka potvrdila odraz od dna/supportu, MŮŽEŠ doporučit 'KÚPIŤ'. 'PRESKOČIŤ' zvol pouze při vysokém technickém riziku (např. nákup pod rezistencí)."
    )
    setup_text = ""
    if setup:
        setup_text = (
            f"- POTVRZENÝ SETUP {setup.get('pattern_name', 'SFP')}: dno ${fmt_price(setup.get('swing_low_15m', 0))} "
            f"proraženo knotem na ${fmt_price(setup.get('candle_low', 0))}, svíčka zavřela zpět na ${fmt_price(setup.get('entry', 0))}, "
            f"objem {setup.get('vol_ratio', 0)}x.\n"
            f"  Stop-Loss ${fmt_price(setup.get('stop_loss', 0))} a Take-Profit ${fmt_price(setup.get('take_profit', 0))} JSOU PEVNĚ DANÉ "
            f"pravidly strategie. Chart je jen opíše, neměň je. Tvým úkolem je obchod potvrdit, nebo zamítnout.\n"
        )
    context_text = ""
    if market_context:
        context_text = (
            "- RANNÍ PŘEHLED TRHU (zprávy, výsledky firem, tiskovky a makro události dne; ber v úvahu, "
            "u výsledků nebo Fedu dnes počítej s vyšší volatilitou):\n" + market_context + "\n"
        )
    cap = FICTITIOUS_CAPITAL * (MAX_ALLOCATION_PCT / 100.0)
    return f"""Jsi profesionální autonomní trading tým složený z těchto 5 rolí:
1. NEWS ANALYST:
   - Analyzuje dostupné titulky zpráv (i ranní přehled trhu, pokud je ve vstupních datech) a vysvětlí důvod pohybu ceny PŘESNĚ VE 2 VĚTÁCH.
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
   - Maximální alokace na 1 obchod je {MAX_ALLOCATION_PCT:.0f} % z kapitálu (= {cap:.0f} USD).
   - Platforma: {platform_name}. Limit otevřených pozic pro tuto platformu je {max_positions} (aktuálně otevřeno: {open_positions_cnt}/{max_positions}).
   - Pokud je verdikt "KÚPIŤ" a počet otevřených pozic na této platformě je méně než {max_positions}: alokuje max {cap:.0f} USD.
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
{f"- Data k {data_as_of}." + chr(10) if data_as_of else ""}- 1hodinový pohyb ceny: {price_move_pct:+.2f}%
- Volume spike: {volume_ratio:.2f}x 20denního průměrného objemu
- Počet otevřených pozic pro {platform_name}: {open_positions_cnt} / {max_positions}
{setup_text}{context_text}- Dostupné zprávy z trhu:
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
    "open_positions": {open_positions_cnt},
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


async def ask_gemini(prompt: str, ticker: str) -> Optional[Dict[str, Any]]:
    """Zavolá Flash Lite kaskádu. None = žádný model nedostupný / chyba."""
    from google.genai import types
    from google.genai.errors import ClientError
    from gemini_team import get_gemini_client
    from model_manager import quota_manager

    client = get_gemini_client()
    for m in quota_manager.lite_models():
        mid = m["id"]
        try:
            cfg = types.GenerateContentConfig(
                response_mime_type="application/json", temperature=0.2,
                thinking_config=(types.ThinkingConfig(thinking_level=m["thinking_level"]) if m.get("thinking_level")
                                 else types.ThinkingConfig(thinking_budget=0)))
            resp = await asyncio.wait_for(client.aio.models.generate_content(model=mid, contents=prompt, config=cfg),
                                          timeout=20.0)
            text = resp.text or ""
            if not text:
                continue
            quota_manager.record_call_success(mid, component=GEMINI_COMPONENT)
            data = json.loads(text)
            data["model_used"] = mid
            return data
        except asyncio.TimeoutError:
            quota_manager.record_rate_limit(mid, cooldown_minutes=15)
        except ClientError as ce:
            if "429" in str(ce) or "RESOURCE_EXHAUSTED" in str(ce):
                quota_manager.record_quota_error(mid, str(ce))
            logger.warning(f"Stará metoda {ticker}: {mid} chyba {ce}")
        except Exception as e:
            logger.warning(f"Stará metoda {ticker}: {mid} chyba {e}")
    return None


# =====================================================================
# PRŮCHOD (volá bot.py každých 5 min)
# =====================================================================

def fetch_1m_since(symbol: str, opened_ms: int) -> List[List[Any]]:
    import binance_client
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    minutes = int((now_ms - opened_ms) / 60000) + 2
    return binance_client.get_klines_sync(symbol, interval="1m", limit=max(2, min(minutes, 15))) or []


def manage_open(now: datetime = None, get_bars: Callable[[str, int], List[List[Any]]] = fetch_1m_since,
                path: Path = None) -> List[Dict[str, Any]]:
    """Zavře papírové pozice na SL / TP podle 1m svíček za posledních ~15 min."""
    trades = load_ledger(path)
    now = now or datetime.now(timezone.utc)
    events = []
    for t in open_positions(trades):
        try:
            bars = get_bars(t["ticker"], int(t.get("opened_ms", 0)))
            hit = exit_for_bars(t, bars, now)
            if hit:
                close_trade(t, hit[0], hit[1], now)
                events.append({"type": "close", "trade": t})
        except Exception as e:
            logger.error(f"Stará metoda: chyba při správě {t.get('ticker')}: {e}")
    if events:
        save_ledger(trades, path)
    return events


async def run_pass(fetch_data: Callable[[str], Optional[Dict[str, Any]]],
                   now: datetime = None) -> List[Dict[str, Any]]:
    """Jeden průchod: správa otevřených pozic, pak spouštěče a Gemini pro volné symboly. Vrací události."""
    if not ENABLED:
        return []
    now = now or datetime.now(timezone.utc)
    events = await asyncio.to_thread(manage_open, now)
    if Path(KILL_SWITCH_FILE).exists():
        return events
    st = load_state(now)
    trades = load_ledger()
    log = []
    for sym in CRYPTO_WATCHLIST:
        if can_open(trades, sym) or not cooldown_ok(st, sym, now):
            continue
        try:
            data = await asyncio.to_thread(fetch_data, sym)
        except Exception as e:
            logger.warning(f"Stará metoda {sym}: data nejdou stáhnout ({e})")
            continue
        if not data:
            continue
        triggers = old_trigger(data)
        if not triggers:
            continue
        st["signals"] = st.get("signals", 0) + 1
        if st["gemini_calls"] >= MAX_GEMINI_CALLS_PER_DAY:
            log.append(f"{sym}: signál ({', '.join(triggers)}), ale denní strop {MAX_GEMINI_CALLS_PER_DAY} volání")
            continue
        st["gemini_calls"] += 1
        st["last_call"][sym] = now.isoformat()
        save_state(st)
        try:
            import news_briefing
            ctx = news_briefing.get_context_for_ticker(sym)
        except Exception:
            ctx = ""
        setup = data.get("sfp_data") if data.get("sfp_detected") else None
        price = float(data["price"])
        prompt = build_prompt(sym, price, float(data.get("price_move_pct") or 0), float(data.get("volume_ratio") or 0),
                              data.get("headlines") or [], len(open_positions(trades)), setup, ctx,
                              data.get("data_as_of") or "")
        resp = await ask_gemini(prompt, sym)
        if resp is None:
            log.append(f"{sym}: Gemini (Flash Lite) nedostupný")
            continue
        dec = decide(resp, price, setup)
        if not dec["buy"]:
            log.append(f"{sym}: {dec['reason']}")
            continue
        trade = new_trade(sym, dec["chart"], now, triggers, resp, resp.get("model_used", ""))
        if trade:
            trades.append(trade)
            save_ledger(trades)
            events.append({"type": "open", "trade": trade})
            log.append(f"{sym}: KOUPENO (papír)")
    save_state(st)
    if log:
        logger.info("🕰️ STARÁ METODA (papír): " + " | ".join(log))
    return events


def format_event(ev: Dict[str, Any]) -> str:
    t = ev["trade"]
    sym = t["ticker"]
    if ev["type"] == "open":
        return (f"🕰️ **[STARÁ METODA – PAPÍR] Nákup {sym}** za ${fmt_price(t['entry'])} | SL ${fmt_price(t['stop_loss'])} "
                f"| TP ${fmt_price(t['take_profit'])} ({t.get('rrr')}) | spouštěč: {', '.join(t.get('triggers') or [])} | "
                f"Gemini: {str(t.get('reasoning') or '')[:200]} | jen papír, žádný příkaz na burzu")
    emoji = "🟢" if t.get("pnl_usd", 0) > 0 else "🔴"
    s = summary(load_ledger())
    return (f"{emoji} **[STARÁ METODA – PAPÍR] Prodej {sym}** za ${fmt_price(t['exit_price'])} ({t['exit_reason']}) | "
            f"vstup ${fmt_price(t['entry'])} | {t['pnl_pct']:+.2f} %, ${t['pnl_usd']:+.2f} po poplatcích | "
            f"celkem {s['closed']} obchodů, ${s['total_usd']:+.2f}")
