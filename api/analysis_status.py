import base64
import json
import urllib.error
import urllib.parse
import urllib.request

from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from analysis_common import (
    OPENAI_URL,
    get_openai_key,
    json_response,
    parse_completed_response,
)

from api.user_security import (
    extract_bearer_token,
    get_supabase_service_key,
    get_supabase_url,
    read_secure_job_token,
    verify_access_token,
)


def _supabase_headers():
    key = get_supabase_service_key()

    return {
        "apikey": key,
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def retrieve_response(response_id, key):
    request = urllib.request.Request(
        f"{OPENAI_URL}/{response_id}",
        headers={
            "Authorization": "Bearer " + key,
            "Accept": "application/json",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=20,
        ) as response:
            return json.loads(
                response.read().decode("utf-8")
            )

    except urllib.error.HTTPError as exc:
        print(
            "OpenAI status error:",
            exc.code,
        )
        raise RuntimeError(
            "Analysis status is temporarily unavailable."
        ) from exc

    except (
        urllib.error.URLError,
        TimeoutError,
    ) as exc:
        raise RuntimeError(
            "Analysis status is temporarily unavailable."
        ) from exc


def _extract_job_token(handler):
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


def _encode_result(result):
    raw = json.dumps(
        result,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    return (
        base64.urlsafe_b64encode(raw)
        .decode("ascii")
        .rstrip("=")
    )


def _save_completed_result(
    user_id,
    response_id,
    result,
):
    encoded_result = _encode_result(
        result
    )

    url = (
        get_supabase_url()
        + "/rest/v1/analysis_jobs?"
        + "user_id=eq."
        + urllib.parse.quote(
            str(user_id),
            safe="",
        )
        + "&openai_response_id=eq."
        + urllib.parse.quote(
            str(response_id),
            safe="",
        )
    )

    payload = {
        "status":
            "completed",

        "openai_response_id":
            encoded_result,

        "error_message":
            None,
    }

    request = urllib.request.Request(
        url,
        data=json.dumps(
            payload,
            separators=(",", ":"),
        ).encode("utf-8"),
        headers={
            **_supabase_headers(),
            "Prefer":
                "return=minimal",
        },
        method="PATCH",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=20,
        ) as response:
            response.read()

        return True

    except Exception as exc:
        print(
            "Completed history save failed:",
            repr(exc),
        )
        return False


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        json_response(
            self,
            204,
            {},
        )

    def do_GET(self):
        try:
            key = get_openai_key()

            access_token = extract_bearer_token(
                self
            )

            user = verify_access_token(
                access_token
            )

            current_user_id = str(
                user["id"]
            )

            token = _extract_job_token(
                self
            )

            (
                response_id,
                instrument,
                trade_focus,
                token_user_id,
            ) = read_secure_job_token(
                token
            )

            if token_user_id != current_user_id:
                json_response(
                    self,
                    403,
                    {
                        "error":
                            "This analysis job does not belong to this user."
                    },
                )
                return

            response = retrieve_response(
                response_id,
                key,
            )

            status = str(
                response.get(
                    "status",
                    "queued",
                )
            ).lower()

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

            if status == "completed":
                result = parse_completed_response(
                    response,
                    instrument,
                    trade_focus,
                )

                history_saved = _save_completed_result(
                    current_user_id,
                    response_id,
                    result,
                )

                json_response(
                    self,
                    200,
                    {
                        "status":
                            "completed",

                        "result":
                            result,

                        "history_saved":
                            history_saved,
                    },
                )
                return

            if status in {
                "failed",
                "cancelled",
                "incomplete",
                "expired",
            }:
                json_response(
                    self,
                    200,
                    {
                        "status":
                            "failed",

                        "error":
                            "The analysis did not complete.",
                    },
                )
                return

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
                        str(exc)
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
            print(
                "analysis_status error:",
                repr(exc),
            )

            json_response(
                self,
                200,
                {
                    "status":
                        "in_progress",

                    "poll_after_seconds":
                        4,
                },
            )
