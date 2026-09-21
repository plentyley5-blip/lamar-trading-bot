import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

from analysis_common import (
    OPENAI_URL,
    api_key,
    json_response,
    parse_completed_response,
    read_job_token,
)


class TemporaryStatusError(Exception):
    pass


class PermanentStatusError(Exception):
    pass


def get_query_parameter(path, name):
    if "?" not in path:
        return None

    query = path.split("?", 1)[1]

    for part in query.split("&"):
        if "=" not in part:
            continue

        key, value = part.split("=", 1)

        if key == name:
            return value

    return None


def retrieve_openai_response(response_id, key):
    url = f"{OPENAI_URL}/{response_id}"

    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")

            if not raw:
                raise TemporaryStatusError()

            return json.loads(raw)

    except urllib.error.HTTPError as exc:
        if exc.code in (408, 409, 425, 429, 500, 502, 503, 504):
            raise TemporaryStatusError()

        if exc.code == 404:
            raise PermanentStatusError("Analysis job was not found.")

        raise PermanentStatusError(
            "The analysis service rejected the status request."
        )

    except urllib.error.URLError:
        raise TemporaryStatusError()

    except TimeoutError:
        raise TemporaryStatusError()

    except json.JSONDecodeError:
        raise TemporaryStatusError()


def get_response_status(response_data):
    status = response_data.get("status")

    if isinstance(status, str):
        return status.lower()

    return ""


class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        try:
            token = get_query_parameter(self.path, "job_id")

            if not token:
                json_response(
                    self,
                    {
                        "status": "failed",
                        "error": "Missing analysis job ID.",
                    },
                    400,
                )
                return

            try:
                response_id, instrument, trade_focus = read_job_token(token)
            except Exception:
                json_response(
                    self,
                    {
                        "status": "failed",
                        "error": "Invalid or expired analysis job.",
                    },
                    400,
                )
                return

            key = api_key()

            if not key:
                json_response(
                    self,
                    {
                        "status": "failed",
                        "error": "Analysis service is not configured.",
                    },
                    500,
                )
                return

            try:
                response_data = retrieve_openai_response(
                    response_id,
                    key,
                )

            except TemporaryStatusError:
                json_response(
                    self,
                    {
                        "status": "retry",
                        "error": "Analysis status is temporarily unavailable.",
                        "poll_after_seconds": 5,
                    },
                    503,
                )
                return

            except PermanentStatusError as exc:
                json_response(
                    self,
                    {
                        "status": "failed",
                        "error": str(exc),
                    },
                    200,
                )
                return

            status = get_response_status(response_data)

            # ---------------------------------------------------------
            # ANALYSIS STILL RUNNING
            # ---------------------------------------------------------

            if status in (
                "queued",
                "in_progress",
                "processing",
            ):
                json_response(
                    self,
                    {
                        "status": "in_progress",
                        "poll_after_seconds": 3,
                    },
                    200,
                )
                return

            # ---------------------------------------------------------
            # ANALYSIS COMPLETED
            # ---------------------------------------------------------

            if status == "completed":
                try:
                    result = parse_completed_response(
                        response_data,
                        instrument,
                        trade_focus,
                    )

                    result["status"] = "completed"

                    json_response(
                        self,
                        result,
                        200,
                    )
                    return

                except Exception:
                    json_response(
                        self,
                        {
                            "status": "failed",
                            "error": "The analysis completed, but the result could not be read.",
                        },
                        200,
                    )
                    return

            # ---------------------------------------------------------
            # ANALYSIS FAILED
            # ---------------------------------------------------------

            if status in (
                "failed",
                "cancelled",
                "canceled",
                "expired",
                "incomplete",
            ):
                error_message = "The analysis did not complete."

                if status == "cancelled" or status == "canceled":
                    error_message = "The analysis was cancelled."

                elif status == "expired":
                    error_message = "The analysis job expired."

                elif status == "incomplete":
                    error_message = "The analysis ended before a complete result was produced."

                elif status == "failed":
                    error_message = "The analysis service could not complete the analysis."

                json_response(
                    self,
                    {
                        "status": "failed",
                        "error": error_message,
                    },
                    200,
                )
                return

            # ---------------------------------------------------------
            # UNKNOWN STATUS
            # ---------------------------------------------------------

            json_response(
                self,
                {
                    "status": "in_progress",
                    "poll_after_seconds": 3,
                },
                200,
            )

        except Exception:
            json_response(
                self,
                {
                    "status": "failed",
                    "error": "Unable to retrieve the analysis status.",
                },
                500,
            )

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
