
import base64
import json
import os
import time
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

from api.user_security import release_analysis_slot


SUPABASE_URL = os.environ.get(
    "SUPABASE_URL",
    "",
).strip().rstrip("/")

SUPABASE_SERVICE_ROLE_KEY = os.environ.get(
    "SUPABASE_SERVICE_ROLE_KEY",
    "",
).strip()

WORKER_SECRET = os.environ.get(
    "ANALYSIS_WORKER_SECRET",
    "",
).strip()

SUPABASE_TIMEOUT_SECONDS = 15
MAX_REQUEST_BODY_BYTES = 30 * 1024 * 1024


def log_event(event, job_nonce="", elapsed=None, detail=""):
    parts = ["LM_ANALYZER_WORKER", event]

    if job_nonce:
        parts.append("job=" + job_nonce[:12])

    if elapsed is not None:
        parts.append("seconds=" + f"{elapsed:.2f}")

    if detail:
        parts.append(detail[:300])

    print(" | ".join(parts), flush=True)


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
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header(
        "Access-Control-Allow-Headers",
        "Content-Type, Authorization, X-Analysis-Worker-Secret",
    )
    handler.send_header(
        "Access-Control-Allow-Methods",
        "POST, GET, OPTIONS",
    )
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()

    if status != 204:
        handler.wfile.write(body)


def supabase_request(method, path, payload=None, timeout=SUPABASE_TIMEOUT_SECONDS):
    if not SUPABASE_URL:
        raise RuntimeError("SUPABASE_URL is not configured.")

    if not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY is not configured."
        )

    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": "Bearer " + SUPABASE_SERVICE_ROLE_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
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
            f"Supabase request failed ({exc.code}): {detail[:1000]}"
        ) from exc

    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(
            "Supabase connection failed: " + str(exc)
        ) from exc


def claim_job(job_nonce):
    rows = supabase_request(
        "POST",
        "/rest/v1/rpc/claim_analysis_job",
        {"p_job_nonce": job_nonce},
    )

    if not isinstance(rows, list) or not rows:
        return None

    return rows[0]


def patch_job(job_nonce, values, only_if_processing=False):
    encoded_nonce = urllib.parse.quote(
        job_nonce,
        safe="",
    )

    path = (
        "/rest/v1/analysis_jobs"
        "?job_nonce=eq."
        + encoded_nonce
    )

    # Only update a job that is still processing. This helps prevent
    # a late worker response from overwriting a completed or failed job.
    if only_if_processing:
        path += "&status=eq.processing"

    return supabase_request(
        "PATCH",
        path,
        values,
    )


