import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler


OPENAI_URL = "https://api.openai.com/v1/responses"
MODEL = "gpt-5.6-luna"

# Limit simultaneous OpenAI calls on one warm Vercel instance.
# This is local protection; Vercel may still create multiple instances.
OPENAI_CONCURRENCY = 2
OPENAI_SEMAPHORE = threading.BoundedSemaphore(OPENAI_CONCURRENCY)


SYSTEM = """
You are the vision analysis engine for LAMAR TRADING BOT.

Analyze the supplied 4H and 15M trading charts for the selected instrument.

Return ONLY JSON matching the supplied schema.

Be conservative:
- If chart quality, visible price labels, structure, or setup evidence is insufficient, return NO TRADE.
- Never invent exact entry, stop-loss, take-profit, or risk-reward values.
- Confidence is an analysis-confidence score, not a guaranteed probability of profit.
- For BUY or SELL, only provide exact prices when they can be reasonably read from the screenshots.
- For NO TRADE, set entry, stop_loss, take_profit_1,
  take_profit_2, and risk_reward to empty strings.
- Explain the decision using visible chart evidence.
- Use support/resistance, price action, liquidity behavior,
  Fibonacci/premium-discount context, and multi-timeframe confirmation internally.
- Do not present strategy names as UI labels.
"""


OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "signal": {
            "type": "string",
            "enum": ["BUY", "SELL", "NO TRADE"],
        },
        "confidence": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
        },
        "instrument": {
            "type": "string",
        },
        "higher_timeframe_context": {
            "type": "string",
        },
        "lower_timeframe_confirmation": {
            "type": "string",
        },
        "entry": {
            "type": "string",
        },
        "stop_loss": {
            "type": "string",
        },
        "take_profit_1": {
            "type": "string",
        },
        "take_profit_2": {
            "type": "string",
        },
        "risk_reward": {
            "type": "string",
        },
        "data_analysis": {
            "type": "string",
        },
        "explanation": {
            "type": "string",
        },
        "warnings": {
            "type": "string",
        },
    },
    "required": [
        "signal",
        "confidence",
        "instrument",
        "higher_timeframe_context",
        "lower_timeframe_confirmation",
        "entry",
        "stop_loss",
        "take_profit_1",
        "take_profit_2",
        "risk_reward",
        "data_analysis",
        "explanation",
        "warnings",
    ],
}


def send_json(
    handler,
    status_code,
    payload,
    extra_headers=None,
):
    raw = json.dumps(payload).encode("utf-8")

    handler.send_response(status_code)

    handler.send_header(
        "Content-Type",
        "application/json; charset=utf-8",
    )

    handler.send_header(
        "Access-Control-Allow-Origin",
        "*",
    )

    handler.send_header(
        "Access-Control-Allow-Headers",
        "Content-Type, Authorization",
    )

    handler.send_header(
        "Access-Control-Allow-Methods",
        "POST, OPTIONS",
    )

    if extra_headers:
        for name, value in extra_headers.items():
            handler.send_header(
                name,
                str(value),
            )

    handler.end_headers()
    handler.wfile.write(raw)


def parse_error_body(raw_body):
    try:
        data = json.loads(
            raw_body.decode(
                "utf-8",
                errors="replace",
            )
        )
    except Exception:
        return "", ""

    error = data.get("error")

    if not isinstance(error, dict):
        return "", ""

    code = str(
        error.get("code") or ""
    )

    message = str(
        error.get("message") or ""
    )

    return code, message


def safe_openai_error(http_error):
    body = b""

    try:
        body = http_error.read()
    except Exception:
        pass

    error_code, error_message = parse_error_body(body)

    if http_error.code == 401:
        return (
            "OPENAI_API_KEY was rejected by OpenAI.",
            error_code,
        )

    if http_error.code == 429:
        billing_codes = {
            "insufficient_quota",
            "credit_balance_exhausted",
            "organization_usage_limit_exceeded",
            "organization_spend_limit_exceeded",
            "project_spend_limit_exceeded",
        }

        if error_code in billing_codes:
            return (
                "OpenAI usage or billing limit reached. "
                "Check the API project's billing and limits.",
                error_code,
            )

        return (
            "OpenAI is temporarily rate limiting requests. "
            "Please try again shortly.",
            error_code,
        )

    if http_error.code in {
        500,
        502,
        503,
        504,
    }:
        return (
            "OpenAI is temporarily unavailable. "
            "Please try again shortly.",
            error_code,
        )

    if error_message:
        return (
            f"OpenAI request failed: {error_message}",
            error_code,
        )

    return (
        f"OpenAI request failed with HTTP {http_error.code}.",
        error_code,
    )


