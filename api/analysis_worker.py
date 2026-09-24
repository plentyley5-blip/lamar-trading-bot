import json
import os
import base64
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler

from analysis_common import (
    create_background_response,
    parse_completed_response,
)
from analysis_common import validate_focus, validate_image_data_url, validate_instrument
from user_security import get_supabase_url, get_supabase_service_role_key


WORKER_SECRET = os.environ.get("ANALYSIS_WORKER_SECRET", "")


def json_response(handler, status, payload):
    body = json.dumps(payload).encode("utf-8")

    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)


def supabase_request(method, path, payload=None):
    url = get_supabase_url().rstrip("/") + path
    key = get_supabase_service_role_key()

    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    data = None

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")

            if not raw:
                return None

            return json.loads(raw)

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Supabase request failed ({exc.code}): {detail}"
        )


def claim_job(job_nonce):
    rows = supabase_request(
        "POST",
        "/rest/v1/rpc/claim_analysis_job",
        {
            "p_job_nonce": job_nonce
        }
    )

    if not rows:
        return None

    return rows[0]


def update_job(job_nonce, values):
    supabase_request(
        "PATCH",
        "/rest/v1/analysis_jobs",
        None
    )


def patch_job(job_nonce, values):
    url = (
        get_supabase_url().rstrip("/")
        + "/rest/v1/analysis_jobs"
        + "?job_nonce=eq."
        + urllib.parse.quote(job_nonce, safe="")
    )

    key = get_supabase_service_role_key()

    request = urllib.request.Request(
        url,
        data=json.dumps(values).encode("utf-8"),
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
        method="PATCH",
    )

    try:
        with urllib.request.urlopen(request, timeout=30):
            return

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Unable to update analysis job ({exc.code}): {detail}"
        )


def encode_result(result):
    raw = json.dumps(
        result,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")

    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class handler(BaseHTTPRequestHandler):

    def do_POST(self):

        try:

            if WORKER_SECRET:
                supplied = self.headers.get(
                    "x-analysis-worker-secret",
                    ""
                )

                if supplied != WORKER_SECRET:
                    json_response(
                        self,
                        401,
                        {"error": "Unauthorized"}
                    )
                    return

            length = int(
                self.headers.get(
                    "Content-Length",
                    "0"
                )
            )

            raw = self.rfile.read(length)

            payload = json.loads(
                raw.decode("utf-8")
            )

            record = payload.get(
                "record",
                payload
            )

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
                    {"error": "Missing job_nonce"}
                )
                return

            job = claim_job(job_nonce)

            if not job:
                json_response(
                    self,
                    200,
                    {
                        "status": "ignored",
                        "reason": "Job was already claimed or does not exist."
                    }
                )
                return

            user_id = job["user_id"]
            instrument = job["instrument"]
            trade_focus = job["trade_focus"]
            h4_image = job["higher_timeframe_image"]
            m15_image = job["lower_timeframe_image"]

            validate_instrument(instrument)
            validate_focus(trade_focus)

            validate_image_data_url(
                h4_image,
                "higher_timeframe_image"
            )

            validate_image_data_url(
                m15_image,
                "lower_timeframe_image"
            )

            try:

                raw_response = create_background_response(
                    instrument=instrument,
                    trade_focus=trade_focus,
                    higher_timeframe_image=h4_image,
                    lower_timeframe_image=m15_image,
                )

                result = parse_completed_response(
                    raw_response,
                    instrument=instrument,
                )

                patch_job(
                    job_nonce,
                    {
                        "status": "completed",
                        "openai_response_id": encode_result(
                            result
                        ),
                        "error_message": None,
                    }
                )

                json_response(
                    self,
                    200,
                    {
                        "status": "completed",
                        "job_nonce": job_nonce,
                        "user_id": user_id,
                    }
                )

            except Exception as exc:

                patch_job(
                    job_nonce,
                    {
                        "status": "failed",
                        "error_message": str(exc)[:2000],
                    }
                )

                json_response(
                    self,
                    200,
                    {
                        "status": "failed",
                        "job_nonce": job_nonce,
                    }
                )

        except Exception as exc:

            json_response(
                self,
                500,
                {
                    "error": str(exc)
                }
            )
