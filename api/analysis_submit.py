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


SUPABASE_URL = os.environ.get(
    "SUPABASE_URL",
    ""
).strip().rstrip("/")

SUPABASE_SERVICE_ROLE_KEY = os.environ.get(
    "SUPABASE_SERVICE_ROLE_KEY",
    ""
).strip()


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
            "JOB_TOKEN_SECRET is not configured."
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

    encoded = base64.urlsafe_b64encode(
        raw
    ).decode("ascii").rstrip("=")

    signature = hmac.new(
        get_job_secret(),
        encoded.encode("ascii"),
        hashlib.sha256,
    ).digest()

    encoded_signature = (
        base64.urlsafe_b64encode(
            signature
        )
        .decode("ascii")
        .rstrip("=")
    )

    return encoded + "." + encoded_signature


def supabase_request(
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

    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": (
            "Bearer "
            + SUPABASE_SERVICE_ROLE_KEY
        ),
        "Content-Type": "application/json",
        "Accept": "application/json",
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
            f"({exc.code}): {detail}"
        )

    except urllib.error.URLError as exc:
        raise RuntimeError(
            "Supabase connection failed: "
            + str(exc)
        )


def create_queued_job(
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

        # IMPORTANT:
        # Store the instrument by itself.
        "instrument": instrument,

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

    result = supabase_request(
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


def read_json(handler):
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


def authenticate_user(handler):
    # IMPORTANT:
    # extract_bearer_token expects the handler.
    token = extract_bearer_token(
        handler
    )

    user = verify_access_token(
        token
    )

    if not isinstance(user, dict):
        raise ValueError(
            "Invalid authentication response."
        )

    return user


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
            # AUTHENTICATE
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

            # -----------------------------------------
            # CREATE SIGNED TOKEN
            # -----------------------------------------
            job_token = create_job_token(
                job_nonce=job_nonce,
                instrument=instrument,
                trade_focus=trade_focus,
                user_id=user_id,
            )

            # -----------------------------------------
            # SUCCESS
            # -----------------------------------------
            json_response(
                self,
                202,
                {
                    "status": "queued",
                    "job_id": job_token,
                    "job_nonce": job_nonce,
                },
            )

        except ValueError as exc:

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

        except RuntimeError as exc:

            if reserved_slot and user_id:
                try:
                    release_analysis_slot(
                        user_id
                    )
                except Exception:
                    pass

            message = str(exc)
            lowered = message.lower()

            if (
                "limit" in lowered
                or "daily" in lowered
                or "analyses" in lowered
            ):
                status = 429
            else:
                status = 500

            json_response(
                self,
                status,
                {
                    "error": message
                },
            )

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
                    "error": str(exc)
                },
            )

    def do_GET(self):
        json_response(
            self,
            200,
            {
                "status":
                    "analysis submit online"
            },
        )
