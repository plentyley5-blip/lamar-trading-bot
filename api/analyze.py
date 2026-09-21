import json
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

from analysis_common import (
    OPENAI_URL,
    SYSTEM_PROMPT,
    MODEL,
    OUTPUT_SCHEMA,
    api_key,
    json_response,
    make_job_token,
)


MAX_REQUEST_BYTES = 25 * 1024 * 1024
MAX_RETRY_ATTEMPTS = 6


def read_request_body(handler):
    content_length = handler.headers.get("Content-Length")

    if not content_length:
        raise ValueError("Request body is missing.")

    try:
        length = int(content_length)
    except ValueError:
        raise ValueError("Invalid request size.")

    if length <= 0:
        raise ValueError("Request body is empty.")

    if length > MAX_REQUEST_BYTES:
        raise ValueError("Request is too large.")

    body = handler.rfile.read(length)

    if not body:
        raise ValueError("Request body is empty.")

    return body


def validate_image(value, field_name):
    if not isinstance(value, str):
        raise ValueError(f"{field_name} is required.")

    if not value.startswith("data:image/"):
        raise ValueError(
            f"{field_name} must be a valid image data URL."
        )

    if "," not in value:
        raise ValueError(
            f"{field_name} is not a valid image."
        )

    return value


def call_openai_background(payload, key):
    url = OPENAI_URL

    body = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )

    last_error = None

    for attempt in range(MAX_RETRY_ATTEMPTS):
        try:
            with urllib.request.urlopen(
                request,
                timeout=45,
            ) as response:

                raw = response.read().decode("utf-8")

                if not raw:
                    raise RuntimeError(
                        "The analysis service returned an empty response."
                    )

                return json.loads(raw)

        except urllib.error.HTTPError as exc:
            last_error = exc

            if exc.code in (429, 500, 502, 503, 504):
                if attempt < MAX_RETRY_ATTEMPTS - 1:
                    continue

            try:
                error_body = exc.read().decode("utf-8")
                error_json = json.loads(error_body)

                message = (
                    error_json
                    .get("error", {})
                    .get("message")
                )

                if message:
                    raise RuntimeError(
                        "The analysis service rejected the request."
                    )

            except json.JSONDecodeError:
                pass

            raise RuntimeError(
                "The analysis service rejected the request."
            )

        except urllib.error.URLError as exc:
            last_error = exc

            if attempt < MAX_RETRY_ATTEMPTS - 1:
                continue

            raise RuntimeError(
                "The analysis service could not be reached."
            )

        except TimeoutError as exc:
            last_error = exc

            if attempt < MAX_RETRY_ATTEMPTS - 1:
                continue

            raise RuntimeError(
                "The analysis service timed out."
            )

        except json.JSONDecodeError:
            raise RuntimeError(
                "The analysis service returned an invalid response."
            )

    raise RuntimeError(
        "The analysis service could not start the analysis."
    )


def build_payload(
    instrument,
    trade_focus,
    higher_timeframe_image,
    lower_timeframe_image,
):
    user_text = f"""
Analyze this trading setup for LAMAR.

Instrument:
{instrument}

Trade focus:
{trade_focus}

The first image is the 4H chart.

The second image is the 15M chart.

Analyze both timeframes together.

Do not force a trade.

Return BUY, SELL, or NO TRADE according to the evidence visible in the charts.

Use the complete analysis instructions and JSON schema provided by the system.
"""

    return {
        "model": MODEL,
        "background": True,
        "instructions": SYSTEM_PROMPT,
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": user_text,
                    },
                    {
                        "type": "input_image",
                        "image_url": higher_timeframe_image,
                        "detail": "high",
                    },
                    {
                        "type": "input_image",
                        "image_url": lower_timeframe_image,
                        "detail": "high",
                    },
                ],
            }
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "lamar_trade_analysis",
                "strict": True,
                "schema": OUTPUT_SCHEMA,
            }
        },
        "max_output_tokens": 3000,
    }


class handler(BaseHTTPRequestHandler):

    def do_POST(self):
        try:
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
                body = read_request_body(self)

                data = json.loads(
                    body.decode("utf-8")
                )

            except json.JSONDecodeError:
                json_response(
                    self,
                    {
                        "status": "failed",
                        "error": "Invalid JSON request.",
                    },
                    400,
                )
                return

            except ValueError as exc:
                json_response(
                    self,
                    {
                        "status": "failed",
                        "error": str(exc),
                    },
                    400,
                )
                return

            instrument = data.get("instrument")

            if not isinstance(instrument, str):
                json_response(
                    self,
                    {
                        "status": "failed",
                        "error": "Instrument is required.",
                    },
                    400,
                )
                return

            instrument = instrument.strip()

            if not instrument:
                json_response(
                    self,
                    {
                        "status": "failed",
                        "error": "Instrument is required.",
                    },
                    400,
                )
                return

            trade_focus = data.get(
                "trade_focus",
                "DAY TRADE",
            )

            if not isinstance(trade_focus, str):
                json_response(
                    self,
                    {
                        "status": "failed",
                        "error": "Trade focus is invalid.",
                    },
                    400,
                )
                return

            trade_focus = trade_focus.strip().upper()

            allowed_focus = {
                "SCALP",
                "DAY TRADE",
                "SWING",
            }

            if trade_focus not in allowed_focus:
                json_response(
                    self,
                    {
                        "status": "failed",
                        "error": "Trade focus must be SCALP, DAY TRADE, or SWING.",
                    },
                    400,
                )
                return

            try:
                higher_image = validate_image(
                    data.get("higher_timeframe_image"),
                    "4H chart",
                )

                lower_image = validate_image(
                    data.get("lower_timeframe_image"),
                    "15M chart",
                )

            except ValueError as exc:
                json_response(
                    self,
                    {
                        "status": "failed",
                        "error": str(exc),
                    },
                    400,
                )
                return

            payload = build_payload(
                instrument,
                trade_focus,
                higher_image,
                lower_image,
            )

            try:
                response_data = call_openai_background(
                    payload,
                    key,
                )

            except RuntimeError:
                json_response(
                    self,
                    {
                        "status": "failed",
                        "error": "The analysis could not be started. Please try again.",
                    },
                    503,
                )
                return

            response_id = response_data.get("id")

            if not response_id:
                json_response(
                    self,
                    {
                        "status": "failed",
                        "error": "The analysis service did not create a job.",
                    },
                    502,
                )
                return

            job_token = make_job_token(
                response_id,
                instrument,
                trade_focus,
            )

            json_response(
                self,
                {
                    "status": "queued",
                    "job_id": job_token,
                    "poll_after_seconds": 3,
                },
                202,
            )

        except Exception:
            json_response(
                self,
                {
                    "status": "failed",
                    "error": "Unable to start the analysis.",
                },
                500,
            )

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header(
            "Access-Control-Allow-Origin",
            "*",
        )
        self.send_header(
            "Access-Control-Allow-Methods",
            "POST, OPTIONS",
        )
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type",
        )
        self.end_headers()