def retry_delay(
    attempt,
    retry_after_header=None,
):
    try:
        retry_after = float(
            retry_after_header
        )

        if retry_after >= 0:
            return min(
                retry_after,
                20.0,
            )

    except (
        TypeError,
        ValueError,
    ):
        pass

    base_delay = min(
        2 ** attempt,
        12,
    )

    jitter = random.uniform(
        0.1,
        0.8,
    )

    return base_delay + jitter


def call_openai(
    payload,
    api_key,
):
    headers = {
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    body = json.dumps(
        payload
    ).encode("utf-8")

    # Four total attempts:
    # initial request + three retries.
    max_attempts = 4

    for attempt in range(max_attempts):

        request = urllib.request.Request(
            OPENAI_URL,
            data=body,
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=120,
            ) as response:

                return json.loads(
                    response.read().decode(
                        "utf-8"
                    )
                )

        except urllib.error.HTTPError as exc:

            retry_after = exc.headers.get(
                "Retry-After"
            )

            error_message, error_code = (
                safe_openai_error(exc)
            )

            retryable = exc.code in {
                429,
                500,
                502,
                503,
                504,
            }

            billing_or_quota_error = (
                error_code in {
                    "insufficient_quota",
                    "credit_balance_exhausted",
                    "organization_usage_limit_exceeded",
                    "organization_spend_limit_exceeded",
                    "project_spend_limit_exceeded",
                }
            )

            if (
                not retryable
                or billing_or_quota_error
                or attempt == max_attempts - 1
            ):
                raise RuntimeError(
                    error_message
                )

            delay = retry_delay(
                attempt + 1,
                retry_after_header=retry_after,
            )

            time.sleep(delay)

        except (
            urllib.error.URLError,
            TimeoutError,
        ) as exc:

            if attempt == max_attempts - 1:
                raise RuntimeError(
                    "The connection to OpenAI timed out. "
                    "Please try again."
                ) from exc

            time.sleep(
                retry_delay(
                    attempt + 1
                )
            )


def extract_output_text(
    api_response,
):
    output_text = api_response.get(
        "output_text"
    )

    if (
        isinstance(output_text, str)
        and output_text.strip()
    ):
        return output_text

    for item in api_response.get(
        "output",
        [],
    ):

        if not isinstance(
            item,
            dict,
        ):
            continue

        for content in item.get(
            "content",
            [],
        ):

            if (
                isinstance(
                    content,
                    dict,
                )
                and content.get("type")
                == "output_text"
            ):

                text = content.get(
                    "text"
                )

                if (
                    isinstance(text, str)
                    and text.strip()
                ):
                    return text

    raise RuntimeError(
        "No analysis returned by the model."
    )


def analyze_chart(
    instrument,
    higher_image,
    lower_image,
    api_key,
):
    user_content = [
        {
            "type": "input_text",
            "text": (
                f"Instrument: {instrument}. "
                "The first image is the 4H chart. "
                "The second image is the 15M chart. "
                "Analyze both together."
            ),
        },
        {
            "type": "input_image",
            "image_url": higher_image,
            "detail": "high",
        },
        {
            "type": "input_image",
            "image_url": lower_image,
            "detail": "high",
        },
    ]

    payload = {
        "model": MODEL,

        "input": [
            {
                "role": "system",
                "content": SYSTEM,
            },
            {
                "role": "user",
                "content": user_content,
            },
        ],

        "text": {
            "format": {
                "type": "json_schema",
                "name": "lamar_trade_analysis",
                "strict": True,
                "schema": OUTPUT_SCHEMA,
            }
        },

        # Keep output controlled for a high-volume
        # screenshot-analysis application.
        "max_output_tokens": 2500,
    }

    return call_openai(
        payload=payload,
        api_key=api_key,
    )


class handler(
    BaseHTTPRequestHandler
):

    def do_OPTIONS(self):
        send_json(
            self,
            204,
            {},
        )

    def do_POST(self):

        if self.path != "/api/analyze":
            send_json(
                self,
                404,
                {
                    "error": "Not found"
                },
            )
            return

        api_key = os.getenv(
            "OPENAI_API_KEY"
        )

        if not api_key:
            send_json(
                self,
                500,
                {
                    "error": (
                        "OPENAI_API_KEY is not "
                        "configured on the server"
                    )
                },
            )
            return

        try:
            content_length = int(
                self.headers.get(
                    "Content-Length",
                    "0",
                )
            )

        except ValueError:
            send_json(
                self,
                400,
                {
                    "error": (
                        "Invalid request length."
                    )
                },
            )
            return

        if content_length <= 0:
            send_json(
                self,
                400,
                {
                    "error": (
                        "Request body is empty."
                    )
                },
            )
            return

        # Protect the server from accidental
        # extremely large chart uploads.
        if content_length > (
            25 * 1024 * 1024
        ):
            send_json(
                self,
                413,
                {
                    "error": (
                        "Chart upload is too large. "
                        "Use smaller screenshots."
                    )
                },
            )
            return

        try:
            raw_body = self.rfile.read(
                content_length
            )

            data = json.loads(
                raw_body
            )

            instrument = str(
                data.get(
                    "instrument",
                    "Unknown",
                )
            ).strip()

            higher_image = data.get(
                "higher_timeframe_image"
            )

            lower_image = data.get(
                "lower_timeframe_image"
            )

            if not higher_image or not lower_image:
                send_json(
                    self,
                    400,
                    {
                        "error": (
                            "Both 4H and 15M "
                            "chart images are required."
                        )
                    },
                )
                return

            # Prevent too many OpenAI calls from
            # running simultaneously on this instance.
            acquired = OPENAI_SEMAPHORE.acquire(
                timeout=20
            )

            if not acquired:
                send_json(
                    self,
                    429,
                    {
                        "error": (
                            "LAMAR is handling other "
                            "analyses right now. "
                            "Please try again in "
                            "a few seconds."
                        )
                    },
                    extra_headers={
                        "Retry-After": "10"
                    },
                )
                return

            try:
                result = analyze_chart(
                    instrument=instrument,
                    higher_image=higher_image,
                    lower_image=lower_image,
                    api_key=api_key,
                )

            finally:
                OPENAI_SEMAPHORE.release()

            # Always return the selected instrument
            # from the user's request.
            result["instrument"] = instrument

            send_json(
                self,
                200,
                result,
            )

        except json.JSONDecodeError:
            send_json(
                self,
                400,
                {
                    "error": (
                        "Invalid JSON request."
                    )
                },
            )

        except RuntimeError as exc:
            message = str(exc)

            lower_message = (
                message.lower()
            )

            if (
                "rate limiting" in lower_message
                or "temporarily rate limiting"
                in lower_message
            ):
                send_json(
                    self,
                    429,
                    {
                        "error": message
                    },
                    extra_headers={
                        "Retry-After": "10"
                    },
                )

            elif (
                "usage or billing limit"
                in lower_message
            ):
                send_json(
                    self,
                    429,
                    {
                        "error": message
                    },
                )

            elif "api_key" in lower_message:
                send_json(
                    self,
                    500,
                    {
                        "error": message
                    },
                )

            elif (
                "timed out" in lower_message
            ):
                send_json(
                    self,
                    504,
                    {
                        "error": message
                    },
                )

            else:
                send_json(
                    self,
                    502,
                    {
                        "error": message
                    },
                )

        except Exception:
            send_json(
                self,
                500,
                {
                    "error": (
                        "Unexpected server error. "
                        "Please try again."
                    )
                },
            )
