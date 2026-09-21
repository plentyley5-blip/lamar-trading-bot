import os
import json
import time
import hmac
import hashlib
import base64
import urllib.request
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs


OPENAI_URL = "https://api.openai.com/v1/responses"
JOB_TTL_SECONDS = 86400


def send_json(handler, status, data):
    body = json.dumps(data).encode("utf-8")

    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def get_secret():
    secret = os.environ.get("JOB_TOKEN_SECRET")

    if not secret:
        secret = os.environ.get("OPENAI_API_KEY")

    if not secret:
        raise Exception("JOB_TOKEN_SECRET and OPENAI_API_KEY are both missing")

    return secret.encode("utf-8")


def verify_token(token):

    if "." not in token:
        raise Exception("Token has no signature")

    encoded_payload, signature = token.rsplit(".", 1)

    expected = hmac.new(
        get_secret(),
        encoded_payload.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(signature, expected):
        raise Exception("Invalid job signature")

    padding = "=" * (-len(encoded_payload) % 4)

    decoded = base64.urlsafe_b64decode(
        encoded_payload + padding
    )

    payload = json.loads(
        decoded.decode("utf-8")
    )

    response_id = payload.get("r")
    created = payload.get("t")
    instrument = payload.get("i")
    trade_focus = payload.get("f")

    if not response_id:
        raise Exception("Token has no response ID")

    if not created:
        raise Exception("Token has no creation time")

    if time.time() - int(created) > JOB_TTL_SECONDS:
        raise Exception("Job token expired")

    return (
        response_id,
        instrument,
        trade_focus
    )


def get_response(response_id):

    api_key = os.environ.get("OPENAI_API_KEY")

    if not api_key:
        raise Exception("OPENAI_API_KEY is missing")

    url = OPENAI_URL + "/" + response_id

    request = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json"
        },
        method="GET"
    )

    with urllib.request.urlopen(
        request,
        timeout=45
    ) as response:

        data = response.read().decode("utf-8")

        return json.loads(data)


class handler(BaseHTTPRequestHandler):

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
            "Content-Type"
        )

        self.end_headers()

    def do_GET(self):

        try:

            parsed = urlparse(self.path)

            params = parse_qs(
                parsed.query
            )

            jobs = params.get(
                "job_id"
            )

            if not jobs:
                send_json(
                    self,
                    400,
                    {
                        "status": "failed",
                        "error": "Missing job_id"
                    }
                )
                return

            token = jobs[0]

            response_id, instrument, trade_focus = verify_token(
                token
            )

            data = get_response(
                response_id
            )

            openai_status = data.get(
                "status"
            )

            print(
                "OPENAI STATUS:",
                openai_status
            )

            if openai_status in [
                "queued",
                "in_progress",
                "processing"
            ]:

                send_json(
                    self,
                    200,
                    {
                        "status": "in_progress"
                    }
                )
                return

            if openai_status == "completed":

                output = data.get(
                    "output",
                    []
                )

                text_parts = []

                for item in output:

                    if not isinstance(
                        item,
                        dict
                    ):
                        continue

                    content = item.get(
                        "content",
                        []
                    )

                    for part in content:

                        if not isinstance(
                            part,
                            dict
                        ):
                            continue

                        if isinstance(
                            part.get("text"),
                            str
                        ):
                            text_parts.append(
                                part["text"]
                            )

                text = "".join(
                    text_parts
                ).strip()

                if not text:
                    raise Exception(
                        "OpenAI completed but returned no text"
                    )

                result = json.loads(
                    text
                )

                send_json(
                    self,
                    200,
                    {
                        "status": "completed",
                        "result": result
                    }
                )
                return

            if openai_status in [
                "failed",
                "cancelled",
                "canceled",
                "expired",
                "incomplete"
            ]:

                print(
                    "OPENAI TERMINAL RESPONSE:",
                    json.dumps(data)
                )

                send_json(
                    self,
                    200,
                    {
                        "status": "failed",
                        "error": "OpenAI analysis failed",
                        "openai_status": openai_status
                    }
                )
                return

            raise Exception(
                "Unknown OpenAI response status: "
                + str(openai_status)
            )

        except Exception as error:

            print(
                "ANALYSIS_STATUS_ERROR:",
                repr(error)
            )

            send_json(
                self,
                500,
                {
                    "status": "server_error",
                    "error": str(error)
                }
            )