def encode_result(result):
    raw = json.dumps(
        result,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    return (
        base64.urlsafe_b64encode(raw)
        .decode("ascii")
        .rstrip("=")
    )


def safe_release_slot(user_id):
    if not user_id:
        return

    try:
        release_analysis_slot(user_id)
    except Exception as exc:
        log_event(
            "SLOT_RELEASE_WARNING",
            detail=str(exc),
        )


def read_request_body(handler):
    raw_length = handler.headers.get(
        "Content-Length",
        "0",
    )

    try:
        content_length = int(raw_length)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid Content-Length.") from exc

    if content_length <= 0:
        raise ValueError("Empty request body.")

    if content_length > MAX_REQUEST_BODY_BYTES:
        raise ValueError("Request body is too large.")

    raw = handler.rfile.read(content_length)

    if len(raw) != content_length:
        raise ValueError("Incomplete request body.")

    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid JSON.") from exc


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        json_response(self, 204, {})

    def do_POST(self):
        started = time.monotonic()
        job_nonce = ""
        user_id = ""
        claimed = False

        try:
            # -----------------------------------------
            # WORKER SECRET
            # -----------------------------------------
            if WORKER_SECRET:
                supplied_secret = self.headers.get(
                    "x-analysis-worker-secret",
                    "",
                ).strip()

                if not supplied_secret or supplied_secret != WORKER_SECRET:
                    json_response(
                        self,
                        401,
                        {"error": "Unauthorized"},
                    )
                    return

            # -----------------------------------------
            # REQUEST BODY
            # -----------------------------------------
            payload = read_request_body(self)

            if not isinstance(payload, dict):
                raise ValueError("Invalid webhook payload.")

            record = payload.get("record", payload)

            if not isinstance(record, dict):
                raise ValueError("Invalid webhook record.")

            job_nonce = str(
                record.get("job_nonce", "")
            ).strip()

            if not job_nonce:
                raise ValueError("Missing job_nonce.")

            log_event("RECEIVED", job_nonce)

            # -----------------------------------------
            # CLAIM JOB
            # -----------------------------------------
            claim_started = time.monotonic()
            job = claim_job(job_nonce)

            log_event(
                "CLAIM_FINISHED",
                job_nonce,
                time.monotonic() - claim_started,
            )

            if not job:
                log_event("IGNORED_ALREADY_CLAIMED", job_nonce)

                json_response(
                    self,
                    200,
                    {
                        "status": "ignored",
                        "reason": (
                            "Job already claimed or does not exist."
                        ),
                    },
                )
                return

            claimed = True

            # -----------------------------------------
            # JOB DATA
            # -----------------------------------------
            user_id = str(
                job.get("user_id", "")
            ).strip()

            instrument = str(
                job.get("instrument", "")
            ).strip().upper()

            trade_focus = str(
                job.get("trade_focus", "")
            ).strip().upper()

            h4_image = job.get("higher_timeframe_image")
            m15_image = job.get("lower_timeframe_image")

            # -----------------------------------------
            # VALIDATION
            # -----------------------------------------
            validate_instrument(instrument)
            validate_focus(trade_focus)

            validate_image_data_url(
                h4_image,
                "higher_timeframe_image",
            )

            validate_image_data_url(
                m15_image,
                "lower_timeframe_image",
            )

            log_event(
                "VALIDATION_FINISHED",
                job_nonce,
                time.monotonic() - started,
            )

            # -----------------------------------------
            # GEMINI ANALYSIS
            # -----------------------------------------
            ai_started = time.monotonic()

            raw_response = create_background_response(
                instrument=instrument,
                trade_focus=trade_focus,
                higher_image=h4_image,
                lower_image=m15_image,
            )

            ai_elapsed = time.monotonic() - ai_started

            log_event(
                "GEMINI_FINISHED",
                job_nonce,
                ai_elapsed,
            )

            # -----------------------------------------
            # PARSE RESULT
            # -----------------------------------------
            parse_started = time.monotonic()

            result = parse_completed_response(
                raw_response,
                instrument=instrument,
            )

            if not isinstance(result, dict):
                raise RuntimeError(
                    "The analysis returned an invalid result."
                )

            log_event(
                "PARSE_FINISHED",
                job_nonce,
                time.monotonic() - parse_started,
            )

            # -----------------------------------------
            # SAVE RESULT
            # -----------------------------------------
            save_started = time.monotonic()

            encoded_result = encode_result(result)

            patch_job(
                job_nonce,
                {
                    "status": "completed",
                    "openai_response_id": encoded_result,
                    "error_message": None,
                },
                only_if_processing=True,
            )

            log_event(
                "SAVE_FINISHED",
                job_nonce,
                time.monotonic() - save_started,
            )

            log_event(
                "REQUEST_FINISHED",
                job_nonce,
                time.monotonic() - started,
            )

            json_response(
                self,
                200,
                {
                    "status": "completed",
                    "job_id": job_nonce,
                },
            )

        except Exception as exc:
            error_message = str(exc).strip()

            if not error_message:
                error_message = "Analysis processing failed."

            log_event(
                "FAILED",
                job_nonce,
                time.monotonic() - started,
                error_message,
            )

            if job_nonce and claimed:
                try:
                    patch_job(
                        job_nonce,
                        {
                            "status": "failed",
                            "error_message": error_message[:2000],
                        },
                        only_if_processing=True,
                    )
                except Exception as patch_exc:
                    log_event(
                        "FAILURE_SAVE_WARNING",
                        job_nonce,
                        detail=str(patch_exc),
                    )

            if claimed:
                safe_release_slot(user_id)

            try:
                json_response(
                    self,
                    200,
                    {
                        "status": "failed",
                        "job_id": job_nonce,
                        "error": error_message[:2000],
                    },
                )
            except Exception:
                pass

    def do_GET(self):
        json_response(
            self,
            200,
            {"status": "analysis worker online"},
        )
