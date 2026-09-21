import json
from http.server import BaseHTTPRequestHandler

from analysis_common import (
    create_background_response,
    create_job_token,
    json_response,
    validate_focus,
    validate_image_data_url,
    validate_instrument,
)


MAX_REQUEST_BYTES = 25 * 1024 * 1024


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        json_response(
            self,
            204,
            {}
        )

    def do_POST(self):

        try:
            content_length = int(
                self.headers.get("Content-Length", "0")
            )

            if content_length <= 0:
                raise ValueError(
                    "Request body is empty."
                )

            if content_length > MAX_REQUEST_BYTES:
                raise ValueError(
                    "Request is too large. "
                    "Please use smaller chart images."
                )

            raw_body = self.rfile.read(content_length)

            try:
                body = json.loads(
                    raw_body.decode("utf-8")
                )
            except Exception:
                raise ValueError(
                    "Request body must contain valid JSON."
                )

            if not isinstance(body, dict):
                raise ValueError(
                    "Request body must be a JSON object."
                )

            instrument = validate_instrument(
                body.get("instrument")
            )

            trade_focus = validate_focus(
                body.get("trade_focus")
            )

            higher_image = validate_image_data_url(
                body.get("higher_timeframe_image"),
                "higher_timeframe_image"
            )

            lower_image = validate_image_data_url(
                body.get("lower_timeframe_image"),
                "lower_timeframe_image"
            )

            openai_response = create_background_response(
                instrument=instrument,
                trade_focus=trade_focus,
                higher_image=higher_image,
                lower_image=lower_image
            )

            response_id = str(
                openai_response.get("id", "")
            ).strip()

            if not response_id:
                raise RuntimeError(
                    "OpenAI returned no response ID."
                )

            job_token = create_job_token(
                response_id=response_id,
                instrument=instrument,
                trade_focus=trade_focus
            )

            json_response(
                self,
                202,
                {
                    "status": "queued",
                    "job_id": job_token
                }
            )

        except Exception as exc:

            json_response(
                self,
                500,
                {
                    "status": "failed",
                    "error": str(exc)
                }
            )
