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

from api.user_security import (
    extract_bearer_token,
    verify_access_token,
)


JOB_TTL_SECONDS = 24 * 60 * 60

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


def read_job_token(token):
    if not token or "." not in token:
        raise ValueError(
            "Invalid analysis job."
        )

    encoded_payload, encoded_signature = (
        token.split(".", 1)
    )

    expected_signature = hmac.new(
        get_job_secret(),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()

    expected_encoded = (
        base64.urlsafe_b64encode(
            expected_signature
        )
        .decode("ascii")
        .rstrip("=")
    )

    if not hmac.compare_digest(
        encoded_signature,
        expected_encoded,
    ):
        raise ValueError(
            "Invalid analysis job."
        )

    padded = (
        encoded_payload
        + "="
        * (-len(encoded_payload) % 4)
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

    if not isinstance(
        payload,
        dict,
    ):
        raise ValueError(
            "Invalid analysis job."
        )

    job_nonce = str(
        payload.get(
            "job_nonce",
            "",
        )
    ).strip()

    instrument = str(
        payload.get(
            "instrument",
            "",
        )
    ).strip().upper()

    trade_focus = str(
        payload.get(
            "trade_focus",
            "",
        )
    ).strip().upper()

    user_id = str(
        payload.get(
            "user_id",
            "",
        )
    ).strip()

    created_at = int(
        payload.get(
            "created_at",
            0,
        )
    )

    if not job_nonce or not user_id:
        raise ValueError(
            "Invalid analysis job."
        )

    if not instrument or not trade_focus:
        raise ValueError(
            "Invalid analysis job."
        )

    if (
        created_at <= 0
        or int(time.time()) - created_at
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


def supabase_request(
    method,
    path,
    payload=None,
):
    if not SUPABASE_URL:
        raise RuntimeError(
            "SUPABASE_URL is not configured."
        )

    if not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY is not configured."
        )

    data = None

    if payload is not None:
        data = json.dumps(
            payload
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
        },
        method=method,
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=30,
        ) as response:

            raw = response.read().decode(
                "utf-8",
                errors="replace",
            )

            return (
                json.loads(raw)
                if raw
                else None
            )

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            "Supabase request failed "
            f"({exc.code}): {detail}"
        )


def load_job(
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

    rows = supabase_request(
        "GET",
        path,
    )

    if not isinstance(
        rows,
        list,
    ) or not rows:
        return None

    return rows[0]


def decode_result(value):
    if not isinstance(
        value,
        str,
    ) or not value:
        return None

    try:
        padded = (
            value
            + "="
            * (-len(value) % 4)
        )

        raw = base64.urlsafe_b64decode(
            padded.encode("ascii")
        )

        result = json.loads(
            raw.decode("utf-8")
        )

        return (
            result
            if isinstance(result, dict)
            else None
        )

    except Exception:
        return None


def check_session(
    handler,
    token_user_id,
):
    authorization = handler.headers.get(
        "Authorization",
        "",
    ).strip()

    if not authorization:
        return

    # IMPORTANT:
    # extract_bearer_token expects handler.
    bearer = extract_bearer_token(
        handler
    )

    session_user = verify_access_token(
        bearer
    )

    session_user_id = str(
        session_user.get(
            "id",
            "",
        )
    ).strip()

    if (
        not session_user_id
        or session_user_id
        != token_user_id
    ):
        raise ValueError(
            "This analysis job belongs to another user."
        )


def get_job_id(handler):
    query = urllib.parse.urlparse(
        handler.path
    ).query

    params = urllib.parse.parse_qs(
        query
    )

    values = params.get(
        "job_id",
        [],
    )

    return (
        str(values[0]).strip()
        if values
        else ""
    )


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

            job_token = get_job_id(
                self
            )

            if not job_token:
                json_response(
                    self,
                    400,
                    {
                        "error":
                            "Missing analysis job ID."
                    },
                )
                return

            (
                job_nonce,
                instrument,
                trade_focus,
                token_user_id,
            ) = read_job_token(
                job_token
            )

            check_session(
                self,
                token_user_id,
            )

            job = load_job(
                token_user_id,
                job_nonce,
            )

            if not job:
                json_response(
                    self,
                    404,
                    {
                        "error":
                            "Analysis job was not found."
                    },
                )
                return

            database_user_id = str(
                job.get(
                    "user_id",
                    "",
                )
            ).strip()

            if database_user_id != token_user_id:
                json_response(
                    self,
                    403,
                    {
                        "error":
                            "You do not have access to this analysis."
                    },
                )
                return

            status = str(
                job.get(
                    "status",
                    "",
                )
            ).strip().lower()

            if status in {
                "queued",
                "processing",
            }:

                json_response(
                    self,
                    200,
                    {
                        "status":
                            status,
                        "job_id":
                            job_token,
                    },
                )
                return

            if status == "failed":

                json_response(
                    self,
                    200,
                    {
                        "status":
                            "failed",
                        "job_id":
                            job_token,
                        "error":
                            str(
                                job.get(
                                    "error_message",
                                    "Analysis processing failed.",
                                )
                            ),
                    },
                )
                return

            if status == "completed":

                result = decode_result(
                    job.get(
                        "openai_response_id"
                    )
                )

                if result is None:
                    json_response(
                        self,
                        500,
                        {
                            "status":
                                "failed",
                            "job_id":
                                job_token,
                            "error":
                                "The analysis result could not be read.",
                        },
                    )
                    return

                json_response(
                    self,
                    200,
                    {
                        "status":
                            "completed",
                        "job_id":
                            job_token,
                        "result":
                            result,
                    },
                )
                return

            json_response(
                self,
                200,
                {
                    "status":
                        "processing",
                    "job_id":
                        job_token,
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

        except Exception as exc:

            json_response(
                self,
                500,
                {
                    "error": str(exc)
                },
            )
