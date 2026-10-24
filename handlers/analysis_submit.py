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
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
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



ACTIVE_QUOTA_STATUSES = "in.%28queued%2Cprocessing%2Ccompleted%29"
KAMPALA = ZoneInfo("Africa/Kampala")

def _get_rows(path, timeout=15):
    result = supabase_request("GET", path, timeout=timeout)
    if not isinstance(result, list):
        raise RuntimeError("Supabase returned an invalid quota response.")
    return result

def _get_quota(user_id):
    """Read subscription and existing job history; requires no schema changes.

    Existing analysis_jobs history is used as the usage ledger. Because no DB
    reservation/RPC changes are allowed, concurrent submissions can race; see
    the deployment note accompanying this file.
    """
    uid = urllib.parse.quote(str(user_id), safe="")
    subs = _get_rows(
        "/rest/v1/subscriptions?user_id=eq." + uid
        + "&select=plan,duration_months,status,starts_at,expires_at&limit=1"
    )
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(KAMPALA)

    if subs:
        sub = subs[0] or {}
        status = str(sub.get("status", "")).strip().lower()
        try:
            starts = datetime.fromisoformat(str(sub.get("starts_at", "")).replace("Z", "+00:00"))
            expires = datetime.fromisoformat(str(sub.get("expires_at", "")).replace("Z", "+00:00"))
            if starts.tzinfo is None: starts = starts.replace(tzinfo=timezone.utc)
            if expires.tzinfo is None: expires = expires.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return {"allowed": False, "reason": "subscription_invalid", "quota_type": "blocked", "limit": 0, "used": 0, "remaining": 0}

        if status != "active" or starts > now_utc or expires <= now_utc:
            return {"allowed": False, "reason": "subscription_expired", "quota_type": "blocked", "limit": 0, "used": 0, "remaining": 0}

        try:
            months = int(sub.get("duration_months") or 0)
        except (TypeError, ValueError):
            months = 0
        limits = {1: 10, 3: 30, 6: 50}
        limit = limits.get(months)
        if not limit:
            return {"allowed": False, "reason": "unsupported_plan", "quota_type": "blocked", "limit": 0, "used": 0, "remaining": 0}

        day_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end_local = day_start_local + timedelta(days=1)
        start_utc = day_start_local.astimezone(timezone.utc).isoformat()
        end_utc = day_end_local.astimezone(timezone.utc).isoformat()
        jobs = _get_rows(
            "/rest/v1/analysis_jobs?user_id=eq." + uid
            + "&status=" + ACTIVE_QUOTA_STATUSES
            + "&created_at=gte." + urllib.parse.quote(start_utc, safe="")
            + "&created_at=lt." + urllib.parse.quote(end_utc, safe="")
            + "&select=id&limit=1000"
        )
        used = len(jobs)
        return {"allowed": used < limit, "reason": "ok" if used < limit else "daily_limit", "quota_type": "paid_daily", "limit": limit, "used": used, "remaining": max(0, limit-used), "plan": str(sub.get("plan") or "")}

    # No subscription row means a never-subscribed free account. Existing jobs
    # are the only available lifetime ledger without adding a database column.
    jobs = _get_rows(
        "/rest/v1/analysis_jobs?user_id=eq." + uid
        + "&status=" + ACTIVE_QUOTA_STATUSES
        + "&select=id&limit=5"
    )
    used = len(jobs)
    limit = 4
    return {"allowed": used < limit, "reason": "ok" if used < limit else "free_lifetime_limit", "quota_type": "free_lifetime", "limit": limit, "used": used, "remaining": max(0, limit-used)}


def _sync_legacy_profile_count(user_id, used):
    """Best-effort profile counter for existing clients; quota checks use history."""
    uid = urllib.parse.quote(str(user_id), safe="")
    today = datetime.now(KAMPALA).date().isoformat()
    try:
        supabase_request(
            "PATCH", "/rest/v1/profiles?id=eq." + uid,
            {"daily_analysis_count": int(used), "last_analysis_date": today},
            timeout=10, extra_headers={"Prefer": "return=minimal"},
        )
    except Exception:
        # Do not fail an otherwise valid queued analysis due to a legacy UI counter.
        pass


