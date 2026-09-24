import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

from analysis_common import (
    create_background_response,
    parse_completed_response,
    validate_focus,
    validate_image_data_url,
    validate_instrument,
)

from user_security import (
    is_owner_user,
    release_analysis_slot,
)


SUPABASE_URL = os.environ.get(
    "SUPABASE_URL",
    ""
).rstrip("/")

SUPABASE_SERVICE_ROLE_KEY = os.environ.get(
    "SUPABASE_SERVICE_ROLE_KEY",
    ""
)

WORKER_SECRET = os.environ.get(
    "ANALYSIS_WORKER_SECRET",
    ""
)


def json_response(handler, status, payload):
    body = json.dumps(
        payload,
        ensure_ascii=False
    ).encode("utf-8")

    handler.send_response(status)

    handler.send_header(
        "Content-Type",
        "application/json"
    )

    handler.send_header(
        "Cache-Control",
        "no-store"
    )

    handler.send_header(
        "Access-Control-Allow-Origin",
        "*"
    )

    handler.end_headers()

    handler.wfile.write(body)


def supabase_request(
    method,
    path,
    payload=None,
    timeout=30
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
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization":
            "Bearer "
            + SUPABASE_SERVICE_ROLE_KEY,
        "Content-Type":
            "application/json",
    }

    data = None

    if payload is not None:
        data = json.dumps(
            payload,
            ensure_ascii=False
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
            timeout=timeout
        ) as response:

            raw = (
                response
                .read()
                .decode("utf-8")
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
                errors="replace"
            )
        )

        raise RuntimeError(
            f"Supabase request failed "
            f"({exc.code}): {detail}"
        )


def claim_job(job_nonce):
    """
    Atomically changes queued -> processing.

    Only one worker can successfully claim
    a particular job.
    """

    rows = supabase_request(
        "POST",
        "/rest/v1/rpc/claim_analysis_job",
        {
            "p_job_nonce": job_nonce
        },
        timeout=30
    )

    if not rows:
        return None

    if not isinstance(rows, list):
        return None

    return rows[0]


def patch_job(
    job_nonce,
    values
):
    encoded_nonce = urllib.parse.quote(
        job_nonce,
        safe=""
    )

    url = (
        "/rest/v1/analysis_jobs"
        "?job_nonce=eq."
        + encoded_nonce
    )

    supabase_request(
        "PATCH",
        url,
        values,
        timeout=30
    )


def encode_result(result):
    raw = json.dumps(
        result,
        ensure_ascii=False,
        separators=(
            ",",
            ":"
        )
    ).encode("utf-8")

    return (
        base64.urlsafe_b64encode(
            raw
        )
        .decode("ascii")
        .rstrip("=")
    )


def safe_release_slot(user_id):
    """
    Releases the reserved daily slot when
    a non-owner job fails.
    """

    try:

        if not user_id:
            return

        if is_owner_user(user_id):
            return

        release_analysis_slot(
            user_id
        )

    except Exception:
        # Never replace the original analysis
        # error with a slot-release error.
        pass


class handler(
    BaseHTTPRequestHandler
):

    def do_POST(self):

        try:

            # ---------------------------------
            # 1. Verify worker request
            # ---------------------------------

            if WORKER_SECRET:

                supplied_secret = (
                    self.headers.get(
                        "x-analysis-worker-secret",
                        ""
                    )
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
                        }
                    )

                    return

            # ---------------------------------
            # 2. Read webhook body
            # ---------------------------------

            content_length = int(
                self.headers.get(
                    "Content-Length",
                    "0"
                )
            )

            if content_length <= 0:

                json_response(
                    self,
                    400,
                    {
                        "error":
                            "Empty request body."
                    }
                )

                return

            raw_body = self.rfile.read(
                content_length
            )

            try:

                payload = json.loads(
                    raw_body.decode(
                        "utf-8"
                    )
                )

            except Exception:

                json_response(
                    self,
                    400,
                    {
                        "error":
                            "Invalid JSON."
                    }
                )

                return

            # ---------------------------------
            # 3. Get inserted database record
            # ---------------------------------

            record = payload.get(
                "record",
                payload
            )

            if not isinstance(
                record,
                dict
            ):

                json_response(
                    self,
                    400,
                    {
                        "error":
                            "Invalid webhook record."
                    }
                )

                return

            job_nonce = str(
                record.get(
                    "job_nonce",
                    ""
                )
            ).strip()

            if not job_nonce:

                json_response(
                    self,
                    400,
                    {
                        "error":
                            "Missing job_nonce."
                    }
                )

                return

            # ---------------------------------
            # 4. Atomically claim the job
            # ---------------------------------

            job = claim_job(
                job_nonce
            )

            if not job:

                # Another worker may already have
                # claimed it. This is NOT an error.
                json_response(
                    self,
                    200,
                    {
                        "status":
                            "ignored",
                        "reason":
                            "Job already claimed "
                            "or does not exist."
                    }
                )

                return

            # ---------------------------------
            # 5. Extract job data
            # ---------------------------------

            user_id = str(
                job.get(
                    "user_id",
                    ""
                )
            ).strip()

            instrument = str(
                job.get(
                    "instrument",
                    ""
                )
            ).strip()

            trade_focus = str(
                job.get(
                    "trade_focus",
                    ""
                )
            ).strip()

            h4_image = job.get(
                "higher_timeframe_image"
            )

            m15_image = job.get(
                "lower_timeframe_image"
            )

            try:

                # ---------------------------------
                # 6. Validate job
                # ---------------------------------

                validate_instrument(
                    instrument
                )

                validate_focus(
                    trade_focus
                )

                validate_image_data_url(
                    h4_image,
                    "higher_timeframe_image"
                )

                validate_image_data_url(
                    m15_image,
                    "lower_timeframe_image"
                )

                # ---------------------------------
                # 7. Run Gemini analysis
                # ---------------------------------

                raw_response = (
                    create_background_response(
                        instrument=instrument,
                        trade_focus=trade_focus,
                        higher_timeframe_image=h4_image,
                        lower_timeframe_image=m15_image,
                    )
                )

                # ---------------------------------
                # 8. Parse Gemini result
                # ---------------------------------

                result = (
                    parse_completed_response(
                        raw_response,
                        instrument=instrument
                    )
                )

                # ---------------------------------
                # 9. Save completed result
                # ---------------------------------

                encoded_result = encode_result(
                    result
                )

                patch_job(
                    job_nonce,
                    {
                        "status":
                            "completed",

                        "openai_response_id":
                            encoded_result,

                        "error_message":
                            None,
                    }
                )

                json_response(
                    self,
                    200,
                    {
                        "status":
                            "completed",

                        "job_id":
                            job_nonce,
                    }
                )

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

                # ---------------------------------
                # 10. Mark job failed
                # ---------------------------------

                try:

                    patch_job(
                        job_nonce,
                        {
                            "status":
                                "failed",

                            "error_message":
                                error_message,
                        }
                    )

                except Exception:
                    pass

                # ---------------------------------
                # 11. Return reserved daily slot
                # ---------------------------------

                safe_release_slot(
                    user_id
                )

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
                    }
                )

        except Exception as exc:

            json_response(
                self,
                500,
                {
                    "error":
                        str(exc)
                }
            )

    def do_GET(self):

        json_response(
            self,
            200,
            {
                "status":
                    "analysis worker online"
            }
        )
