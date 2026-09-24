import json
import os
import time
import uuid
import base64
import hashlib
import hmac
import urllib.error
import urllib.request

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
# JOB TOKEN SECRET
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


# =========================================================
# CREATE JOB TOKEN
# =========================================================

def create_job_token(
    job_nonce,
    instrument,
    trade_focus,
    user_id,
):
    created_at = int(
        time.time()
    )

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
        base64.urlsafe_b64encode(
            raw
        )
        .decode("ascii")
        .rstrip("=")
    )

    signature = hmac.new(
        get_job_secret(),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()

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


# =========================================================
# SUPABASE REQUEST
# =========================================================

def supabase_request(
    method,
    path,
    payload=None,
    timeout=30,
):
    if not SUPABASE_URL:
        raise RuntimeError(
            "SUPABASE_URL is missing."
        )

    if not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY is missing."
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
        headers={
            "apikey":
                SUPABASE_SERVICE_ROLE_KEY,

            "Authorization":
                "Bearer "
                + SUPABASE_SERVICE_ROLE_KEY,

            "Content-Type":
                "application/json",

            "Accept":
                "application/json",
        },
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

        details = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            "Supabase request failed "
            f"({exc.code}) "
            + details
        )

    except urllib.error.URLError as exc:

        raise RuntimeError(
            "Supabase connection failed: "
            + str(exc)
        )


# =========================================================
# CREATE QUEUED JOB
# =========================================================

def create_queued_job(
    user_id,
    instrument,
    trade_focus,
    higher_timeframe_image,
    lower_timeframe_image,
):
    job_nonce = uuid.uuid4().hex

    # Explicit UUID for the table id.
    # This prevents a NULL id when the database
    # column is NOT NULL without a default.
    job_id = str(
        uuid.uuid4()
    )

    payload = {
        "id":
            job_id,

        "job_nonce":
            job_nonce,

        "user_id":
            user_id,

        # IMPORTANT:
        # Keep instrument as plain EURUSD/XAUUSD/etc.
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
    }

    result = supabase_request(
        "POST",
        "/rest/v1/analysis_jobs",
        payload,
        timeout=30,
    )

    if result is None:
        raise RuntimeError(
            "The analysis job was not created."
        )

    return job_nonce


# =========================================================
# READ JSON BODY
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

    # IMPORTANT:
    # extract_bearer_token expects the
    # request handler, NOT the raw header.
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

    # -----------------------------------------------------
    # OPTIONS
    # -----------------------------------------------------

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
            "GET, POST, OPTIONS",
        )

        self.send_header(
            "Cache-Control",
            "no-store",
        )

        self.end_headers()


    # -----------------------------------------------------
    # POST
    # -----------------------------------------------------

    def do_POST(self):

        reserved_slot = False
        user_id = ""

        try:

            # =============================================
            # 1. AUTHENTICATE
            # =============================================

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


            # =============================================
            # 2. READ REQUEST
            # =============================================

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


            # =============================================
            # 3. READ INPUTS
            # =============================================

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


            # =============================================
            # 4. VALIDATE
            # =============================================

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


            # =============================================
            # 5. DAILY ANALYSIS LIMIT
            # =============================================

            owner = is_owner_user(
                user
            )

            if not owner:

                reserve_analysis_slot(
                    user_id
                )

                reserved_slot = True


            # =============================================
            # 6. CREATE JOB
            # =============================================

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


            # =============================================
            # 7. SIGNED JOB TOKEN
            # =============================================

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


            # =============================================
            # 8. SUCCESS
            # =============================================

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


        # =================================================
        # VALIDATION / USER ERROR
        # =================================================

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
                        str(exc),
                },
            )

            return


        # =================================================
        # DATABASE / SERVER ERROR
        # =================================================

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

            json_response(
                self,
                500,
                {
                    "error":
                        message,
                },
            )

            return


        # =================================================
        # UNEXPECTED ERROR
        # =================================================

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
                        str(exc),
                },
            )

            return


    # -----------------------------------------------------
    # GET
    # -----------------------------------------------------

    def do_GET(self):

        json_response(
            self,
            200,
            {
                "status":
                    "analysis submit online",
            },
        )
