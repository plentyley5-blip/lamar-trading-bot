"""LM Analyzer high-impact economic calendar endpoint.

Prevents yesterday's events from remaining visible after the calendar day rolls
in Africa/Kampala, refreshes the upstream feed periodically, sorts upcoming
items chronologically, and disables intermediary/browser caching.
"""

import json
from html.parser import HTMLParser
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, date, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from threading import Lock
from zoneinfo import ZoneInfo

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CALENDAR_HTML_URLS = (
    "https://www.forexfactory.com/calendar?week=next",
    "https://www.forexfactory.com/calendar?goto=upnext",
    "https://www.forexfactory.com/calendar",
)
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


def _normalize_events(data, today, end_day=None):
    if not isinstance(data, list):
        raise RuntimeError("Economic calendar returned an unexpected format.")

    out = []
    seen = set()
    end_day = end_day or (today + timedelta(days=7))
    for item in data:
        if not isinstance(item, dict) or clean(item.get("impact")).lower() not in ("high", "high impact", "high impact expected"):
            continue

        title = clean(item.get("title") or item.get("event"))
        currency = clean(item.get("country") or item.get("currency"))
        event_dt = _event_datetime(item)

        if not title or not currency or event_dt is None:
            continue
        if event_dt.date() < today or event_dt.date() > end_day:
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
    return out[:50]


class _ForexFactoryHTMLParser(HTMLParser):
    """Extract event metadata from Forex Factory calendar rows when weekly JSON ends early."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.events = []
        self._row = None
        self._cell_class = ""
        self._cell_text = []

    @staticmethod
    def _attrs(attrs):
        return {k: v for k, v in attrs if k}

    def handle_starttag(self, tag, attrs):
        a = self._attrs(attrs)
        classes = set((a.get("class") or "").split())
        if self._row is not None and any("impact-red" in c for c in classes):
            self._row["impact"] = "High"
        if tag == "tr" and "calendar__row" in classes:
            self._row = {
                "date": a.get("data-event-datetime") or a.get("data-datetime") or "",
                "title": a.get("data-event-title") or a.get("data-title") or "",
                "currency": a.get("data-event-currency") or a.get("data-currency") or "",
                "impact": a.get("data-event-impact") or a.get("data-impact") or "",
                "actual": a.get("data-event-actual") or "",
                "forecast": a.get("data-event-forecast") or "",
                "previous": a.get("data-event-previous") or "",
            }
        elif self._row is not None and tag == "td":
            self._cell_class = a.get("class", "")
            self._cell_text = []

    def handle_data(self, data):
        if self._row is not None and self._cell_class:
            value = data.strip()
            if value:
                self._cell_text.append(value)

    def handle_endtag(self, tag):
        if self._row is None:
            return
        if tag == "td" and self._cell_class:
            value = " ".join(self._cell_text).strip()
            cls = self._cell_class
            if value:
                if "calendar__currency" in cls and not self._row["currency"]:
                    self._row["currency"] = value
                elif "calendar__event" in cls and not self._row["title"]:
                    self._row["title"] = value
                elif "calendar__impact" in cls and not self._row["impact"]:
                    self._row["impact"] = "High" if "high" in cls.lower() or "icon--ff-impact-red" in cls.lower() else value
                elif "calendar__actual" in cls and not self._row["actual"]:
                    self._row["actual"] = value
                elif "calendar__forecast" in cls and not self._row["forecast"]:
                    self._row["forecast"] = value
                elif "calendar__previous" in cls and not self._row["previous"]:
                    self._row["previous"] = value
            self._cell_class = ""
            self._cell_text = []
        elif tag == "tr":
            row = self._row
            if row and row.get("title") and row.get("currency") and row.get("date"):
                impact = row.get("impact", "")
                if "high" in impact.lower() or "red" in impact.lower():
                    row["impact"] = "High"
                    row["country"] = row.get("currency", "")
                    self.events.append(row)
            self._row = None


def _fetch_url(url, accept="application/json"):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; LM-Analyzer/1.2; +https://lamar-trading-bot.vercel.app)",
            "Accept": accept,
            "Cache-Control": "no-cache, no-store, max-age=0",
            "Pragma": "no-cache",
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=12) as upstream:
        return upstream.read().decode("utf-8", "replace")


def _fetch_calendar_data(now_ts, now_local):
    """Merge weekly JSON with HTML calendar fallback for the next seven days."""
    gathered = []
    errors = []
    refresh_bucket = int(now_ts // CACHE_TTL_SECONDS)
    separator = "&" if "?" in CALENDAR_URL else "?"
    json_url = CALENDAR_URL + separator + urllib.parse.urlencode({"lm_refresh": refresh_bucket})
    try:
        gathered.extend(json.loads(_fetch_url(json_url, "application/json")))
    except Exception as exc:
        errors.append(f"weekly JSON: {type(exc).__name__}")

    # Forex Factory does not reliably publish a next-week JSON feed. The HTML
    # calendar is the fallback for Saturday/Sunday and the upcoming week.
    for base_url in CALENDAR_HTML_URLS:
        try:
            url = base_url + ("&" if "?" in base_url else "?") + urllib.parse.urlencode({"lm_refresh": refresh_bucket})
            html = _fetch_url(url, "text/html,application/xhtml+xml")
            parser = _ForexFactoryHTMLParser()
            parser.feed(html)
            if parser.events:
                gathered.extend(parser.events)
                break
        except Exception as exc:
            errors.append(f"calendar HTML: {type(exc).__name__}")

    if not gathered:
        raise RuntimeError("Economic calendar sources did not return usable events.")
    return _normalize_events(gathered, now_local.date(), now_local.date() + timedelta(days=7))


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

    try:
        events = _fetch_calendar_data(now_ts, now_local)
    except Exception as exc:
        # If the provider briefly fails, allow already-fetched same-day cache only.
        with _CACHE_LOCK:
            if CACHE["day"] == today_text and CACHE["events"]:
                CACHE["stale"] = True
                CACHE["expires"] = time.time() + 60
                return list(CACHE["events"])
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
                    "source": "Forex Factory calendar + weekly JSON",
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
