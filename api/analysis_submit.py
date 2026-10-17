"""LM Analyzer chart-analysis submission endpoint.

Queue-only endpoint: validates the request, authenticates the user, reserves
one daily analysis slot when applicable, stores a queued job in Supabase, and
returns a signed job token consumed by the existing analysis_status/worker
flow. It deliberately does not bypass subscription or daily-limit checks.
"""

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler

from analysis_common import (
    json_response,
    validate_focus,
    validate_image_data_url,
    validate_instrument,
)
from api.user_security import (
    extract_bearer_token,
    is_owner_user,
    release_analysis_slot,
    reserve_analysis_slot,
    verify_access_token,
)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
MAX_REQUEST_BYTES = 25 * 1024 * 1024


def get_job_secret():
    # Keep the same precedence as the existing job-token verification flow.
    secret = os.environ.get("JOB_TOKEN_SECRET", "").strip()
    if not secret:
        secret = os.environ.get("GEMINI_API_KEY", "").strip()
    if not secret:
        raise RuntimeError("JOB_TOKEN_SECRET is not configured.")
    return secret.encode("utf-8")


def create_job_token(job_nonce, instrument, trade_focus, user_id):
    payload = {
        "job_nonce": job_nonce,
        "instrument": instrument,
        "trade_focus": trade_focus,
        "user_id": user_id,
        "created_at": int(time.time()),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    signature = hmac.new(get_job_secret(), encoded.encode("ascii"), hashlib.sha256).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return encoded + "." + encoded_signature


def supabase_request(method, path, payload=None, timeout=30, extra_headers=None):
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

    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        SUPABASE_URL + path,
        data=data,
        headers=headers,
        method=method,
    )

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
        raise RuntimeError("Supabase request failed ({}): {}".format(exc.code, detail[:2500])) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Supabase connection failed: " + str(exc)) from exc


def create_queued_job(user_id, instrument, trade_focus, higher_timeframe_image, lower_timeframe_image):
    job_nonce = uuid.uuid4().hex
    payload = {
        "job_nonce": job_nonce,
        "user_id": user_id,
        "instrument": instrument,
        "trade_focus": trade_focus,
        "higher_timeframe_image": higher_timeframe_image,
        "lower_timeframe_image": lower_timeframe_image,
        "status": "queued",
        "openai_response_id": None,
        "error_message": None,
    }
    result = supabase_request(
        "POST",
        "/rest/v1/analysis_jobs",
        payload,
        timeout=30,
        extra_headers={"Prefer": "return=representation"},
    )
    if not result:
        raise RuntimeError("The analysis job could not be created.")
    return job_nonce


def read_json(handler):
    try:
        content_length = int(handler.headers.get("Content-Length", "0"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid Content-Length.") from exc
    if content_length <= 0:
        raise ValueError("Request body is empty.")
    if content_length > MAX_REQUEST_BYTES:
        raise ValueError("Request body is too large.")
    raw = handler.rfile.read(content_length)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid JSON request.") from exc
    if not isinstance(data, dict):
        raise ValueError("Invalid request body.")
    return data


def authenticate_user(handler):
    token = extract_bearer_token(handler)
    user = verify_access_token(token)
    if not isinstance(user, dict):
        raise ValueError("Invalid authentication response.")
    return user


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self):
        json_response(self, 200, {"status": "analysis submit online"})

    def do_POST(self):
        reserved_slot = False
        user_id = ""
        try:
            user = authenticate_user(self)
            user_id = str(user.get("id", "")).strip()
            if not user_id:
                raise ValueError("Invalid authenticated user.")

            body = read_json(self)
            instrument = str(body.get("instrument", "")).strip().upper()
            trade_focus = str(body.get("trade_focus", "")).strip().upper()
            higher_image = body.get("higher_timeframe_image")
            lower_image = body.get("lower_timeframe_image")

            # Use the project's existing validators and supported instrument list.
            validate_instrument(instrument)
            validate_focus(trade_focus)
            validate_image_data_url(higher_image, "higher_timeframe_image")
            validate_image_data_url(lower_image, "lower_timeframe_image")

            owner = is_owner_user(user)
            if not owner:
                reserve_analysis_slot(user_id)
                reserved_slot = True

            job_nonce = create_queued_job(
                user_id=user_id,
                instrument=instrument,
                trade_focus=trade_focus,
                higher_timeframe_image=higher_image,
                lower_timeframe_image=lower_image,
            )
            job_token = create_job_token(job_nonce, instrument, trade_focus, user_id)
            reserved_slot = False  # Reservation belongs to the successfully queued job.

            json_response(self, 202, {
                "status": "queued",
                "job_id": job_token,
                "job_nonce": job_nonce,
                "poll_after_seconds": 2,
                "is_owner": owner,
                "daily_limit": None if owner else 4,
            })

        except ValueError as exc:
            if reserved_slot and user_id:
                try:
                    release_analysis_slot(user_id)
                except Exception:
                    pass
            json_response(self, 400, {"error": str(exc)})

        except RuntimeError as exc:
            if reserved_slot and user_id:
                try:
                    release_analysis_slot(user_id)
                except Exception:
                    pass
            message = str(exc)
            lowered = message.lower()
            status = 429 if any(word in lowered for word in ("limit", "daily", "analyses")) else 500
            json_response(self, status, {"error": message})

        except Exception as exc:
            if reserved_slot and user_id:
                try:
                    release_analysis_slot(user_id)
                except Exception:
                    pass
            json_response(self, 500, {"error": "Analysis submission failed: " + str(exc)})

    def log_message(self, format, *args):
        # Leave platform logs concise; do not log submitted chart images or tokens.
        return
