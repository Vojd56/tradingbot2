"""Skutečné počty volání Gemini API přímo od Googlu (Cloud Monitoring).

Potřebuje soubor gcp_monitoring.json (klíč servisního účtu s rolí Monitoring Viewer)
ve složce bota. Bez něj vrací None a nic nedělá. Nikdy nevyhazuje výjimku.

Počítá od posledního resetu kvót (půlnoc v Kalifornii = 9:00 Praha).
"""
import json
import os
import time
import logging
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

BASE = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(BASE, "gcp_monitoring.json")
SERVICE = "generativelanguage.googleapis.com"
API = "https://monitoring.googleapis.com/v3/projects/{p}/timeSeries"

_cache = {"t": 0, "data": None}
CACHE_SECONDS = 240  # Monitoring má zpoždění pár minut, častěji nemá smysl


def is_configured():
    return os.path.exists(KEY_FILE)


def _token_and_project():
    from google.oauth2 import service_account
    import google.auth.transport.requests as gtr

    creds = service_account.Credentials.from_service_account_file(
        KEY_FILE, scopes=["https://www.googleapis.com/auth/monitoring.read"])
    creds.refresh(gtr.Request())
    with open(KEY_FILE, encoding="utf-8") as f:
        project = json.load(f).get("project_id")
    return creds.token, project


def _quota_day_start(now_utc):
    """Začátek aktuálního dne kvót = půlnoc America/Los_Angeles."""
    try:
        from zoneinfo import ZoneInfo
        la = now_utc.astimezone(ZoneInfo("America/Los_Angeles"))
        start = la.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.astimezone(timezone.utc)
    except Exception:
        start = (now_utc - timedelta(hours=8)).replace(hour=0, minute=0, second=0, microsecond=0)
        return start + timedelta(hours=8)


def _query(token, project, flt, start, end, group_by=None):
    secs = max(60, int((end - start).total_seconds()))
    params = {
        "filter": flt,
        "interval.startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "interval.endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "aggregation.alignmentPeriod": f"{secs}s",
        "aggregation.perSeriesAligner": "ALIGN_SUM",
    }
    if group_by:
        params["aggregation.crossSeriesReducer"] = "REDUCE_SUM"
        params["aggregation.groupByFields"] = group_by
    out = []
    page = None
    for _ in range(10):
        if page:
            params["pageToken"] = page
        r = requests.get(API.format(p=project), params=params,
                         headers={"Authorization": f"Bearer {token}"}, timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"Monitoring HTTP {r.status_code}: {r.text[:200]}")
        j = r.json()
        for ts in j.get("timeSeries", []):
            total = 0
            for p in ts.get("points", []):
                v = p.get("value", {})
                total += float(v.get("int64Value") or v.get("doubleValue") or 0)
            labels = {}
            labels.update(ts.get("metric", {}).get("labels", {}))
            labels.update(ts.get("resource", {}).get("labels", {}))
            out.append({"labels": labels, "value": int(round(total))})
        page = j.get("nextPageToken")
        if not page:
            break
    return out


def fetch():
    """Vrátí dict se skutečnými počty od Googlu, nebo None."""
    if not is_configured():
        return None
    now = time.time()
    if _cache["data"] is not None and now - _cache["t"] < CACHE_SECONDS:
        return _cache["data"]
    try:
        token, project = _token_and_project()
        end = datetime.now(timezone.utc)
        start = _quota_day_start(end)

        # 1) všechny požadavky podle metody a kódu odpovědi (200, 429, ...)
        req = _query(
            token, project,
            f'metric.type="serviceruntime.googleapis.com/api/request_count" '
            f'AND resource.type="consumed_api" AND resource.label.service="{SERVICE}"',
            start, end,
            group_by=["metric.label.response_code", "resource.label.method"])
        by_code = {}
        total = 0
        for s in req:
            code = s["labels"].get("response_code", "?")
            by_code[code] = by_code.get(code, 0) + s["value"]
            total += s["value"]

        # 2) spotřeba kvót (u Gemini obvykle i podle modelu)
        quota = []
        try:
            q = _query(
                token, project,
                f'metric.type="serviceruntime.googleapis.com/quota/rate/net_usage" '
                f'AND resource.type="consumer_quota" AND resource.label.service="{SERVICE}"',
                start, end)
            per = {}
            for s in q:
                lb = s["labels"]
                model = lb.get("model") or lb.get("base_model") or lb.get("location") or ""
                qm = (lb.get("quota_metric") or "").split("/")[-1]
                key = (model, qm)
                per[key] = per.get(key, 0) + s["value"]
            quota = [{"model": k[0], "quota": k[1], "used": v}
                     for k, v in sorted(per.items(), key=lambda x: -x[1]) if v]
        except Exception as e:
            logger.debug(f"Gemini quota metrika: {e}")

        data = {
            "source": "google_cloud_monitoring",
            "project": project,
            "since": start.isoformat(),
            "fetched_at": end.isoformat(),
            "requests_total": total,
            "by_code": dict(sorted(by_code.items())),
            "errors_429": by_code.get("429", 0),
            "quota": quota,
            "note": "Google posílá data se zpožděním asi 3 až 5 minut.",
        }
        _cache.update(t=now, data=data)
        return data
    except Exception as e:
        logger.warning(f"Gemini usage z Google Monitoringu: {e}")
        _cache.update(t=now, data=None)
        return None


if __name__ == "__main__":
    print(json.dumps(fetch(), indent=2, ensure_ascii=False))
