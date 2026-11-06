"""Swing stopa D „Lite tým“ (2. 11. 2026, jen papír): 6 rolí na Flash Lite modelech, nikdy silné modely.

Na stejných kandidátech jako ostatní stopy (top N podle skóre):
1) NEWS: proč se akcie hýbe (cituje id zpráv), nebo „žádný jasný důvod“
2) CHART: čte jen čísla spočítaná kódem, setup ano/ne
3) SKEPTIK: 3 nejsilnější důvody proti, verdikt BUY/WAIT/SKIP (bez jasného důvodu ze zpráv vždy SKIP)
4-6) tři nezávislé hlasy BUY/NONE s výstupy NEWS, CHART a SKEPTIKA jako kontextem
Nákup (papír) jen při SKEPTIK = BUY a >= 2/3 BUY. Velikost, stop a výstupy počítá kód jako u ostatních stop.
Rozpočet SWING_GEMINI_BUDGET["swing_D"] volání denně; Flash Lite se nečerpá, když Scoutovi zbývá málo.
"""

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from google.genai import types

import gemini_team
import swing_analyst
from model_manager import quota_manager

logger = logging.getLogger("TradingBot.SwingLiteTeam")

CALL_TIMEOUT_S = 30.0
VOTES = 3
MIN_BUY_VOTES = 2
S, T = types.Schema, types.Type


async def _ask(prompt: str, schema, label: str, budget: swing_analyst.Budget, temperature: float = 0.0) -> Dict[str, Any]:
    models = quota_manager.lite_models()
    if not models:
        raise swing_analyst.BudgetExceeded("Flash Lite pro stopu D není volný (rezerva pro Scout)")
    budget.take()
    try:
        raw, model = await gemini_team._call_models(prompt, label, schema=schema, temperature=temperature,
                                                    timeout=CALL_TIMEOUT_S, models=models, component="swing_D")
    except RuntimeError as e:
        raise swing_analyst.BudgetExceeded(f"Flash Lite nedostupný: {e}") from None
    try:
        return dict(json.loads(raw), _model=model)
    except Exception:
        return {"_model": model}


def _block(c: Dict[str, Any]) -> Tuple[str, str, set]:
    news, nids = swing_analyst._news_lines("C1", c.get("headlines"))
    return swing_analyst._numbers(c), news, set(nids)


async def team_decide(c: Dict[str, Any], budget: swing_analyst.Budget, market: str = "US") -> Dict[str, Any]:
    """Rozhodnutí Lite týmu o jednom kandidátovi. Vyčerpaný rozpočet propadne výš (BudgetExceeded)."""
    nums, news, nids = _block(c)
    head = (f"Akcie [C1] {c['ticker']} ({c.get('sector', '?')}, trh {market}), swing long na dny až týdny.\n"
            f"{swing_analyst.RULES}\n")
    news_d = await _ask(f"""Jsi NEWS analytik. Proč se akcie hýbe? Opři se jen o zprávy níže a cituj jejich id.
Když jasný důvod není, clear_reason = false.
{head}
zprávy:
<untrusted_news>
{news}
</untrusted_news>
Odpověz jen JSON.""", S(type=T.OBJECT, properties={"clear_reason": S(type=T.BOOLEAN), "reason_cs": S(type=T.STRING),
                                                 "cited_ids": S(type=T.ARRAY, items=S(type=T.STRING))},
                       required=["clear_reason", "reason_cs", "cited_ids"]), f"D news {c['ticker']}", budget)
    cited = [str(x) for x in news_d.get("cited_ids") or []]
    clear = bool(news_d.get("clear_reason")) and bool(cited) and all(x in nids for x in cited)

    chart_d = await _ask(f"""Jsi CHART analytik. Čísla spočítal kód z denních svíček, nepočítej vlastní.
Je tu dobrý swing setup pro long (trend, momentum, vzdálenost od supportu, plán SL a cíle)?
{head}
[C1] čísla: {nums}
Odpověz jen JSON.""", S(type=T.OBJECT, properties={"setup": S(type=T.BOOLEAN), "reason_cs": S(type=T.STRING)},
                       required=["setup", "reason_cs"]), f"D chart {c['ticker']}", budget)

    sk = await _ask(f"""Jsi SKEPTIK. Najdi 3 nejsilnější důvody proti nákupu a dej verdikt BUY, WAIT nebo SKIP.
{head}
[C1] čísla: {nums}
NEWS: {'jasný důvod' if clear else 'ŽÁDNÝ jasný důvod'}: {gemini_team.sanitize_headline(news_d.get('reason_cs', ''))[:200]}
CHART: setup {'ano' if chart_d.get('setup') else 'ne'}: {gemini_team.sanitize_headline(chart_d.get('reason_cs', ''))[:200]}
Odpověz jen JSON.""", S(type=T.OBJECT, properties={"reasons": S(type=T.ARRAY, items=S(type=T.STRING)),
                                                 "verdict": S(type=T.STRING, enum=["BUY", "WAIT", "SKIP"])},
                       required=["reasons", "verdict"]), f"D skeptik {c['ticker']}", budget)
    verdict = str(sk.get("verdict", "SKIP")).upper()
    if verdict not in ("BUY", "WAIT", "SKIP") or not clear:
        verdict = "SKIP"          # bez jasného důvodu ze zpráv se nekupuje (pravidlo stopy D)

    votes: List[Dict[str, Any]] = []
    if verdict == "BUY":
        ctx = (f"NEWS: {gemini_team.sanitize_headline(news_d.get('reason_cs', ''))[:200]} | "
               f"CHART: {gemini_team.sanitize_headline(chart_d.get('reason_cs', ''))[:200]} | "
               f"SKEPTIK proti: {'; '.join(gemini_team.sanitize_headline(x)[:120] for x in (sk.get('reasons') or [])[:3])}")
        for k in range(VOTES):
            v = await _ask(f"""Jsi nezávislý hlasující člen týmu. Na základě podkladů rozhodni BUY (koupit zítra / dnes 15 min
po otevření s plánem kódu) nebo NONE.
{head}
[C1] čísla: {nums}
Podklady týmu: {ctx}
Odpověz jen JSON.""", S(type=T.OBJECT, properties={"decision": S(type=T.STRING, enum=["BUY", "NONE"]),
                                                 "reason_cs": S(type=T.STRING)}, required=["decision", "reason_cs"]),
                           f"D hlas {c['ticker']} #{k + 1}", budget, temperature=0.4)
            votes.append({"decision": "BUY" if str(v.get("decision", "")).upper() == "BUY" else "NONE",
                          "reason_cs": gemini_team.sanitize_headline(v.get("reason_cs", ""))[:200], "model": v.get("_model")})
    buys = sum(v["decision"] == "BUY" for v in votes)
    return {"ticker": c["ticker"], "buy": verdict == "BUY" and buys >= MIN_BUY_VOTES, "buy_votes": buys,
            "news": {"clear": clear, "reason_cs": gemini_team.sanitize_headline(news_d.get("reason_cs", ""))[:200],
                     "cited": cited, "model": news_d.get("_model")},
            "chart": {"setup": bool(chart_d.get("setup")), "reason_cs": gemini_team.sanitize_headline(chart_d.get("reason_cs", ""))[:200]},
            "skeptic": {"verdict": verdict, "reasons": [gemini_team.sanitize_headline(x)[:200] for x in (sk.get("reasons") or [])[:3]]},
            "votes": votes}
