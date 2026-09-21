import json
import urllib.error
import urllib.request

from analysis_common import (
    OPENAI_URL,
    get_openai_key,
    json_response,
    parse_completed_response,
    read_job_token,
)


def retrieve_openai_response(response_id):

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
            "Authorization": "Bearer " + api_key,
            "Accept": "application/json"
        }
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=30
        ) as response:

            response_body = (
                response.read()
                .decode("utf-8")
            )

            if response.status < 200 or response.status >= 300:
                raise RuntimeError(
                    "OpenAI retrieve returned HTTP "
                    + str(response.status)
                    + ": "
                    + response_body
                )

            return json.loads(response_body)

    except urllib.error.HTTPError as exc:

        error_body = (
            exc.read()
            .decode("utf-8", errors="replace")
        )

        if exc.code == 404:
            raise RuntimeError(
                "OpenAI could not find response ID "
                + response_id
                + ". HTTP 404. "
                + error_body
            )

        if exc.code == 401:
            raise RuntimeError(
                "OpenAI authentication failed. "
                "Check the Production OPENAI_API_KEY."
            )

        raise RuntimeError(
            "OpenAI retrieve failed. HTTP "
            + str(exc.code)
            + ": "
            + error_body
        )

    except urllib.error.URLError as exc:

        raise RuntimeError(
            "Could not connect to OpenAI while checking "
            "the analysis: "
            + str(exc.reason)
        )


class handler:

    def __init__(self, request, context=None):
        self.request = request
        self.context = context

    def __call__(self):
        return self.handle()

    def handle(self):

        try:
            method = getattr(
                self.request,
                "method",
                "GET"
            )

            if method == "OPTIONS":
                return {
                    "statusCode": 204,
                    "headers": {
                        "Access-Control-Allow-Origin": "*",
                        "Access-Control-Allow-Methods": "GET, OPTIONS",
                        "Access-Control-Allow-Headers": "Content-Type"
                    },
                    "body": ""
                }

            query = getattr(
                self.request,
                "query",
                {}
            )

            job_id = ""

            if isinstance(query, dict):
                job_id = str(
                    query.get("job_id", "")
                ).strip()

            if not job_id:
                return {
                    "statusCode": 400,
                    "headers": {
                        "Content-Type": "application/json",
                        "Access-Control-Allow-Origin": "*"
                    },
                    "body": json.dumps({
                        "status": "failed",
                        "error": "job_id is required."
                    })
                }

            response_id, instrument, trade_focus = (
                read_job_token(job_id)
            )

            response_data = retrieve_openai_response(
                response_id
            )

            openai_status = str(
                response_data.get("status", "")
            ).lower().strip()

            if openai_status in [
                "queued",
                "in_progress",
                "processing"
            ]:

                return {
                    "statusCode": 200,
                    "headers": {
                        "Content-Type": "application/json",
                        "Cache-Control": "no-store",
                        "Access-Control-Allow-Origin": "*"
                    },
                    "body": json.dumps({
                        "status": "in_progress"
                    })
                }

            if openai_status == "completed":

                result = parse_completed_response(
                    response_data,
                    instrument,
                    trade_focus
                )

                return {
                    "statusCode": 200,
                    "headers": {
                        "Content-Type": "application/json",
                        "Cache-Control": "no-store",
                        "Access-Control-Allow-Origin": "*"
                    },
                    "body": json.dumps({
                        "status": "completed",
                        "result": result
                    })
                }

            if openai_status in [
                "failed",
                "cancelled",
                "canceled",
                "incomplete",
                "expired"
            ]:

                error_information = (
                    response_data.get("error")
                    or response_data.get("incomplete_details")
                    or {
                        "message":
                        "OpenAI analysis ended with status "
                        + openai_status
                    }
                )

                return {
                    "statusCode": 200,
                    "headers": {
                        "Content-Type": "application/json",
                        "Cache-Control": "no-store",
                        "Access-Control-Allow-Origin": "*"
                    },
                    "body": json.dumps({
                        "status": "failed",
                        "error": error_information
                    })
                }

            return {
                "statusCode": 200,
                "headers": {
                    "Content-Type": "application/json",
                    "Cache-Control": "no-store",
                    "Access-Control-Allow-Origin": "*"
                },
                "body": json.dumps({
                    "status": "in_progress"
                })
            }

        except Exception as exc:

            error_text = str(exc)

            return {
                "statusCode": 200,
                "headers": {
                    "Content-Type": "application/json",
                    "Cache-Control": "no-store",
                    "Access-Control-Allow-Origin": "*"
                },
                "body": json.dumps({
                    "status": "failed",
                    "error": error_text
                })
            }


# Vercel Python legacy handler compatibility.
try:
    from http.server import BaseHTTPRequestHandler

    class VercelHandler(BaseHTTPRequestHandler):

        def _run(self):
            request_path = self.path

            query = {}

            if "?" in request_path:
                query_string = request_path.split("?", 1)[1]

                for part in query_string.split("&"):
                    if "=" in part:
                        key, value = part.split("=", 1)

                        if key == "job_id":
                            from urllib.parse import unquote
                            query["job_id"] = unquote(value)

            class RequestWrapper:
                method = self.command
                query = query

            result = handler(RequestWrapper()).handle()

            status_code = result.get(
                "statusCode",
                200
            )

            headers = result.get(
                "headers",
                {}
            )

            body = result.get(
                "body",
                ""
            )

            self.send_response(status_code)

            for key, value in headers.items():
                self.send_header(
                    key,
                    str(value)
                )

            self.end_headers()

            if body:
                self.wfile.write(
                    body.encode("utf-8")
                )

        def do_GET(self):
            self._run()

        def do_OPTIONS(self):
            self._run()

except Exception:
    VercelHandler = None


# Vercel expects `handler` to be exported.
if VercelHandler is not None:
    handler = VercelHandler
