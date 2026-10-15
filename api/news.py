
import json
import time
import urllib.request
from datetime import datetime, timezone
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CACHE_SECONDS = 300

CACHE = {
    "expires": 0.0,
    "fetched_at": 0,
    "events": [],
}


def response(handler, status, payload):
    body = json.dumps(
        payload, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")

    handler.send_response(status)
    handler.send_header(
        "Content-Type", "application/json; charset=utf-8"
    )
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header(
        "Access-Control-Allow-Headers", "Content-Type, Authorization"
    )
    handler.send_header(
        "Access-Control-Allow-Methods", "GET, OPTIONS"
    )
    handler.send_header(
        "Cache-Control", "no-store, no-cache, must-revalidate, max-age=0"
    )
    handler.send_header("Pragma", "no-cache")
    handler.send_header("Expires", "0")
    handler.send_header("Date", formatdate(usegmt=True))
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()

    if status != 204:
        try:
            handler.wfile.write(body)
        except Exception:
            pass


def clean(value):
    return "" if value is None else str(value).strip()


def parse_event_date(value):
    """Return a comparable timestamp and a display date/time."""
    value = clean(value)
    if not value:
        return None, "", ""

    dt = None
    normalized = value.replace("Z", "+00:00")

    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        formats = (
            "%m-%d-%Y %H:%M",
            "%m-%d-%Y %I:%M%p",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%m/%d/%Y %H:%M",
            "%m/%d/%Y %I:%M %p",
        )

        for fmt in formats:
            try:
                dt = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue

    if dt is None:
        return None, value, ""

    # Keep the source's displayed time convention, but use UTC
    # timestamps for reliable sorting and past-event filtering.
    if dt.tzinfo is None:
        comparable = dt.replace(tzinfo=timezone.utc)
    else:
        comparable = dt.astimezone(timezone.utc)

    return (
        comparable.timestamp(),
        dt.strftime("%a %d %b %Y"),
        dt.strftime("%H:%M"),
    )


def fetch():
    now = time.time()

    if CACHE["expires"] > now:
        return CACHE["events"], CACHE["fetched_at"]

    request = urllib.request.Request(
        CALENDAR_URL,
        headers={
            "User-Agent": "LM-Analyzer/1.0",
            "Accept": "application/json",
            "Cache-Control": "no-cache",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as result:
            raw = result.read().decode("utf-8", "replace")
    except Exception as exc:
        # Keep the previous cache available as a fallback, but do
        # not pretend it was freshly fetched.
        if CACHE["events"]:
            return CACHE["events"], CACHE["fetched_at"]
        raise RuntimeError(
            "Economic calendar is temporarily unavailable."
        ) from exc

    try:
        data = json.loads(raw)
    except Exception as exc:
        raise RuntimeError(
            "Economic calendar returned invalid data."
        ) from exc

    if not isinstance(data, list):
        raise RuntimeError(
            "Economic calendar returned an unexpected format."
        )

    events = []

    for item in data:
        if not isinstance(item, dict):
            continue

        if clean(item.get("impact")).lower() != "high":
            continue

        title = clean(item.get("title") or item.get("event"))
        currency = clean(
            item.get("country") or item.get("currency")
        )

        if not title or not currency:
            continue

        raw_date = clean(
            item.get("date") or item.get("event_dt")
        )
        timestamp, display_date, display_time = parse_event_date(
            raw_date
        )

        events.append({
            "currency": currency,
            "title": title,
            "impact": "High",
            "date": display_date,
            "time": display_time,
            "actual": clean(item.get("actual")),
            "forecast": clean(item.get("forecast")),
            "previous": clean(item.get("previous")),
            "_timestamp": timestamp,
        })

    # Put upcoming events first and in chronological order.
    # Events without parseable dates are placed after dated events.
    events.sort(
        key=lambda event: (
            event["_timestamp"] is None,
            event["_timestamp"] or float("inf"),
        )
    )

    current_time = time.time()

    upcoming = [
        event for event in events
        if event["_timestamp"] is not None
        and event["_timestamp"] >= current_time
    ]

    # If there are upcoming events, don't fill the card with
    # events that already happened earlier in the week.
    if upcoming:
        selected = upcoming[:20]
    else:
        dated_past = [
            event for event in events
            if event["_timestamp"] is not None
        ]
        selected = dated_past[-20:] if dated_past else events[:20]

    # Remove internal sorting field from the API response.
    public_events = [
        {key: value for key, value in event.items()
         if key != "_timestamp"}
        for event in selected
    ]

    CACHE["events"] = public_events
    CACHE["fetched_at"] = int(current_time)
    CACHE["expires"] = current_time + CACHE_SECONDS

    return CACHE["events"], CACHE["fetched_at"]


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        response(self, 204, {})

    def do_GET(self):
        try:
            events, fetched_at = fetch()
            response(self, 200, {
                "source": "Forex Factory",
                "updated_at": fetched_at,
                "events": events,
            })
        except RuntimeError as exc:
            response(self, 502, {"error": str(exc)})
        except Exception:
            response(self, 500, {
                "error": "News backend error."
            })
