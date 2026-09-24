import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
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


# ============================================================
# JOB TOKEN CONFIGURATION
# ============================================================

JOB_TTL_SECONDS = 15 * 60


def _job_secret():
    secret = os.getenv("JOB_TOKEN_SECRET", "").strip()

    if not secret:
        secret = os.getenv("GEMINI_API_KEY", "").strip()

    if not secret:
        raise RuntimeError(
            "JOB_TOKEN_SECRET or GEMINI_API_KEY is missing from Vercel."
        )

    return secret.encode("utf-8")


def _create_job_token(
    job_nonce,
    instrument,
    trade_focus,
    user_id,
):
    payload = {
        "job_nonce": str(job_nonce),
        "instrument": str(instrument),
        "trade_focus": str(trade_focus),
        "user_id": str(user_id),
        "created_at": int(time.time()),
    }

    raw = json.dumps(
        payload,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    signature = hmac.new(
        _job_secret(),
        raw,
        hashlib.sha256,
    ).digest()

    encoded_payload = (
        base64.urlsafe_b64encode(raw)
        .decode("ascii")
        .rstrip("=")
    )

    encoded_signature = (
        base64.urlsafe_b64encode(signature)
        .decode("ascii")
        .rstrip("=")
    )

    return encoded_payload + "." + encoded_signature


# ============================================================
# SUPABASE
# ============================================================

def _supabase_url():
    url = os.getenv(
        "SUPABASE_URL",
        "",
    ).strip().rstrip("/")

    if not url:
        raise RuntimeError(
            "SUPABASE_URL is missing from Vercel."
        )

    for suffix in (
        "/rest/v1",
        "/auth/v1",
        "/storage/v1",
    ):
        if url.endswith(suffix):
            url = url[:-len(suffix)]

    return url


def _supabase_service_key():
    key = os.getenv(
        "SUPABASE_SERVICE_ROLE_KEY",
        "",
    ).strip()

    if not key:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY is missing from Vercel."
        )

    return key


def _supabase_headers():
    key = _supabase_service_key()

    return {
        "apikey": key,
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


# ============================================================
# JOB STORAGE
# ============================================================

def _create_job_record(
    user_id,
    job_nonce,
    instrument,
    trade_focus,
    higher_image,
    lower_image,
):
    """
    Creates the job before the worker is started.

    The job payload is stored in analysis_jobs using the existing
    table. The worker will later update this record with the
    completed result.

    The image data is stored in the job record so the worker can
    process the job independently of the submit request.
    """

    payload = {
        "user_id": str(user_id),

        # Unique internal identifier.
        "instrument": (
            str(instrument)
            + "|"
            + str(job_nonce)
        ),

        "trade_focus": str(trade_focus),

        # Job state.
        "status": "queued",

        # The worker uses this data.
        "higher_timeframe_image": higher_image,
        "lower_timeframe_image": lower_image,

        # Empty until completed.
        "openai_response_id": "",
    }

    body = json.dumps(
        payload,
        separators=(",", ":"),
    ).encode("utf-8")

    request = urllib.request.Request(
        _supabase_url()
        + "/rest/v1/analysis_jobs",

        data=body,

        headers={
            **_supabase_headers(),
            "Prefer": "return=minimal",
        },

        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=15,
        ) as response:
            response.read()

    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            "The analysis job could not be created: "
            + raw
        ) from exc

    except (
        urllib.error.URLError,
        TimeoutError,
    ) as exc:

        raise RuntimeError(
            "Supabase could not create the analysis job."
        ) from exc


# ============================================================
# WORKER TRIGGER
# ============================================================

def _worker_url():
    """
    URL of the separate analysis worker.

    VERCEL_URL is automatically supplied by Vercel.
    VERCEL_PROJECT_PRODUCTION_URL is preferred when available.
    """

    explicit = os.getenv(
        "ANALYSIS_WORKER_URL",
        "",
    ).strip()

    if explicit:
        return explicit.rstrip("/")

    production_url = os.getenv(
        "VERCEL_PROJECT_PRODUCTION_URL",
        "",
    ).strip()

    if production_url:
        if production_url.startswith("http"):
            return production_url.rstrip("/")

        return "https://" + production_url.rstrip("/")

    vercel_url = os.getenv(
        "VERCEL_URL",
        "",
    ).strip()

    if vercel_url:
        if vercel_url.startswith("http"):
            return vercel_url.rstrip("/")

        return "https://" + vercel_url.rstrip("/")

    raise RuntimeError(
        "ANALYSIS_WORKER_URL or VERCEL_URL is missing."
    )


def _worker_secret():
    secret = os.getenv(
        "ANALYSIS_WORKER_SECRET",
        "",
    ).strip()

    if not secret:
        raise RuntimeError(
            "ANALYSIS_WORKER_SECRET is missing from Vercel."
        )

    return secret


