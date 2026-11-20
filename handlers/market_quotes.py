"""Authenticated market quotes for LM Analyzer's MARKETS tab.

Uses Twelve Data's batch /quote endpoint for supported symbols and a shared
Supabase cache so multiple Vercel instances do not repeatedly consume credits.
A quote is never fabricated. US500 is broker/provider-specific, so its exact
provider symbol must be configured explicitly in TWELVE_DATA_US500_SYMBOL.
"""

import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

from analysis_common import json_response
from service_cache import (
    SharedCacheError,
    claim_provider_credits,
    claim_shared_cache_refresh,
    read_shared_cache,
    release_shared_cache_refresh,
    write_shared_cache,
)
from api.user_security import extract_bearer_token, verify_access_token

PROVIDER_URL = "https://api.twelvedata.com/quote"
CACHE_KEY = "market_quotes"
CACHE_SECONDS = 75
PROVIDER_TIMEOUT_SECONDS = 9
MAX_PROVIDER_CREDITS_PER_REFRESH = 8
PROVIDER_CREDIT_LIMIT_PER_MINUTE = 8

MARKET_CATALOG = {
 "Forex":[("EURUSD","Euro / US Dollar","EUR/USD"),("GBPUSD","British Pound / US Dollar","GBP/USD"),("USDJPY","US Dollar / Japanese Yen","USD/JPY"),("USDCHF","US Dollar / Swiss Franc","USD/CHF"),("USDCAD","US Dollar / Canadian Dollar","USD/CAD"),("AUDUSD","Australian Dollar / US Dollar","AUD/USD"),("NZDUSD","New Zealand Dollar / US Dollar","NZD/USD"),("EURGBP","Euro / British Pound","EUR/GBP"),("EURJPY","Euro / Japanese Yen","EUR/JPY"),("GBPJPY","British Pound / Japanese Yen","GBP/JPY"),("AUDJPY","Australian Dollar / Japanese Yen","AUD/JPY"),("EURCHF","Euro / Swiss Franc","EUR/CHF")],
 "Metals":[("XAUUSD","Gold / US Dollar","XAU/USD"),("XAGUSD","Silver / US Dollar","XAG/USD"),("XPTUSD","Platinum / US Dollar","XPT/USD")],
 "Indices":[("US500","S&P 500 Index",os.environ.get("TWELVE_DATA_US500_SYMBOL","").strip()),("NAS100","Nasdaq 100 Index",os.environ.get("TWELVE_DATA_NAS100_SYMBOL","").strip()),("US30","Dow Jones Index",os.environ.get("TWELVE_DATA_US30_SYMBOL","").strip()),("GER40","Germany 40 Index",os.environ.get("TWELVE_DATA_GER40_SYMBOL","").strip()),("UK100","UK 100 Index",os.environ.get("TWELVE_DATA_UK100_SYMBOL","").strip())],
 "Energy":[("WTI","WTI Crude Oil","WTI"),("BRENT","Brent Crude Oil","BRENT"),("COPPER","Copper","XCU/USD")],
 "Crypto":[("BTCUSD","Bitcoin / US Dollar","BTC/USD"),("ETHUSD","Ethereum / US Dollar","ETH/USD"),("SOLUSD","Solana / US Dollar","SOL/USD"),("XRPUSD","XRP / US Dollar","XRP/USD"),("LTCUSD","Litecoin / US Dollar","LTC/USD"),("ADAUSD","Cardano / US Dollar","ADA/USD")],
 "Stocks":[("AAPL","Apple Inc.","AAPL"),("MSFT","Microsoft Corporation","MSFT"),("NVDA","NVIDIA Corporation","NVDA"),("TSLA","Tesla Inc.","TSLA"),("AMZN","Amazon.com Inc.","AMZN"),("GOOGL","Alphabet Inc.","GOOGL"),("META","Meta Platforms Inc.","META")]
}
VALID_CATEGORIES = tuple(MARKET_CATALOG)


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value):
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc).isoformat().replace("+00:00", "Z")
        except (ValueError, OverflowError, OSError):
            return ""
    text = str(value or "").strip()
    if not text:
        return ""
    if text.isdigit():
        try:
            return datetime.fromtimestamp(int(text), timezone.utc).isoformat().replace("+00:00", "Z")
        except (ValueError, OverflowError, OSError):
            return ""
    return text


def _unavailable(item, reason):
    return {
        "symbol": item["symbol"],
        "name": item["name"],
        "category": item["category"],
        "available": False,
        "price": "",
        "change_percent": None,
        "updated_at": "",
        "error": str(reason or "Quote unavailable.")[:180],
    }


def _provider_error(message):
    value = str(message or "Provider did not return a quote.").strip()
    # Never accidentally include the request URL (which contains the API key).
    return value[:180] if "apikey=" not in value.lower() else "The provider rejected the quote request."


