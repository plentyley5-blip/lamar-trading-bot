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
JOB_TTL_SECONDS = 24 * 60 * 60


def json_response(handler, status_code, data):
    body = json.dumps(data).encode("utf-8")

    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
    handler.send_header(
        "Access-Control-Allow-Headers",
        "Content-Type"
    )
    handler.send_header(
        "Access-Control-Max-Age",
        "86400"
    )
    handler.send_header(
        "Content-Length",
        str(len(body))
    )
    handler.end_headers()
    handler.wfile.write(body)


def get_secret():
    secret = os.environ.get("JOB_TOKEN_SECRET")

    if not secret:
        secret = os.environ.get("OPENAI_API_KEY")

    if not secret:
        raise RuntimeError("Server configuration error")

    return secret.encode("utf-8")


def create_signature(payload):
    return hmac.new(
        get_secret(),
        payload.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()


def read_job_token(token):
    try:
        if "." not in token:
            return None

        encoded_payload, supplied_signature = token.rsplit(".", 1)

        expected_signature = create_signature(encoded_payload)

        if not hmac.compare_digest(
            supplied_signature,
            expected_signature
        ):
            return None

        padding = "=" * (-len(encoded_payload) % 4)

        raw_payload = base64.urlsafe_b64decode(
            encoded_payload + padding
        )

        payload = json.loads(
            raw_payload.decode("utf-8")
        )

        response_id = payload.get("r")
        created_at = payload.get("t")
        instrument = payload.get("i")
        trade_focus = payload.get("f")

        if not response_id:
            return None

        if not created_at:
            return None

        if time.time() - int(created_at) > JOB_TTL_SECONDS:
            return None

        return {
            "response_id": response_id,
            "instrument": instrument or "",
            "trade_focus": trade_focus or ""
        }

    except Exception:
        return None


def get_openai_response(response_id):
    api_key = os.environ.get("OPENAI_API_KEY")

    if not api_key:
        raise RuntimeError("Server configuration error")

    url = OPENAI_URL + "/" + response_id

    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json"
        }
    )

    with urllib.request.urlopen(
        request,
        timeout=45
    ) as response:

        raw = response.read().decode("utf-8")

        return json.loads(raw)


def extract_text(response_data):
    output = response_data.get("output", [])

    if not isinstance(output, list):
        return ""

    pieces = []

    for item in output:
        if not isinstance(item, dict):
            continue

        content = item.get("content", [])

        if not isinstance(content, list):
            continue

        for part in content:
            if not isinstance(part, dict):
                continue

            text = part.get("text")

            if isinstance(text, str):
                pieces.append(text)

    return "".join(pieces).strip()


def parse_analysis(response_data, instrument, trade_focus):

    text = extract_text(response_data)

    if not text:
        return None

    try:
        result = json.loads(text)
    except Exception:
        return None

    if not isinstance(result, dict):
        return None

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
        return None

    confidence = result.get("confidence")

    try:
        confidence = int(confidence)
    except Exception:
        return None

    confidence = max(
        0,
        min(100, confidence)
    )

    result["confidence"] = confidence

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

            parsed = urlparse(self.path)

            params = parse_qs(
                parsed.query
            )

            job_values = params.get(
                "job_id",
                []
            )

            if not job_values:
                json_response(
                    self,
                    400,
                    {
                        "status": "failed",
                        "error": "Missing job_id"
                    }
                )
                return

            token = job_values[0]

            job = read_job_token(token)

            if not job:
                json_response(
                    self,
                    400,
                    {
                        "status": "failed",
                        "error": "Invalid or expired analysis job"
                    }
                )
                return

            response_id = job["response_id"]
            instrument = job["instrument"]
            trade_focus = job["trade_focus"]

            try:
                response_data = get_openai_response(
                    response_id
                )

            except urllib.error.HTTPError as error:

                if error.code in [408, 409, 429, 500, 502, 503, 504]:

                    json_response(
                        self,
                        200,
                        {
                            "status": "retry"
                        }
                    )
                    return

                json_response(
                    self,
                    200,
                    {
                        "status": "failed",
                        "error": "Analysis service error"
                    }
                )
                return

            except (
                urllib.error.URLError,
                TimeoutError
            ):

                json_response(
                    self,
                    200,
                    {
                        "status": "retry"
                    }
                )
                return

            openai_status = response_data.get(
                "status"
            )

            if openai_status in [
                "queued",
                "in_progress",
                "processing"
            ]:

                json_response(
                    self,
                    200,
                    {
                        "status": "in_progress"
                    }
                )
                return

            if openai_status == "completed":

                result = parse_analysis(
                    response_data,
                    instrument,
                    trade_focus
                )

                if result is None:

                    json_response(
                        self,
                        200,
                        {
                            "status": "failed",
                            "error": "Analysis returned invalid data"
                        }
                    )
                    return

                json_response(
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

                json_response(
                    self,
                    200,
                    {
                        "status": "failed",
                        "error": "Analysis could not be completed"
                    }
                )
                return

            json_response(
                self,
                200,
                {
                    "status": "in_progress"
                }
            )

        except Exception:

            json_response(
                self,
                500,
                {
                    "status": "failed",
                    "error": "Analysis status server error"
                }
            )
