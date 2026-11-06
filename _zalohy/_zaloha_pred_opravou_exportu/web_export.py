"""Export stavu bota na web (dashboard na Vercelu, data v Supabase).

Každých pár minut sesbírá stav z lokálních souborů bota + souhrn účtu T212 a Binance
a pošle ho jedním voláním RPC `ingest` do Supabase. Na web NIKDY nejdou API klíče
brokerů ani Gemini: posílá se jen to, co je v payloadu níže.

Nastavení v souboru dashboard_config.json ve složce bota (nebo proměnné prostředí stejného jména):
  {"DASHBOARD_SUPABASE_URL": "https://xxxx.supabase.co",
   "DASHBOARD_SUPABASE_KEY": "sb_publishable_...",   (veřejný klíč, jen pro volání RPC)
   "DASHBOARD_TOKEN": "..."}                          (tajný token, ověřuje ho funkce ingest)
Bez nich se export tiše vypne. Vypnutí: smaž soubor nebo nastav "enabled": false.
"""
from __future__ import annotations

import json
import logging
import os
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger("web_export")
BASE = Path(__file__).resolve().parent

MAX_TRADES = 300
MAX_SIGNALS = 150
MAX_SWING_EVENTS = 200


def _cfg(name: str) -> str:
    v = os.getenv(name)
    if v:
        return v
    c = _load_json("dashboard_config.json", {}) or {}
    return str(c.get(name) or "")


def is_configured() -> bool:
    c = _load_json("dashboard_config.json", {}) or {}
    if c.get("enabled") is False:
        return False
    return bool(_cfg("DASHBOARD_SUPABASE_URL") and _cfg("DASHBOARD_SUPABASE_KEY") and _cfg("DASHBOARD_TOKEN"))


def _load_json(name: str, default: Any = None) -> Any:
    p = BASE / name
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _tail_jsonl(name: str, n: int) -> List[Dict[str, Any]]:
    p = BASE / name
    out: List[Dict[str, Any]] = []
    try:
        with open(p, encoding="utf-8") as f:
            for line in deque(f, maxlen=n):
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        pass
    except Exception:
        pass
    return out


def _f(x: Any) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None


def _t212_block() -> Dict[str, Any]:
    try:
        import trading212
        if not trading212.is_configured():
            return {}
        summary = trading212.get_account_summary_sync()
        portfolio = trading212.get_portfolio_or_none_sync()
        keep = ("ticker", "quantity", "averagePrice", "currentPrice", "ppl", "fxPpl",
                "initialFillDate", "maxBuy", "maxSell", "pieQuantity")
        items = [{k: p.get(k) for k in keep if k in p} for p in (portfolio or [])]
        return {"summary": summary, "portfolio": items, "ok": summary is not None}
    except Exception as e:
        logger.warning(f"web_export: T212 nedostupné: {e}")
        return {"ok": False, "error": str(e)[:200]}


def _binance_block() -> Dict[str, Any]:
    try:
        import binance_client
        if not binance_client.is_configured():
            return {}
        acc = binance_client.get_account_info_sync()
        if not acc:
            return {"ok": False}
        pos = []
        for p in acc.get("positions", []):
            if _f(p.get("positionAmt")):
                pos.append({k: p.get(k) for k in ("symbol", "positionAmt", "entryPrice", "unrealizedProfit",
                                                  "leverage", "notional", "updateTime") if k in p})
        return {"ok": True,
                "totalMarginBalance": _f(acc.get("totalMarginBalance")),
                "totalWalletBalance": _f(acc.get("totalWalletBalance")),
                "totalUnrealizedProfit": _f(acc.get("totalUnrealizedProfit")),
                "availableBalance": _f(acc.get("availableBalance")),
                "positions": pos}
    except Exception as e:
        logger.warning(f"web_export: Binance nedostupné: {e}")
        return {"ok": False, "error": str(e)[:200]}


