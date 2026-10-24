"""Exchange a Google ID token for an LM Analyzer Supabase session.

This is an additive Vercel endpoint at POST /api/google_login. It does not
change the existing /api/auth email/password, refresh, or profile handlers.
The Supabase Google provider must be enabled and configured for this project.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple


def _response(payload: Dict[str, Any], status: int = 200) -> Dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json; charset=utf-8",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Cache-Control": "no-store, no-cache, must-revalidate",
        },
        "body": json.dumps(payload, ensure_ascii=False),
    }


def _config() -> Tuple[str, str]:
    base_url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    for suffix in ("/rest/v1", "/auth/v1", "/storage/v1"):
        if base_url.endswith(suffix):
            base_url = base_url[: -len(suffix)].rstrip("/")
    if not base_url or not base_url.startswith("https://"):
        raise RuntimeError("Supabase URL is not configured correctly.")
    if not service_key:
        raise RuntimeError("Supabase server key is not configured.")
    return base_url, service_key


def _request_json(
    base_url: str,
    service_key: str,
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    timeout: int = 25,
) -> Tuple[int, Any]:
    url = base_url + "/" + path.lstrip("/")
    headers = {
        "Accept": "application/json",
        "apikey": service_key,
        "Authorization": "Bearer " + service_key,
    }
    body = None
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(text) if text else {}
            except json.JSONDecodeError:
                parsed = {"message": "Authentication service returned an invalid response."}
            return response.status, parsed
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(text) if text else {}
        except json.JSONDecodeError:
            parsed = {"message": "Authentication request failed."}
        return exc.code, parsed
    except (urllib.error.URLError, TimeoutError):
        return 503, {"message": "Authentication service is temporarily unavailable."}


def _error_message(value: Any, fallback: str) -> str:
    if isinstance(value, dict):
        message = (
            value.get("msg")
            or value.get("message")
            or value.get("error_description")
            or value.get("error")
            or fallback
        )
    else:
        message = fallback
    message = str(message).strip()
    # Avoid leaking HTML or unbounded provider responses to the mobile UI.
    if not message or len(message) > 220 or "<html" in message.lower():
        return fallback
    return message


def _ensure_profile(base_url: str, service_key: str, user: Dict[str, Any]) -> str:
    user_id = str(user.get("id", "")).strip()
    email = str(user.get("email", "")).strip().lower()
    if not user_id or not email:
        raise RuntimeError("Google sign-in returned incomplete account details.")

    query = "/rest/v1/profiles?id=eq." + urllib.parse.quote(user_id, safe="") + "&select=*&limit=1"
    status, rows = _request_json(base_url, service_key, "GET", query)
    if status != 200:
        raise RuntimeError("Your LM Analyzer profile could not be loaded.")
    if isinstance(rows, list) and rows:
        return str((rows[0] or {}).get("full_name", "")).strip()

    metadata = user.get("user_metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    name = (
        str(metadata.get("full_name", "")).strip()
        or str(metadata.get("name", "")).strip()
        or str(metadata.get("given_name", "")).strip()
        or email.split("@", 1)[0]
    )
    insert_headers_key = service_key
    status, inserted = _request_json(
        base_url,
        insert_headers_key,
        "POST",
        "/rest/v1/profiles",
        {"id": user_id, "full_name": name, "email": email},
        timeout=25,
    )
    if status in (200, 201) and isinstance(inserted, list) and inserted:
        return str((inserted[0] or {}).get("full_name", name)).strip() or name

    # A concurrent sign-in or a database trigger may have created it already.
    status, rows = _request_json(base_url, service_key, "GET", query)
    if status == 200 and isinstance(rows, list) and rows:
        return str((rows[0] or {}).get("full_name", name)).strip() or name
    raise RuntimeError("Your LM Analyzer profile could not be created.")


def _get_body(request: Any) -> Dict[str, Any]:
    try:
        value = request.get_json(silent=True)
        if isinstance(value, dict):
            return value
    except Exception:
        pass
    try:
        raw = getattr(request, "body", b"")
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if not raw and hasattr(request, "get_data"):
            raw = request.get_data(as_text=True)
        value = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def handler(request: Any) -> Dict[str, Any]:
    method = str(getattr(request, "method", "POST")).upper()
    if method == "OPTIONS":
        return _response({"success": True}, 200)
    if method != "POST":
        return _response({"error": "Use POST for Google sign-in."}, 405)

    body = _get_body(request)
    id_token = str(body.get("id_token", "")).strip()
    nonce = str(body.get("nonce", "")).strip()
    if not id_token or len(id_token) > 20000:
        return _response({"error": "Google did not provide a valid ID token. Please try again."}, 400)
    if not nonce or len(nonce) > 512:
        return _response({"error": "The Google sign-in security check is missing. Please try again."}, 400)

    try:
        base_url, service_key = _config()
        # Supabase Auth verifies Google's ID token and nonce, then issues the
        # same Supabase access/refresh tokens used by all existing LM Analyzer APIs.
        status, session = _request_json(
            base_url,
            service_key,
            "POST",
            "/auth/v1/token?grant_type=id_token",
            {"provider": "google", "id_token": id_token, "nonce": nonce},
            timeout=30,
        )
        if status not in (200, 201) or not isinstance(session, dict):
            return _response({"error": _error_message(session, "Google sign-in failed. Check the Google provider configuration.")}, status if 400 <= status < 500 else 503)

        user = session.get("user")
        if not isinstance(user, dict) or not user.get("id"):
            return _response({"error": "Google sign-in succeeded without a valid user session."}, 502)

        full_name = _ensure_profile(base_url, service_key, user)
        access_token = str(session.get("access_token", "")).strip()
        refresh_token = str(session.get("refresh_token", "")).strip()
        if not access_token or not refresh_token:
            return _response({"error": "Supabase did not return a renewable LM Analyzer session."}, 502)

        email = str(user.get("email", "")).strip().lower()
        return _response(
            {
                "success": True,
                "message": "Google sign-in successful.",
                "user": {"id": str(user.get("id")), "email": email, "full_name": full_name},
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_in": session.get("expires_in"),
                "token_type": session.get("token_type", "bearer"),
                "session": {"access_token": access_token, "refresh_token": refresh_token},
            },
            200,
        )
    except RuntimeError as exc:
        return _response({"error": str(exc)[:220]}, 503)
    except Exception:
        return _response({"error": "Google sign-in could not be completed. Please try again."}, 500)
