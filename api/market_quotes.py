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

BASE_SYMBOLS = [
    {"symbol": "XAUUSD", "name": "Gold / US Dollar", "provider_symbol": "XAU/USD"},
    {"symbol": "EURUSD", "name": "Euro / US Dollar", "provider_symbol": "EUR/USD"},
    {"symbol": "GBPUSD", "name": "British Pound / US Dollar", "provider_symbol": "GBP/USD"},
    {"symbol": "USDJPY", "name": "US Dollar / Japanese Yen", "provider_symbol": "USD/JPY"},
    {"symbol": "BTCUSD", "name": "Bitcoin / US Dollar", "provider_symbol": "BTC/USD"},
]


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
        "available": True,
        "price": str(price_raw).strip(),
        "change_percent": round(percent, 6) if percent is not None else None,
        "updated_at": _parse_time(provider_row.get("last_quote_at") or provider_row.get("timestamp") or provider_row.get("datetime")),
        "error": "",
    }


def _fetch_quotes(api_key):
    items = list(BASE_SYMBOLS)
    us500_symbol = os.environ.get("TWELVE_DATA_US500_SYMBOL", "").strip()
    if us500_symbol:
        items.append({
            "symbol": "US500",
            "name": "S&P 500 / US500 (configured provider symbol)",
            "provider_symbol": us500_symbol,
        })
    else:
        items.append({
            "symbol": "US500",
            "name": "S&P 500 / US500",
            "provider_symbol": "",
        })

    provider_symbols = [item["provider_symbol"] for item in items if item["provider_symbol"]]
    if not provider_symbols:
        raise RuntimeError("No market symbols have been configured.")

    query = urllib.parse.urlencode(
        {"symbol": ",".join(provider_symbols), "apikey": api_key},
        safe="/,",
    )
    request = urllib.request.Request(
        PROVIDER_URL + "?" + query,
        headers={"Accept": "application/json", "User-Agent": "LMAnalyzer/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=PROVIDER_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8", errors="replace"))
            reason = payload.get("message") or "Provider HTTP error {}".format(exc.code)
        except Exception:
            reason = "Provider HTTP error {}".format(exc.code)
        raise RuntimeError(_provider_error(reason)) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Market provider is temporarily unavailable.") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Market provider returned an invalid response.") from exc

    if not isinstance(data, dict):
        raise RuntimeError("Market provider returned an invalid response.")
    if data.get("status") == "error" or data.get("code"):
        raise RuntimeError(_provider_error(data.get("message")))

    results = []
    for item in items:
        provider_symbol = item["provider_symbol"]
        if not provider_symbol:
            results.append(_unavailable(
                item,
                "US500 is a broker-specific index/CFD. Configure TWELVE_DATA_US500_SYMBOL only after confirming an exact, licensed provider symbol; no proxy price is substituted.",
            ))
            continue
        quote = _quote_for_symbol(data, provider_symbol)
        results.append(_quote_item(item, quote))

    return {
        "markets": results,
        "provider": "Twelve Data",
        "configured": True,
        "updated_at": _utc_now(),
        "cached": False,
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

        api_key = os.environ.get("TWELVE_DATA_API_KEY", "").strip()
        if not api_key:
            json_response(self, 503, {
                "error": "Live market data is not configured. Add TWELVE_DATA_API_KEY to Vercel Environment Variables and redeploy.",
                "configured": False,
            })
            return

        try:
            snapshot = read_shared_cache(CACHE_KEY)
            if _snapshot_is_fresh(snapshot):
                json_response(self, 200, _response_payload(snapshot, True))
                return

            if claim_shared_cache_refresh(CACHE_KEY, lock_seconds=25):
                try:
                    payload = _fetch_quotes(api_key)
                    write_shared_cache(CACHE_KEY, payload)
                    # Re-read database timestamp written by Supabase to keep the
                    # cache age authoritative across serverless instances.
                    saved = read_shared_cache(CACHE_KEY)
                    json_response(self, 200, _response_payload(saved, False))
                    return
                except Exception:
                    release_shared_cache_refresh(CACHE_KEY)
                    raise

            # Another Vercel invocation owns the refresh lock. Wait briefly for
            # it to publish a shared result instead of burning duplicate credits.
            for _ in range(6):
                time.sleep(1)
                snapshot = read_shared_cache(CACHE_KEY)
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
