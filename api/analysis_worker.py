import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

from api.analysis_common import (
    create_background_response,
    parse_completed_response,
    validate_focus,
    validate_image_data_url,
    validate_instrument,
)

from api.user_security import (
    is_owner_user,
    release_analysis_slot,
)


# =========================================================
# CONFIG
# =========================================================

SUPABASE_URL = os.environ.get(
    "SUPABASE_URL",
    ""
).strip().rstrip("/")

SUPABASE_SERVICE_ROLE_KEY = os.environ.get(
    "SUPABASE_SERVICE_ROLE_KEY",
    ""
).strip()

WORKER_SECRET = os.environ.get(
    "ANALYSIS_WORKER_SECRET",
    ""
).strip()


# =========================================================
# RESPONSE
# =========================================================

def json_response(handler, status, payload):

    body = json.dumps(
        payload,
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")

    handler.send_response(status)

    handler.send_header(
        "Content-Type",
        "application/json; charset=utf-8",
    )

    handler.send_header(
        "Cache-Control",
        "no-store",
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
        "Content-Length",
        str(len(body)),
    )

    handler.end_headers()

    handler.wfile.write(body)


# =========================================================
# SUPABASE REQUEST
# =========================================================

def supabase_request(
    method,
    path,
    payload=None,
    timeout=30,
):

    if not SUPABASE_URL:
        raise RuntimeError(
            "SUPABASE_URL is not configured."
        )

    if not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY is not configured."
        )

    url = (
        SUPABASE_URL
        + path
    )

    headers = {
        "apikey":
            SUPABASE_SERVICE_ROLE_KEY,

        "Authorization":
            "Bearer "
            + SUPABASE_SERVICE_ROLE_KEY,

        "Content-Type":
            "application/json",

        "Accept":
            "application/json",
    }

    data = None

    if payload is not None:

        data = json.dumps(
            payload,
            ensure_ascii=False,
        ).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=timeout,
        ) as response:

            raw = (
                response
                .read()
                .decode(
                    "utf-8",
                    errors="replace",
                )
            )

            if not raw:
                return None

            try:
                return json.loads(raw)

            except json.JSONDecodeError:
                return raw

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
            f"({exc.code}): {detail}"
        )

    except urllib.error.URLError as exc:

        raise RuntimeError(
            "Supabase connection failed: "
            + str(exc)
        )


# =========================================================
# CLAIM JOB
# =========================================================

def claim_job(job_nonce):

    rows = supabase_request(
        "POST",
        "/rest/v1/rpc/claim_analysis_job",
        {
            "p_job_nonce":
                job_nonce
        },
        timeout=30,
    )

    if not rows:
        return None

    if not isinstance(
        rows,
        list,
    ):
        return None

    return rows[0]


# =========================================================
# UPDATE JOB
# =========================================================

def patch_job(
    job_nonce,
    values,
):

    encoded_nonce = urllib.parse.quote(
        job_nonce,
        safe="",
    )

    path = (
        "/rest/v1/analysis_jobs"
        "?job_nonce=eq."
        + encoded_nonce
    )

    supabase_request(
        "PATCH",
        path,
        values,
        timeout=30,
    )


# =========================================================
# ENCODE RESULT
# =========================================================

def encode_result(result):

    raw = json.dumps(
        result,
        ensure_ascii=False,
        separators=(
            ",",
            ":",
        ),
    ).encode("utf-8")

    return (
        base64.urlsafe_b64encode(
            raw
        )
        .decode("ascii")
        .rstrip("=")
    )


# =========================================================
# RELEASE DAILY SLOT
# =========================================================

def safe_release_slot(user_id):

    try:

        if not user_id:
            return

        if is_owner_user(
            user_id
        ):
            return

        release_analysis_slot(
            user_id
        )

    except Exception:
        pass


# =========================================================
# READ WEBHOOK BODY
# =========================================================

def read_request_body(handler):

    raw_length = handler.headers.get(
        "Content-Length",
        "0",
    )

    try:
        content_length = int(
            raw_length
        )

    except ValueError:
        raise ValueError(
            "Invalid Content-Length."
        )

    if content_length <= 0:
        raise ValueError(
            "Empty request body."
        )

    if content_length > (
        30 * 1024 * 1024
    ):
        raise ValueError(
            "Request body is too large."
        )

    raw_body = handler.rfile.read(
        content_length
    )

    try:

        return json.loads(
            raw_body.decode(
                "utf-8"
            )
        )

    except Exception:

        raise ValueError(
            "Invalid JSON."
        )


# =========================================================
# HANDLER
# =========================================================

