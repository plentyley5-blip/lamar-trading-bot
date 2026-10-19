"""Small shared Supabase cache for Vercel serverless functions.

Requires sql/lm_service_cache.sql to be run once in the Supabase SQL Editor.
Uses the existing api.user_security Supabase environment helpers.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from api.user_security import get_supabase_service_key, get_supabase_url


class SharedCacheError(RuntimeError):
    pass


def _config():
    base = get_supabase_url().strip().rstrip("/")
    key = get_supabase_service_key().strip()
    if not base or not key:
        raise SharedCacheError("Supabase cache configuration is missing.")
    return base, key


def _headers(key, include_content_type=False):
    headers = {
        "apikey": key,
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
    }
    if include_content_type:
        headers["Content-Type"] = "application/json"
    return headers


def _request_json(method, url, key, body=None, timeout=8, prefer=None):
    headers = _headers(key, include_content_type=body is not None)
    if prefer:
        headers["Prefer"] = prefer
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        if exc.code in (400, 404, 406, 409, 422):
            raise SharedCacheError(
                "Supabase shared cache is not ready. Run sql/lm_service_cache.sql in Supabase SQL Editor."
            ) from exc
        raise SharedCacheError("Supabase shared cache request failed (HTTP {}).".format(exc.code)) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SharedCacheError("Supabase shared cache is temporarily unavailable.") from exc

    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SharedCacheError("Supabase returned an invalid shared-cache response.") from exc


def read_shared_cache(cache_key):
    base, key = _config()
    params = urllib.parse.urlencode(
        {"select": "cache_key,payload,updated_at,refresh_lock_until", "cache_key": "eq." + cache_key, "limit": "1"},
        safe="(),.*:",
    )
    result = _request_json("GET", base + "/rest/v1/lm_service_cache?" + params, key)
    if not isinstance(result, list) or not result or not isinstance(result[0], dict):
        raise SharedCacheError(
            "Supabase shared-cache rows are missing. Run sql/lm_service_cache.sql in Supabase SQL Editor."
        )
    return result[0]


def claim_shared_cache_refresh(cache_key, lock_seconds=25):
    base, key = _config()
    result = _request_json(
        "POST",
        base + "/rest/v1/rpc/claim_lm_service_cache_refresh",
        key,
        body={"p_cache_key": cache_key, "p_lock_seconds": int(lock_seconds)},
        timeout=8,
    )
    return result is True


def write_shared_cache(cache_key, payload):
    base, key = _config()
    params = urllib.parse.urlencode({"cache_key": "eq." + cache_key}, safe="(),.*:")
    body = {
        "payload": payload,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "refresh_lock_until": None,
    }
    _request_json(
        "PATCH",
        base + "/rest/v1/lm_service_cache?" + params,
        key,
        body=body,
        timeout=8,
        prefer="return=minimal",
    )


def release_shared_cache_refresh(cache_key):
    try:
        base, key = _config()
        params = urllib.parse.urlencode({"cache_key": "eq." + cache_key}, safe="(),.*:")
        _request_json(
            "PATCH",
            base + "/rest/v1/lm_service_cache?" + params,
            key,
            body={"refresh_lock_until": None},
            timeout=5,
            prefer="return=minimal",
        )
    except Exception:
        # The lease expires automatically; release is best-effort only.
        pass
