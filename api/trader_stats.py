"""Aggregate real, completed LM Analyzer analyses from the previous 24 hours.

BUY/SELL sentiment uses directional results only. NO TRADE results never enter
that percentage denominator. Shared Supabase caching avoids a full history scan
for every user every minute.
"""

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler

from analysis_common import json_response
from service_cache import (
    SharedCacheError,
    claim_shared_cache_refresh,
    read_shared_cache,
    release_shared_cache_refresh,
    write_shared_cache,
)
from api.user_security import (
    extract_bearer_token,
    get_supabase_service_key,
    get_supabase_url,
    verify_access_token,
)

CACHE_KEY = "trader_stats"
CACHE_SECONDS = 60
PAGE_SIZE = 1000
MAX_ROWS = 10000
TRACKED_SYMBOLS = ("XAUUSD", "EURUSD", "BTCUSD")


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _supabase_headers():
    key = get_supabase_service_key().strip()
    if not key:
        raise RuntimeError("Supabase service configuration is missing.")
    return {
        "apikey": key,
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
    }


def _fetch_page(since, offset):
    base = get_supabase_url().strip().rstrip("/")
    if not base:
        raise RuntimeError("Supabase URL is missing.")
    params = {
        "status": "eq.completed",
        "created_at": "gte." + since,
        "select": "id,instrument,created_at,openai_response_id",
        "order": "created_at.desc",
        "limit": str(PAGE_SIZE),
        "offset": str(offset),
    }
    url = base + "/rest/v1/analysis_jobs?" + urllib.parse.urlencode(params, safe="(),.*:")
    request = urllib.request.Request(url, headers=_supabase_headers(), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            raw = response.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
    except urllib.error.HTTPError as exc:
        # Don't log any keys or access tokens. The message is server-side only.
        print("trader_stats Supabase query failed with HTTP", exc.code)
        raise RuntimeError("Activity statistics could not be loaded from the database.") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Supabase is temporarily unavailable.") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("Supabase returned an invalid analysis-history response.") from exc
    if not isinstance(data, list):
        raise RuntimeError("Supabase returned an invalid analysis-history response.")
    return data


def _decode_result(row):
    encoded = str(row.get("openai_response_id") or "").strip()
    if not encoded or encoded == "pending":
        return None
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        result = json.loads(decoded)
        return result if isinstance(result, dict) else None
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        # Existing historical rows may contain a provider response ID instead
        # of the encoded completed result. They must not be guessed or counted.
        return None


def _normalize_signal(value):
    signal = str(value or "").strip().upper().replace("_", " ").replace("-", " ")
    return " ".join(signal.split())


def _normalize_symbol(value):
    symbol = str(value or "").strip().upper()
    if "|" in symbol:
        symbol = symbol.split("|", 1)[0].strip()
    return symbol


def _build_stats(rows, since, coverage_limited=False):
    buys = sells = no_trade = 0
    attention_counts = {symbol: 0 for symbol in TRACKED_SYMBOLS}
    counted_rows = 0

    for row in rows:
        if not isinstance(row, dict):
            continue
        result = _decode_result(row)
        if not result:
            continue
        instrument = _normalize_symbol(row.get("instrument") or result.get("instrument"))
        if not instrument:
            continue

        counted_rows += 1
        signal = _normalize_signal(result.get("signal") or result.get("trade_idea"))
        if signal == "BUY" or signal.startswith("BUY "):
            buys += 1
        elif signal == "SELL" or signal.startswith("SELL "):
            sells += 1
        elif signal in {"NO TRADE", "NO SETUP", "NO SIGNAL"}:
            no_trade += 1

        if instrument in attention_counts:
            attention_counts[instrument] += 1

    directional = buys + sells
    buy_percent = buys / directional * 100.0 if directional else 0.0
    sell_percent = sells / directional * 100.0 if directional else 0.0
    attention = [
        {"symbol": symbol, "count": attention_counts[symbol]}
        for symbol in TRACKED_SYMBOLS
    ]
    attention.sort(key=lambda item: (-item["count"], TRACKED_SYMBOLS.index(item["symbol"])))

    return {
        "total_analyses": counted_rows,
        "buys": buys,
        "sells": sells,
        "no_trade": no_trade,
        "buy_percent": round(buy_percent, 1),
        "sell_percent": round(sell_percent, 1),
        "attention": attention,
        "window_start": since,
        "updated_at": _utc_now(),
        "coverage_limited": bool(coverage_limited),
        "definition": "Completed LM Analyzer analysis results, not actual broker positions.",
    }


def _calculate_stats():
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat().replace("+00:00", "Z")
    rows = []
    offset = 0
    coverage_limited = False
    while offset < MAX_ROWS:
        page = _fetch_page(since, offset)
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += len(page)
    if len(rows) >= MAX_ROWS:
        coverage_limited = True
    return _build_stats(rows, since, coverage_limited)


def _cache_is_fresh(row):
    payload = row.get("payload")
    if not isinstance(payload, dict) or "total_analyses" not in payload:
        return False
    raw = str(row.get("updated_at") or "")
    try:
        updated = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - updated).total_seconds() < CACHE_SECONDS
    except (ValueError, TypeError):
        return False


def _payload_from_cache(row, cached):
    payload = dict(row.get("payload") or {})
    payload["cached"] = bool(cached)
    payload["cache_updated_at"] = str(row.get("updated_at") or "")
    return payload


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        json_response(self, 204, {})

    def do_GET(self):
        try:
            token = extract_bearer_token(self)
            verify_access_token(token)
        except Exception as exc:
            json_response(self, 401, {"error": str(exc) or "Sign in to load activity statistics."})
            return

        try:
            snapshot = read_shared_cache(CACHE_KEY)
            if _cache_is_fresh(snapshot):
                json_response(self, 200, _payload_from_cache(snapshot, True))
                return

            if claim_shared_cache_refresh(CACHE_KEY, lock_seconds=35):
                try:
                    payload = _calculate_stats()
                    write_shared_cache(CACHE_KEY, payload)
                    saved = read_shared_cache(CACHE_KEY)
                    json_response(self, 200, _payload_from_cache(saved, False))
                    return
                except Exception:
                    release_shared_cache_refresh(CACHE_KEY)
                    raise

            # Another invocation is refreshing the global statistics snapshot.
            # Wait briefly so simultaneous app users share one database scan.
            import time
            for _ in range(7):
                time.sleep(1)
                snapshot = read_shared_cache(CACHE_KEY)
                if _cache_is_fresh(snapshot):
                    json_response(self, 200, _payload_from_cache(snapshot, True))
                    return
            json_response(self, 503, {"error": "Trader statistics are refreshing. Please retry shortly."})
        except SharedCacheError as exc:
            json_response(self, 503, {"error": str(exc)})
        except RuntimeError as exc:
            json_response(self, 503, {"error": str(exc)})
        except Exception as exc:
            print("trader_stats unexpected error:", str(exc)[:250])
            json_response(self, 500, {"error": "Activity statistics request failed."})

    def log_message(self, *_args):
        return
