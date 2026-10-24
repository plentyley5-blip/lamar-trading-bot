"""LM Analyzer Google sign-in endpoint for Vercel: POST /api/google_login."""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler
from typing import Any, Dict, Optional, Tuple


MAX_BODY_BYTES = 64 * 1024
MAX_ID_TOKEN_CHARS = 20_000
MAX_NONCE_CHARS = 512


def _json_bytes(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _send(handler: BaseHTTPRequestHandler, status: int, payload: Dict[str, Any]) -> None:
    body = _json_bytes(payload)
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
    handler.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
    handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


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
    prefer: Optional[str] = None,
) -> Tuple[int, Any]:
    url = base_url + "/" + path.lstrip("/")
    headers = {
        "Accept": "application/json",
        "apikey": service_key,
        "Authorization": "Bearer " + service_key,
    }
    if prefer:
        headers["Prefer"] = prefer
    body = None
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                parsed = {"message": "Authentication service returned an invalid response."}
            return response.status, parsed
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw else {}
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
    if not message or len(message) > 220 or "<html" in message.lower():
        return fallback
    return message


def _ensure_profile(base_url: str, service_key: str, user: Dict[str, Any]) -> str:
    user_id = str(user.get("id", "")).strip()
    email = str(user.get("email", "")).strip().lower()
    if not user_id or not email:
        raise RuntimeError("Google sign-in returned incomplete account details.")

    query = (
        "/rest/v1/profiles?id=eq."
        + urllib.parse.quote(user_id, safe="")
        + "&select=*&limit=1"
    )
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
    status, inserted = _request_json(
        base_url,
        service_key,
        "POST",
        "/rest/v1/profiles",
        {"id": user_id, "full_name": name, "email": email},
        timeout=25,
        prefer="return=representation",
    )
    if status in (200, 201) and isinstance(inserted, list) and inserted:
        return str((inserted[0] or {}).get("full_name", name)).strip() or name

    # A trigger or parallel sign-in may have created the profile concurrently.
    status, rows = _request_json(base_url, service_key, "GET", query)
    if status == 200 and isinstance(rows, list) and rows:
        return str((rows[0] or {}).get("full_name", name)).strip() or name
    raise RuntimeError("Your LM Analyzer profile could not be created.")


def _process_google_login(body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    id_token = str(body.get("id_token", "")).strip()
    nonce = str(body.get("nonce", "")).strip()
    if not id_token or len(id_token) > MAX_ID_TOKEN_CHARS:
        return 400, {"error": "Google did not provide a valid ID token. Please try again."}
    if not nonce or len(nonce) > MAX_NONCE_CHARS:
        return 400, {"error": "The Google sign-in security check is missing. Please try again."}

    try:
        base_url, service_key = _config()
        status, session = _request_json(
            base_url,
            service_key,
            "POST",
            "/auth/v1/token?grant_type=id_token",
            {"provider": "google", "id_token": id_token, "nonce": nonce},
            timeout=30,
        )
        if status not in (200, 201) or not isinstance(session, dict):
            if 400 <= status < 500:
                return status, {"error": _error_message(session, "Google sign-in failed. Check the Google provider configuration.")}
            return 503, {"error": "Google sign-in service is temporarily unavailable. Please try again."}

        user = session.get("user")
        if not isinstance(user, dict) or not user.get("id"):
            return 502, {"error": "Google sign-in succeeded without a valid user session."}

        full_name = _ensure_profile(base_url, service_key, user)
        access_token = str(session.get("access_token", "")).strip()
        refresh_token = str(session.get("refresh_token", "")).strip()
        if not access_token or not refresh_token:
            return 502, {"error": "Supabase did not return a renewable LM Analyzer session."}

        email = str(user.get("email", "")).strip().lower()
        return 200, {
            "success": True,
            "message": "Google sign-in successful.",
            "user": {"id": str(user.get("id")), "email": email, "full_name": full_name},
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in": session.get("expires_in"),
            "token_type": session.get("token_type", "bearer"),
            "session": {"access_token": access_token, "refresh_token": refresh_token},
        }
    except RuntimeError as exc:
        return 503, {"error": str(exc)[:220]}
    except Exception:
        return 500, {"error": "Google sign-in could not be completed. Please try again."}


class handler(BaseHTTPRequestHandler):
    """Vercel Python Function request handler."""

    def do_OPTIONS(self) -> None:
        _send(self, 200, {"success": True})

    def do_GET(self) -> None:
        _send(self, 405, {"error": "Use POST for Google sign-in."})

    def do_POST(self) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            _send(self, 400, {"error": "Invalid request body length."})
            return
        if content_length < 0 or content_length > MAX_BODY_BYTES:
            _send(self, 413, {"error": "Request body is too large."})
            return
        try:
            raw = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("JSON body must be an object.")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            _send(self, 400, {"error": "Invalid JSON request body."})
            return
        status, result = _process_google_login(body)
        _send(self, status, result)

    def log_message(self, format: str, *args: Any) -> None:
        # Keep tokens and sign-in details out of routine request logs.
        return
