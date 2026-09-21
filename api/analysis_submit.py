import json
from http.server import BaseHTTPRequestHandler

from analysis_common import (
    create_background_response,
    json_response,
    validate_focus,
    validate_image_data_url,
    validate_instrument,
)

from user_security import (
    create_secure_job_token,
    extract_bearer_token,
    release_analysis_slot,
    reserve_analysis_slot,
    verify_access_token,
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

            access_token = extract_bearer_token(
                self.headers
            )

            user = verify_access_token(
                access_token
            )

            user_id = str(
                user.get("id", "")
            ).strip()

            if not user_id:
                raise ValueError(
                    "Your account could not be identified."
                )

            content_length = int(
                self.headers.get(
                    "Content-Length",
                    "0"
                )
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

            raw_body = self.rfile.read(
                content_length
            )

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
                body.get(
                    "higher_timeframe_image"
                ),
                "higher_timeframe_image"
            )

            lower_image = validate_image_data_url(
                body.get(
                    "lower_timeframe_image"
                ),
                "lower_timeframe_image"
            )

            new_count = reserve_analysis_slot(
                user_id
            )

            if new_count == -1:

                json_response(
                    self,
                    429,
                    {
                        "status": "limit_reached",
                        "error":
                            "You have used all 4 analyses for today.",
                        "analyses_today": 4,
                        "daily_limit": 4
                    }
                )

                return

            try:

                openai_response = (
                    create_background_response(
                        instrument=instrument,
                        trade_focus=trade_focus,
                        higher_image=higher_image,
                        lower_image=lower_image
                    )
                )

            except Exception:

                release_analysis_slot(
                    user_id
                )

                raise

            response_id = str(
                openai_response.get(
                    "id",
                    ""
                )
            ).strip()

            if not response_id:

                release_analysis_slot(
                    user_id
                )

                raise RuntimeError(
                    "OpenAI returned no response ID."
                )

            job_token = create_secure_job_token(
                response_id=response_id,
                instrument=instrument,
                trade_focus=trade_focus,
                user_id=user_id
            )

            json_response(
                self,
                202,
                {
                    "status": "queued",
                    "job_id": job_token,
                    "analyses_today": new_count,
                    "daily_limit": 4
                }
            )

        except Exception as exc:

            status_code = 401

            message = str(exc)

            if (
                "Request" in message
                or "Instrument" in message
                or "Trade focus" in message
                or "image" in message
            ):
                status_code = 400

            elif (
                "OpenAI" in message
                or "Unable" in message
            ):
                status_code = 500

            json_response(
                self,
                status_code,
                {
                    "status": "failed",
                    "error": message
                }
            )
