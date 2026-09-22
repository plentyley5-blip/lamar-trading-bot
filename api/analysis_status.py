import json
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
            timeout=30,
        ) as response:
            return json.loads(
                response.read().decode("utf-8")
            )

    except urllib.error.HTTPError as exc:

        raw = ""

        try:
            raw = exc.read().decode(
                "utf-8",
                errors="replace",
            )
        except Exception:
            pass

        try:
            data = json.loads(raw)

            if isinstance(data, dict):
                error = data.get("error")

                if isinstance(error, dict):

                    message = str(
                        error.get(
                            "message",
                            "",
                        )
                    ).strip()

                    if message:
                        raise RuntimeError(
                            message
                        )

        except RuntimeError:
            raise

        except Exception:
            pass

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


def extract_job_token(handler):
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


def extract_failure_reason(response):
    error = response.get(
        "error"
    )

    if isinstance(
        error,
        dict,
    ):

        message = str(
            error.get(
                "message",
                "",
            )
        ).strip()

        code = str(
            error.get(
                "code",
                "",
            )
        ).strip()

        if message and code:
            return f"{message} ({code})"

        if message:
            return message

        if code:
            return code

    incomplete = response.get(
        "incomplete_details"
    )

    if isinstance(
        incomplete,
        dict,
    ):

        reason = str(
            incomplete.get(
                "reason",
                "",
            )
        ).strip()

        if reason:
            return (
                "The analysis became incomplete. "
                f"Reason: {reason}"
            )

    return (
        "The analysis did not complete."
    )


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):

        json_response(
            self,
            204,
            {},
        )

    def do_GET(self):

        try:

            key = api_key()

            if not key:

                json_response(
                    self,
                    500,
                    {
                        "error":
                            "Server configuration is incomplete."
                    },
                )

                return

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

            token = extract_job_token(
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

            response = retrieve_response(
                response_id,
                key,
            )

            status = str(
                response.get(
                    "status",
                    "queued",
                )
            ).strip()

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

                result = (
                    parse_completed_response(
                        response,
                        instrument,
                        trade_focus,
                    )
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

                return

            if status in {
                "failed",
                "cancelled",
                "incomplete",
                "expired",
            }:

                reason = (
                    extract_failure_reason(
                        response
                    )
                )

                json_response(
                    self,
                    200,
                    {
                        "status":
                            "failed",

                        "error":
                            reason,
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
