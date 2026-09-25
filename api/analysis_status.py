import base64
import json
import os
import urllib.error
import urllib.request

from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from analysis_common import (
    OPENAI_URL,
    api_key,
    json_response,
    parse_completed_response,
)

from api.user_security import (
    extract_bearer_token,
    read_secure_job_token,
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


def supabase_request(
    method,
    path,
    payload=None,
    timeout=20,
):

    if not SUPABASE_URL:
        raise RuntimeError(
            "SUPABASE_URL is missing."
        )

    if not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY is missing."
        )

    body = None

    if payload is not None:

        body = json.dumps(
            payload,
            ensure_ascii=False,
        ).encode("utf-8")

    request = urllib.request.Request(
        SUPABASE_URL + path,
        data=body,
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

            raw = (
                response.read()
                .decode(
                    "utf-8",
                    errors="replace",
                )
            )

            if not raw:
                return None

            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw

    except urllib.error.HTTPError as exc:

        detail = (
            exc.read()
            .decode(
                "utf-8",
                errors="replace",
            )
        )

        raise RuntimeError(
            "Supabase request failed "
            f"({exc.code}) "
            + detail
        )


def retrieve_response(
    response_id,
    key,
):

    request = urllib.request.Request(

        f"{OPENAI_URL}/{response_id}",

        headers={
            "Authorization":
                "Bearer "
                + key,

            "Accept":
                "application/json",
        },

        method="GET",
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=20,
        ) as response:

            return json.loads(
                response.read()
                .decode(
                    "utf-8"
                )
            )

    except urllib.error.HTTPError as exc:

        detail = (
            exc.read()
            .decode(
                "utf-8",
                errors="replace",
            )
        )

        raise RuntimeError(
            "Analysis status is temporarily unavailable: "
            + detail[:500]
        )


def encode_result(
    result,
):

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


def save_completed_result(
    user_id,
    response_id,
    result,
):

    encoded_result = encode_result(
        result
    )

    # Only update this user's row and
    # this exact OpenAI response.
    user = (
        urllib.parse.quote(
            user_id,
            safe="",
        )
    )

    response = (
        urllib.parse.quote(
            response_id,
            safe="",
        )
    )

    path = (
        "/rest/v1/analysis_jobs"
        "?user_id=eq."
        + user
        + "&openai_response_id=eq."
        + response
    )

    supabase_request(
        "PATCH",
        path,
        {
            "status":
                "completed",

            "openai_response_id":
                encoded_result,

            "error_message":
                None,
        },
    )


def save_failed_result(
    user_id,
    response_id,
    message,
):

    user = (
        urllib.parse.quote(
            user_id,
            safe="",
        )
    )

    response = (
        urllib.parse.quote(
            response_id,
            safe="",
        )
    )

    path = (
        "/rest/v1/analysis_jobs"
        "?user_id=eq."
        + user
        + "&openai_response_id=eq."
        + response
    )

    supabase_request(
        "PATCH",
        path,
        {
            "status":
                "failed",

            # Must remain non-null.
            "openai_response_id":
                "failed",

            "error_message":
                message[:2000],
        },
    )


def extract_job_token(
    handler,
):

    parsed = urlparse(
        handler.path
    )

    values = parse_qs(
        parsed.query
    )

    return values.get(
        "job_id",
        [""],
    )[0]


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

            # -----------------------------------------
            # SERVER KEY
            # -----------------------------------------

            key = api_key()

            if not key:

                json_response(
                    self,
                    500,
                    {
                        "error":
                            "Server configuration is incomplete.",
                    },
                )

                return


            # -----------------------------------------
            # USER
            # -----------------------------------------

            access_token = (
                extract_bearer_token(
                    self
                )
            )

            user = verify_access_token(
                access_token
            )

            current_user_id = str(
                user["id"]
            )


            # -----------------------------------------
            # JOB TOKEN
            # -----------------------------------------

            token = (
                extract_job_token(
                    self
                )
            )

            if not token:

                json_response(
                    self,
                    400,
                    {
                        "error":
                            "Missing analysis job ID.",
                    },
                )

                return


            (
                response_id,
                instrument,
                trade_focus,
                token_user_id,
            ) = read_secure_job_token(
                token
            )


            # -----------------------------------------
            # OWNERSHIP
            # -----------------------------------------

            if (
                token_user_id
                != current_user_id
            ):

                json_response(
                    self,
                    403,
                    {
                        "error":
                            "This analysis job does not belong to this user.",
                    },
                )

                return


            # -----------------------------------------
            # OPENAI STATUS
            # -----------------------------------------

            response = retrieve_response(
                response_id,
                key
            )

            status = str(
                response.get(
                    "status",
                    "queued",
                )
            ).strip().lower()


            # -----------------------------------------
            # STILL RUNNING
            # -----------------------------------------

            if status in {
                "queued",
                "in_progress",
            }:

                json_response(
                    self,
                    200,
                    {
                        "status":
                            status,

                        "poll_after_seconds":
                            2,
                    },
                )

                return


            # -----------------------------------------
            # COMPLETED
            # -----------------------------------------

            if status == "completed":

                result = (
                    parse_completed_response(
                        response,
                        instrument,
                        trade_focus,
                    )
                )

                # Save completed result for History.
                try:

                    save_completed_result(
                        current_user_id,
                        response_id,
                        result,
                    )

                except Exception:

                    # Do not prevent the user
                    # from receiving the result
                    # because History storage failed.
                    pass


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

                return


            # -----------------------------------------
            # FAILED
            # -----------------------------------------

            if status in {
                "failed",
                "cancelled",
                "expired",
                "incomplete",
            }:

                message = (
                    "The analysis did not complete."
                )

                try:

                    save_failed_result(
                        current_user_id,
                        response_id,
                        message,
                    )

                except Exception:
                    pass


                json_response(
                    self,
                    200,
                    {
                        "status":
                            "failed",

                        "error":
                            message,
                    },
                )

                return


            # -----------------------------------------
            # UNKNOWN
            # -----------------------------------------

            json_response(
                self,
                200,
                {
                    "status":
                        "in_progress",

                    "poll_after_seconds":
                        3,
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
                200,
                {
                    "status":
                        "in_progress",

                    "poll_after_seconds":
                        4,

                    "message":
                        str(exc),
                },
            )

        except Exception as exc:

            json_response(
                self,
                200,
                {
                    "status":
                        "in_progress",

                    "poll_after_seconds":
                        4,

                    "message":
                        str(exc),
                },
            )
