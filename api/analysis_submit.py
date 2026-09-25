import json
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

from analysis_common import (
    OPENAI_URL,
    MODEL,
    OUTPUT_SCHEMA,
    SYSTEM_PROMPT,
    get_openai_key,
    json_response,
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
        length = int(
            handler.headers.get(
                "Content-Length",
                "0"
            )
        )
    except ValueError:
        raise ValueError("Invalid request.")

    if length <= 0 or length > 25 * 1024 * 1024:
        raise ValueError("Invalid chart request.")

    raw = handler.rfile.read(length)

    try:
        data = json.loads(
            raw.decode("utf-8")
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            "Invalid request JSON."
        ) from exc

    if not isinstance(data, dict):
        raise ValueError("Invalid request.")

    return data


def _extract_response_id(data):
    """
    Accept the normal OpenAI Responses API shape and a few
    defensive alternatives so we never incorrectly report
    'no job id' when an ID is actually present.
    """

    if not isinstance(data, dict):
        return ""

    candidates = [
        data.get("id"),

        data.get("response_id"),

        (
            data.get("response", {}).get("id")
            if isinstance(data.get("response"), dict)
            else None
        ),

        (
            data.get("data", {}).get("id")
            if isinstance(data.get("data"), dict)
            else None
        ),

        (
            data.get("result", {}).get("id")
            if isinstance(data.get("result"), dict)
            else None
        ),
    ]

    for value in candidates:
        value = str(value or "").strip()
        if value:
            return value

    return ""


def _openai_background_request(
    instrument,
    trade_focus,
    higher_image,
    lower_image,
):
    key = get_openai_key()

    payload = {
        "model": MODEL,
        "background": True,
        "store": True,

        "instructions": SYSTEM_PROMPT,

        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            f"Instrument: {instrument}\n"
                            f"Trade focus: {trade_focus}\n\n"
                            "Image 1 is the 4H chart.\n"
                            "Image 2 is the 15M chart.\n"
                            "Analyze both charts together.\n"
                            "Use only visible chart evidence.\n"
                            "Do not invent exact prices."
                        ),
                    },
                    {
                        "type": "input_image",
                        "image_url": higher_image,
                        "detail": "low",
                    },
                    {
                        "type": "input_image",
                        "image_url": lower_image,
                        "detail": "low",
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

        "max_output_tokens": 1200,
    }

    body = json.dumps(
        payload,
        separators=(",", ":")
    ).encode("utf-8")

    request = urllib.request.Request(
        OPENAI_URL,
        data=body,
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=55,
        ) as response:

            raw = response.read().decode(
                "utf-8"
            )

            parsed = json.loads(
                raw
            )

            return parsed

    except urllib.error.HTTPError as exc:

        raw = exc.read().decode(
            "utf-8",
            errors="replace"
        )

        print(
            "OPENAI HTTP ERROR:",
            exc.code,
            raw,
        )

        try:
            error_data = json.loads(
                raw
            )
        except Exception:
            error_data = {
                "raw": raw
            }

        message = ""

        if isinstance(
            error_data,
            dict
        ):
            error_obj = error_data.get(
                "error"
            )

            if isinstance(
                error_obj,
                dict
            ):
                message = str(
                    error_obj.get(
                        "message",
                        ""
                    )
                )

            if not message:
                message = str(
                    error_data.get(
                        "message",
                        ""
                    )
                )

        if exc.code == 401:
            raise RuntimeError(
                "The OpenAI API key was rejected."
            ) from exc

        if exc.code == 429:
            raise RuntimeError(
                "The AI service is temporarily rate limited."
            ) from exc

        if message:
            raise RuntimeError(
                "OpenAI error: " + message
            ) from exc

        raise RuntimeError(
            "OpenAI HTTP error " + str(exc.code)
        ) from exc

    except urllib.error.URLError as exc:

        raise RuntimeError(
            "Unable to connect to OpenAI: "
            + str(exc.reason)
        ) from exc

    except TimeoutError as exc:

        raise RuntimeError(
            "OpenAI request timed out."
        ) from exc


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        json_response(
            self,
            204,
            {}
        )

    def do_POST(self):

        reserved = False
        user = None

        try:

            # -----------------------------
            # AUTHENTICATION
            # -----------------------------

            access_token = extract_bearer_token(
                self
            )

            user = verify_access_token(
                access_token
            )

            user_id = str(
                user["id"]
            )

            owner = is_owner_user(
                user
            )

            # -----------------------------
            # REQUEST
            # -----------------------------

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
                "4H chart"
            )

            lower_image = validate_image_data_url(
                data.get(
                    "lower_timeframe_image"
                ),
                "15M chart"
            )

            # -----------------------------
            # DAILY LIMIT
            # -----------------------------

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
                        }
                    )

                    return

                reserved = True

                remaining = max(
                    0,
                    4 - slot
                )

            else:

                remaining = None

            # -----------------------------
            # CREATE OPENAI BACKGROUND JOB
            # -----------------------------

            try:

                response = _openai_background_request(
                    instrument,
                    trade_focus,
                    higher_image,
                    lower_image
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

            print(
                "OPENAI CREATE RESPONSE:",
                json.dumps(
                    response,
                    ensure_ascii=False
                )
            )

            # -----------------------------
            # EXTRACT REAL RESPONSE ID
            # -----------------------------

            response_id = _extract_response_id(
                response
            )

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

                # DO NOT hide the actual server response.
                json_response(
                    self,
                    503,
                    {
                        "error":
                            "OpenAI created a response, but no response ID was returned.",

                        "openai_response":
                            response,
                    }
                )

                return

            # -----------------------------
            # CREATE SECURE JOB TOKEN
            # -----------------------------

            job_id = create_secure_job_token(
                response_id,
                instrument,
                trade_focus,
                user_id
            )

            # -----------------------------
            # SUCCESS
            # -----------------------------

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
                }
            )

        except ValueError as exc:

            json_response(
                self,
                400,
                {
                    "error":
                        str(exc)
                }
            )

        except RuntimeError as exc:

            if reserved and user:
                try:
                    release_analysis_slot(
                        str(user["id"])
                    )
                except Exception:
                    pass

            json_response(
                self,
                503,
                {
                    "error":
                        str(exc)
                }
            )

        except Exception as exc:

            if reserved and user:
                try:
                    release_analysis_slot(
                        str(user["id"])
                    )
                except Exception:
                    pass

            print(
                "ANALYSIS SUBMIT CRASH:",
                repr(exc)
            )

            json_response(
                self,
                500,
                {
                    "error":
                        "Analysis submit failed.",

                    "details":
                        str(exc),
                }
            )
