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
from http.server import BaseHTTPRequestHandler

from analysis_common import (
    json_response,
    validate_focus,
    validate_image_data_url,
    validate_instrument,
)

from user_security import (
    extract_bearer_token,
    is_owner_user,
    release_analysis_slot,
    reserve_analysis_slot,
    verify_access_token,
)


# ---------------------------------------------------------
# CONFIG
# ---------------------------------------------------------

JOB_TTL_SECONDS = 24 * 60 * 60

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get(
    "SUPABASE_SERVICE_ROLE_KEY",
    ""
)


# ---------------------------------------------------------
# JOB TOKEN
# ---------------------------------------------------------

def _job_secret():
    secret = os.environ.get("JOB_TOKEN_SECRET", "").strip()

    if not secret:
        secret = os.environ.get("GEMINI_API_KEY", "").strip()

    if not secret:
        raise RuntimeError(
            "JOB_TOKEN_SECRET is not configured."
        )

    return secret.encode("utf-8")


def _create_job_token(
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

    encoded = base64.urlsafe_b64encode(
        raw
    ).decode("ascii").rstrip("=")

    signature = hmac.new(
        _job_secret(),
        encoded.encode("ascii"),
        hashlib.sha256,
    ).digest()

    encoded_signature = base64.urlsafe_b64encode(
        signature
    ).decode("ascii").rstrip("=")

    return encoded + "." + encoded_signature


# ---------------------------------------------------------
# SUPABASE
# ---------------------------------------------------------

def _supabase_request(
    method,
    path,
    payload=None,
    timeout=30,
    extra_headers=None,
):
    if not SUPABASE_URL:
        raise RuntimeError(
            "SUPABASE_URL is not configured."
        )

    if not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY is not configured."
        )

    url = SUPABASE_URL + path

    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": (
            "Bearer " + SUPABASE_SERVICE_ROLE_KEY
        ),
        "Content-Type": "application/json",
    }

    if extra_headers:
        headers.update(extra_headers)

    data = None

    if payload is not None:
        data = json.dumps(
            payload,
            ensure_ascii=False,
        ).encode("utf-8")

    request = urllib.request.Request(
        url,
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
            f"Supabase request failed "
            f"({exc.code}): {detail}"
        )

    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Supabase connection failed: {exc}"
        )


# ---------------------------------------------------------
# CREATE QUEUED JOB
# ---------------------------------------------------------

def _create_queued_job(
    user_id,
    instrument,
    trade_focus,
    higher_timeframe_image,
    lower_timeframe_image,
):
    job_nonce = uuid.uuid4().hex

    payload = {
        "job_nonce": job_nonce,
        "user_id": user_id,

        # Keep this format for compatibility with
        # the existing database/worker structure.
        "instrument": (
            instrument + "|" + job_nonce
        ),

        "trade_focus": trade_focus,

        "higher_timeframe_image": (
            higher_timeframe_image
        ),

        "lower_timeframe_image": (
            lower_timeframe_image
        ),

        "status": "queued",

        "openai_response_id": None,

        "error_message": None,
    }

    result = _supabase_request(
        "POST",
        "/rest/v1/analysis_jobs",
        payload,
        timeout=30,
        extra_headers={
            "Prefer": "return=representation",
        },
    )

    if not result:
        raise RuntimeError(
            "The analysis job could not be created."
        )

    return job_nonce


# ---------------------------------------------------------
# REQUEST BODY
# ---------------------------------------------------------

def _read_json(handler):
    raw_length = handler.headers.get(
        "Content-Length",
        "0",
    )

    try:
        content_length = int(raw_length)
    except ValueError:
        raise ValueError(
            "Invalid Content-Length."
        )

    if content_length <= 0:
        raise ValueError(
            "Request body is empty."
        )

    # Protect the endpoint from oversized requests.
    if content_length > 25 * 1024 * 1024:
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


# ---------------------------------------------------------
# AUTHENTICATION
# ---------------------------------------------------------

def _authenticate_user(handler):
    token = extract_bearer_token(
        handler.headers.get(
            "Authorization",
            "",
        )
    )

    if not token:
        raise ValueError(
            "Authentication required."
        )

    return verify_access_token(token)


# ---------------------------------------------------------
# HANDLER
# ---------------------------------------------------------

class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(204)
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
            "POST, OPTIONS",
        )
        self.end_headers()


    def do_POST(self):

        reserved_slot = False
        user_id = None

        try:
            # ---------------------------------------------
            # AUTH
            # ---------------------------------------------

            user = _authenticate_user(self)

            if not isinstance(user, dict):
                raise RuntimeError(
                    "Invalid authentication response."
                )

            user_id = str(
                user.get("id", "")
            ).strip()

            if not user_id:
                raise ValueError(
                    "Invalid authenticated user."
                )


            # ---------------------------------------------
            # BODY
            # ---------------------------------------------

            body = _read_json(self)

            if not isinstance(body, dict):
                raise ValueError(
                    "Invalid request body."
                )


            # ---------------------------------------------
            # INPUTS
            # ---------------------------------------------

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

            higher_timeframe_image = body.get(
                "higher_timeframe_image"
            )

            lower_timeframe_image = body.get(
                "lower_timeframe_image"
            )


            # ---------------------------------------------
            # VALIDATION
            # ---------------------------------------------

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


            # ---------------------------------------------
            # DAILY SLOT
            # ---------------------------------------------

            owner = is_owner_user(
                user_id
            )

            if not owner:

                reserve_analysis_slot(
                    user_id
                )

                reserved_slot = True


            # ---------------------------------------------
            # CREATE QUEUED JOB
            # ---------------------------------------------

            job_nonce = _create_queued_job(
                user_id=user_id,
                instrument=instrument,
                trade_focus=trade_focus,
                higher_timeframe_image=(
                    higher_timeframe_image
                ),
                lower_timeframe_image=(
                    lower_timeframe_image
                ),
            )


            # ---------------------------------------------
            # SIGNED JOB TOKEN
            # ---------------------------------------------

            job_token = _create_job_token(
                job_nonce=job_nonce,
                instrument=instrument,
                trade_focus=trade_focus,
                user_id=user_id,
            )


            # ---------------------------------------------
            # SUCCESS
            # ---------------------------------------------
            #
            # IMPORTANT:
            # Gemini is NOT called here.
            #
            # Supabase INSERT webhook starts the worker.
            #
            # ---------------------------------------------

            json_response(
                self,
                202,
                {
                    "status": "queued",
                    "job_id": job_token,
                    "job_nonce": job_nonce,
                },
            )

            return


        except ValueError as exc:

            # If the daily slot was reserved and something
            # failed afterward, give the slot back.

            if reserved_slot and user_id:
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
                    "error": str(exc)
                },
            )

            return


        except RuntimeError as exc:

            if reserved_slot and user_id:
                try:
                    release_analysis_slot(
                        user_id
                    )
                except Exception:
                    pass

            message = str(exc)

            # Daily-limit errors should remain 429.
            lowered = message.lower()

            if (
                "limit" in lowered
                or "daily" in lowered
                or "analyses" in lowered
            ):
                json_response(
                    self,
                    429,
                    {
                        "error": message
                    },
                )
            else:
                json_response(
                    self,
                    500,
                    {
                        "error": message
                    },
                )

            return


        except Exception as exc:

            if reserved_slot and user_id:
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
                    "error": (
                        "Unable to create "
                        "analysis job."
                    )
                },
            )

            return


    def do_GET(self):
        json_response(
            self,
            200,
            {
                "status": "analysis submit online"
            },
        )
