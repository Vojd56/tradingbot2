"""Modul pro správu multi-tier rotace modelů Gemini a denních limitů.

Kaskádovité přepínání při vyčerpání limitu nebo 429 Rate Limit.
Limity = free tier Google AI Studio podle screenshotu uživatele 28. 10. 2026 (RPD = požadavky za den).
Nejdřív chytřejší Flash modely (po 20/den), pak vysokokapacitní Flash Lite (po 500/den).
Kvóty sdílí všechno, co používá stejný API klíč, proto rozhoduje hlavně 429 od Googlu:
denní kvóta (RequestsPerDay) = pauza do resetu o půlnoci pacifického času, minutová = 15 min.
"""

import json
import logging
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional
from zoneinfo import ZoneInfo
import discord

logger = logging.getLogger("TradingBot.ModelManager")

QUOTA_FILE = Path(__file__).resolve().parent / "model_quota.json"
PACIFIC_TZ = ZoneInfo("America/Los_Angeles")  # Google resetuje denní kvóty o půlnoci pacifického času
PRAGUE_TZ = ZoneInfo("Europe/Prague")

MODEL_TIERS = [
    {"id": "gemini-3.8-flash", "name": "Gemini 3.8 Flash", "daily_limit": 20,
     "description": "Primární model s nejvyšší analytickou přesností (5/min, 20/den)"},
    {"id": "gemini-3.6-flash", "name": "Gemini 3.6 Flash", "daily_limit": 20,
     "description": "Záložní model 1. stupně (5/min, 20/den)"},
    {"id": "gemini-3.5-flash", "name": "Gemini 3.5 Flash", "daily_limit": 20,
     "description": "Záložní model 2. stupně (20/den)"},
    {"id": "gemini-3-flash-preview", "name": "Gemini 3 Flash", "daily_limit": 20,
     "description": "Záložní model 3. stupně (5/min, 20/den)"},
    # gemini-2.5-flash je v AI Studiu vidět, ale API vrací 404 "no longer available to new users"
    # 3.5 Flash Lite nepřijímá thinking_budget=0 (400 INVALID_ARGUMENT), proto thinking_level
    {"id": "gemini-3.5-flash-lite", "name": "Gemini 3.5 Flash Lite", "daily_limit": 500, "thinking_level": "low",
     "description": "Vysokokapacitní model (15/min, 500/den)"},
    {"id": "gemini-3.1-flash-lite", "name": "Gemini 3.1 Flash Lite", "daily_limit": 500,
     "description": "Vysokokapacitní model (15/min, 500/den)"},
]


