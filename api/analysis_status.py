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
from user_security import (
    extract_bearer_token,
    verify_access_token,
)


# ---------------------------------------------------------
# CONFIG
# ---------------------------------------------------------

JOB_TTL_SECONDS = 24 * 60 * 60

SUPABASE_URL = os.environ.get(
    "SUPABASE_URL",
    ""
).rstrip("/")

SUPABASE_SERVICE_ROLE_KEY = os.environ.get(
    "SUPABASE_SERVICE_ROLE_KEY",
    "")


# ---------------------------------------------------------
# JOB TOKEN
# ---------------------------------------------------------

def _job_secret():
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


def _read_job_token(token):
    if not token or "." not in token:
        raise ValueError(
            "Invalid analysis job."
        )

    encoded_payload, encoded_signature = (
        token.split(".", 1)
    )

    if not encoded_payload or not encoded_signature:
        raise ValueError(
            "Invalid analysis job."
        )

    expected_signature = hmac.new(
        _job_secret(),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()

    expected_encoded = base64.urlsafe_b64encode(
        expected_signature
    ).decode("ascii").rstrip("=")

    if not hmac.compare_digest(
        encoded_signature,
        expected_encoded,
    ):
        raise ValueError(
            "Invalid analysis job."
        )

    padded = encoded_payload + (
        "=" * (
            -len(encoded_payload) % 4
        )
    )

    try:
        raw = base64.urlsafe_b64decode(
            padded.encode("ascii")
        )

        payload = json.loads(
            raw.decode("utf-8")
        )

    except Exception:
        raise ValueError(
            "Invalid analysis job."
        )

    if not isinstance(payload, dict):
        raise ValueError(
            "Invalid analysis job."
        )

    job_nonce = str(
        payload.get(
            "job_nonce",
            ""
        )
    ).strip()

    instrument = str(
        payload.get(
            "instrument",
            ""
        )
    ).strip().upper()

    trade_focus = str(
        payload.get(
            "trade_focus",
            ""
        )
    ).strip().upper()

    user_id = str(
        payload.get(
            "user_id",
            ""
        )
    ).strip()

    try:
        created_at = int(
            payload.get(
                "created_at",
                0
            )
        )
    except Exception:
        created_at = 0

    if not job_nonce:
        raise ValueError(
            "Invalid analysis job."
        )

    if not user_id:
        raise ValueError(
            "Invalid analysis job."
        )

    if not instrument:
        raise ValueError(
            "Invalid analysis job."
        )

    if not trade_focus:
        raise ValueError(
            "Invalid analysis job."
        )

    if created_at <= 0:
        raise ValueError(
            "Invalid analysis job."
        )

    # Prevent old job tokens from being reused forever.
    if (
        int(time.time()) - created_at
        > JOB_TTL_SECONDS
    ):
        raise ValueError(
            "This analysis job has expired."
        )

    return (
        job_nonce,
        instrument,
        trade_focus,
        user_id,
    )


# ---------------------------------------------------------
# SUPABASE
# ---------------------------------------------------------

def _supabase_request(
    method,
    path,
    payload=None,
    timeout=30,
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
            "Bearer "
            + SUPABASE_SERVICE_ROLE_KEY
        ),
        "Content-Type": "application/json",
    }

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
            "Supabase request failed "
            f"({exc.code}): {detail}"
        )

    except urllib.error.URLError as exc:

        raise RuntimeError(
            "Supabase connection failed: "
            + str(exc)
        )


# ---------------------------------------------------------
# FIND JOB
# ---------------------------------------------------------

def _load_job(
    user_id,
    job_nonce,
):
    encoded_nonce = urllib.parse.quote(
        job_nonce,
        safe="",
    )

    encoded_user = urllib.parse.quote(
        user_id,
        safe="",
    )

    path = (
        "/rest/v1/analysis_jobs"
        "?select="
        "job_nonce,"
        "user_id,"
        "instrument,"
        "trade_focus,"
        "status,"
        "openai_response_id,"
        "error_message,"
        "created_at"
        "&job_nonce=eq."
        + encoded_nonce
        + "&user_id=eq."
        + encoded_user
        + "&limit=1"
    )

    rows = _supabase_request(
        "GET",
        path,
        timeout=30,
    )

    if not rows:
        return None

    if not isinstance(rows, list):
        return None

    if len(rows) == 0:
        return None

    return rows[0]


# ---------------------------------------------------------
# RESULT DECODING
# ---------------------------------------------------------

def _decode_result(encoded_result):
    if not encoded_result:
        return None

    if not isinstance(
        encoded_result,
        str,
    ):
        return None

    try:
        padded = encoded_result + (
            "=" * (
                -len(encoded_result) % 4
            )
        )

        raw = base64.urlsafe_b64decode(
            padded.encode("ascii")
        )

        result = json.loads(
            raw.decode("utf-8")
        )

        if not isinstance(
            result,
            dict,
        ):
            return None

        return result

    except Exception:
        return None


# ---------------------------------------------------------
# OPTIONAL SESSION CHECK
# ---------------------------------------------------------

