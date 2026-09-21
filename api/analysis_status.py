import json
import urllib.error
import urllib.request

from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from analysis_common import (
    OPENAI_URL,
    get_openai_key,
    parse_completed_response,
)

from user_security import (
    extract_bearer_token,
    read_secure_job_token,
    verify_access_token,
)


def retrieve_response(response_id):

    api_key = get_openai_key()

    url = (
        OPENAI_URL
        + "/"
        + response_id
    )

    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization":
                "Bearer " + api_key,
            "Accept":
                "application/json",
        },
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=30
        ) as response:

            body = response.read().decode(
                "utf-8"
            )

            return (
                response.status,
                json.loads(body)
            )

    except urllib.error.HTTPError as exc:

        body = exc.read().decode(
            "utf-8",
            errors="replace"
        )

        return (
            exc.code,
            {
                "error": {
                    "http_status": exc.code,
                    "message": body
                }
            }
        )

    except urllib.error.URLError as exc:

        return (
            0,
            {
                "error": {
                    "message":
                        "Connection to OpenAI failed: "
                        + str(exc.reason)
                }
            }
        )


class handler(BaseHTTPRequestHandler):

    def send_json(
        self,
        status_code,
        data
    ):

        body = json.dumps(
            data,
            ensure_ascii=False
        ).encode("utf-8")

        self.send_response(
            status_code
        )

        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8"
        )

        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
        )

        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, OPTIONS"
        )

        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization"
        )

        self.send_header(
            "Cache-Control",
            "no-store, no-cache, must-revalidate"
        )

        self.send_header(
            "Content-Length",
            str(len(body))
        )

        self.end_headers()

        self.wfile.write(body)

    def do_OPTIONS(self):

        self.send_response(204)

        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
        )

        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, OPTIONS"
        )

        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization"
        )

        self.end_headers()

    def do_GET(self):

        try:

            access_token = extract_bearer_token(
                self.headers
            )

            user = verify_access_token(
                access_token
            )

            authenticated_user_id = str(
                user.get("id", "")
            ).strip()

            if not authenticated_user_id:

                self.send_json(
                    401,
                    {
                        "status": "failed",
                        "error":
                            "Your account could not be identified."
                    }
                )

                return

            parsed = urlparse(
                self.path
            )

            params = parse_qs(
                parsed.query
            )

            job_values = params.get(
                "job_id",
                []
            )

            if not job_values:

                self.send_json(
                    400,
                    {
                        "status": "failed",
                        "error":
                            "job_id is required"
                    }
                )

                return

            job_id = job_values[0].strip()

            if not job_id:

                self.send_json(
                    400,
                    {
                        "status": "failed",
                        "error":
                            "job_id is empty"
                    }
                )

                return

            (
                response_id,
                instrument,
                trade_focus,
                job_user_id
            ) = read_secure_job_token(
                job_id
            )

            if (
                job_user_id
                != authenticated_user_id
            ):

                self.send_json(
                    403,
                    {
                        "status": "failed",
                        "error":
                            "You are not authorized to access this analysis."
                    }
                )

                return

            http_status, response_data = (
                retrieve_response(
                    response_id
                )
            )

            if http_status != 200:

                error_object = (
                    response_data.get(
                        "error",
                        {}
                    )
                )

                message = (
                    error_object.get(
                        "message",
                        "Unknown OpenAI error"
                    )
                )

                self.send_json(
                    200,
                    {
                        "status": "failed",
                        "error":
                            "OpenAI response lookup failed. "
                            + "HTTP "
                            + str(http_status)
                            + ". "
                            + str(message)
                    }
                )

                return

            openai_status = str(
                response_data.get(
                    "status",
                    ""
                )
            ).lower().strip()

            if openai_status in (
                "queued",
                "in_progress",
                "processing"
            ):

                self.send_json(
                    200,
                    {
                        "status": "in_progress"
                    }
                )

                return

            if openai_status == "completed":

                result = parse_completed_response(
                    response_data,
                    instrument,
                    trade_focus
                )

                self.send_json(
                    200,
                    {
                        "status": "completed",
                        "result": result
                    }
                )

                return

            if openai_status in (
                "failed",
                "cancelled",
                "canceled",
                "incomplete",
                "expired"
            ):

                error_value = (
                    response_data.get(
                        "error"
                    )
                    or response_data.get(
                        "incomplete_details"
                    )
                    or (
                        "OpenAI analysis ended "
                        "with status: "
                        + openai_status
                    )
                )

                self.send_json(
                    200,
                    {
                        "status": "failed",
                        "error": error_value
                    }
                )

                return

            self.send_json(
                200,
                {
                    "status": "failed",
                    "error":
                        "Unknown analysis status: "
                        + openai_status
                }
            )

        except Exception as exc:

            message = str(exc)

            status_code = 401

            if "job" in message.lower():
                status_code = 400

            self.send_json(
                status_code,
                {
                    "status": "failed",
                    "error": message
                }
            )
