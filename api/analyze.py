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
# This helps prevent bursts from one server instance.
OPENAI_CONCURRENCY = 2
OPENAI_SEMAPHORE = threading.BoundedSemaphore(
    OPENAI_CONCURRENCY
)

# Basic per-instance cooldown by client IP.
# This is only local-instance protection, not a global user limiter.
CLIENT_COOLDOWN_SECONDS = 8
CLIENT_LAST_REQUEST = {}
CLIENT_LOCK = threading.Lock()


SYSTEM_PROMPT = """
You are the vision analysis engine for LAMAR TRADING BOT.

Analyze the supplied 4H and 15M trading charts for the selected instrument.

Return ONLY JSON matching the supplied schema.

Rules:

1. Use both screenshots together.
2. Be conservative.
3. If the chart evidence is insufficient, return NO TRADE.
4. Never invent an exact entry, stop loss, take profit, or risk/reward.
5. Confidence is an analysis-confidence score, not a guaranteed probability of profit.
6. For BUY or SELL, only provide exact price levels when those levels can be reasonably read from the screenshots.
7. For NO TRADE, return empty strings for:
   entry
   stop_loss
   take_profit_1
   take_profit_2
   risk_reward
8. Explain the decision using visible chart evidence.
9. Evaluate:
   - higher-timeframe structure
   - support/resistance
   - price action
   - liquidity behavior
   - Fibonacci context
   - premium/discount context
   - lower-timeframe confirmation
10. Do not present internal strategy names as UI categories.
11. Mention warnings when chart quality or price visibility is poor.
"""


OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "signal": {
            "type": "string",
            "enum": [
                "BUY",
                "SELL",
                "NO TRADE"
            ],
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
    body = json.dumps(
        payload
    ).encode("utf-8")

    handler.send_response(
        status_code
    )

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

    handler.send_header(
        "Cache-Control",
        "no-store",
    )

    if extra_headers:
        for key, value in extra_headers.items():
            handler.send_header(
                key,
                str(value),
            )

    handler.end_headers()

    try:
        handler.wfile.write(
            body
        )
    except Exception:
        pass


def get_client_ip(handler):
    forwarded = (
        handler.headers.get(
            "X-Forwarded-For",
            ""
        )
        .split(",")[0]
        .strip()
    )

    if forwarded:
        return forwarded

    return (
        getattr(
            handler,
            "client_address",
            ("unknown", 0),
        )[0]
        or "unknown"
    )


def check_client_cooldown(client_ip):
    now = time.time()

    with CLIENT_LOCK:
        previous = CLIENT_LAST_REQUEST.get(
            client_ip
        )

        if previous is not None:
            elapsed = now - previous

            if elapsed < CLIENT_COOLDOWN_SECONDS:
                return (
                    False,
                    CLIENT_COOLDOWN_SECONDS
                    - elapsed,
                )

        CLIENT_LAST_REQUEST[client_ip] = now

    return True, 0.0


def calculate_backoff(
    attempt,
    retry_after=None,
):
    if retry_after:
        try:
            seconds = float(
                retry_after
            )

            if seconds >= 0:
                return min(
                    seconds,
                    20.0,
                )
        except (
            TypeError,
            ValueError,
        ):
            pass

    base = min(
        2 ** attempt,
        12,
    )

    jitter = random.uniform(
        0.2,
        1.0,
    )

    return base + jitter


def parse_openai_error(
    http_error
):
    raw = b""

    try:
        raw = http_error.read()
    except Exception:
        pass

    try:
        data = json.loads(
            raw.decode(
                "utf-8",
                errors="replace",
            )
        )
    except Exception:
        return "", ""

    error = data.get(
        "error"
    )

    if not isinstance(
        error,
        dict,
    ):
        return "", ""

    code = str(
        error.get(
            "code",
            ""
        )
    )

    message = str(
        error.get(
            "message",
            ""
        )
    )

    return code, message


def classify_openai_error(
    http_error
):
    code, message = parse_openai_error(
        http_error
    )

    if http_error.code == 401:
        return (
            401,
            "The OpenAI API key was rejected. Check OPENAI_API_KEY in Vercel Production.",
            code,
            False,
        )

    if http_error.code == 429:
        non_retryable_limits = {
            "insufficient_quota",
            "credit_balance_exhausted",
            "organization_usage_limit_exceeded",
            "organization_spend_limit_exceeded",
            "project_spend_limit_exceeded",
        }

        if code in non_retryable_limits:
            return (
                429,
                "OpenAI usage or billing limit reached. Check the API project's billing, credits, and limits.",
                code,
                False,
            )

        return (
            429,
            "OpenAI is temporarily rate limiting requests. Please try again shortly.",
            code,
            True,
        )

    if http_error.code in {
        500,
        502,
        503,
        504,
    }:
        return (
            http_error.code,
            "OpenAI is temporarily unavailable. Please try again shortly.",
            code,
            True,
        )

    if message:
        return (
            502,
            f"OpenAI request failed: {message}",
            code,
            False,
        )

    return (
        502,
        f"OpenAI request failed with HTTP {http_error.code}.",
        code,
        False,
    )


def call_openai(
    payload,
    api_key,
):
    request_body = json.dumps(
        payload
    ).encode("utf-8")

    headers = {
        "Authorization": (
            "Bearer " + api_key
        ),
        "Content-Type": (
            "application/json"
        ),
        "Accept": (
            "application/json"
        ),
    }

    max_attempts = 4

    for attempt in range(
        max_attempts
    ):
        request = urllib.request.Request(
            OPENAI_URL,
            data=request_body,
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=120,
            ) as response:

                response_body = (
                    response.read()
                    .decode(
                        "utf-8"
                    )
                )

                return json.loads(
                    response_body
                )

        except urllib.error.HTTPError as error:
            status_code, message, _, retryable = (
                classify_openai_error(
                    error
                )
            )

            if (
                retryable
                and attempt <
                max_attempts - 1
            ):
                delay = calculate_backoff(
                    attempt + 1,
                    error.headers.get(
                        "Retry-After"
                    ),
                )

                time.sleep(
                    delay
                )

                continue

            raise OpenAIRequestError(
                status_code=status_code,
                message=message,
            ) from error

        except (
            urllib.error.URLError,
            TimeoutError,
        ) as error:

            if attempt < (
                max_attempts - 1
            ):
                time.sleep(
                    calculate_backoff(
                        attempt + 1
                    )
                )
                continue

            raise OpenAIRequestError(
                status_code=504,
                message=(
                    "The connection to OpenAI timed out. "
                    "Please try again."
                ),
            ) from error


class OpenAIRequestError(
    Exception
):
    def __init__(
        self,
        status_code,
        message,
    ):
        super().__init__(
            message
        )

        self.status_code = (
            status_code
        )
        self.message = message


def extract_output_text(
    response_data
):
    direct = response_data.get(
        "output_text"
    )

    if (
        isinstance(
            direct,
            str,
        )
        and direct.strip()
    ):
        return direct

    output_items = (
        response_data.get(
            "output",
            []
        )
    )

    for item in output_items:
        if not isinstance(
            item,
            dict,
        ):
            continue

        contents = item.get(
            "content",
            []
        )

        for content in contents:
            if not isinstance(
                content,
                dict,
            ):
                continue

            if (
                content.get("type")
                == "output_text"
            ):
                text = content.get(
                    "text"
                )

                if (
                    isinstance(
                        text,
                        str,
                    )
                    and text.strip()
                ):
                    return text

    raise RuntimeError(
        "The model returned no analysis text."
    )


def clean_json_text(
    text
):
    text = text.strip()

    if text.startswith(
        "```"
    ):
        lines = text.splitlines()

        if lines:
            lines = lines[1:]

        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        text = "\n".join(
            lines
        ).strip()

    return text


def parse_model_result(
    response_data,
    instrument,
):
    output_text = extract_output_text(
        response_data
    )

    output_text = clean_json_text(
        output_text
    )

    try:
        result = json.loads(
            output_text
        )
    except json.JSONDecodeError as error:
        raise RuntimeError(
            "The model returned an invalid analysis format."
        ) from error

    if not isinstance(
        result,
        dict,
    ):
        raise RuntimeError(
            "The model returned an invalid analysis object."
        )

    result["instrument"] = instrument

    signal = str(
        result.get(
            "signal",
            "NO TRADE"
        )
    ).upper().strip()

    if signal not in {
        "BUY",
        "SELL",
        "NO TRADE",
    }:
        signal = "NO TRADE"

    result["signal"] = signal

    try:
        confidence = int(
            result.get(
                "confidence",
                0
            )
        )
    except (
        TypeError,
        ValueError,
    ):
        confidence = 0

    result["confidence"] = max(
        0,
        min(
            100,
            confidence,
        ),
    )

    # Never allow a NO TRADE response
    # to accidentally expose stale price values.
    if signal == "NO TRADE":
        result["entry"] = ""
        result["stop_loss"] = ""
        result["take_profit_1"] = ""
        result["take_profit_2"] = ""
        result["risk_reward"] = ""

    return result


def run_analysis(
    instrument,
    higher_image,
    lower_image,
    api_key,
):
    input_items = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT,
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": (
                        f"Instrument: {instrument}\n\n"
                        "Image 1 is the 4H chart.\n"
                        "Image 2 is the 15M chart.\n\n"
                        "Analyze both screenshots together."
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
            ],
        },
    ]

    payload = {
        "model": MODEL,
        "input": input_items,
        "text": {
            "format": {
                "type": "json_schema",
                "name": (
                    "lamar_trade_analysis"
                ),
                "strict": True,
                "schema": OUTPUT_SCHEMA,
            }
        },
        "max_output_tokens": 2000,
    }

    response = call_openai(
        payload=payload,
        api_key=api_key,
    )

    return parse_model_result(
        response,
        instrument,
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

        if self.path != (
            "/api/analyze"
        ):
            send_json(
                self,
                404,
                {
                    "error": (
                        "Not found."
                    )
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
                        "OPENAI_API_KEY is not configured on the server."
                    )
                },
            )
            return

        client_ip = get_client_ip(
            self
        )

        allowed, retry_seconds = (
            check_client_cooldown(
                client_ip
            )
        )

        if not allowed:
            retry_after = max(
                1,
                int(
                    retry_seconds
                    + 0.999
                ),
            )

            send_json(
                self,
                429,
                {
                    "error": (
                        "Please wait a few seconds "
                        "before starting another analysis."
                    )
                },
                extra_headers={
                    "Retry-After": str(
                        retry_after
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
                        "Invalid request size."
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

        # Protect the server from huge uploads.
        if content_length > (
            25 * 1024 * 1024
        ):
            send_json(
                self,
                413,
                {
                    "error": (
                        "Chart upload is too large. "
                        "Please use smaller screenshots."
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
                    "",
                )
            ).strip()

            higher_image = data.get(
                "higher_timeframe_image"
            )

            lower_image = data.get(
                "lower_timeframe_image"
            )

            if not instrument:
                send_json(
                    self,
                    400,
                    {
                        "error": (
                            "Instrument is required."
                        )
                    },
                )
                return

            if (
                not isinstance(
                    higher_image,
                    str,
                )
                or not higher_image.startswith(
                    "data:image/"
                )
            ):
                send_json(
                    self,
                    400,
                    {
                        "error": (
                            "A valid 4H chart image is required."
                        )
                    },
                )
                return

            if (
                not isinstance(
                    lower_image,
                    str,
                )
                or not lower_image.startswith(
                    "data:image/"
                )
            ):
                send_json(
                    self,
                    400,
                    {
                        "error": (
                            "A valid 15M chart image is required."
                        )
                    },
                )
                return

            acquired = (
                OPENAI_SEMAPHORE.acquire(
                    timeout=20
                )
            )

            if not acquired:
                send_json(
                    self,
                    429,
                    {
                        "error": (
                            "LAMAR is handling other "
                            "analyses right now. "
                            "Please try again in a few seconds."
                        )
                    },
                    extra_headers={
                        "Retry-After": "10"
                    },
                )
                return

            try:
                result = run_analysis(
                    instrument=instrument,
                    higher_image=higher_image,
                    lower_image=lower_image,
                    api_key=api_key,
                )
            finally:
                OPENAI_SEMAPHORE.release()

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

        except OpenAIRequestError as error:
            send_json(
                self,
                error.status_code,
                {
                    "error": (
                        error.message
                    )
                },
                extra_headers=(
                    {
                        "Retry-After": "10"
                    }
                    if error.status_code == 429
                    else None
                ),
            )

        except RuntimeError as error:
            send_json(
                self,
                502,
                {
                    "error": str(
                        error
                    )
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
