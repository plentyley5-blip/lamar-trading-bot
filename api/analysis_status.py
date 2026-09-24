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
from urllib.parse import parse_qs, urlparse

from analysis_common import json_response

from api.user_security import (
    extract_bearer_token,
    verify_access_token,
)


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

    return secret.encode("utf-8")


def _read_job_token(token):

    try:

        parts = str(
            token or ""
        ).split(".", 1)

        if len(parts) != 2:

            raise ValueError(
                "Invalid analysis job."
            )

        encoded_payload = parts[0]
        encoded_signature = parts[1]

        raw = base64.urlsafe_b64decode(
            encoded_payload
            + "="
            * (
                -len(encoded_payload)
                % 4
            )
        )

        supplied_signature = (
            base64.urlsafe_b64decode(
                encoded_signature
                + "="
                * (
                    -len(encoded_signature)
                    % 4
                )
            )
        )

        expected_signature = hmac.new(
            _job_secret(),
            raw,
            hashlib.sha256,
        ).digest()

        if not hmac.compare_digest(
            supplied_signature,
            expected_signature,
        ):

            raise ValueError(
                "Invalid analysis job signature."
            )

        payload = json.loads(
            raw.decode("utf-8")
        )

        if not isinstance(
            payload,
            dict,
        ):

            raise ValueError(
                "Invalid analysis job."
            )

        created_at = int(
            payload.get(
                "created_at",
                0,
            )
        )

        if (
            created_at <= 0
            or time.time() - created_at
            > JOB_TTL_SECONDS
        ):

            raise ValueError(
                "Analysis job has expired."
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
        ).strip()

        trade_focus = str(
            payload.get(
                "trade_focus",
                "",
            )
        ).strip()

        user_id = str(
            payload.get(
                "user_id",
                "",
            )
        ).strip()

        if (
            not job_nonce
            or not instrument
            or not user_id
        ):

            raise ValueError(
                "Invalid analysis job."
            )

        return (
            job_nonce,
            instrument,
            trade_focus,
            user_id,
        )

    except ValueError:
        raise

    except Exception as exc:

        raise ValueError(
            "Invalid analysis job."
        ) from exc


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

        "Accept":
            "application/json",
    }


# ============================================================
# LOAD JOB
# ============================================================

def _load_job(
    user_id,
    job_nonce,
    instrument,
    trade_focus,
):

    query = (

        _supabase_url()
        + "/rest/v1/analysis_jobs?"

        + "user_id=eq."
        + urllib.parse.quote(
            str(user_id),
            safe="",
        )

        + "&job_nonce=eq."
        + urllib.parse.quote(
            str(job_nonce),
            safe="",
        )

        + "&instrument=eq."
        + urllib.parse.quote(
            str(instrument),
            safe="",
        )

        + "&trade_focus=eq."
        + urllib.parse.quote(
            str(trade_focus),
            safe="",
        )

        + "&select="
          "status,"
          "openai_response_id,"
          "error_message,"
          "created_at"

        + "&limit=1"
    )

    request = urllib.request.Request(
        query,
        headers=_supabase_headers(),
        method="GET",
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=15,
        ) as response:

            rows = json.loads(
                response
                .read()
                .decode("utf-8")
            )

    except urllib.error.HTTPError as exc:

        raw = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            "Supabase could not retrieve the analysis job: "
            + raw
        ) from exc

    except (
        urllib.error.URLError,
        TimeoutError,
    ) as exc:

        raise RuntimeError(
            "Supabase is temporarily unavailable."
        ) from exc

    if (
        not isinstance(
            rows,
            list,
        )
        or not rows
    ):

        return None

    return rows[0]


# ============================================================
# OPTIONAL SESSION CHECK
# ============================================================