def _quote_for_symbol(data, provider_symbol):
    if not isinstance(data, dict):
        return None
    candidate = data.get(provider_symbol)
    if isinstance(candidate, dict):
        return candidate
    norm = provider_symbol.replace("/", "").replace(":", "").upper()
    for key, value in data.items():
        if isinstance(value, dict):
            key_norm = str(key).replace("/", "").replace(":", "").upper()
            symbol_norm = str(value.get("symbol") or "").replace("/", "").replace(":", "").upper()
            if key_norm == norm or symbol_norm == norm:
                return value
    values = data.get("data") or data.get("values")
    if isinstance(values, list):
        for value in values:
            if isinstance(value, dict):
                symbol_norm = str(value.get("symbol") or "").replace("/", "").replace(":", "").upper()
                if symbol_norm == norm:
                    return value
    # A single-symbol response is returned directly by the provider.
    if str(data.get("symbol") or "").replace("/", "").replace(":", "").upper() == norm:
        return data
    return None


def _quote_item(item, provider_row):
    if not isinstance(provider_row, dict):
        return _unavailable(item, "Provider did not return a quote for this symbol.")
    if provider_row.get("status") == "error" or provider_row.get("code"):
        return _unavailable(item, _provider_error(provider_row.get("message")))

    price_raw = provider_row.get("close") or provider_row.get("price") or provider_row.get("last")
    try:
        price = float(price_raw)
        if not math.isfinite(price) or price <= 0:
            raise ValueError("invalid price")
    except (TypeError, ValueError):
        return _unavailable(item, "Provider did not return a valid price for this symbol.")

    percent = None
    try:
        raw_percent = provider_row.get("percent_change")
        if raw_percent not in (None, ""):
            parsed_percent = float(raw_percent)
            if math.isfinite(parsed_percent) and abs(parsed_percent) < 1_000_000:
                percent = parsed_percent
    except (TypeError, ValueError):
        percent = None
    if percent is None:
        try:
            current = float(provider_row.get("close"))
            previous = float(provider_row.get("previous_close"))
            if math.isfinite(current) and math.isfinite(previous) and previous > 0:
                percent = (current / previous - 1.0) * 100.0
        except (TypeError, ValueError):
            percent = None

    return {
        "symbol": item["symbol"],
        "name": item["name"],
        "category": item["category"],
        "available": True,
        "price": str(price_raw).strip(),
        "change_percent": round(percent, 6) if percent is not None else None,
        "updated_at": _parse_time(provider_row.get("last_quote_at") or provider_row.get("timestamp") or provider_row.get("datetime")),
        "error": "",
    }


