import json
import os
import time
import uuid
import base64
import hashlib
import hmac
import urllib.error
import urllib.request
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
                return json.loads(raw)
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

    # Explicit UTC timestamp.
    created_at = (
        datetime.now(
            timezone.utc
        ).isoformat()
    )

    payload = {
        # Required primary key
        "id":
            job_id,

        # Required job identifier
        "job_nonce":
            job_nonce,

        # Logged-in user
        "user_id":
            user_id,

        # IMPORTANT:
        # Store the instrument alone.
        "instrument":
            instrument,

        "trade_focus":
            trade_focus,

        "higher_timeframe_image":
            higher_timeframe_image,

        "lower_timeframe_image":
            lower_timeframe_image,

        "status":
            "queued",

        "openai_response_id":
            None,

        "error_message":
            None,

        # Explicitly provide created_at
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
# REQUEST BODY
# =========================================================

def read_json(handler):
    content_length_header = (
        handler.headers.get(
            "Content-Length",
            "0",
        )
    )

    try:
        content_length = int(
            content_length_header
        )
    except ValueError:
        raise ValueError(
            "Invalid Content-Length."
        )

    if content_length <= 0:
        raise ValueError(
            "Request body is empty."
        )

    if content_length > (
        25 * 1024 * 1024
    ):
        raise ValueError(
            "Request body is too large."
        )

    raw = handler.rfile.read(
        content_length
    )

    try:
        return json.loads(
            raw.decode("utf-8")
        )
    except Exception:
        raise ValueError(
            "Invalid JSON request."
        )


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
            "Invalid authentication response."
        )

    return user


# =========================================================
# HTTP HANDLER
# =========================================================

class handler(
    BaseHTTPRequestHandler
):

    def do_OPTIONS(self):

        self.send_response(
            204
        )

        self.send_header(
            "Access-Control-Allow-Origin",
            "*",
        )

        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization",
        )

        self.send_header(
            "Access-Control-Allow-Methods",
            "POST, GET, OPTIONS",
        )

        self.send_header(
            "Cache-Control",
            "no-store",
        )

        self.end_headers()


    def do_POST(self):

        reserved_slot = False
        user_id = ""

        try:

            # -----------------------------------------
            # AUTH
            # -----------------------------------------

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


            # -----------------------------------------
            # BODY
            # -----------------------------------------

            body = read_json(
                self
            )

            if not isinstance(
                body,
                dict,
            ):
                raise ValueError(
                    "Invalid request body."
                )


            # -----------------------------------------
            # INPUTS
            # -----------------------------------------

            instrument = str(
                body.get(
                    "instrument",
                    "",
                )
            ).strip().upper()

            trade_focus = str(
                body.get(
                    "trade_focus",
                    "",
                )
            ).strip().upper()

            higher_timeframe_image = (
                body.get(
                    "higher_timeframe_image"
                )
            )

            lower_timeframe_image = (
                body.get(
                    "lower_timeframe_image"
                )
            )


            # -----------------------------------------
            # VALIDATION
            # -----------------------------------------

            validate_instrument(
                instrument
            )

            validate_focus(
                trade_focus
            )

            validate_image_data_url(
                higher_timeframe_image,
                "higher_timeframe_image",
            )

            validate_image_data_url(
                lower_timeframe_image,
                "lower_timeframe_image",
            )


            # -----------------------------------------
            # DAILY LIMIT
            # -----------------------------------------
            #
            # IMPORTANT:
            # Pass the complete user object to
            # is_owner_user(), not just the UUID.
            #

            owner = is_owner_user(
                user
            )

            if not owner:

                reserve_analysis_slot(
                    user_id
                )

                reserved_slot = True


            # -----------------------------------------
            # CREATE JOB
            # -----------------------------------------

            job_nonce = create_queued_job(
                user_id=
                    user_id,

                instrument=
                    instrument,

                trade_focus=
                    trade_focus,

                higher_timeframe_image=
                    higher_timeframe_image,

                lower_timeframe_image=
                    lower_timeframe_image,
            )


            # -----------------------------------------
            # TOKEN
            # -----------------------------------------

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


            # -----------------------------------------
            # SUCCESS
            # -----------------------------------------

            json_response(
                self,
                202,
                {
                    "status":
                        "queued",

                    "job_id":
                        job_token,

                    "job_nonce":
                        job_nonce,
                },
            )

            return


        except ValueError as exc:

            if (
                reserved_slot
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
                400,
                {
                    "error":
                        str(exc)
                },
            )

            return


        except RuntimeError as exc:

            if (
                reserved_slot
                and user_id
            ):
                try:
                    release_analysis_slot(
                        user_id
                    )
                except Exception:
                    pass

            message = str(
                exc
            )

            lowered = (
                message.lower()
            )

            if (
                "limit" in lowered
                or "daily" in lowered
                or "analyses" in lowered
            ):

                json_response(
                    self,
                    429,
                    {
                        "error":
                            message
                    },
                )

            else:

                json_response(
                    self,
                    500,
                    {
                        "error":
                            message
                    },
                )

            return


        except Exception as exc:

            if (
                reserved_slot
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
                        str(exc)
                },
            )

            return


    def do_GET(self):

        json_response(
            self,
            200,
            {
                "status":
                    "analysis submit online"
            },
        )
