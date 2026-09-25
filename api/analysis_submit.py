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
    get_supabase_service_key,
    get_supabase_url,
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
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid request JSON.") from exc

    if not isinstance(data, dict):
        raise ValueError("Invalid request.")

    return data


def _supabase_headers():
    key = get_supabase_service_key()

    return {
        "apikey": key,
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _save_queued_job(
    user_id,
    instrument,
    trade_focus,
    response_id,
    higher_image,
    lower_image,
    status,
):
    payload = {
        "user_id": str(user_id),
        "instrument": str(instrument),
        "trade_focus": str(trade_focus),
        "status": str(status or "queued"),
        "openai_response_id": str(response_id),
        "higher_timeframe_image": higher_image,
        "lower_timeframe_image": lower_image,
    }

    url = get_supabase_url() + "/rest/v1/analysis_jobs"

    request = urllib.request.Request(
        url,
        data=json.dumps(
            payload,
            separators=(",", ":"),
        ).encode("utf-8"),
        headers={
            **_supabase_headers(),
            "Prefer": "return=minimal",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=20,
        ) as response:
            response.read()

        return True

    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(
            "utf-8",
            errors="replace",
        )
        print(
            "Supabase history insert failed:",
            raw,
        )
        return False

    except Exception as exc:
        print(
            "Supabase history insert failed:",
            str(exc),
        )
        return False


def _openai_request(
    instrument,
    trade_focus,
    higher_image,
    lower_image,
    output_tokens=1200,
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
                            "Analyze both supplied charts together.\n"
                            "Image 1 = 4H higher timeframe.\n"
                            "Image 2 = 15M execution timeframe.\n"
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
        "max_output_tokens": output_tokens,
    }

    request = urllib.request.Request(
        OPENAI_URL,
        data=json.dumps(
            payload,
            separators=(",", ":"),
        ).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=55,
        ) as response:
            raw = response.read().decode("utf-8")
            result = json.loads(raw)

            if not isinstance(result, dict):
                raise RuntimeError(
                    "The AI service returned an invalid response."
                )

            return result

    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        print(
            "OpenAI HTTP error:",
            exc.code,
            raw,
        )

        if exc.code == 401:
            raise RuntimeError(
                "The server AI credential was rejected."
            ) from exc

        if exc.code == 429:
            raise RuntimeError(
                "The AI service is temporarily rate limited."
            ) from exc

        raise RuntimeError(
            "The AI analysis service could not start."
        ) from exc

    except urllib.error.URLError as exc:
        raise RuntimeError(
            "The AI analysis service could not be reached."
        ) from exc

    except TimeoutError as exc:
        raise RuntimeError(
            "The AI analysis service timed out."
        ) from exc


def _create_background_analysis(
    instrument,
    trade_focus,
    higher_image,
    lower_image,
):
    try:
        return _openai_request(
            instrument,
            trade_focus,
            higher_image,
            lower_image,
            1200,
        )

    except RuntimeError as exc:
        if "rate limited" not in str(exc).lower():
            raise

        return _openai_request(
            instrument,
            trade_focus,
            higher_image,
            lower_image,
            700,
        )


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

        try:
            access_token = extract_bearer_token(self)

            user = verify_access_token(
                access_token
            )

            user_id = str(
                user["id"]
            )

            owner = is_owner_user(
                user
            )

            data = _read_json(
                self
            )

            instrument = validate_instrument(
                data.get("instrument")
            )

            trade_focus = validate_focus(
                data.get(
                    "trade_focus",
                    "DAY TRADE",
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
                    4 - slot,
                )

            else:
                remaining = None

            try:
                response = _create_background_analysis(
                    instrument,
                    trade_focus,
                    higher_image,
                    lower_image,
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
                response.get("id", "")
            ).strip()

            status = str(
                response.get(
                    "status",
                    "queued",
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

                print(
                    "OpenAI returned no response ID:",
                    response,
                )

                raise RuntimeError(
                    "The AI service returned no job ID."
                )

            # History save is deliberately non-fatal.
            # The analysis must still work even if the database
            # history write has a temporary problem.
            history_saved = _save_queued_job(
                user_id,
                instrument,
                trade_focus,
                response_id,
                higher_image,
                lower_image,
                status,
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
                        None if owner else 4,

                    "remaining":
                        remaining,

                    "history_saved":
                        history_saved,
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

        except Exception as exc:
            if reserved and user:
                try:
                    release_analysis_slot(
                        str(user["id"])
                    )
                except Exception:
                    pass

            print(
                "analysis_submit error:",
                repr(exc),
            )

            json_response(
                self,
                500,
                {
                    "error":
                        "The analysis could not be started."
                },
            )