class ModelQuotaManager:
    """Sleduje denní spotřebu hovorů na jednotlivé modely Gemini a řídí kaskádovité přepínání."""

    def __init__(self, filepath: Path = QUOTA_FILE):
        self.filepath = filepath
        self.data = self._load()

    def _get_today_str(self) -> str:
        # Den počítadel podle pacifického času: Google resetuje denní kvóty o půlnoci PT (09:00 Praha).
        # Dřív se počítadla nulovala o pražské půlnoci, takže 0:00-9:00 se volání počítala do nového dne.
        return datetime.now(PACIFIC_TZ).date().isoformat()

    def _load(self) -> Dict[str, Any]:
        today_str = self._get_today_str()
        if self.filepath.exists():
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    content = json.load(f)
                    if content.get("date") == today_str:
                        return content
            except Exception as e:
                logger.error(f"Chyba při čtení model_quota.json: {e}")

        # Nový den
        return {
            "date": today_str,
            "counts": {m["id"]: 0 for m in MODEL_TIERS},
            "rate_limited_until": {}
        }

    def _save(self) -> None:
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Chyba při ukládání model_quota.json: {e}")

    def _check_and_reset_if_new_day(self) -> None:
        today_str = self._get_today_str()
        if self.data.get("date") != today_str:
            # Pauzy po 429 se přenáší: denní kvóta Googlu se resetuje až o půlnoci pacifického času (09:00 Praha)
            self.data = {
                "date": today_str,
                "counts": {m["id"]: 0 for m in MODEL_TIERS},
                "rate_limited_until": self.data.get("rate_limited_until", {})
            }
            self._save()

    def is_model_rate_limited(self, model_id: str) -> bool:
        """Zjistí, zda je model v dočasné penalizaci kvůli 429 (např. 10 minut)."""
        self._check_and_reset_if_new_day()
        until_iso = self.data.get("rate_limited_until", {}).get(model_id)
        if not until_iso:
            return False
        try:
            until_dt = datetime.fromisoformat(until_iso)
            if datetime.now(timezone.utc) < until_dt:
                return True
        except Exception:
            pass
        return False

    def record_rate_limit(self, model_id: str, cooldown_minutes: int = 15) -> None:
        """Zaznamená chybu 429 Rate Limit a pozastaví model na X minut."""
        self._check_and_reset_if_new_day()
        until_dt = datetime.now(timezone.utc) + timedelta(minutes=cooldown_minutes)
        if "rate_limited_until" not in self.data:
            self.data["rate_limited_until"] = {}
        self.data["rate_limited_until"][model_id] = until_dt.isoformat()
        self._save()
        logger.warning(f"⚠️ Model {model_id} narazil na 429 Rate Limit. Pozastavuji na {cooldown_minutes} minut (do {until_dt.astimezone(PRAGUE_TZ).strftime('%H:%M:%S')} Praha).")

    def record_quota_error(self, model_id: str, error_text: str) -> None:
        """429 od Googlu: denní kvóta -> pauza do resetu (půlnoc pacifického času), jinak 15 min."""
        if "PerDay" in error_text or "per day" in error_text:
            now_pt = datetime.now(PACIFIC_TZ)
            reset = (now_pt + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
            minutes = max(15, int((reset - now_pt).total_seconds() // 60))
            logger.warning(f"⚠️ Model {model_id}: vyčerpaná DENNÍ kvóta, další pokus až po resetu.")
            self.record_rate_limit(model_id, cooldown_minutes=minutes)
        else:
            self.record_rate_limit(model_id, cooldown_minutes=15)

    def record_call_success(self, model_id: str) -> None:
        """Zaznamená úspěšné volání modelu."""
        self._check_and_reset_if_new_day()
        if "counts" not in self.data:
            self.data["counts"] = {}
        self.data["counts"][model_id] = self.data["counts"].get(model_id, 0) + 1
        self._save()

    def get_candidate_models(self) -> List[Dict[str, Any]]:
        """Vrátí seřazený seznam modelů vhodných k volání v pořadí kaskády.

        Modely, které vyčerpaly denní limit nebo mají aktivní 429 cooldown, jsou odsunuty na konec.
        """
        self._check_and_reset_if_new_day()
        available = []
        exhausted = []

        for m in MODEL_TIERS:
            mid = m["id"]
            limit = m["daily_limit"]
            count = self.data.get("counts", {}).get(mid, 0)
            is_rl = self.is_model_rate_limited(mid)

            if count < limit and not is_rl:
                available.append(m)
            else:
                exhausted.append(m)

        return available + exhausted

    def get_status_embed(self) -> discord.Embed:
        """Vytvoří Discord Embed s přehledem kvót všech modelů."""
        self._check_and_reset_if_new_day()
        embed = discord.Embed(
            title="🤖 Stav Multi-Tier Kaskády Modelů Gemini",
            description="Automatické přepínání při vyčerpání denního limitu nebo 429 Rate Limitu.",
            color=discord.Color.blue(),
            timestamp=datetime.now()
        )

        for m in MODEL_TIERS:
            mid = m["id"]
            name = m["name"]
            limit = m["daily_limit"]
            count = self.data.get("counts", {}).get(mid, 0)
            is_rl = self.is_model_rate_limited(mid)

            remaining = max(0, limit - count)
            if is_rl:
                status_icon = "🔴 429 Rate Limit (Dočasný cooldown)"
            elif remaining == 0:
                status_icon = "🟡 Denní limit vyčerpán"
            else:
                status_icon = "🟢 Aktivní"

            embed.add_field(
                name=f"{name} (`{mid}`)",
                value=(
                    f"• Stav: **{status_icon}**\n"
                    f"• Využito dnes: **{count} / {limit}** volání (zbývá **{remaining}**)\n"
                    f"• Účel: *{m['description']}*"
                ),
                inline=False
            )

        embed.set_footer(text="AIBot 2.0 • Inteligentní Quota Fallback Engine")
        return embed


quota_manager = ModelQuotaManager()
