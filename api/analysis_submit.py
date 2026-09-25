import json
import os
import time
import uuid
import urllib.error
import urllib.request

from http.server import BaseHTTPRequestHandler

from analysis_common import (
    create_background_response,
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


SUPABASE_URL = os.environ.get(
    "SUPABASE_URL",
    ""
).strip().rstrip("/")

SUPABASE_SERVICE_ROLE_KEY = os.environ.get(
    "SUPABASE_SERVICE_ROLE_KEY",
    ""
).strip()


def supabase_insert_job(
    user_id,
    job_nonce,
    instrument,
    trade_focus,
    response_id,
    higher_image,
    lower_image,
    status,
):
    if not SUPABASE_URL:
        raise RuntimeError(
            "SUPABASE_URL is missing."
        )

    if not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY is missing."
        )

    payload = {
        "id":
            str(uuid.uuid4()),

        "job_nonce":
            job_nonce,

        "user_id":
            user_id,

        "instrument":
            instrument,

        "trade_focus":
            trade_focus,

        "status":
            status,

        # This column is NOT NULL.
        "openai_response_id":
            response_id,

        "higher_timeframe_image":
            higher_image,

        "lower_timeframe_image":
            lower_image,

        "error_message":
            None,
    }

    body = json.dumps(
        payload,
        ensure_ascii=False,
    ).encode("utf-8")

    request = urllib.request.Request(
        SUPABASE_URL
        + "/rest/v1/analysis_jobs",
        data=body,
        headers={
            "apikey":
                SUPABASE_SERVICE_ROLE_KEY,

            "Authorization":
                "Bearer "
                + SUPABASE_SERVICE_ROLE_KEY,

            "Content-Type":
                "application/json",

            "Accept":
                "application/json",

            "Prefer":
                "return=minimal",
        },
        method="POST",
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=20,
        ) as response:

            response.read()

    except urllib.error.HTTPError as exc:

        detail = (
            exc.read()
            .decode(
                "utf-8",
                errors="replace",
            )
        )

        raise RuntimeError(
            "Supabase request failed "
            f"({exc.code}) "
            + detail
        )


def read_json(handler):

    try:
        length = int(
            handler.headers.get(
                "Content-Length",
                "0",
            )
        )

    except ValueError:

        raise ValueError(
            "Invalid request."
        )

    if (
        length <= 0
        or length > 25 * 1024 * 1024
    ):

        raise ValueError(
            "Invalid chart request."
        )

    raw = handler.rfile.read(
        length
    )

    try:

        data = json.loads(
            raw.decode(
                "utf-8"
            )
        )

    except json.JSONDecodeError as exc:

        raise ValueError(
            "Invalid request JSON."
        ) from exc

    if not isinstance(
        data,
        dict
    ):

        raise ValueError(
            "Invalid request."
        )

    return data


class handler(
    BaseHTTPRequestHandler
):

    def do_OPTIONS(self):

        json_response(
            self,
            204,
            {}
        )


    def do_GET(self):

        json_response(
            self,
            200,
            {
                "status":
                    "analysis submit online"
            }
        )


    def do_POST(self):

        reserved = False
        user = None
        user_id = ""

        try:

            # =========================================
            # AUTHENTICATION
            # =========================================

            access_token = (
                extract_bearer_token(
                    self
                )
            )

            user = verify_access_token(
                access_token
            )

            user_id = str(
                user.get(
                    "id",
                    ""
                )
            ).strip()

            if not user_id:

                raise ValueError(
                    "Invalid authenticated user."
                )

            owner = is_owner_user(
                user
            )


            # =========================================
            # INPUT
            # =========================================

            data = read_json(
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

            higher_image = (
                validate_image_data_url(
                    data.get(
                        "higher_timeframe_image"
                    ),
                    "4H chart"
                )
            )

            lower_image = (
                validate_image_data_url(
                    data.get(
                        "lower_timeframe_image"
                    ),
                    "15M chart"
                )
            )


            # =========================================
            # DAILY LIMIT
            # =========================================

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
                                False
                        }
                    )

                    return

                reserved = True

                remaining = max(
                    0,
                    4 - int(slot)
                )

            else:

                remaining = None


            # =========================================
            # START OPENAI BACKGROUND JOB
            # =========================================

            try:

                response = (
                    create_background_response(
                        instrument=
                            instrument,

                        trade_focus=
                            trade_focus,

                        higher_image=
                            higher_image,

                        lower_image=
                            lower_image
                    )
                )

            except Exception:

                if reserved:

                    try:
                        release_analysis_slot(
                            user_id
                        )
                    except Exception:
                        pass

                    reserved = False

                raise


            # =========================================
            # OPENAI RESPONSE ID
            # =========================================

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
            ).strip().lower()

            if not response_id:

                if reserved:

                    try:
                        release_analysis_slot(
                            user_id
                        )
                    except Exception:
                        pass

                raise RuntimeError(
                    "The analysis service returned no job ID."
                )


            # =========================================
            # OUR DATABASE JOB
            # =========================================

            job_nonce = uuid.uuid4().hex

            database_status = (
                "completed"
                if status == "completed"
                else "processing"
            )

            try:

                supabase_insert_job(

                    user_id=
                        user_id,

                    job_nonce=
                        job_nonce,

                    instrument=
                        instrument,

                    trade_focus=
                        trade_focus,

                    response_id=
                        response_id,

                    higher_image=
                        higher_image,

                    lower_image=
                        lower_image,

                    status=
                        database_status
                )

            except Exception:

                # OpenAI job is already valid.
                # Do not destroy a working analysis because
                # History storage failed.
                pass


            # =========================================
            # SECURE POLLING TOKEN
            # =========================================

            job_id = (
                create_secure_job_token(
                    response_id,
                    instrument,
                    trade_focus,
                    user_id
                )
            )


            # =========================================
            # RETURN JOB TO APP
            # =========================================

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
                        remaining
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

            json_response(
                self,
                503,
                {
                    "error":
                        str(exc)
                }
            )

        except Exception as exc:

            if (
                reserved
                and user_id
            ):

                try:
                    release_analysis_slot(
                        user_id
                    )
                except Exception:
                    pass

            json_response(
                self,
                500,
                {
                    "error":
                        str(exc)
                }
            )
