"""LM Analyzer analysis-status endpoint compatible with queued analysis_submit jobs."""

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

from analysis_common import json_response
from api.user_security import (
    extract_bearer_token,
    get_supabase_service_key,
    get_supabase_url,
    verify_access_token,
)

JOB_TTL_SECONDS = 24 * 60 * 60


def _job_secret():
    # Must match api/analysis_submit.py:get_job_secret exactly.
    secret = os.environ.get("JOB_TOKEN_SECRET", "").strip()
    if not secret:
        secret = os.environ.get("GEMINI_API_KEY", "").strip()
    if not secret:
        raise RuntimeError("JOB_TOKEN_SECRET is not configured.")
    return secret.encode("utf-8")


def _read_job_token(token):
    """Verify the exact HMAC/base64 format emitted by analysis_submit.py."""
    if not isinstance(token, str) or not token or "." not in token:
        raise ValueError("Invalid analysis job.")

    encoded_payload, encoded_signature = token.split(".", 1)
    if not encoded_payload or not encoded_signature:
        raise ValueError("Invalid analysis job.")

    expected_signature = hmac.new(
        _job_secret(), encoded_payload.encode("ascii"), hashlib.sha256
    ).digest()
    expected_encoded_signature = base64.urlsafe_b64encode(
        expected_signature
    ).decode("ascii").rstrip("=")
    if not hmac.compare_digest(encoded_signature, expected_encoded_signature):
        raise ValueError("Invalid analysis job signature.")

    try:
        padded = encoded_payload + "=" * (-len(encoded_payload) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid analysis job.") from exc

    if not isinstance(payload, dict):
        raise ValueError("Invalid analysis job.")

    job_nonce = str(payload.get("job_nonce", "")).strip()
    instrument = str(payload.get("instrument", "")).strip().upper()
    trade_focus = str(payload.get("trade_focus", "")).strip().upper()
    user_id = str(payload.get("user_id", "")).strip()
    try:
        created_at = int(payload.get("created_at", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid analysis job.") from exc

    if not job_nonce or not user_id or not instrument or not trade_focus or created_at <= 0:
        raise ValueError("Invalid analysis job.")
    age = int(time.time()) - created_at
    if age > JOB_TTL_SECONDS or age < -60:
        raise ValueError("This analysis job has expired or has an invalid timestamp.")

    return job_nonce, instrument, trade_focus, user_id


def _load_job(user_id, job_nonce):
    """Read by job_nonce because analysis_submit.py stores UUID id separately."""
    base = get_supabase_url().strip().rstrip("/")
    key = get_supabase_service_key().strip()
    if not base or not key:
        raise RuntimeError("Analysis database is not configured.")

    query = urllib.parse.urlencode(
        {
            "select": "job_nonce,user_id,instrument,trade_focus,status,openai_response_id,error_message,created_at",
            "job_nonce": "eq." + job_nonce,
            "user_id": "eq." + user_id,
            "limit": "1",
        },
        safe="(),.",
    )
    url = base + "/rest/v1/analysis_jobs?" + query
    request = urllib.request.Request(
        url,
        headers={
            "apikey": key,
            "Authorization": "Bearer " + key,
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError("Unable to read analysis job: " + detail[:1000]) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Analysis status is temporarily unavailable.") from exc

    try:
        rows = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Invalid response from the analysis database.") from exc
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return None
    return rows[0]


def _decode_result(encoded):
    if not isinstance(encoded, str) or not encoded or encoded == "pending":
        raise RuntimeError("Completed analysis has no saved result.")
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        result = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("The saved analysis result is invalid.") from exc
    if not isinstance(result, dict):
        raise RuntimeError("The saved analysis result is invalid.")
    return result


def _check_optional_session(handler, token_user_id):
    """The signed job token authorizes polling; a valid supplied session must match its owner.

    The job token is a signed capability. If the Android access token has expired,
    polling can continue; if it is valid and belongs to another account, reject it.
    """
    auth = str(handler.headers.get("Authorization", "")).strip()
    if not auth:
        return
    try:
        bearer = extract_bearer_token(handler)
        user = verify_access_token(bearer)
    except Exception:
        # Expired/invalid access token does not override a valid signed job token.
        return
    if isinstance(user, dict):
        session_user_id = str(user.get("id", "")).strip()
        if session_user_id and session_user_id != token_user_id:
            raise PermissionError("This analysis job does not belong to this user.")


def _get_job_id(handler):
    parsed = urllib.parse.urlparse(handler.path)
    values = urllib.parse.parse_qs(parsed.query).get("job_id", [])
    return str(values[0]).strip() if values else ""


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        json_response(self, 204, {})

    def do_GET(self):
        try:
            job_token = _get_job_id(self)
            if not job_token:
                json_response(self, 400, {"error": "Analysis job ID is missing."})
                return

            job_nonce, token_instrument, token_focus, token_user_id = _read_job_token(job_token)
            _check_optional_session(self, token_user_id)

            job = _load_job(token_user_id, job_nonce)
            if job is None:
                json_response(self, 404, {"error": "Analysis job was not found."})
                return

            # Enforce ownership against the row as well as against the signed token.
            if str(job.get("user_id", "")).strip() != token_user_id:
                json_response(self, 403, {"error": "This analysis job does not belong to this user."})
                return

            status = str(job.get("status", "queued")).strip().lower() or "queued"
            if status in {"queued", "pending"}:
                json_response(self, 200, {"status": "queued", "job_id": job_token, "poll_after_seconds": 2})
                return
            if status in {"processing", "in_progress", "running"}:
                json_response(self, 200, {"status": "processing", "job_id": job_token, "poll_after_seconds": 2})
                return
            if status == "completed":
                result = _decode_result(str(job.get("openai_response_id", "")).strip())
                json_response(self, 200, {"status": "completed", "job_id": job_token, "result": result})
                return
            if status in {"failed", "cancelled", "expired", "incomplete"}:
                error = str(job.get("error_message", "")).strip() or "The analysis did not complete."
                json_response(self, 200, {"status": "failed", "job_id": job_token, "error": error})
                return

            json_response(self, 200, {"status": "processing", "job_id": job_token, "poll_after_seconds": 3})

        except PermissionError as exc:
            json_response(self, 403, {"error": str(exc)})
        except ValueError as exc:
            json_response(self, 400, {"error": str(exc)})
        except RuntimeError as exc:
            json_response(self, 503, {"error": str(exc)})
        except Exception:
            json_response(self, 500, {"error": "Analysis status could not be read."})

    def log_message(self, format, *args):
        return
