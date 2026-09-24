import json
import os
import time
import uuid
import base64
import hashlib
import hmac
import urllib.error
import urllib.parse
import urllib.request

from datetime import datetime, timezone
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


SUPABASE_URL = os.environ.get(
    "SUPABASE_URL",
    ""
).strip().rstrip("/")

SUPABASE_SERVICE_ROLE_KEY = os.environ.get(
    "SUPABASE_SERVICE_ROLE_KEY",
    ""
).strip()


JOB_TTL_SECONDS = 24 * 60 * 60


# =========================================================
# JOB TOKEN
# =========================================================

def get_job_secret():
    secret = os.environ.get(
        "JOB_TOKEN_SECRET",
        ""
    ).strip()

    if not secret:
        secret = os.environ.get(
            "GEMINI_API_KEY",
            ""
        ).strip()

    if not secret:
        raise RuntimeError(
            "JOB_TOKEN_SECRET or GEMINI_API_KEY is missing."
        )

    return secret.encode("utf-8")


def create_job_token(
    job_nonce,
    instrument,
    trade_focus,
    user_id,
):
    created_at = int(time.time())

    payload = {
        "job_nonce": job_nonce,
        "instrument": instrument,
        "trade_focus": trade_focus,
        "user_id": user_id,
        "created_at": created_at,
    }

    raw = json.dumps(
        payload,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    encoded_payload = (
        base64.urlsafe_b64encode(raw)
        .decode("ascii")
        .rstrip("=")
    )

    signature = hmac.new(
        get_job_secret(),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()

    encoded_signature = (
        base64.urlsafe_b64encode(signature)
        .decode("ascii")
        .rstrip("=")
    )

    return (
        encoded_payload
        + "."
        + encoded_signature
    )


# =========================================================
# SUPABASE
# =========================================================

def supabase_request(
    method,
    path,
    payload=None,
    timeout=30,
    extra_headers=None,
):
    if not SUPABASE_URL:
        raise RuntimeError(
            "SUPABASE_URL is missing."
        )

    if not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY is missing."
        )

    headers = {
        "apikey":
            SUPABASE_SERVICE_ROLE_KEY,

        "Authorization":
            "Bearer "
            + SUPABASE_SERVICE_ROLE_KEY,

        "Content-Type":
            "application/json",

        "Accept":
            "application/json",
    }

    if extra_headers:
        headers.update(
            extra_headers
        )

    data = None

    if payload is not None:
        data = json.dumps(
            payload,
            ensure_ascii=False,
        ).encode("utf-8")

    request = urllib.request.Request(
        SUPABASE_URL + path,
        data=data,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout,
        ) as response:

            raw = response.read().decode(
                "utf-8",
                errors="replace",
            )

            if not raw:
                return None

            try:
                return json.loads(
                    raw
                )
            except json.JSONDecodeError:
                return raw

    except urllib.error.HTTPError as exc:

        detail = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            "Supabase request failed "
            f"({exc.code}) "
            + detail
        )

    except urllib.error.URLError as exc:

        raise RuntimeError(
            "Supabase connection failed: "
            + str(exc)
        )


# =========================================================
# CREATE JOB
# =========================================================

def create_queued_job(
    user_id,
    instrument,
    trade_focus,
    higher_timeframe_image,
    lower_timeframe_image,
):
    job_id = str(
        uuid.uuid4()
    )

    job_nonce = uuid.uuid4().hex

    created_at = (
        datetime.now(
            timezone.utc
        ).isoformat()
    )

    payload = {
        "id":
            job_id,

        "job_nonce":
            job_nonce,

        "user_id":
            user_id,

        "instrument":
            instrument,

        "trade_focus":
            trade_focus,

        "higher_timeframe_image":
            higher_timeframe_image,

        "lower_timeframe_image":
            lower_timeframe_image,

        # The row is immediately being processed.
        "status":
            "processing",

        # This column is NOT NULL in Supabase.
        # It is replaced with the real result below.
        "openai_response_id":
            "pending",

        "error_message":
            None,

        "created_at":
            created_at,
    }

    result = supabase_request(
        "POST",
        "/rest/v1/analysis_jobs",
        payload,
        timeout=30,
        extra_headers={
            "Prefer":
                "return=representation",
        },
    )

    if not result:
        raise RuntimeError(
            "The analysis job could not be created."
        )

    return job_nonce


# =========================================================
# UPDATE JOB
# =========================================================

def update_job(
    job_nonce,
    values,
):
    encoded_nonce = urllib.parse.quote(
        job_nonce,
        safe="",
    )

    return supabase_request(
        "PATCH",
        (
            "/rest/v1/analysis_jobs"
            "?job_nonce=eq."
            + encoded_nonce
        ),
        values,
        timeout=30,
        extra_headers={
            "Prefer":
                "return=minimal",
        },
    )


# =========================================================
# ENCODE RESULT
# =========================================================

