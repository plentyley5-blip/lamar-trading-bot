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
    read_secure_job_token,
)


def retrieve_response(
    response_id,
    key,
):
    request = urllib.request.Request(

        f"{OPENAI_URL}/{response_id}",

        headers={
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
            timeout=30,
        ) as response:

            raw = (
                response
                .read()
                .decode("utf-8")
            )

            return json.loads(
                raw
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

            data = json.loads(
                raw
            )

            if isinstance(
                data,
                dict,
            ):

                error = data.get(
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

                    if message:
                        raise RuntimeError(
                            "OpenAI: " + message
                        )

        except RuntimeError:
            raise

        except Exception:
            pass

        if exc.code == 401:
            raise RuntimeError(
                "The server AI API key was rejected."
            )

        if exc.code == 429:
            raise RuntimeError(
                "The AI service rate or usage limit was reached."
            )

        if 500 <= exc.code <= 599:
            raise RuntimeError(
                "The AI service is temporarily unavailable."
            )

        raise RuntimeError(
            "The analysis result could not be retrieved."
        )

    except (
        urllib.error.URLError,
        TimeoutError,
    ) as exc:

        raise RuntimeError(
            "The analysis service is temporarily unavailable."
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

    token = values.get(
        "job_id",
        [""],
    )[0]

    return str(
        token
    ).strip()


def extract_failure_reason(
    response,
):
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
            return (
                f"{message} ({code})"
            )

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


class handler(
    BaseHTTPRequestHandler
):

    def do_OPTIONS(
        self
    ):
        json_response(
            self,
            204,
            {},
        )

    def do_GET(
        self
    ):

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

            # ------------------------------------------------
            # IMPORTANT:
            #
            # Do NOT verify the Supabase access token again
            # here.
            #
            # /analysis_submit already authenticated the user
            # and created a signed job token containing that
            # user's ID.
            #
            # The signed job token is short-lived and is tied to
            # the authenticated user who created the job.
            # ------------------------------------------------

            job_token = extract_job_token(
                self
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
                response_id,
                instrument,
                trade_focus,
                token_user_id,
            ) = read_secure_job_token(
                job_token
            )

            # The signed token itself is the authorization
            # credential for this short-lived analysis job.
            #
            # token_user_id is deliberately extracted and retained
            # so that the job remains bound to its originating user.
            #
            # It is not compared against a second Supabase request,
            # because that second request was the source of the
            # expired-session failure during polling.

            if not token_user_id:

                json_response(
                    self,
                    400,
                    {
                        "error":
                            "Invalid analysis job."
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
                        "failed",

                    "error":
                        str(exc),
                },
            )

        except Exception as exc:

            json_response(
                self,
                200,
                {
                    "status":
                        "failed",

                    "error":
                        "Analysis status request failed: "
                        + str(exc),
                },
            )
