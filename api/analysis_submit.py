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
    create_background_response,
    json_response,
    parse_completed_response,
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
    secret = os.getenv(
        "JOB_TOKEN_SECRET",
        "",
    ).strip()

    if not secret:
        secret = os.getenv(
            "GEMINI_API_KEY",
            "",
        ).strip()

    if not secret:

        raise RuntimeError(
            "JOB_TOKEN_SECRET or GEMINI_API_KEY is missing from Vercel."
        )

    return secret.encode(
        "utf-8"
    )


def _create_job_token(
    job_nonce,
    instrument,
    trade_focus,
    user_id,
):
    payload = {
        "job_nonce":
            str(job_nonce),

        "instrument":
            str(instrument),

        "trade_focus":
            str(trade_focus),

        "user_id":
            str(user_id),

        "created_at":
            int(time.time()),
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
        base64.urlsafe_b64encode(
            raw
        )
        .decode("ascii")
        .rstrip("=")
    )

    encoded_signature = (
        base64.urlsafe_b64encode(
            signature
        )
        .decode("ascii")
        .rstrip("=")
    )

    return (
        encoded_payload
        + "."
        + encoded_signature
    )


# ============================================================
# SUPABASE HELPERS
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

            url = url[
                :-len(suffix)
            ]

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
        "apikey":
            key,

        "Authorization":
            "Bearer " + key,

        "Content-Type":
            "application/json",

        "Accept":
            "application/json",
    }


def _save_completed_job(
    user_id,
    job_nonce,
    instrument,
    trade_focus,
    result,
):
    """
    Uses the existing analysis_jobs table without requiring
    a database schema migration.

    Existing columns are used as follows:

    openai_response_id -> encoded completed result
    instrument         -> instrument + unique job nonce
    trade_focus        -> actual trade focus
    user_id            -> authenticated owner of job
    """

    result_json = json.dumps(
        result,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    result_blob = (
        base64.urlsafe_b64encode(
            result_json.encode(
                "utf-8"
            )
        )
        .decode("ascii")
        .rstrip("=")
    )

    unique_instrument = (
        instrument
        + "|"
        + job_nonce
    )

    payload = {

        "user_id":
            str(user_id),

        "openai_response_id":
            result_blob,

        "instrument":
            unique_instrument,

        "trade_focus":
            trade_focus,
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

            "Prefer":
                "return=minimal",
        },

        method="POST",
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=20,
        ) as response:

            response.read()

    except urllib.error.HTTPError as exc:

        raw = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            "Analysis result could not be stored: "
            + raw
        ) from exc

    except (
        urllib.error.URLError,
        TimeoutError,
    ) as exc:

        raise RuntimeError(
            "Supabase could not store the analysis result."
        ) from exc


# ============================================================
# REQUEST READER
# ============================================================

def _read_json(
    handler,
):
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

    raw = handler.rfile.read(
        length
    )

    try:

        value = json.loads(
            raw.decode(
                "utf-8"
            )
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

    def do_OPTIONS(
        self
    ):
        json_response(
            self,
            204,
            {},
        )

    def do_POST(
        self
    ):

        reserved = False
        user = None

        try:

            access_token = (
                extract_bearer_token(
                    self
                )
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

            data = _read_json(
                self
            )

            instrument = (
                validate_instrument(
                    data.get(
                        "instrument"
                    )
                )
            )

            trade_focus = validate_focus(
                data.get(
                    "trade_focus",
                    "DAY TRADE",
                )
            )

            higher_image = (
                validate_image_data_url(
                    data.get(
                        "higher_timeframe_image"
                    ),
                    "4H chart",
                )
            )

            lower_image = (
                validate_image_data_url(
                    data.get(
                        "lower_timeframe_image"
                    ),
                    "15M chart",
                )
            )

            # ----------------------------------------
            # DAILY LIMIT
            # ----------------------------------------

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

            # ----------------------------------------
            # RUN GEMINI DIRECTLY
            # ----------------------------------------

            try:

                raw_response = (
                    create_background_response(
                        instrument=instrument,
                        trade_focus=trade_focus,
                        higher_image=higher_image,
                        lower_image=lower_image,
                    )
                )

                result = (
                    parse_completed_response(
                        raw_response,
                        instrument,
                        trade_focus,
                    )
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

            # ----------------------------------------
            # STORE COMPLETED RESULT
            # ----------------------------------------

            job_nonce = uuid.uuid4().hex

            try:

                _save_completed_job(

                    user_id=user_id,

                    job_nonce=job_nonce,

                    instrument=instrument,

                    trade_focus=trade_focus,

                    result=result,
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

            # ----------------------------------------
            # CREATE SHORT-LIVED SIGNED JOB TOKEN
            # ----------------------------------------

            job_id = _create_job_token(

                job_nonce=
                    job_nonce,

                instrument=
                    instrument,

                trade_focus=
                    trade_focus,

                user_id=
                    user_id,
            )

            # ----------------------------------------
            # IMPORTANT:
            #
            # Gemini analysis has ALREADY completed.
            #
            # Android still receives a job_id because
            # the existing Android app is designed to
            # poll /analysis_status.
            # ----------------------------------------

            json_response(
                self,
                202,
                {
                    "status":
                        "completed",

                    "job_id":
                        job_id,

                    "poll_after_seconds":
                        1,

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

            if (
                reserved
                and user
            ):

                try:

                    release_analysis_slot(
                        str(
                            user["id"]
                        )
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

            if (
                reserved
                and user
            ):

                try:

                    release_analysis_slot(
                        str(
                            user["id"]
                        )
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
