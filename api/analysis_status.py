import base64
import json
import os
import urllib.error
import urllib.parse
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


def retrieve_response(
    response_id,
    key
):

    request = urllib.request.Request(
        f"{OPENAI_URL}/{response_id}",
        headers={
            "Authorization":
                "Bearer " + key,

            "Accept":
                "application/json"
        },
        method="GET"
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=20
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
                errors="replace"
            )
        )

        raise RuntimeError(
            "Analysis status is temporarily unavailable: "
            + detail[:500]
        )


def supabase_patch(
    user_id,
    response_id,
    values
):

    if not SUPABASE_URL:
        return

    if not SUPABASE_SERVICE_ROLE_KEY:
        return

    encoded_user = urllib.parse.quote(
        str(user_id),
        safe=""
    )

    encoded_response = urllib.parse.quote(
        str(response_id),
        safe=""
    )

    path = (
        "/rest/v1/analysis_jobs"
        "?user_id=eq."
        + encoded_user
        + "&openai_response_id=eq."
        + encoded_response
    )

    body = json.dumps(
        values,
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

            "Prefer":
                "return=minimal"
        },
        method="PATCH"
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=20
        ) as response:

            response.read()

    except Exception:

        # History must never prevent
        # the user from seeing the
        # completed analysis.
        pass


def encode_result(
    result
):

    raw = json.dumps(
        result,
        ensure_ascii=False,
        separators=(",", ":")
    ).encode("utf-8")

    return (
        base64.urlsafe_b64encode(
            raw
        )
        .decode("ascii")
        .rstrip("=")
    )


def extract_job_token(
    handler
):

    parsed = urlparse(
        handler.path
    )

    values = parse_qs(
        parsed.query
    )

    return values.get(
        "job_id",
        [""]
    )[0]


class handler(
    BaseHTTPRequestHandler
):

    def do_OPTIONS(self):

        json_response(
            self,
            204,
            {}
        )


    def do_GET(self):

        try:

            # =========================================
            # API KEY
            # =========================================

            key = api_key()

            if not key:

                json_response(
                    self,
                    500,
                    {
                        "error":
                            "Server configuration is incomplete."
                    }
                )

                return


            # =========================================
            # USER
            # =========================================

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


            # =========================================
            # JOB TOKEN
            # =========================================

            token = extract_job_token(
                self
            )

            if not token:

                json_response(
                    self,
                    400,
                    {
                        "error":
                            "Missing analysis job ID."
                    }
                )

                return


            (
                response_id,
                instrument,
                trade_focus,
                token_user_id
            ) = read_secure_job_token(
                token
            )


            # =========================================
            # SECURITY
            # =========================================

            if (
                token_user_id
                != current_user_id
            ):

                json_response(
                    self,
                    403,
                    {
                        "error":
                            "This analysis job does not belong to this user."
                    }
                )

                return


            # =========================================
            # OPENAI
            # =========================================

            response = retrieve_response(
                response_id,
                key
            )

            status = str(
                response.get(
                    "status",
                    "queued"
                )
            ).strip().lower()


            # =========================================
            # STILL PROCESSING
            # =========================================

            if status in {
                "queued",
                "in_progress"
            }:

                json_response(
                    self,
                    200,
                    {
                        "status":
                            status,

                        "poll_after_seconds":
                            2
                    }
                )

                return


            # =========================================
            # COMPLETED
            # =========================================

            if status == "completed":

                result = (
                    parse_completed_response(
                        response,
                        instrument,
                        trade_focus
                    )
                )

                # Save the completed analysis
                # for History.
                encoded_result = encode_result(
                    result
                )

                supabase_patch(
                    current_user_id,
                    response_id,
                    {
                        "status":
                            "completed",

                        "openai_response_id":
                            encoded_result,

                        "error_message":
                            None
                    }
                )

                json_response(
                    self,
                    200,
                    {
                        "status":
                            "completed",

                        "result":
                            result
                    }
                )

                return


            # =========================================
            # FAILED
            # =========================================

            if status in {
                "failed",
                "cancelled",
                "expired",
                "incomplete"
            }:

                supabase_patch(
                    current_user_id,
                    response_id,
                    {
                        "status":
                            "failed",

                        # NOT NULL column.
                        "openai_response_id":
                            "failed",

                        "error_message":
                            "The analysis did not complete."
                    }
                )

                json_response(
                    self,
                    200,
                    {
                        "status":
                            "failed",

                        "error":
                            "The analysis did not complete."
                    }
                )

                return


            # =========================================
            # UNKNOWN
            # =========================================

            json_response(
                self,
                200,
                {
                    "status":
                        "in_progress",

                    "poll_after_seconds":
                        3
                }
            )

        except ValueError as exc:

            json_response(
                self,
                400,
                {
                    "error":
                        str(exc)
                }
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
                        str(exc)
                }
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
                        str(exc)
                }
            )
