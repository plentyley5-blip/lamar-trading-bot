import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from api.user_security import (
    extract_bearer_token,
    get_supabase_service_key,
    get_supabase_url,
    read_secure_job_token,
    verify_access_token,
)


JOB_TTL_SECONDS = 15 * 60


def json_response(
    handler,
    status_code,
    payload,
):

    body = json.dumps(
        payload,
        ensure_ascii=False,
    ).encode("utf-8")

    handler.send_response(
        status_code
    )

    handler.send_header(
        "Content-Type",
        "application/json; charset=utf-8",
    )

    handler.send_header(
        "Access-Control-Allow-Origin",
        "*",
    )

    handler.send_header(
        "Access-Control-Allow-Methods",
        "GET, POST, OPTIONS",
    )

    handler.send_header(
        "Access-Control-Allow-Headers",
        "Content-Type, Authorization",
    )

    handler.send_header(
        "Cache-Control",
        "no-store, no-cache, must-revalidate",
    )

    handler.send_header(
        "Content-Length",
        str(len(body)),
    )

    handler.end_headers()

    try:
        handler.wfile.write(body)
    except Exception:
        pass


def get_job_from_token(
    token,
):
    """
    Uses the existing user_security token format:

    response_id  -> database analysis_jobs.id
    instrument
    trade_focus
    user_id
    """

    try:

        response_id, instrument, trade_focus, user_id = (
            read_secure_job_token(
                token
            )
        )

        return (
            response_id,
            instrument,
            trade_focus,
            user_id,
        )

    except Exception as exc:

        raise ValueError(
            "Invalid analysis job."
        ) from exc


def extract_job_token(
    handler,
):

    parsed = urlparse(
        handler.path
    )

    values = parse_qs(
        parsed.query
    )

    return str(
        values.get(
            "job_id",
            [""],
        )[0]
    ).strip()


def load_completed_job(
    database_job_id,
    user_id,
    instrument,
    trade_focus,
):
    query = (
        get_supabase_url()
        + "/rest/v1/analysis_jobs?"
        + "id=eq."
        + urllib.parse.quote(
            str(database_job_id),
            safe="",
        )
        + "&user_id=eq."
        + urllib.parse.quote(
            str(user_id),
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
        + "&status=eq.completed"
        + "&select=instrument,trade_focus,created_at,openai_response_id"
        + "&limit=1"
    )

    key = get_supabase_service_key()

    request = urllib.request.Request(
        query,

        headers={
            "apikey":
                key,

            "Authorization":
                "Bearer " + key,

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

        print(
            "SUPABASE STATUS ERROR:",
            raw,
        )

        raise RuntimeError(
            "The analysis result could not be loaded."
        ) from exc

    except (
        urllib.error.URLError,
        TimeoutError,
    ) as exc:

        raise RuntimeError(
            "Supabase is temporarily unavailable."
        ) from exc

    if not isinstance(
        rows,
        list,
    ) or not rows:

        return None

    row = rows[0]

    if not isinstance(
        row,
        dict,
    ):
        return None

    encoded_result = str(
        row.get(
            "openai_response_id",
            "",
        )
    ).strip()

    if not encoded_result:
        return None

    try:

        raw = (
            base64.urlsafe_b64decode(
                encoded_result
                + "="
                * (
                    -len(encoded_result)
                    % 4
                )
            )
            .decode("utf-8")
        )

        result = json.loads(
            raw
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

    return result


class handler(
    BaseHTTPRequestHandler
):

    def do_OPTIONS(
        self
    ):

        json_response(
            self,
            204,
            {}
        )

    def do_GET(
        self
    ):

        try:

            # Verify current user.
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

            # Read secure job token.
            job_token = (
                extract_job_token(
                    self
                )
            )

            if not job_token:

                raise ValueError(
                    "Analysis job ID is required."
                )

            (
                database_job_id,
                instrument,
                trade_focus,
                token_user_id,
            ) = get_job_from_token(
                job_token
            )

            # User ownership check.
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
                    },
                )

                return

            # The Gemini analysis is already completed.
            result = load_completed_job(
                database_job_id,
                current_user_id,
                instrument,
                trade_focus,
            )

            if result is None:

                json_response(
                    self,
                    200,
                    {
                        "status":
                            "in_progress",

                        "poll_after_seconds":
                            2,
                    },
                )

                return

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
                        str(exc),
                },
            )

        except RuntimeError as exc:

            json_response(
                self,
                503,
                {
                    "error":
                        str(exc),
                },
            )

        except Exception as exc:

            print(
                "GEMINI STATUS ERROR:",
                repr(exc),
            )

            json_response(
                self,
                500,
                {
                    "error":
                        "Analysis status failed.",

                    "details":
                        str(exc),
                },
            )
