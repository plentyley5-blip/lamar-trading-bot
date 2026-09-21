import json
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

from analysis_common import (
    OPENAI_URL,
    MODEL,
    OUTPUT_SCHEMA,
    SYSTEM_PROMPT,
    api_key,
    classify_error_body,
    json_response,
    make_job_token,
)


MAX_REQUEST_BYTES = 25 * 1024 * 1024
MAX_RETRY_ATTEMPTS = 6


def call_openai_background(payload, key):
    body = json.dumps(payload).encode("utf-8")

    headers = {
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    for attempt in range(MAX_RETRY_ATTEMPTS):
        request = urllib.request.Request(
            OPENAI_URL,
            data=body,
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=45,
            ) as response:

                response_body = (
                    response.read()
                    .decode("utf-8")
                )

                return json.loads(
                    response_body
                )

        except urllib.error.HTTPError as exc:
            raw = b""

            try:
                raw = exc.read()
            except Exception:
                pass

            code, message = classify_error_body(
                raw
            )

            retryable = exc.code in {
                429,
                500,
                502,
                503,
                504,
            }

            permanent_limit = code in {
                "insufficient_quota",
                "credit_balance_exhausted",
                "organization_usage_limit_exceeded",
                "organization_spend_limit_exceeded",
                "project_spend_limit_exceeded",
            }

            if (
                retryable
                and not permanent_limit
                and attempt < MAX_RETRY_ATTEMPTS - 1
            ):
                retry_after = exc.headers.get(
                    "Retry-After"
                )

                try:
                    delay = float(
                        retry_after
                    )

                    delay = max(
                        1.0,
                        min(delay, 15.0),
                    )

                except (
                    TypeError,
                    ValueError,
                ):
                    delay = min(
                        2 ** attempt,
                        12,
                    )

                time.sleep(delay)
                continue

            if exc.code == 401:
                raise RuntimeError(
                    "The server API credential was rejected."
                ) from exc

            if (
                exc.code == 429
                and permanent_limit
            ):
                raise RuntimeError(
                    "The AI service usage or billing limit has been reached."
                ) from exc

            if exc.code == 429:
                raise RuntimeError(
                    "The AI service is temporarily rate limited."
                ) from exc

            if message:
                raise RuntimeError(
                    "The AI analysis service rejected the request."
                ) from exc

            raise RuntimeError(
                "The AI analysis service could not be reached."
            ) from exc

        except (
            urllib.error.URLError,
            TimeoutError,
        ) as exc:

            if attempt < MAX_RETRY_ATTEMPTS - 1:
                time.sleep(
                    min(
                        2 ** attempt,
                        8,
                    )
                )
                continue

            raise RuntimeError(
                "The AI analysis service could not be reached."
            ) from exc

    raise RuntimeError(
        "The AI analysis service could not be reached."
    )


def read_request_body(handler):
    try:
        content_length = int(
            handler.headers.get(
                "Content-Length",
                "0",
            )
        )
    except ValueError:
        raise ValueError(
            "Invalid request size."
        )

    if content_length <= 0:
        raise ValueError(
            "Request body is empty."
        )

    if content_length > MAX_REQUEST_BYTES:
        raise ValueError(
            "Chart upload is too large. "
            "Please use smaller screenshots."
        )

    raw = handler.rfile.read(
        content_length
    )

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "Invalid JSON request."
        ) from exc


def validate_image(
    image,
    timeframe,
):
    if not isinstance(
        image,
        str,
    ):
        raise ValueError(
            f"A valid {timeframe} chart image is required."
        )

    if not image.startswith(
        "data:image/"
    ):
        raise ValueError(
            f"A valid {timeframe} chart image is required."
        )

    if len(image) > MAX_REQUEST_BYTES:
        raise ValueError(
            f"The {timeframe} chart image is too large."
        )


def build_background_payload(
    instrument,
    trade_focus,
    higher_image,
    lower_image,
):
    user_text = (
        f"Selected instrument: {instrument}\n"
        f"Trade focus: {trade_focus}\n\n"
        "Image 1 is the 4H chart.\n"
        "Image 2 is the 15M chart.\n\n"
        "Analyze both screenshots together.\n"
        "Do not assume information that cannot be reasonably "
        "read from the screenshots."
    )

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
                        "image_url": higher_image,
                        "detail": "high",
                    },
                    {
                        "type": "input_image",
                        "image_url": lower_image,
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

    def do_OPTIONS(self):
        json_response(
            self,
            204,
            {},
        )

    def do_POST(self):

        key = api_key()

        if not key:
            json_response(
                self,
                500,
                {
                    "error": (
                        "Server configuration is incomplete."
                    )
                },
            )
            return

        try:
            data = read_request_body(
                self
            )

            instrument = str(
                data.get(
                    "instrument",
                    "",
                )
            ).strip()

            trade_focus = str(
                data.get(
                    "trade_focus",
                    "",
                )
            ).strip().upper()

            higher_image = data.get(
                "higher_timeframe_image"
            )

            lower_image = data.get(
                "lower_timeframe_image"
            )

            if not instrument:
                json_response(
                    self,
                    400,
                    {
                        "error": (
                            "Instrument is required."
                        )
                    },
                )
                return

            if trade_focus not in {
                "SCALP",
                "DAY TRADE",
                "SWING",
            }:
                json_response(
                    self,
                    400,
                    {
                        "error": (
                            "A valid trade focus is required."
                        )
                    },
                )
                return

            validate_image(
                higher_image,
                "4H",
            )

            validate_image(
                lower_image,
                "15M",
            )

            payload = build_background_payload(
                instrument=instrument,
                trade_focus=trade_focus,
                higher_image=higher_image,
                lower_image=lower_image,
            )

            response = call_openai_background(
                payload,
                key,
            )

            response_id = response.get(
                "id"
            )

            if not response_id:
                raise RuntimeError(
                    "The AI analysis could not be assigned a job."
                )

            status = str(
                response.get(
                    "status",
                    "queued",
                )
            )

            job_token = make_job_token(
                response_id,
                instrument,
                trade_focus,
            )

            json_response(
                self,
                202,
                {
                    "success": True,
                    "status": status,
                    "job_id": job_token,
                    "poll_after_seconds": 2,
                },
            )

        except ValueError as exc:
            json_response(
                self,
                400,
                {
                    "error": str(exc)
                },
            )

        except RuntimeError as exc:
            message = str(exc)

            if (
                "rate limited"
                in message.lower()
            ):
                json_response(
                    self,
                    503,
                    {
                        "error": (
                            "The analysis service is temporarily "
                            "handling a high request load. "
                            "Please try again shortly."
                        )
                    },
                )
                return

            if (
                "billing"
                in message.lower()
                or "usage"
                in message.lower()
            ):
                json_response(
                    self,
                    503,
                    {
                        "error": (
                            "The analysis service is unavailable "
                            "because its usage limit has been reached."
                        )
                    },
                )
                return

            json_response(
                self,
                503,
                {
                    "error": (
                        "The analysis could not be started."
                    )
                },
            )

        except Exception:
            json_response(
                self,
                500,
                {
                    "error": (
                        "Unexpected server error."
                    )
                },
            )
