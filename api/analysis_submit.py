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
from datetime import datetime, timezone
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
WORKER_WAKE_TIMEOUT_SECONDS = 4
DEFAULT_PRODUCTION_HOST = "lamar-trading-bot.vercel.app"


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
    # These fields are required by the existing Supabase schema and worker.
    # openai_response_id is NOT NULL; the worker replaces "pending" with
    # the encoded Gemini analysis after processing the queued job.
    job_id = str(uuid.uuid4())
    job_nonce = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "id": job_id,
        "job_nonce": job_nonce,
        "user_id": user_id,
        "instrument": instrument,
        "trade_focus": trade_focus,
        "higher_timeframe_image": higher_timeframe_image,
        "lower_timeframe_image": lower_timeframe_image,
        "status": "queued",
        "openai_response_id": "pending",
        "error_message": None,
        "created_at": created_at,
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


def _production_host():
    """Use the stable production domain for production worker calls."""
    target_env = os.environ.get("VERCEL_ENV", "").strip().lower()
    if target_env == "production":
        host = (
            os.environ.get("VERCEL_PROJECT_PRODUCTION_URL", "").strip()
            or os.environ.get("VERCEL_URL", "").strip()
            or DEFAULT_PRODUCTION_HOST
        )
    else:
        host = (
            os.environ.get("VERCEL_URL", "").strip()
            or os.environ.get("VERCEL_PROJECT_PRODUCTION_URL", "").strip()
            or DEFAULT_PRODUCTION_HOST
        )
    host = host.removeprefix("https://").removeprefix("http://")
    return host.split("/", 1)[0].strip()


def _mark_failed_if_still_queued(job_nonce, user_id, reason, release_reserved_slot=False):
    """Fail a stuck job only if a worker has not already claimed it."""
    nonce = urllib.parse.quote(job_nonce, safe="")
    uid = urllib.parse.quote(user_id, safe="")
    query = (
        "/rest/v1/analysis_jobs?job_nonce=eq." + nonce
        + "&user_id=eq." + uid
        + "&status=eq.queued"
    )
    try:
        updated = supabase_request(
            "PATCH",
            query,
            {
                "status": "failed",
                "error_message": (reason or "Analysis worker could not be started.")[:2000],
            },
            timeout=15,
            extra_headers={"Prefer": "return=representation"},
        )
        # Release the reservation only when this function actually changed
        # the job from queued to failed. If a worker already claimed it, its
        # status no longer matches and its own success/failure path owns the slot.
        if isinstance(updated, list) and updated:
            if release_reserved_slot:
                try:
                    release_analysis_slot(user_id)
                except Exception:
                    pass
            return True
    except Exception:
        pass
    return False