def _trigger_worker(
    job_nonce,
):
    """
    Starts the worker through a separate HTTP request.

    The worker owns the actual Gemini operation.

    We deliberately use a short HTTP timeout here. The submit
    endpoint must NOT wait for Gemini to finish.
    """

    payload = {
        "job_nonce": str(job_nonce),
    }

    body = json.dumps(
        payload,
        separators=(",", ":"),
    ).encode("utf-8")

    request = urllib.request.Request(
        _worker_url()
        + "/api/analysis_worker",

        data=body,

        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Worker-Secret": _worker_secret(),
        },

        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=3,
        ) as response:

            response.read()

    except urllib.error.HTTPError as exc:

        raw = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            "The analysis worker rejected the job: "
            + raw
        ) from exc

    except (
        urllib.error.URLError,
        TimeoutError,
    ) as exc:

        raise RuntimeError(
            "The analysis worker could not be reached."
        ) from exc


# ============================================================
# REQUEST READER
# ============================================================

def _read_json(handler):
    try:
        length = int(
            handler.headers.get(
                "Content-Length",
                "0",
            )
        )

    except ValueError:
        raise ValueError(
            "Invalid request."
        )

    if (
        length <= 0
        or length > 25 * 1024 * 1024
    ):
        raise ValueError(
            "Invalid chart request."
        )

    raw = handler.rfile.read(length)

    try:
        value = json.loads(
            raw.decode("utf-8")
        )

    except json.JSONDecodeError as exc:
        raise ValueError(
            "Invalid request JSON."
        ) from exc

    if not isinstance(
        value,
        dict,
    ):
        raise ValueError(
            "Invalid request."
        )

    return value


# ============================================================
# HANDLER
# ============================================================

class handler(
    BaseHTTPRequestHandler
):

    def do_OPTIONS(self):
        json_response(
            self,
            204,
            {},
        )

    def do_POST(self):

        reserved = False
        user = None
        job_nonce = None

        try:

            # ------------------------------------------------
            # AUTHENTICATION
            # ------------------------------------------------

            access_token = extract_bearer_token(
                self
            )

            user = verify_access_token(
                access_token
            )

            user_id = str(
                user["id"]
            )

            owner = is_owner_user(
                user
            )

            # ------------------------------------------------
            # REQUEST
            # ------------------------------------------------

            data = _read_json(
                self
            )

            instrument = validate_instrument(
                data.get(
                    "instrument"
                )
            )

            trade_focus = validate_focus(
                data.get(
                    "trade_focus",
                    "DAY TRADE",
                )
            )

            higher_image = validate_image_data_url(
                data.get(
                    "higher_timeframe_image"
                ),
                "4H chart",
            )

            lower_image = validate_image_data_url(
                data.get(
                    "lower_timeframe_image"
                ),
                "15M chart",
            )

            # ------------------------------------------------
            # DAILY LIMIT
            # ------------------------------------------------

            if not owner:

                slot = reserve_analysis_slot(
                    user_id
                )

                if slot == -1:

                    json_response(
                        self,
                        429,
                        {
                            "error":
                                "Daily analysis limit reached.",

                            "daily_limit":
                                4,

                            "remaining":
                                0,

                            "is_owner":
                                False,
                        },
                    )

                    return

                reserved = True

                remaining = max(
                    0,
                    4 - slot,
                )

            else:

                remaining = None

            # ------------------------------------------------
            # UNIQUE JOB
            # ------------------------------------------------

            job_nonce = uuid.uuid4().hex

            # ------------------------------------------------
            # CREATE JOB
            # ------------------------------------------------

            try:

                _create_job_record(
                    user_id=user_id,
                    job_nonce=job_nonce,
                    instrument=instrument,
                    trade_focus=trade_focus,
                    higher_image=higher_image,
                    lower_image=lower_image,
                )

            except Exception:

                if reserved:

                    try:
                        release_analysis_slot(
                            user_id
                        )
                    except Exception:
                        pass

                raise

            # ------------------------------------------------
            # SIGNED JOB TOKEN
            # ------------------------------------------------

            job_id = _create_job_token(
                job_nonce=job_nonce,
                instrument=instrument,
                trade_focus=trade_focus,
                user_id=user_id,
            )

            # ------------------------------------------------
            # START WORKER
            # ------------------------------------------------

            try:

                _trigger_worker(
                    job_nonce
                )

            except Exception:

                if reserved:

                    try:
                        release_analysis_slot(
                            user_id
                        )
                    except Exception:
                        pass

                raise

            # ------------------------------------------------
            # RETURN IMMEDIATELY
            # ------------------------------------------------

            json_response(
                self,
                202,
                {
                    "status":
                        "queued",

                    "job_id":
                        job_id,

                    "poll_after_seconds":
                        2,

                    "is_owner":
                        owner,

                    "daily_limit":
                        None
                        if owner
                        else 4,

                    "remaining":
                        remaining,
                },
            )

        except ValueError as exc:

            json_response(
                self,
                400,
                {
                    "error":
                        str(exc)
                },
            )

        except RuntimeError as exc:

            if reserved and user:

                try:
                    release_analysis_slot(
                        str(user["id"])
                    )
                except Exception:
                    pass

            json_response(
                self,
                503,
                {
                    "error":
                        str(exc)
                },
            )

        except Exception as exc:

            if reserved and user:

                try:
                    release_analysis_slot(
                        str(user["id"])
                    )
                except Exception:
                    pass

            json_response(
                self,
                500,
                {
                    "error":
                        "The analysis could not be started: "
                        + str(exc)
                },
            )