OPTIONAL_TIMEFRAMES = ("1D", "1H", "5M", "1M")

def normalize_optional_images(value):
    """Normalize the legacy inline optional image map, if an older client sends it."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Additional timeframe images must be an object.")
    if len(value) > len(OPTIONAL_TIMEFRAMES):
        raise ValueError("Too many additional timeframe images.")
    normalized = {}
    for raw_timeframe, image in value.items():
        timeframe = str(raw_timeframe).strip().upper()
        if timeframe not in OPTIONAL_TIMEFRAMES:
            raise ValueError("Unsupported additional timeframe: " + timeframe)
        if timeframe in normalized:
            raise ValueError("Duplicate additional timeframe: " + timeframe)
        normalized[timeframe] = validate_image_data_url(image, timeframe + " chart")
    return normalized

def verify_optional_upload_ids(user_id, value):
    """Ensure every staged chart upload belongs to this user and timeframe."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Additional timeframe upload IDs must be an object.")
    if len(value) > len(OPTIONAL_TIMEFRAMES):
        raise ValueError("Too many additional timeframe uploads.")
    uid = urllib.parse.quote(str(user_id), safe="")
    normalized = {}
    for raw_timeframe, raw_id in value.items():
        timeframe = str(raw_timeframe).strip().upper()
        if timeframe not in OPTIONAL_TIMEFRAMES:
            raise ValueError("Unsupported additional timeframe: " + timeframe)
        if timeframe in normalized:
            raise ValueError("Duplicate additional timeframe: " + timeframe)
        try:
            upload_id = str(uuid.UUID(str(raw_id).strip()))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("The uploaded " + timeframe + " chart reference is invalid.") from exc
        rows = supabase_request(
            "GET",
            "/rest/v1/analysis_chart_uploads?id=eq." + upload_id
            + "&user_id=eq." + uid
            + "&timeframe=eq." + urllib.parse.quote(timeframe, safe="")
            + "&select=id,timeframe&limit=1",
            timeout=15,
        )
        if not isinstance(rows, list) or not rows or str(rows[0].get("timeframe", "")).upper() != timeframe:
            raise ValueError("The uploaded " + timeframe + " chart is missing or expired. Please select it again.")
        normalized[timeframe] = upload_id
    return normalized