class handler(
    BaseHTTPRequestHandler
):

    # -----------------------------------------------------
    # OPTIONS
    # -----------------------------------------------------

    def do_OPTIONS(self):

        json_response(
            self,
            204,
            {},
        )


    # -----------------------------------------------------
    # POST
    # -----------------------------------------------------

    def do_POST(self):

        job_nonce = ""
        user_id = ""

        try:

            # =============================================
            # 1. VERIFY WORKER SECRET
            # =============================================

            if WORKER_SECRET:

                supplied_secret = (
                    self.headers.get(
                        "x-analysis-worker-secret",
                        "",
                    ).strip()
                )

                if (
                    not supplied_secret
                    or supplied_secret
                    != WORKER_SECRET
                ):

                    json_response(
                        self,
                        401,
                        {
                            "error":
                                "Unauthorized"
                        },
                    )

                    return


            # =============================================
            # 2. READ WEBHOOK
            # =============================================

            payload = read_request_body(
                self
            )

            if not isinstance(
                payload,
                dict,
            ):

                json_response(
                    self,
                    400,
                    {
                        "error":
                            "Invalid webhook payload."
                    },
                )

                return


            # =============================================
            # 3. GET DATABASE RECORD
            # =============================================

            record = payload.get(
                "record",
                payload,
            )

            if not isinstance(
                record,
                dict,
            ):

                json_response(
                    self,
                    400,
                    {
                        "error":
                            "Invalid webhook record."
                    },
                )

                return


            job_nonce = str(
                record.get(
                    "job_nonce",
                    "",
                )
            ).strip()

            if not job_nonce:

                json_response(
                    self,
                    400,
                    {
                        "error":
                            "Missing job_nonce."
                    },
                )

                return


            # =============================================
            # 4. CLAIM JOB
            # =============================================

            job = claim_job(
                job_nonce
            )

            if not job:

                json_response(
                    self,
                    200,
                    {
                        "status":
                            "ignored",

                        "reason":
                            "Job already claimed "
                            "or does not exist.",
                    },
                )

                return


            # =============================================
            # 5. EXTRACT JOB
            # =============================================

            user_id = str(
                job.get(
                    "user_id",
                    "",
                )
            ).strip()

            instrument = str(
                job.get(
                    "instrument",
                    "",
                )
            ).strip()

            trade_focus = str(
                job.get(
                    "trade_focus",
                    "",
                )
            ).strip()

            h4_image = job.get(
                "higher_timeframe_image"
            )

            m15_image = job.get(
                "lower_timeframe_image"
            )


            # =============================================
            # 6. VALIDATE
            # =============================================

            validate_instrument(
                instrument
            )

            validate_focus(
                trade_focus
            )

            validate_image_data_url(
                h4_image,
                "higher_timeframe_image",
            )

            validate_image_data_url(
                m15_image,
                "lower_timeframe_image",
            )


            # =============================================
            # 7. RUN AI ANALYSIS
            # =============================================

            raw_response = (
                create_background_response(
                    instrument=instrument,
                    trade_focus=trade_focus,
                    higher_timeframe_image=h4_image,
                    lower_timeframe_image=m15_image,
                )
            )


            # =============================================
            # 8. PARSE RESULT
            # =============================================

            result = (
                parse_completed_response(
                    raw_response,
                    instrument=instrument,
                )
            )

            if not isinstance(
                result,
                dict,
            ):

                raise RuntimeError(
                    "The analysis returned an invalid result."
                )


            # =============================================
            # 9. ENCODE RESULT
            # =============================================

            encoded_result = encode_result(
                result
            )


            # =============================================
            # 10. SAVE COMPLETED JOB
            # =============================================

            patch_job(
                job_nonce,
                {
                    "status":
                        "completed",

                    "openai_response_id":
                        encoded_result,

                    "error_message":
                        None,
                },
            )


            # =============================================
            # 11. SUCCESS
            # =============================================

            json_response(
                self,
                200,
                {
                    "status":
                        "completed",

                    "job_id":
                        job_nonce,
                },
            )

            return


        except Exception as exc:

            error_message = str(
                exc
            ).strip()

            if not error_message:

                error_message = (
                    "Analysis processing failed."
                )

            error_message = (
                error_message[:2000]
            )


            # =============================================
            # MARK JOB FAILED
            # =============================================

            if job_nonce:

                try:

                    patch_job(
                        job_nonce,
                        {
                            "status":
                                "failed",

                            "error_message":
                                error_message,
                        },
                    )

                except Exception:
                    pass


            # =============================================
            # RELEASE SLOT
            # =============================================

            safe_release_slot(
                user_id
            )


            # =============================================
            # RETURN FAILURE
            # =============================================

            json_response(
                self,
                200,
                {
                    "status":
                        "failed",

                    "job_id":
                        job_nonce,

                    "error":
                        error_message,
                },
            )

            return


    # -----------------------------------------------------
    # GET
    # -----------------------------------------------------

    def do_GET(self):

        json_response(
            self,
            200,
            {
                "status":
                    "analysis worker online"
            },
        )
