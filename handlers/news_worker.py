"""Scheduled high-impact news assessment worker. One queued instrument per invocation."""
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from zoneinfo import ZoneInfo

from analysis_common import json_response
from api.lm_push import notify_user_ids
from api.user_security import get_supabase_service_key, get_supabase_url
from handlers.news import fetch as fetch_calendar
from handlers.news_signal import (
    RELATED, TZ, parse_event_dt, validate_candles, ai_assess, trade_levels, fmt
)
from handlers.market_candles import _candles

LOCAL = ZoneInfo("Africa/Kampala")
PRE_MINUTES = 15
POST_MINUTES = 5
POST_WINDOW_HOURS = 4


def _base():
    return get_supabase_url().rstrip("/")


def _headers():
    key = get_supabase_service_key()
    return {"apikey": key, "Authorization": "Bearer " + key,
            "Accept": "application/json", "Content-Type": "application/json"}


def _request(method, path, payload=None, prefer=None):
    headers = _headers()
    if prefer:
        headers["Prefer"] = prefer
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    req = urllib.request.Request(_base() + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=12) as response:
        raw = response.read().decode("utf-8", "replace")
        return json.loads(raw) if raw else None


def _event_key(event):
    raw = "|".join(str(event.get(k, "")) for k in ("currency", "title", "date", "time"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _instruments(currency):
    return sorted(RELATED.get(str(currency).upper(), set()))


def _enqueue_upcoming_events():
    events = fetch_calendar()
    now = datetime.now(LOCAL)
    jobs = []
    for event in events:
        currency = str(event.get("currency", "")).upper()
        if currency not in RELATED:
            continue
        event_dt = parse_event_dt(event)
        if event_dt < now - timedelta(hours=POST_WINDOW_HOURS):
            continue
        key = _event_key(event)
        for symbol in _instruments(currency):
            pre_due = event_dt - timedelta(minutes=PRE_MINUTES)
            post_due = event_dt + timedelta(minutes=POST_MINUTES)
            if now <= event_dt:
                jobs.append({
                    "event_key": key, "event_data": event, "instrument": symbol,
                    "stage": "PRE_NEWS", "due_at": pre_due.isoformat(),
                    "expires_at": event_dt.isoformat(),
                })
            if now <= event_dt + timedelta(hours=POST_WINDOW_HOURS):
                jobs.append({
                    "event_key": key, "event_data": event, "instrument": symbol,
                    "stage": "POST_NEWS", "due_at": post_due.isoformat(),
                    "expires_at": (event_dt + timedelta(hours=POST_WINDOW_HOURS)).isoformat(),
                })
    if jobs:
        _request("POST", "/rest/v1/lm_news_signal_jobs",
                 jobs, prefer="resolution=ignore-duplicates,return=minimal")
    return len(jobs)


def _claim_job():
    result = _request("POST", "/rest/v1/rpc/claim_lm_news_signal_job", {})
    if isinstance(result, list):
        return result[0] if result else None
    return result if isinstance(result, dict) and result.get("id") else None


def _patch_job(job_id, payload):
    query = urllib.parse.urlencode({"id": "eq." + str(job_id)})
    _request("PATCH", "/rest/v1/lm_news_signal_jobs?" + query,
             payload, prefer="return=minimal")


def _active_subscriber_ids():
    query = urllib.parse.urlencode({
        "select": "user_id", "status": "eq.active",
        "expires_at": "gt." + datetime.now(timezone.utc).isoformat(),
        "limit": "1000",
    })
    rows = _request("GET", "/rest/v1/subscriptions?" + query)
    return list(dict.fromkeys(str(x.get("user_id", "")).strip()
                              for x in (rows or []) if x.get("user_id")))


def _assess(job):
    event = job.get("event_data") or {}
    symbol = str(job.get("instrument", "")).upper()
    stage = str(job.get("stage", ""))
    if symbol not in _instruments(event.get("currency")):
        raise ValueError("Instrument is not related to the event currency.")
    api_key = os.environ.get("TWELVE_DATA_API_KEY", "").strip()
    candles = _candles(symbol, "1min", api_key)
    rows, age = validate_candles(
        candles.get("candles"), candles.get("updated_at"), False
    )
    assessment = ai_assess(event, symbol, stage, rows)
    result = {
        "event_key": job.get("event_key"), "instrument": symbol,
        "event_title": event.get("title", ""),
        "event_currency": event.get("currency", ""),
        "stage": stage, **assessment,
        "current_price": fmt(rows[-1]["close"]),
        "market_data_age_seconds": age,
        "setup_status": "NO_SETUP", "order_type": "NONE",
        "entry": "", "stop_loss": "", "take_profit_1": "", "take_profit_2": "",
        "warning": "News-related market analysis is uncertain. This is not a guaranteed prediction or financial advice.",
    }
    if assessment["signal"] not in ("BUY", "SELL") or assessment["confidence"] < 65:
        result["signal"] = "WAIT"
        result["reasons"].append("Evidence did not meet the minimum confidence threshold for a trade setup.")
        return result

    levels = trade_levels(rows, assessment["signal"])
    if not levels:
        result["signal"] = "WAIT"
        result["confidence"] = min(result["confidence"], 50)
        result["reasons"].append("A safe structural entry and stop-loss could not be calculated.")
        return result

    reached = levels.pop("entry_reached")
    late = levels.pop("late_entry")
    result.update(levels)
    if stage == "PRE_NEWS":
        result["setup_status"] = "PRE_NEWS_POTENTIAL_" + assessment["signal"]
        result["order_type"] = "NONE"
        result["warning"] = "Conditional pre-news levels only. Do not enter before the event reaction confirms the setup."
    elif late:
        result["setup_status"] = "LATE_ENTRY"
        result["signal"] = "WAIT"
        result["warning"] = "The entry has moved too far or TP1 has been reached. Do not chase price."
    else:
        result["setup_status"] = "READY" if reached else "WAITING"
        result["order_type"] = "MARKET" if reached else "WAIT"
        if reached:
            result["headline"] = symbol + " " + assessment["signal"] + " NOW"
        else:
            result["warning"] = "Entry has not been reached. Wait for price to reach the stated entry; do not chase."
    return result


def _process_one():
    job = _claim_job()
    if not job:
        return {"processed": False, "reason": "queue_empty"}
    now = datetime.now(timezone.utc)
    expires = datetime.fromisoformat(str(job["expires_at"]).replace("Z", "+00:00"))
    if expires <= now:
        _patch_job(job["id"], {"status": "skipped", "error": "Assessment window expired.",
                               "updated_at": now.isoformat()})
        return {"processed": True, "status": "skipped"}
    try:
        result = _assess(job)
        patch = {"status": "completed", "result": result, "error": None,
                 "updated_at": datetime.now(timezone.utc).isoformat()}
        # Reserve the one notification attempt before sending to prevent concurrent duplicate alerts.
        should_notify = (
            job.get("stage") == "POST_NEWS"
            and result.get("setup_status") == "READY"
            and result.get("order_type") == "MARKET"
            and result.get("signal") in ("BUY", "SELL")
            and not bool(job.get("notification_sent"))
            and bool(result.get("entry") and result.get("stop_loss")
                     and result.get("take_profit_1") and result.get("take_profit_2"))
        )
        if should_notify:
            patch["notification_sent"] = True
        _patch_job(job["id"], patch)
        if should_notify:
            user_ids = _active_subscriber_ids()
            if user_ids:
                notify_user_ids(
                    user_ids,
                    result.get("headline") or (result["instrument"] + " " + result["signal"] + " NOW"),
                    "High-impact news setup confirmed. Entry " + result["entry"]
                    + " | SL " + result["stop_loss"] + " | TP1 " + result["take_profit_1"]
                    + " | TP2 " + result["take_profit_2"],
                    {"type": "high_impact_news_signal", "event_key": str(job["event_key"]),
                     "job_id": str(job["id"]), "instrument": result["instrument"],
                     "stage": "POST_NEWS"},
                )
        return {"processed": True, "status": "completed",
                "notification_eligible": should_notify}
    except Exception as exc:
        attempts = int(job.get("attempt_count") or 1)
        status = "pending" if attempts < 3 else "failed"
        _patch_job(job["id"], {"status": status, "error": str(exc)[:240],
                               "updated_at": datetime.now(timezone.utc).isoformat(),
                               "locked_at": None})
        return {"processed": True, "status": status, "error": str(exc)[:160]}


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        json_response(self, 204, {})

    def do_POST(self):
        secret = os.environ.get("LM_NEWS_WORKER_SECRET", "").strip()
        timestamp = str(self.headers.get("X-LM-Timestamp", "")).strip()
        signature = str(self.headers.get("X-LM-Signature", "")).strip().lower()
        try:
            ts = int(timestamp)
        except (TypeError, ValueError):
            ts = 0
        expected = hmac.new(secret.encode(), timestamp.encode(), hashlib.sha256).hexdigest() if secret else ""
        if (not secret or not ts or abs(int(time.time()) - ts) > 180
                or not hmac.compare_digest(signature, expected)):
            json_response(self, 403, {"error": "Worker authentication failed."})
            return
        try:
            queued = _enqueue_upcoming_events()
            processed = _process_one()
            json_response(self, 200, {"ok": True, "queued_candidates": queued, **processed})
        except Exception as exc:
            print("news_worker error:", str(exc)[:240])
            json_response(self, 503, {"error": "News worker could not complete this cycle."})

    def log_message(self, *_args):
        return
