```python
import os
import json
import time
import hmac
import hashlib
import base64
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs


OPENAI_URL = "https://api.openai.com/v1/responses"
JOB_TTL_SECONDS = 86400


def send_json(handler, status_code, data):
    body = json.dumps(data).encode("utf-8")

    handler.send_response(status_code)
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
        raise Exception("Server configuration error")

    return secret.encode("utf-8")


def verify_job_token(token):

    if "." not in token:
        raise Exception("Invalid analysis job")

    encoded_payload, supplied_signature = token.rsplit(".", 1)

    expected_signature = hmac.new(
        get_secret(),
        encoded_payload.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(
        supplied_signature,
        expected_signature
    ):
        raise Exception("Invalid analysis job")

    padding = "=" * (-len(encoded_payload) % 4)

    decoded = base64.urlsafe_b64decode(
        encoded_payload + padding
    )

    payload = json.loads(
        decoded.decode("utf-8")
    )

    response_id = payload.get("r")
    created_at = payload.get("t")
    instrument = payload.get("i", "")
    trade_focus = payload.get("f", "")

    if not response_id:
        raise Exception("Analysis response ID missing")

    if not created_at:
        raise Exception("Analysis job timestamp missing")

    if time.time() - int(created_at) > JOB_TTL_SECONDS:
        raise Exception("Analysis job expired")

    return response_id, instrument, trade_focus


def get_openai_response(response_id):

    api_key = os.environ.get("OPENAI_API_KEY")

    if not api_key:
        raise Exception("OpenAI API key is not configured")

    request = urllib.request.Request(
        OPENAI_URL + "/" + response_id,
        method="GET",
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json"
        }
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=45
        ) as response:

            raw = response.read().decode("utf-8")

            return json.loads(raw)

    except urllib.error.HTTPError as error:

        try:
            error_body = error.read().decode("utf-8")
        except Exception:
            error_body = ""

        raise Exception(
            "OpenAI HTTP "
            + str(error.code)
            + ": "
            + error_body[:1000]
        )

    except urllib.error.URLError as error:

        raise Exception(
            "OpenAI connection error: "
            + str(error.reason)
        )


def extract_output_text(data):

    output = data.get("output", [])

    if not isinstance(output, list):
        return ""

    parts = []

    for item in output:

        if not isinstance(item, dict):
            continue

        content = item.get("content", [])

        if not isinstance(content, list):
            continue

        for content_item in content:

            if not isinstance(
                content_item,
                dict
            ):
                continue

            text_value = content_item.get("text")

            if isinstance(
                text_value,
                str
            ):
                parts.append(text_value)

    return "".join(parts).strip()


def parse_completed_result(
    data,
    instrument,
    trade_focus
):

    text = extract_output_text(data)

    if not text:
        raise Exception(
            "OpenAI completed without returning analysis data"
        )

    try:

        result = json.loads(text)

    except Exception as error:

        raise Exception(
            "OpenAI returned invalid JSON: "
            + str(error)
        )

    if not isinstance(result, dict):
        raise Exception(
            "OpenAI returned an invalid analysis object"
        )

    result["instrument"] = result.get(
        "instrument",
        instrument
    )

    result["trade_focus"] = result.get(
        "trade_focus",
        trade_focus
    )

    signal = result.get("signal")

    if signal not in [
        "BUY",
        "SELL",
        "NO TRADE"
    ]:
        raise Exception(
            "Analysis returned an invalid signal"
        )

    try:

        confidence = int(
            result.get(
                "confidence",
                0
            )
        )

    except Exception:

        confidence = 0

    result["confidence"] = max(
        0,
        min(
            100,
            confidence
        )
    )

    return result


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

            parsed_url = urlparse(
                self.path
            )

            query = parse_qs(
                parsed_url.query
            )

            job_ids = query.get(
                "job_id"
            )

            if not job_ids:

                send_json(
                    self,
                    400,
                    {
                        "status": "failed",
                        "error": "Missing analysis job"
                    }
                )

                return

            token = job_ids[0]

            response_id, instrument, trade_focus = verify_job_token(
                token
            )

            data = get_openai_response(
                response_id
            )

            openai_status = data.get(
                "status"
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

                result = parse_completed_result(
                    data,
                    instrument,
                    trade_focus
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

                error_details = data.get(
                    "error"
                )

                if error_details is None:
                    error_details = data.get(
                        "incomplete_details"
                    )

                if error_details is None:
                    error_details = data.get(
                        "status_details"
                    )

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
                        "openai_status": openai_status,
                        "details": error_details
                    }
                )

                return

            send_json(
                self,
                200,
                {
                    "status": "failed",
                    "error": "Unknown OpenAI analysis status",
                    "openai_status": openai_status
                }
            )

        except Exception as error:

            print(
                "ANALYSIS STATUS ERROR:",
                repr(error)
            )

            send_json(
                self,
                200,
                {
                    "status": "failed",
                    "error": str(error)
                }
            )
```
