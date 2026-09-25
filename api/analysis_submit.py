import json
from http.server import BaseHTTPRequestHandler

from analysis_common import (
    create_background_response,
    json_response,
    parse_completed_response,
    validate_focus,
    validate_image_data_url,
    validate_instrument,
)
from api.user_security import (
    create_secure_job_token,
    extract_bearer_token,
    is_owner_user,
    release_analysis_slot,
    reserve_analysis_slot,
    verify_access_token,
)


def _read_json(handler):
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        raise ValueError("Invalid request.")

    if length <= 0 or length > 25 * 1024 * 1024:
        raise ValueError("Invalid chart request.")

    raw = handler.rfile.read(length)

    try:
        value = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid request JSON.") from exc

    if not isinstance(value, dict):
        raise ValueError("Invalid request.")

    return value


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        json_response(
            self,
            204,
            {},
        )

    def do_POST(self):
        reserved = False
        user = None
        owner = False

        try:
            access_token = extract_bearer_token(
                self
            )

            user = verify_access_token(
                access_token
            )

            owner = is_owner_user(
                user
            )

            user_id = str(
                user["id"]
            )

            data = _read_json(
                self
            )

            instrument = validate_instrument(
                data.get(
                    "instrument"
                )
            )

            trade_focus = validate_focus(
                data.get(
                    "trade_focus",
                    "DAY TRADE"
                )
            )

            higher_image = validate_image_data_url(
                data.get(
                    "higher_timeframe_image"
                ),
                "4H chart",
            )

            lower_image = validate_image_data_url(
                data.get(
                    "lower_timeframe_image"
                ),
                "15M chart",
            )

            if not owner:

                slot = reserve_analysis_slot(
                    user_id
                )

                if slot == -1:

                    json_response(
                        self,
                        429,
                        {
                            "error":
                                "Daily analysis limit reached.",

                            "daily_limit":
                                4,

                            "remaining":
                                0,

                            "is_owner":
                                False,
                        },
                    )

                    return

                reserved = True

                remaining = max(
                    0,
                    4 - slot
                )

            else:

                remaining = None

            try:

                response = create_background_response(
                    instrument=instrument,
                    trade_focus=trade_focus,
                    higher_image=higher_image,
                    lower_image=lower_image,
                )

            except Exception:

                if reserved:

                    try:
                        release_analysis_slot(
                            user_id
                        )
                    except Exception:
                        pass

                raise

            response_id = str(
                response.get(
                    "id",
                    ""
                )
            ).strip()

            status = str(
                response.get(
                    "status",
                    "queued"
                )
            ).strip() or "queued"

            if not response_id:

                if reserved:

                    try:
                        release_analysis_slot(
                            user_id
                        )
                    except Exception:
                        pass

                raise RuntimeError(
                    "The analysis service returned no job id."
                )

            job_id = create_secure_job_token(
                response_id,
                instrument,
                trade_focus,
                user_id,
            )

            json_response(
                self,
                202,
                {
                    "status":
                        status,

                    "job_id":
                        job_id,

                    "poll_after_seconds":
                        2,

                    "is_owner":
                        owner,

                    "daily_limit":
                        None
                        if owner
                        else 4,

                    "remaining":
                        remaining,
                },
            )

        except ValueError as exc:

            json_response(
                self,
                400,
                {
                    "error":
                        str(exc)
                },
            )

        except RuntimeError as exc:

            json_response(
                self,
                503,
                {
                    "error":
                        str(exc)
                },
            )

        except Exception:

            if reserved and user:

                try:
                    release_analysis_slot(
                        str(user["id"])
                    )
                except Exception:
                    pass

            json_response(
                self,
                500,
                {
                    "error":
                        "The analysis could not be started."
                },
            )
