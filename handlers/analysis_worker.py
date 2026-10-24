import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler

from analysis_common import (
    create_background_response,
    parse_completed_response,
    validate_focus,
    validate_image_data_url,
    validate_instrument,
)

from api.user_security import (
    release_analysis_slot,
)


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


def json_response(
    handler,
    status,
    payload,
):
    body = json.dumps(
        payload,
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")

    handler.send_response(
        status
    )

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
        "Content-Type, Authorization, x-analysis-worker-secret",
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

    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
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
        SUPABASE_URL + path,
        data=data,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout,
        ) as response:

            raw = response.read().decode(
                "utf-8",
                errors="replace",
            )

            if not raw:
                return None

            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(
            "utf-8",
            errors="replace",
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


def claim_job(job_nonce):
    rows = supabase_request(
        "POST",
        "/rest/v1/rpc/claim_analysis_job",
        {
            "p_job_nonce": job_nonce
        },
        timeout=30,
    )

    if not isinstance(
        rows,
        list,
    ) or not rows:
        return None

    nonce = urllib.parse.quote(str(job_nonce), safe="")
    full_rows = supabase_request(
        "GET",
        "/rest/v1/analysis_jobs?job_nonce=eq." + nonce + "&select=*&limit=1",
        timeout=20,
    )
    if not isinstance(full_rows, list) or not full_rows:
        raise RuntimeError("The claimed analysis job could not be reloaded.")
    return full_rows[0]


def patch_job(
    job_nonce,
    values,
):
    encoded_nonce = urllib.parse.quote(
        job_nonce,
        safe="",
    )

    supabase_request(
        "PATCH",
        (
            "/rest/v1/analysis_jobs"
            "?job_nonce=eq."
            + encoded_nonce
        ),
        values,
        timeout=30,
    )


def encode_result(result):
    raw = json.dumps(
        result,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    return (
        base64.urlsafe_b64encode(
            raw
        )
        .decode("ascii")
        .rstrip("=")
    )


def safe_release_slot(user_id):
    try:
        if user_id:
            release_analysis_slot(
                user_id
            )
    except Exception:
        pass


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

    if content_length > 30 * 1024 * 1024:
        raise ValueError(
            "Request body is too large."
        )

    raw = handler.rfile.read(
        content_length
    )

    try:
        return json.loads(
            raw.decode("utf-8")
        )
    except Exception:
        raise ValueError(
            "Invalid JSON."
        )


def load_optional_timeframe_images(user_id, job, cleanup_ids):
    allowed = ("1D", "1H", "5M", "1M")
    images = {}
    legacy = job.get("additional_timeframe_images") or {}
    if not isinstance(legacy, dict) or len(legacy) > len(allowed):
        raise ValueError("Stored additional timeframe images are invalid.")
    for raw_timeframe, image in legacy.items():
        timeframe = str(raw_timeframe).strip().upper()
        if timeframe not in allowed or timeframe in images:
            raise ValueError("Stored additional timeframe key is invalid.")
        images[timeframe] = validate_image_data_url(image, timeframe + " chart")

    upload_ids = job.get("additional_timeframe_upload_ids") or {}
    if not isinstance(upload_ids, dict) or len(upload_ids) > len(allowed):
        raise ValueError("Stored additional timeframe upload references are invalid.")
    encoded_user_id = urllib.parse.quote(str(user_id), safe="")
    for raw_timeframe, raw_upload_id in upload_ids.items():
        timeframe = str(raw_timeframe).strip().upper()
        if timeframe not in allowed or timeframe in images:
            raise ValueError("Stored additional timeframe upload key is invalid or duplicated.")
        try:
            upload_id = str(uuid.UUID(str(raw_upload_id).strip()))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("Stored " + timeframe + " chart upload reference is invalid.") from exc
        if upload_id not in cleanup_ids:
            cleanup_ids.append(upload_id)
        rows = supabase_request(
            "GET",
            "/rest/v1/analysis_chart_uploads?id=eq." + upload_id
            + "&user_id=eq." + encoded_user_id
            + "&timeframe=eq." + urllib.parse.quote(timeframe, safe="")
            + "&select=id,timeframe,image_data_url&limit=1",
            timeout=20,
        )
        if not isinstance(rows, list) or not rows:
            raise ValueError("The uploaded " + timeframe + " chart is missing or expired. Please upload it again.")
        row = rows[0]
        if str(row.get("timeframe", "")).upper() != timeframe:
            raise ValueError("The uploaded chart timeframe does not match the request.")
        images[timeframe] = validate_image_data_url(row.get("image_data_url"), timeframe + " chart")
    return images

def delete_staged_timeframe_uploads(user_id, upload_ids):
    if not user_id or not upload_ids:
        return
    safe_ids = []
    for value in upload_ids:
        try:
            safe_ids.append(str(uuid.UUID(str(value))))
        except (ValueError, TypeError, AttributeError):
            continue
    if not safe_ids:
        return
    query = (
        "/rest/v1/analysis_chart_uploads?id=in.%28" + ",".join(safe_ids) + "%29"
        + "&user_id=eq." + urllib.parse.quote(str(user_id), safe="")
    )
    try:
        supabase_request("DELETE", query, timeout=15)
    except Exception:
        pass


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        json_response(
            self,
            204,
            {},
        )

    def do_POST(self):
        job_nonce = ""
        user_id = ""
        staged_upload_ids = []

        try:

            # -----------------------------------------
            # WORKER SECRET
            # -----------------------------------------
            if not WORKER_SECRET:
                json_response(
                    self,
                    503,
                    {
                        "error": "worker_secret_not_configured",
                        "message": "ANALYSIS_WORKER_SECRET is not configured on the worker function.",
                    },
                )
                return

            supplied_secret = self.headers.get(
                "x-analysis-worker-secret",
                "",
            ).strip()

            if not supplied_secret or supplied_secret != WORKER_SECRET:
                json_response(
                    self,
                    401,
                    {
                        "error": "invalid_worker_secret",
                        "message": "Worker authentication failed.",
                    },
                )
                return

            # -----------------------------------------
            # BODY
            # -----------------------------------------
            payload = read_request_body(
                self
            )

            if not isinstance(
                payload,
                dict,
            ):
                raise ValueError(
                    "Invalid webhook payload."
                )

            record = payload.get(
                "record",
                payload,
            )

            if not isinstance(
                record,
                dict,
            ):
                raise ValueError(
                    "Invalid webhook record."
                )

            job_nonce = str(
                record.get(
                    "job_nonce",
                    "",
                )
            ).strip()

            if not job_nonce:
                raise ValueError(
                    "Missing job_nonce."
                )

            # -----------------------------------------
            # CLAIM
            # -----------------------------------------
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

            # -----------------------------------------
            # JOB DATA
            # -----------------------------------------
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
            ).strip().upper()

            trade_focus = str(
                job.get(
                    "trade_focus",
                    "",
                )
            ).strip().upper()

            h4_image = job.get(
                "higher_timeframe_image"
            )

            m15_image = job.get(
                "lower_timeframe_image"
            )
            additional_images = load_optional_timeframe_images(
                user_id, job, staged_upload_ids
            )

            # -----------------------------------------
            # VALIDATION
            # -----------------------------------------
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

            # -----------------------------------------
            # AI
            # -----------------------------------------
            raw_response = (
                create_background_response(
                    instrument=instrument,
                    trade_focus=trade_focus,
                    higher_image=h4_image,
                    lower_image=m15_image,
                    additional_images=additional_images,
                )
            )

            # -----------------------------------------
            # PARSE
            # -----------------------------------------
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

            # -----------------------------------------
            # SAVE
            # -----------------------------------------
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
                },
            )

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

        except Exception as exc:

            error_message = str(
                exc
            ).strip()

            if not error_message:
                error_message = (
                    "Analysis processing failed."
                )

            if job_nonce:
                try:
                    patch_job(
                        job_nonce,
                        {
                            "status":
                                "failed",
                            "error_message":
                                error_message[:2000],
                        },
                    )
                except Exception:
                    pass

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
                        error_message[:2000],
                },
            )
        finally:
            delete_staged_timeframe_uploads(user_id, staged_upload_ids)

    def do_GET(self):
        json_response(
            self,
            200,
            {
                "status":
                    "analysis worker online"
            },
        )
