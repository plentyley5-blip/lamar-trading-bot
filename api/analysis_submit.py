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


def call_openai_background(payload, key):
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    max_attempts = 6
    for attempt in range(max_attempts):
        request = urllib.request.Request(
            OPENAI_URL,
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = b""
            try:
                raw = exc.read()
            except Exception:
                pass
            code, message = classify_error_body(raw)
            retryable = exc.code in {429, 500, 502, 503, 504}
            non_retryable_quota = code in {
                "insufficient_quota",
                "credit_balance_exhausted",
                "organization_usage_limit_exceeded",
                "organization_spend_limit_exceeded",
                "project_spend_limit_exceeded",
            }
            if retryable and not non_retryable_quota and attempt < max_attempts - 1:
                retry_after = exc.headers.get("Retry-After")
                try:
                    delay = max(0.5, min(float(retry_after), 12.0))
                except (TypeError, ValueError):
                    delay = min(2 ** attempt, 12) + 0.25
                time.sleep(delay)
                continue
            if exc.code == 401:
                raise RuntimeError("The server API credential was rejected.") from exc
            if exc.code == 429 and non_retryable_quota:
                raise RuntimeError("The AI service usage or billing limit has been reached.") from exc
            if exc.code == 429:
                raise RuntimeError("The AI service is temporarily rate limited.") from exc
            if message:
                raise RuntimeError("AI analysis could not be started.") from exc
            raise RuntimeError("AI analysis could not be started.") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt < max_attempts - 1:
                time.sleep(min(2 ** attempt, 8))
                continue
            raise RuntimeError("The AI analysis service could not be reached.") from exc
    raise RuntimeError("AI analysis could not be started.")


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        json_response(self, 204, {})

    def do_POST(self):
        key = api_key()
        if not key:
            json_response(self, 500, {"error": "Server configuration is incomplete."})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            json_response(self, 400, {"error": "Invalid request."})
            return

        if length <= 0 or length > 25 * 1024 * 1024:
            json_response(self, 400, {"error": "Invalid chart request."})
            return

        try:
            data = json.loads(self.rfile.read(length))
            instrument = str(data.get("instrument", "")).strip()
            higher_image = data.get("higher_timeframe_image")
            lower_image = data.get("lower_timeframe_image")

            if not instrument:
                json_response(self, 400, {"error": "Instrument is required."})
                return
            if not isinstance(higher_image, str) or not higher_image.startswith("data:image/"):
                json_response(self, 400, {"error": "A valid 4H chart is required."})
                return
            if not isinstance(lower_image, str) or not lower_image.startswith("data:image/"):
                json_response(self, 400, {"error": "A valid 15M chart is required."})
                return

            payload = {
                "model": MODEL,
                "background": True,
                "instructions": SYSTEM_PROMPT,
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    f"Instrument: {instrument}. "
                                    "Image 1 is the 4H chart. "
                                    "Image 2 is the 15M chart. "
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
                "max_output_tokens": 2000,
            }

            response = call_openai_background(payload, key)
            response_id = response.get("id")
            status = response.get("status", "queued")
            if not response_id:
                raise RuntimeError("AI analysis could not be started.")

            token = make_job_token(response_id, instrument)

            json_response(
                self,
                202,
                {
                    "status": status,
                    "job_id": token,
                    "poll_after_seconds": 2,
                },
            )

        except json.JSONDecodeError:
            json_response(self, 400, {"error": "Invalid request."})
        except RuntimeError as exc:
            message = str(exc)
            if "rate limited" in message.lower():
                json_response(self, 503, {"error": "The analysis service is handling a high request load."})
            elif "billing limit" in message.lower() or "usage" in message.lower():
                json_response(self, 503, {"error": "The analysis service is unavailable for new requests."})
            else:
                json_response(self, 503, {"error": "The analysis could not be started."})
        except Exception:
            json_response(self, 500, {"error": "The analysis could not be started."})