def create_queued_job(
    user_id, instrument, trade_focus, higher_timeframe_image, lower_timeframe_image,
    additional_timeframe_images=None, additional_timeframe_upload_ids=None,
):
    # These fields are required by the existing Supabase schema and worker.
    # openai_response_id is NOT NULL; the worker replaces "pending" with
    # the encoded Gemini analysis after processing the queued job.
    job_id = str(uuid.uuid4())
    job_nonce = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat()
    additional_timeframe_images = additional_timeframe_images or {}
    additional_timeframe_upload_ids = additional_timeframe_upload_ids or {}
    payload = {
        "id": job_id,
        "job_nonce": job_nonce,
        "user_id": user_id,
        "instrument": instrument,
        "trade_focus": trade_focus,
        "higher_timeframe_image": higher_timeframe_image,
        "lower_timeframe_image": lower_timeframe_image,
        "additional_timeframe_images": additional_timeframe_images,
        "additional_timeframe_upload_ids": additional_timeframe_upload_ids,
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
    """Resolve this deployment's host without accepting a full URL path."""
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
            # This quota implementation does not reserve the legacy RPC slot.
            # The analysis history is the usage ledger, so no slot release is needed here.
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
            reason = "The analysis worker rejected its internal secret. Check ANALYSIS_WORKER_SECRET in Vercel."
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
            additional_timeframe_images = normalize_optional_images(
                body.get("additional_timeframe_images", {})
            )
            additional_timeframe_upload_ids = verify_optional_upload_ids(
                user_id, body.get("additional_timeframe_upload_ids", {})
            )
            if set(additional_timeframe_images).intersection(additional_timeframe_upload_ids):
                raise ValueError("Each optional timeframe must be submitted only once.")

            # Use the project's existing validators and supported instrument list.
            validate_instrument(instrument)
            validate_focus(trade_focus)
            validate_image_data_url(higher_image, "higher_timeframe_image")
            validate_image_data_url(lower_image, "lower_timeframe_image")

            # Confirm job-token signing is configured before reserving a slot
            # or inserting a row, so a missing secret cannot strand a queued job.
            get_job_secret()

            owner = is_owner_user(user)
            quota = None
            remaining = None
            if not owner:
                quota = _get_quota(user_id)
                if not quota.get("allowed"):
                    reason = quota.get("reason")
                    if reason == "subscription_expired":
                        message = "Your LM Analyzer subscription has expired or is inactive. Renew to continue analysing charts."
                        status_code = 403
                    elif reason == "unsupported_plan":
                        message = "This subscription plan does not include analysis access. Please contact LM Analyzer support."
                        status_code = 403
                    elif reason == "free_lifetime_limit":
                        message = "Your 4 free lifetime analyses have been used. Subscribe to continue."
                        status_code = 429
                    else:
                        message = "Your daily analysis limit has been reached. Try again after midnight Uganda time."
                        status_code = 429
                    json_response(self, status_code, {
                        "error": message, "reason": reason,
                        "quota_type": quota.get("quota_type"),
                        "daily_limit": quota.get("limit") if quota.get("quota_type") == "paid_daily" else None,
                        "lifetime_limit": 4 if quota.get("quota_type") == "free_lifetime" else None,
                        "used": quota.get("used", 0), "remaining": 0, "is_owner": False,
                    })
                    return
                remaining = quota.get("remaining")

            job_nonce = create_queued_job(
                user_id=user_id,
                instrument=instrument,
                trade_focus=trade_focus,
                higher_timeframe_image=higher_image,
                lower_timeframe_image=lower_image,
                additional_timeframe_images=additional_timeframe_images,
                additional_timeframe_upload_ids=additional_timeframe_upload_ids,
            )
            job_token = create_job_token(job_nonce, instrument, trade_focus, user_id)

            # Explicitly wake the worker as well as supporting a Supabase
            # Database Webhook. This prevents an indefinitely queued job when
            # the webhook is missing or was misconfigured.
            # No legacy daily-slot reservation is used: quota is computed from
            # existing job history and the subscription row.
            wake_analysis_worker(job_nonce, user_id, False)
            if not owner and quota is not None:
                _sync_legacy_profile_count(user_id, int(quota.get("used", 0)) + 1)

            json_response(self, 202, {
                "status": "queued",
                "job_id": job_token,
                "job_nonce": job_nonce,
                "poll_after_seconds": 2,
                "is_owner": owner,
                "daily_limit": None if owner or not quota or quota.get("quota_type") != "paid_daily" else quota.get("limit"),
                "lifetime_limit": None if owner or not quota or quota.get("quota_type") != "free_lifetime" else 4,
                "quota_type": "owner_unlimited" if owner else (quota or {}).get("quota_type"),
                "used": None if owner else int((quota or {}).get("used", 0)) + 1,
                "remaining": None if owner else max(0, int((quota or {}).get("remaining", 0)) - 1),
            })

        except ValueError as exc:
            if job_nonce and user_id:
                _mark_failed_if_still_queued(
                    job_nonce, user_id, "Analysis submission failed: " + str(exc), False
                )
            json_response(self, 400, {"error": str(exc)})

        except RuntimeError as exc:
            if job_nonce and user_id:
                _mark_failed_if_still_queued(
                    job_nonce, user_id, "Analysis submission failed: " + str(exc), False
                )
            message = str(exc)
            lowered = message.lower()
            status = 429 if any(word in lowered for word in ("limit", "daily", "analyses")) else 500
            json_response(self, status, {"error": message})

        except Exception as exc:
            if job_nonce and user_id:
                _mark_failed_if_still_queued(
                    job_nonce, user_id, "Analysis submission failed: " + str(exc), False
                )
            json_response(self, 500, {"error": "Analysis submission failed: " + str(exc)})

    def log_message(self, format, *args):
        # Leave platform logs concise; do not log submitted chart images or tokens.
        return