def _check_session(
    handler,
    token_user_id,
):
    authorization = handler.headers.get(
        "Authorization",
        "",
    ).strip()

    # The status endpoint can operate using the
    # signed job token alone.
    #
    # If Android also sends its normal session token,
    # verify it and make sure it belongs to the same user.

    if not authorization:
        return

    bearer = extract_bearer_token(
        authorization
    )

    if not bearer:
        raise ValueError(
            "Invalid authentication."
        )

    session_user = verify_access_token(
        bearer
    )

    if not isinstance(
        session_user,
        dict,
    ):
        raise ValueError(
            "Invalid authentication."
        )

    session_user_id = str(
        session_user.get(
            "id",
            ""
        )
    ).strip()

    if not session_user_id:
        raise ValueError(
            "Invalid authentication."
        )

    if session_user_id != token_user_id:
        raise ValueError(
            "This analysis job belongs to another user."
        )


# ---------------------------------------------------------
# QUERY PARAMETER
# ---------------------------------------------------------

def _get_job_id(handler):
    query = urllib.parse.urlparse(
        handler.path
    ).query

    params = urllib.parse.parse_qs(
        query
    )

    values = params.get(
        "job_id",
        []
    )

    if not values:
        return ""

    return str(
        values[0]
    ).strip()


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
            "GET, OPTIONS",
        )

        self.end_headers()


    def do_GET(self):

        try:

            # ---------------------------------------------
            # GET JOB TOKEN
            # ---------------------------------------------

            job_token = _get_job_id(
                self
            )

            if not job_token:
                json_response(
                    self,
                    400,
                    {
                        "error": (
                            "Missing analysis "
                            "job ID."
                        )
                    },
                )
                return


            # ---------------------------------------------
            # VERIFY SIGNED TOKEN
            # ---------------------------------------------

            (
                job_nonce,
                instrument,
                trade_focus,
                token_user_id,
            ) = _read_job_token(
                job_token
            )


            # ---------------------------------------------
            # OPTIONAL AUTH SESSION
            # ---------------------------------------------

            _check_session(
                self,
                token_user_id,
            )


            # ---------------------------------------------
            # LOAD ONLY THIS USER'S JOB
            # ---------------------------------------------

            job = _load_job(
                token_user_id,
                job_nonce,
            )

            if not job:

                json_response(
                    self,
                    404,
                    {
                        "error": (
                            "Analysis job "
                            "was not found."
                        )
                    },
                )

                return


            # ---------------------------------------------
            # EXTRA OWNERSHIP CHECK
            # ---------------------------------------------

            database_user_id = str(
                job.get(
                    "user_id",
                    ""
                )
            ).strip()

            if database_user_id != token_user_id:

                json_response(
                    self,
                    403,
                    {
                        "error": (
                            "You do not have "
                            "access to this "
                            "analysis."
                        )
                    },
                )

                return


            # ---------------------------------------------
            # DATABASE STATUS
            # ---------------------------------------------

            status = str(
                job.get(
                    "status",
                    ""
                )
            ).strip().lower()


            # ---------------------------------------------
            # QUEUED
            # ---------------------------------------------

            if status == "queued":

                json_response(
                    self,
                    200,
                    {
                        "status": "queued",
                        "job_id": job_token,
                    },
                )

                return


            # ---------------------------------------------
            # PROCESSING
            # ---------------------------------------------

            if status == "processing":

                json_response(
                    self,
                    200,
                    {
                        "status": "processing",
                        "job_id": job_token,
                    },
                )

                return


            # ---------------------------------------------
            # FAILED
            # ---------------------------------------------

            if status == "failed":

                error_message = str(
                    job.get(
                        "error_message",
                        ""
                    )
                ).strip()

                if not error_message:
                    error_message = (
                        "Analysis processing "
                        "failed."
                    )

                json_response(
                    self,
                    200,
                    {
                        "status": "failed",
                        "job_id": job_token,
                        "error": error_message,
                    },
                )

                return


            # ---------------------------------------------
            # COMPLETED
            # ---------------------------------------------

            if status == "completed":

                result = _decode_result(
                    job.get(
                        "openai_response_id"
                    )
                )

                if result is None:

                    # The worker marked it completed,
                    # but the result is missing/corrupt.
                    # Do not pretend the analysis exists.

                    json_response(
                        self,
                        500,
                        {
                            "status": "failed",
                            "job_id": job_token,
                            "error": (
                                "The analysis result "
                                "could not be read."
                            ),
                        },
                    )

                    return


                json_response(
                    self,
                    200,
                    {
                        "status": "completed",
                        "job_id": job_token,
                        "result": result,
                    },
                )

                return


            # ---------------------------------------------
            # UNKNOWN STATUS
            # ---------------------------------------------

            json_response(
                self,
                200,
                {
                    "status": "processing",
                    "job_id": job_token,
                },
            )

        except ValueError as exc:

            json_response(
                self,
                400,
                {
                    "error": str(exc)
                },
            )

        except RuntimeError as exc:

            json_response(
                self,
                500,
                {
                    "error": str(exc)
                },
            )

        except Exception:

            json_response(
                self,
                500,
                {
                    "error": (
                        "Unable to check "
                        "analysis status."
                    )
                },
            )
