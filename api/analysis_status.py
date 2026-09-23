import json
import urllib.error
import urllib.request

from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from analysis_common import (
    GEMINI_URL,
    api_key,
    json_response,
    parse_completed_response,
)

from api.user_security import (
    read_secure_job_token,
)


def retrieve_response(
    interaction_id,
    key,
):
    request = urllib.request.Request(

        f"{GEMINI_URL}/{interaction_id}",

        headers={
            "x-goog-api-key":
                key,

            "Accept":
                "application/json",

            "Api-Revision":
                "2026-05-20",
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
                            "Gemini: " + message
                        )

        except RuntimeError:
            raise

        except Exception:
            pass

        if exc.code == 401:
            raise RuntimeError(
                "The Gemini API key was rejected."
            )

        if exc.code == 429:
            raise RuntimeError(
                "Gemini rate limit or quota reached."
            )

        raise RuntimeError(
            "The Gemini analysis result could not be retrieved."
        ) from exc

    except (
        urllib.error.URLError,
        TimeoutError,
    ) as exc:

        raise RuntimeError(
            "Gemini is temporarily unavailable."
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


def extract_failure_reason(
    response,
):
    errors = response.get(
        "errors"
    )

    if isinstance(
        errors,
        list,
    ):

        messages = []

        for error in errors:

            if not isinstance(
                error,
                dict,
            ):
                continue

            message = str(
                error.get(
                    "message",
                    "",
                )
            ).strip()

            if message:
                messages.append(
                    message
                )

        if messages:
            return "; ".join(
                messages
            )

    return (
        "The Gemini analysis did not complete."
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
                            "GEMINI_API_KEY is missing from Vercel."
                    },
                )

                return

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
                interaction_id,
                instrument,
                trade_focus,
                user_id,
            ) = read_secure_job_token(
                job_token
            )

            if not user_id:

                json_response(
                    self,
                    400,
                    {
                        "error":
                            "Invalid analysis job."
                    },
                )

                return

            interaction = retrieve_response(
                interaction_id,
                key,
            )

            status = str(
                interaction.get(
                    "status",
                    "in_progress",
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
                        interaction,
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
            }:

                reason = (
                    extract_failure_reason(
                        interaction
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