def encode_result(result):
    raw = json.dumps(
        result,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    return (
        base64.urlsafe_b64encode(
            raw
        )
        .decode("ascii")
        .rstrip("=")
    )


# =========================================================
# REQUEST BODY
# =========================================================

def read_json(handler):
    try:
        content_length = int(
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
        content_length <= 0
        or content_length > 25 * 1024 * 1024
    ):
        raise ValueError(
            "Invalid chart request."
        )

    raw = handler.rfile.read(
        content_length
    )

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


# =========================================================
# AUTHENTICATION
# =========================================================

def authenticate_user(handler):
    token = extract_bearer_token(
        handler
    )

    user = verify_access_token(
        token
    )

    if not isinstance(
        user,
        dict,
    ):
        raise ValueError(
            "Invalid authenticated user."
        )

    return user


# =========================================================
# HTTP HANDLER
# =========================================================

class handler(
    BaseHTTPRequestHandler
):

    def do_OPTIONS(self):

        json_response(
            self,
            204,
            {},
        )


    def do_GET(self):

        json_response(
            self,
            200,
            {
                "status":
                    "analysis submit online",
            },
        )


    def do_POST(self):

        reserved = False
        user_id = ""
        job_nonce = ""

        try:

            # -------------------------------------------------
            # 1. AUTH
            # -------------------------------------------------

            user = authenticate_user(
                self
            )

            user_id = str(
                user.get(
                    "id",
                    "",
                )
            ).strip()

            if not user_id:
                raise ValueError(
                    "Invalid authenticated user."
                )

            owner = is_owner_user(
                user
            )


            # -------------------------------------------------
            # 2. REQUEST
            # -------------------------------------------------

            data = read_json(
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


            # -------------------------------------------------
            # 3. DAILY LIMIT
            # -------------------------------------------------

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
                    4 - int(slot),
                )

            else:

                remaining = None


            # -------------------------------------------------
            # 4. CREATE DATABASE JOB
            # -------------------------------------------------

            job_nonce = create_queued_job(
                user_id=
                    user_id,

                instrument=
                    instrument,

                trade_focus=
                    trade_focus,

                higher_timeframe_image=
                    higher_image,

                lower_timeframe_image=
                    lower_image,
            )


            # -------------------------------------------------
            # 5. RUN GEMINI DIRECTLY
            #
            # No Supabase webhook is required.
            # This removes the long queued delay.
            # -------------------------------------------------

            try:

                raw_response = (
                    create_background_response(
                        instrument=
                            instrument,

                        trade_focus=
                            trade_focus,

                        higher_image=
                            higher_image,

                        lower_image=
                            lower_image,
                    )
                )

                result = (
                    parse_completed_response(
                        raw_response,
                        instrument=
                            instrument,
                        trade_focus=
                            trade_focus,
                    )
                )

                if not isinstance(
                    result,
                    dict,
                ):
                    raise RuntimeError(
                        "The analysis returned an invalid result."
                    )

                encoded_result = encode_result(
                    result
                )


                # -------------------------------------------------
                # 6. SAVE COMPLETED RESULT
                # -------------------------------------------------

                update_job(
                    job_nonce,
                    {
                        "status":
                            "completed",

                        "openai_response_id":
                            encoded_result,

                        "error_message":
                            None,
                    },
                )


            except Exception as analysis_error:

                error_message = str(
                    analysis_error
                ).strip()

                if not error_message:
                    error_message = (
                        "Analysis processing failed."
                    )

                try:

                    update_job(
                        job_nonce,
                        {
                            "status":
                                "failed",

                            "error_message":
                                error_message[:2000],

                            # Keep this column NOT NULL.
                            "openai_response_id":
                                "failed",
                        },
                    )

                except Exception:
                    pass

                if reserved:

                    try:
                        release_analysis_slot(
                            user_id
                        )
                    except Exception:
                        pass

                    reserved = False

                raise RuntimeError(
                    error_message
                )


            # -------------------------------------------------
            # 7. CREATE SECURE JOB TOKEN
            # -------------------------------------------------

            job_token = create_job_token(
                job_nonce=
                    job_nonce,

                instrument=
                    instrument,

                trade_focus=
                    trade_focus,

                user_id=
                    user_id,
            )


            # -------------------------------------------------
            # 8. RETURN COMPLETED JOB
            # -------------------------------------------------

            json_response(
                self,
                200,
                {
                    "status":
                        "completed",

                    "job_id":
                        job_token,

                    "job_nonce":
                        job_nonce,

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
                        str(exc),
                },
            )


        except RuntimeError as exc:

            json_response(
                self,
                500,
                {
                    "error":
                        str(exc),
                },
            )


        except Exception as exc:

            if (
                reserved
                and user_id
            ):

                try:
                    release_analysis_slot(
                        user_id
                    )
                except Exception:
                    pass

            json_response(
                self,
                500,
                {
                    "error":
                        str(exc),
                },
            )