def _fetch_quotes(api_key, category, previous_payload=None):
    """Refresh only the symbols covered by the shared credit budget.

    Unrefreshed symbols retain their last valid cached quote. A rotating cursor
    ensures every configured instrument gets refreshed over successive cycles.
    """
    items = [{"symbol": s, "name": n, "provider_symbol": p, "category": category}
             for s, n, p in MARKET_CATALOG[category]]
    previous_payload = previous_payload if isinstance(previous_payload, dict) else {}
    previous_rows = {
        str(row.get("symbol")): row
        for row in previous_payload.get("markets", [])
        if isinstance(row, dict) and row.get("symbol")
    }
    configured_items = [item for item in items if item["provider_symbol"]]

    if not configured_items:
        return {
            "markets": [
                _unavailable(item, "Configure the exact provider symbol for this index in Vercel.")
                for item in items
            ],
            "provider": "Twelve Data",
            "configured": True,
            "updated_at": _utc_now(),
            "cached": False,
            "rotation_offset": 0,
        }

    try:
        rotation_offset = int(previous_payload.get("rotation_offset", 0))
    except (TypeError, ValueError):
        rotation_offset = 0
    rotation_offset %= len(configured_items)

    requested_credits = min(len(configured_items), MAX_PROVIDER_CREDITS_PER_REFRESH)
    granted_credits = claim_provider_credits(
        requested_credits,
        credit_limit=PROVIDER_CREDIT_LIMIT_PER_MINUTE,
        window_seconds=60,
    )
    if granted_credits <= 0:
        return None

    selected_items = [
        configured_items[(rotation_offset + index) % len(configured_items)]
        for index in range(granted_credits)
    ]
    symbols = [item["provider_symbol"] for item in selected_items]
    query = urllib.parse.urlencode(
        {"symbol": ",".join(symbols), "apikey": api_key},
        safe="/,",
    )
    request = urllib.request.Request(
        PROVIDER_URL + "?" + query,
        headers={"Accept": "application/json", "User-Agent": "LMAnalyzer/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=PROVIDER_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        try:
            reason = json.loads(exc.read().decode("utf-8", errors="replace")).get("message") or "Provider HTTP error {}".format(exc.code)
        except Exception:
            reason = "Provider HTTP error {}".format(exc.code)
        raise RuntimeError(_provider_error(reason)) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Market provider is temporarily unavailable.") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Market provider returned an invalid response.") from exc

    if not isinstance(data, dict) or data.get("status") == "error" or data.get("code"):
        raise RuntimeError(_provider_error((data or {}).get("message")))

    refreshed_rows = {
        item["symbol"]: _quote_item(item, _quote_for_symbol(data, item["provider_symbol"]))
        for item in selected_items
    }

    results = []
    for item in items:
        symbol = item["symbol"]
        if not item["provider_symbol"]:
            results.append(_unavailable(item, "Exact provider symbol is not configured."))
        elif symbol in refreshed_rows:
            results.append(refreshed_rows[symbol])
        elif symbol in previous_rows:
            # Keep the last real provider value until this symbol's next turn.
            retained = dict(previous_rows[symbol])
            retained["name"] = item["name"]
            retained["category"] = category
            results.append(retained)
        else:
            results.append(_unavailable(item, "Waiting for this instrument's first refresh cycle."))

    return {
        "markets": results,
        "provider": "Twelve Data",
        "configured": True,
        "updated_at": _utc_now(),
        "cached": False,
        "rotation_offset": (rotation_offset + granted_credits) % len(configured_items),
        "last_refresh_symbols": [item["symbol"] for item in selected_items],
        "last_refresh_credits": granted_credits,
    }


def _snapshot_is_fresh(row):
    payload = row.get("payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("markets"), list):
        return False
    raw = str(row.get("updated_at") or "")
    try:
        updated = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - updated).total_seconds() < CACHE_SECONDS
    except (ValueError, TypeError):
        return False


def _response_payload(row, cached):
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
            json_response(self, 401, {"error": str(exc) or "Sign in to load market prices."})
            return

        from urllib.parse import parse_qs, urlsplit
        category = (parse_qs(urlsplit(self.path).query).get("category") or ["Forex"])[0]
        if category not in VALID_CATEGORIES:
            json_response(self, 400, {"error": "Unknown market category."})
            return
        cache_key = "market_quotes_" + category.lower()
        api_key = os.environ.get("TWELVE_DATA_API_KEY", "").strip()
        if not api_key:
            json_response(self, 503, {
                "error": "Live market data is not configured. Add TWELVE_DATA_API_KEY to Vercel Environment Variables and redeploy.",
                "configured": False,
            })
            return

        try:
            snapshot = read_shared_cache(cache_key)
            if _snapshot_is_fresh(snapshot):
                json_response(self, 200, _response_payload(snapshot, True))
                return

            if claim_shared_cache_refresh(cache_key, lock_seconds=25):
                try:
                    previous_payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else {}
                    payload = _fetch_quotes(api_key, category, previous_payload)
                    if payload is None:
                        release_shared_cache_refresh(cache_key)
                        if isinstance(previous_payload.get("markets"), list) and previous_payload.get("markets"):
                            stale_response = _response_payload(snapshot, True)
                            stale_response["stale"] = True
                            stale_response["rate_limited"] = True
                            json_response(self, 200, stale_response)
                        else:
                            json_response(self, 429, {
                                "error": "The shared Twelve Data credit allowance is used for this minute. Existing prices will remain visible; retry after the next minute.",
                                "retry_after_seconds": 60,
                            })
                        return
                    write_shared_cache(cache_key, payload)
                    # Re-read database timestamp written by Supabase to keep the
                    # cache age authoritative across serverless instances.
                    saved = read_shared_cache(cache_key)
                    json_response(self, 200, _response_payload(saved, False))
                    return
                except Exception:
                    release_shared_cache_refresh(cache_key)
                    raise

            # Another Vercel invocation owns the refresh lock. Wait briefly for
            # it to publish a shared result instead of burning duplicate credits.
            for _ in range(6):
                time.sleep(1)
                snapshot = read_shared_cache(cache_key)
                if _snapshot_is_fresh(snapshot):
                    json_response(self, 200, _response_payload(snapshot, True))
                    return
            json_response(self, 503, {"error": "Market prices are refreshing. Please retry shortly."})
        except SharedCacheError as exc:
            json_response(self, 503, {"error": str(exc)})
        except RuntimeError as exc:
            json_response(self, 503, {"error": str(exc) or "Live market data is unavailable."})
        except Exception as exc:
            print("market_quotes error:", str(exc)[:250])
            json_response(self, 500, {"error": "Live market prices could not be loaded."})

    def log_message(self, *_args):
        return
