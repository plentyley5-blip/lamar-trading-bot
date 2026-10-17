"""LM Analyzer high-impact economic calendar endpoint.

Prevents yesterday's events from remaining visible after the calendar day rolls
in Africa/Kampala, refreshes the upstream feed periodically, sorts upcoming
items chronologically, and disables intermediary/browser caching.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, date, timezone
from http.server import BaseHTTPRequestHandler
from threading import Lock
from zoneinfo import ZoneInfo

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
LOCAL_TZ = ZoneInfo("Africa/Kampala")
CACHE_TTL_SECONDS = 300
CACHE = {
    "expires": 0.0,
    "day": "",
    "updated_at": "",
    "events": [],
    "stale": False,
}
_CACHE_LOCK = Lock()


def response(handler, status, payload):
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, Cache-Control, Pragma")
    handler.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    # Do not let a browser, CDN or proxy retain yesterday's economic-calendar response.
    handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
    handler.send_header("Pragma", "no-cache")
    handler.send_header("Expires", "0")
    handler.send_header("Vary", "Accept-Encoding")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        pass


def clean(value):
    return "" if value is None else str(value).strip()


def _now_local():
    return datetime.now(LOCAL_TZ)


def _parse_event_datetime(value):
    """Parse common calendar timestamp formats and return Africa/Kampala time."""
    raw = clean(value)
    if not raw:
        return None

    normalized = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        dt = None
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
            "%m/%d/%Y %H:%M",
            "%m/%d/%Y",
        ):
            try:
                dt = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            return None

    # Naive event times are treated as Kampala local time; offset-aware feed
    # timestamps are converted to Kampala time before formatting/filtering.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(LOCAL_TZ)


def _event_datetime(item):
    for key in ("date", "event_dt", "datetime", "timestamp"):
        parsed = _parse_event_datetime(item.get(key))
        if parsed is not None:
            return parsed
    return None


def _normalize_events(data, today):
    if not isinstance(data, list):
        raise RuntimeError("Economic calendar returned an unexpected format.")

    out = []
    seen = set()
    for item in data:
        if not isinstance(item, dict) or clean(item.get("impact")).lower() != "high":
            continue

        title = clean(item.get("title") or item.get("event"))
        currency = clean(item.get("country") or item.get("currency"))
        event_dt = _event_datetime(item)

        # Unknown/malformed dates cannot be safely treated as current news.
        # Drop them rather than accidentally showing stale items indefinitely.
        if not title or not currency or event_dt is None:
            continue
        if event_dt.date() < today:
            continue

        key = (currency, title, event_dt.isoformat())
        if key in seen:
            continue
        seen.add(key)

        out.append({
            "currency": currency,
            "title": title,
            "impact": "High",
            "date": event_dt.strftime("%a %d %b %Y"),
            "time": event_dt.strftime("%H:%M"),
            "actual": clean(item.get("actual")),
            "forecast": clean(item.get("forecast")),
            "previous": clean(item.get("previous")),
            "_sort": event_dt.timestamp(),
        })

    out.sort(key=lambda event: event["_sort"])
    for event in out:
        event.pop("_sort", None)
    return out[:30]


def fetch(force=False):
    now_ts = time.time()
    now_local = _now_local()
    today_text = now_local.date().isoformat()

    # A cached response is reusable only for the same local calendar date.
    # The day check invalidates it even if the process survives midnight.
    with _CACHE_LOCK:
        if (
            not force
            and CACHE["expires"] > now_ts
            and CACHE["day"] == today_text
        ):
            return list(CACHE["events"])

    # Cache-buster changes every five-minute bucket, not for every user request.
    # This avoids reusing a stale upstream CDN object while keeping fetch traffic low.
    refresh_bucket = int(now_ts // CACHE_TTL_SECONDS)
    separator = "&" if "?" in CALENDAR_URL else "?"
    url = CALENDAR_URL + separator + urllib.parse.urlencode({"lm_refresh": refresh_bucket})
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "LM-Analyzer/1.1",
            "Accept": "application/json",
            "Cache-Control": "no-cache, no-store, max-age=0",
            "Pragma": "no-cache",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as upstream:
            raw = upstream.read().decode("utf-8", "replace")
        data = json.loads(raw)
        events = _normalize_events(data, now_local.date())
    except Exception as exc:
        # If the provider briefly fails, allow already-fetched same-day data as a
        # marked stale fallback. Never return data cached from a previous day.
        with _CACHE_LOCK:
            if CACHE["day"] == today_text and CACHE["events"]:
                CACHE["stale"] = True
                CACHE["expires"] = time.time() + 60
                return list(CACHE["events"])
        if isinstance(exc, (urllib.error.URLError, TimeoutError)):
            raise RuntimeError("Economic calendar is temporarily unavailable.") from exc
        if isinstance(exc, json.JSONDecodeError):
            raise RuntimeError("Economic calendar returned invalid data.") from exc
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError("Economic calendar could not be refreshed.") from exc

    updated_at = _now_local().isoformat(timespec="seconds")
    with _CACHE_LOCK:
        CACHE.update({
            "expires": time.time() + CACHE_TTL_SECONDS,
            "day": today_text,
            "updated_at": updated_at,
            "events": events,
            "stale": False,
        })
    return list(events)


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        response(self, 204, {})

    def do_GET(self):
        try:
            events = fetch()
            with _CACHE_LOCK:
                payload = {
                    "source": "Forex Factory",
                    "updated_at": CACHE["updated_at"],
                    "business_date": _now_local().date().isoformat(),
                    "stale": bool(CACHE["stale"]),
                    "events": events,
                }
            response(self, 200, payload)
        except RuntimeError as exc:
            response(self, 502, {"error": str(exc), "events": []})
        except Exception:
            response(self, 500, {"error": "News backend error.", "events": []})

    def log_message(self, format_string, *args):
        # Vercel logs are kept quieter; returned errors are already structured.
        return