def wake_analysis_worker(job_nonce, user_id, release_reserved_slot=False):
    """Actively wake the existing worker after queueing the job.

    Supabase may also invoke the worker through a database webhook. The
    worker's claim_analysis_job RPC ensures concurrent wake-ups cannot process
    the same queued row twice. The short client timeout keeps submission from
    waiting for Gemini; the worker continues its own request and the app polls
    analysis_status for the final result.
    """
    host = _production_host()
    if not host:
        _mark_failed_if_still_queued(
            job_nonce, user_id, "Could not resolve the analysis worker host.", release_reserved_slot
        )
        return

    url = "https://" + host + "/api/analysis_worker"
    payload = json.dumps({"record": {"job_nonce": job_nonce}}).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    worker_secret = os.environ.get("ANALYSIS_WORKER_SECRET", "").strip()
    if worker_secret:
        headers["x-analysis-worker-secret"] = worker_secret

    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(
            request, timeout=WORKER_WAKE_TIMEOUT_SECONDS
        ) as response:
            raw = response.read().decode("utf-8", errors="replace")
        try:
            outcome = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            outcome = {}

        # On "ignored", another worker may already have claimed it. Only mark
        # it failed if the database row is still queued after this response.
        if isinstance(outcome, dict) and str(outcome.get("status", "")).lower() == "ignored":
            _mark_failed_if_still_queued(
                job_nonce,
                user_id,
                "The analysis worker could not claim this job. Check the Supabase claim_analysis_job function.",
                release_reserved_slot,
            )
        # Worker failures are saved to the job row by analysis_worker.py; the
        # Android client reads that specific error from analysis_status.
        return

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code in {401, 403}:
            reason = "Worker endpoint returned HTTP {}: {}".format(
                exc.code, detail[:350] or "empty response"
            )
        elif exc.code == 404:
            reason = "The deployed /api/analysis_worker endpoint was not found. Redeploy api/analysis_worker.py."
        else:
            reason = "The analysis worker returned HTTP {}: {}".format(exc.code, detail[:500])
        _mark_failed_if_still_queued(job_nonce, user_id, reason, release_reserved_slot)

    except (urllib.error.URLError, TimeoutError) as exc:
        message = str(exc).lower()
        if "timed out" in message or "timeout" in message:
            # The internal worker request was sent but exceeded our wait limit.
            # Its separate Vercel invocation may still complete; let the app
            # poll status rather than cancelling work that may be in progress.
            return
        _mark_failed_if_still_queued(
            job_nonce,
            user_id,
            "The analysis worker could not be reached: " + str(exc)[:500],
            release_reserved_slot,
        )

    except Exception as exc:
        _mark_failed_if_still_queued(
            job_nonce,
            user_id,
            "The analysis worker could not be started: " + str(exc)[:500],
            release_reserved_slot,
        )


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
        job_nonce = ""
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

            # Confirm job-token signing is configured before reserving a slot
            # or inserting a row, so a missing secret cannot strand a queued job.
            get_job_secret()

            owner = is_owner_user(user)
            remaining = None
            if not owner:
                slot = reserve_analysis_slot(user_id)
                if slot == -1:
                    json_response(self, 429, {
                        "error": "Daily analysis limit reached.",
                        "daily_limit": 4,
                        "remaining": 0,
                        "is_owner": False,
                    })
                    return
                reserved_slot = True
                remaining = max(0, 4 - int(slot))

            job_nonce = create_queued_job(
                user_id=user_id,
                instrument=instrument,
                trade_focus=trade_focus,
                higher_timeframe_image=higher_image,
                lower_timeframe_image=lower_image,
            )
            job_token = create_job_token(job_nonce, instrument, trade_focus, user_id)

            # Explicitly wake the worker as well as supporting a Supabase
            # Database Webhook. This prevents an indefinitely queued job when
            # the webhook is missing or was misconfigured.
            wake_analysis_worker(job_nonce, user_id, reserved_slot)

            json_response(self, 202, {
                "status": "queued",
                "job_id": job_token,
                "job_nonce": job_nonce,
                "poll_after_seconds": 2,
                "is_owner": owner,
                "daily_limit": None if owner else 4,
                "remaining": remaining,
            })

        except ValueError as exc:
            if job_nonce and user_id:
                _mark_failed_if_still_queued(
                    job_nonce, user_id, "Analysis submission failed: " + str(exc), reserved_slot
                )
            elif reserved_slot and user_id:
                try:
                    release_analysis_slot(user_id)
                except Exception:
                    pass
            json_response(self, 400, {"error": str(exc)})

        except RuntimeError as exc:
            if job_nonce and user_id:
                _mark_failed_if_still_queued(
                    job_nonce, user_id, "Analysis submission failed: " + str(exc), reserved_slot
                )
            elif reserved_slot and user_id:
                try:
                    release_analysis_slot(user_id)
                except Exception:
                    pass
            message = str(exc)
            lowered = message.lower()
            status = 429 if any(word in lowered for word in ("limit", "daily", "analyses")) else 500
            json_response(self, status, {"error": message})

        except Exception as exc:
            if job_nonce and user_id:
                _mark_failed_if_still_queued(
                    job_nonce, user_id, "Analysis submission failed: " + str(exc), reserved_slot
                )
            elif reserved_slot and user_id:
                try:
                    release_analysis_slot(user_id)
                except Exception:
                    pass
            json_response(self, 500, {"error": "Analysis submission failed: " + str(exc)})

    def log_message(self, format, *args):
        # Leave platform logs concise; do not log submitted chart images or tokens.
        return