def _config_block() -> Dict[str, Any]:
    try:
        import config as c
        names = ["DAILY_LOSS_LIMIT_USD", "MAX_CONSECUTIVE_LOSSES", "MAX_OPEN_POSITIONS_STOCKS",
                 "MAX_OPEN_POSITIONS_CRYPTO", "MAX_OPEN_POSITIONS", "SWING_LIVE_ENABLED", "SWING_MAX_POSITIONS",
                 "SWING_MAX_OPEN_RISK_PCT", "SWING_MAX_DD_PCT", "MAX_DAILY_ALERTS_TOTAL", "RISK_PER_TRADE_PCT_LIVE",
                 "TURTLE_ENABLED", "KILL_SWITCH_FILE"]
        out = {n: getattr(c, n) for n in names if hasattr(c, n)}
        ks = out.get("KILL_SWITCH_FILE") or "STOP"
        out["kill_switch_on"] = (BASE / str(ks)).exists() if not os.path.isabs(str(ks)) else os.path.exists(str(ks))
        out.pop("KILL_SWITCH_FILE", None)
        return out
    except Exception as e:
        return {"error": str(e)[:200]}


def build_snapshot() -> Dict[str, Any]:
    shadow = _load_json("swing_shadow_state.json", {}) or {}
    fx_usd_eur = _f(shadow.get("fx_usd_eur")) or _f((shadow.get("fx") or {}).get("USD")) or 0.92

    t212 = _t212_block()
    bn = _binance_block()

    summ = (t212.get("summary") or {}) if t212 else {}
    t212_total = _f(summ.get("totalValue"))
    t212_cash = _f((summ.get("cash") or {}).get("availableToTrade"))
    t212_ccy = summ.get("currency") or "EUR"
    crypto_usdt = bn.get("totalMarginBalance") if bn else None
    total_eur = None
    if t212_total is not None:
        total_eur = t212_total + (crypto_usdt * fx_usd_eur if crypto_usdt else 0.0)

    trades = _load_json("paper_trades.json", []) or []
    briefing = _load_json("daily_briefing.json", {}) or {}

    return {
        "v": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fx_usd_eur": fx_usd_eur,
        "equity": {
            "total_eur": total_eur,
            "stocks_eur": (t212_total - t212_cash) if (t212_total is not None and t212_cash is not None) else None,
            "cash_eur": t212_cash,
            "crypto_usdt": crypto_usdt,
            "extra": {"t212_total": t212_total, "t212_ccy": t212_ccy},
        },
        "t212": t212,
        "binance": bn,
        "swing_live": _load_json("swing_live_state.json", {}),
        "swing_shadow": shadow,
        "swing_events": _tail_jsonl("swing_shadow.jsonl", MAX_SWING_EVENTS),
        "trades": trades[-MAX_TRADES:],
        "signals": _tail_jsonl("signals.jsonl", MAX_SIGNALS),
        "briefing": briefing,
        "scanner": _load_json("dynamic_tickers.json", {}),
        "high20": _load_json("high20_state.json", {}),
        "models": _load_json("model_quota.json", {}),
        "catalysts": _load_json("catalyst_cache.json", {}),
        "alerts": _load_json("alert_tracker.json", {}),
        "config": _config_block(),
    }


def push(snapshot: Dict[str, Any]) -> bool:
    url = _cfg("DASHBOARD_SUPABASE_URL").rstrip("/") + "/rest/v1/rpc/ingest"
    key = _cfg("DASHBOARD_SUPABASE_KEY")
    body = json.dumps({"token": _cfg("DASHBOARD_TOKEN"), "payload": snapshot},
                      ensure_ascii=False, default=str)
    r = requests.post(url, data=body.encode("utf-8"), timeout=20,
                      headers={"apikey": key, "Content-Type": "application/json"})
    if r.status_code >= 300:
        logger.warning(f"web_export: Supabase vrátil {r.status_code}: {r.text[:200]}")
        return False
    return True


def run_once() -> bool:
    """Sestaví a pošle snapshot. Nikdy nevyhodí výjimku (bot kvůli webu nesmí spadnout)."""
    if not is_configured():
        return False
    try:
        return push(build_snapshot())
    except Exception as e:
        logger.warning(f"web_export selhal: {e}")
        return False


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv(BASE / ".env")
    except Exception:
        pass
    logging.basicConfig(level=logging.INFO)
    snap = build_snapshot()
    print("velikost payloadu:", len(json.dumps(snap, default=str)) // 1024, "KB; total_eur:", snap["equity"]["total_eur"])
    if is_configured():
        print("odesláno:", push(snap))