def _check_optional_session(
    handler,
    token_user_id,
):

    auth_header = str(
        handler.headers.get(
            "Authorization",
            "",
        )
    ).strip()

    if not auth_header:
        return

    try:

        access_token = (
            extract_bearer_token(
                handler
            )
        )

        user = verify_access_token(
            access_token
        )

        current_user_id = str(
            user.get(
                "id",
                "",
            )
        ).strip()

        if (
            current_user_id
            and current_user_id
            != token_user_id
        ):

            raise PermissionError(
                "This analysis job does not belong to this user."
            )

    except PermissionError:
        raise

    except Exception:

        # The signed job token remains valid even if the
        # Supabase access token has expired during polling.
        return


# ============================================================
# JOB TOKEN FROM URL
# ============================================================

def _extract_job_token(handler):

    parsed = urlparse(
        handler.path
    )

    values = parse_qs(
        parsed.query
    )

    token = values.get(
        "job_id",
        [""],
    )[0]

    return str(
        token
    ).strip()


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

    def do_GET(self):

        try:

            job_token = (
                _extract_job_token(
                    self
                )
            )

            if not job_token:

                json_response(
                    self,
                    400,
                    {
                        "error":
                            "Analysis job token is required."
                    },
                )

                return

            (
                job_nonce,
                instrument,
                trade_focus,
                token_user_id,
            ) = _read_job_token(
                job_token
            )

            try:

                _check_optional_session(
                    self,
                    token_user_id,
                )

            except PermissionError as exc:

                json_response(
                    self,
                    403,
                    {
                        "error":
                            str(exc)
                    },
                )

                return

            job = _load_job(

                user_id=
                    token_user_id,

                job_nonce=
                    job_nonce,

                instrument=
                    instrument,

                trade_focus=
                    trade_focus,
            )

            if job is None:

                json_response(
                    self,
                    404,
                    {
                        "error":
                            "Analysis job was not found."
                    },
                )

                return

            status = str(
                job.get(
                    "status",
                    "queued",
                )
            ).strip().lower()

            # ------------------------------------------------
            # QUEUED
            # ------------------------------------------------

            if status == "queued":

                json_response(
                    self,
                    200,
                    {
                        "status":
                            "queued",

                        "poll_after_seconds":
                            2,
                    },
                )

                return

            # ------------------------------------------------
            # PROCESSING
            # ------------------------------------------------

            if status == "processing":

                json_response(
                    self,
                    200,
                    {
                        "status":
                            "processing",

                        "poll_after_seconds":
                            2,
                    },
                )

                return

            # ------------------------------------------------
            # FAILED
            # ------------------------------------------------

            if status == "failed":

                error_message = str(
                    job.get(
                        "error_message",
                        "",
                    )
                ).strip()

                json_response(
                    self,
                    200,
                    {
                        "status":
                            "failed",

                        "error":
                            error_message
                            or
                            "The analysis failed.",
                    },
                )

                return

            # ------------------------------------------------
            # COMPLETED
            # ------------------------------------------------

            result_blob = str(
                job.get(
                    "openai_response_id",
                    "",
                )
            ).strip()

            if not result_blob:

                json_response(
                    self,
                    200,
                    {
                        "status":
                            "processing",

                        "poll_after_seconds":
                            2,
                    },
                )

                return

            try:

                decoded = (
                    base64.urlsafe_b64decode(
                        result_blob
                        + "="
                        * (
                            -len(result_blob)
                            % 4
                        )
                    )
                    .decode("utf-8")
                )

                result = json.loads(
                    decoded
                )

            except Exception as exc:

                raise RuntimeError(
                    "The stored analysis result is invalid."
                ) from exc

            if not isinstance(
                result,
                dict,
            ):

                raise RuntimeError(
                    "The stored analysis result is invalid."
                )

            json_response(
                self,
                200,
                {
                    "status":
                        "completed",

                    "result":
                        result,
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

            json_response(
                self,
                503,
                {
                    "error":
                        str(exc)
                },
            )

        except Exception as exc:

            json_response(
                self,
                500,
                {
                    "error":
                        "Analysis status request failed: "
                        + str(exc),
                },
            )
