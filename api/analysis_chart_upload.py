"""Authenticated upload endpoint for one optional LM Analyzer chart.

The Android client uploads each selected optional timeframe separately. This
avoids sending a multi-image base64 payload through a single Vercel Function
request (Vercel's documented request/response body limit is 4.5 MB).
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler

from analysis_common import json_response, validate_image_data_url
from api.user_security import extract_bearer_token, verify_access_token

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
MAX_REQUEST_BYTES = 3 * 1024 * 1024
OPTIONAL_TIMEFRAMES = ("1D", "1H", "5M", "1M")


def supabase_request(method, path, payload=None, timeout=20, extra_headers=None):
    if not SUPABASE_URL:
        raise RuntimeError("SUPABASE_URL is not configured.")
    if not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY is not configured.")
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": "Bearer " + SUPABASE_SERVICE_ROLE_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    data = None if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(SUPABASE_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError("Supabase request failed ({}): {}".format(exc.code, detail[:1200])) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Supabase connection failed: " + str(exc)) from exc


def read_json(handler):
    try:
        size = int(handler.headers.get("Content-Length", "0"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid Content-Length.") from exc
    if size <= 0:
        raise ValueError("Request body is empty.")
    if size > MAX_REQUEST_BYTES:
        raise ValueError("One chart upload is too large. Please choose a smaller screenshot.")
    try:
        data = json.loads(handler.rfile.read(size).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid JSON request.") from exc
    if not isinstance(data, dict):
        raise ValueError("Invalid request body.")
    return data


def authenticate_user(handler):
    token = extract_bearer_token(handler)
    user = verify_access_token(token)
    if not isinstance(user, dict) or not str(user.get("id", "")).strip():
        raise ValueError("Please sign in again before uploading charts.")
    return user


def cleanup_expired_uploads_best_effort():
    # Bound abandoned uploads to roughly 24 hours. Cleanup is deliberately
    # best-effort and must not prevent a valid chart upload.
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    try:
        supabase_request(
            "DELETE",
            "/rest/v1/analysis_chart_uploads?created_at=lt." + urllib.parse.quote(cutoff, safe=""),
            timeout=10,
            extra_headers={"Prefer": "return=minimal"},
        )
    except Exception:
        pass


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        json_response(self, 204, {})

    def do_GET(self):
        json_response(self, 200, {"status": "analysis chart upload online"})

    def do_POST(self):
        try:
            user = authenticate_user(self)
            user_id = str(user.get("id", "")).strip()
            body = read_json(self)
            timeframe = str(body.get("timeframe", "")).strip().upper()
            if timeframe not in OPTIONAL_TIMEFRAMES:
                raise ValueError("Unsupported optional timeframe. Use 1D, 1H, 5M, or 1M.")
            image_data_url = validate_image_data_url(body.get("image_data_url"), timeframe + " chart")
            upload_id = str(uuid.uuid4())
            cleanup_expired_uploads_best_effort()
            result = supabase_request(
                "POST",
                "/rest/v1/analysis_chart_uploads",
                {
                    "id": upload_id,
                    "user_id": user_id,
                    "timeframe": timeframe,
                    "image_data_url": image_data_url,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
                timeout=25,
                extra_headers={"Prefer": "return=minimal"},
            )
            json_response(self, 201, {"upload_id": upload_id, "timeframe": timeframe, "status": "uploaded"})
        except ValueError as exc:
            json_response(self, 400, {"error": str(exc)})
        except Exception as exc:
            message = str(exc).strip() or "The chart could not be uploaded."
            json_response(self, 500, {"error": message[:500]})


def handler_entrypoint(request, context=None):
    return handler
