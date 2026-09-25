import json
import base64
from http.server import BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.parse import urlparse, parse_qs, quote
from urllib.error import HTTPError, URLError

from user_security import (
    extract_bearer_token,
    verify_access_token,
    get_supabase_url,
    get_supabase_service_key,
)


def send_json(handler, status_code, payload):
    body = json.dumps(
        payload,
        ensure_ascii=False
    ).encode("utf-8")

    handler.send_response(status_code)
    handler.send_header(
        "Content-Type",
        "application/json; charset=utf-8"
    )
    handler.send_header(
        "Cache-Control",
        "no-store"
    )
    handler.send_header(
        "Access-Control-Allow-Origin",
        "*"
    )
    handler.send_header(
        "Access-Control-Allow-Headers",
        "Authorization, Content-Type"
    )
    handler.send_header(
        "Access-Control-Allow-Methods",
        "GET, OPTIONS"
    )
    handler.send_header(
        "Content-Length",
        str(len(body))
    )
    handler.end_headers()
    handler.wfile.write(body)


def decode_saved_result(encoded):
    if not encoded:
        raise ValueError("Saved analysis result is empty.")

    padding = "=" * (-len(encoded) % 4)

    raw = base64.urlsafe_b64decode(
        encoded + padding
    )

    result = json.loads(
        raw.decode("utf-8")
    )

    if not isinstance(result, dict):
        raise ValueError(
            "Saved analysis result is invalid."
        )

    return result


def get_job(job_id, user_id):
    supabase_url = get_supabase_url().rstrip("/")
    service_key = get_supabase_service_key().strip()

    if not supabase_url:
        raise RuntimeError(
            "Supabase URL is not configured."
        )

    if not service_key:
        raise RuntimeError(
            "Supabase service key is not configured."
        )

    encoded_job_id = quote(
        job_id,
        safe=""
    )

    encoded_user_id = quote(
        user_id,
        safe=""
    )

    url = (
        f"{supabase_url}/rest/v1/analysis_jobs"
        f"?select=id,user_id,instrument,trade_focus,status,"
        f"openai_response_id,created_at,error_message"
        f"&id=eq.{encoded_job_id}"
        f"&user_id=eq.{encoded_user_id}"
        f"&limit=1"
    )

    req = Request(
        url,
        method="GET",
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urlopen(
            req,
            timeout=15
        ) as response:

            raw = response.read().decode(
                "utf-8",
                errors="replace"
            )

    except HTTPError as exc:
        body = exc.read().decode(
            "utf-8",
            errors="replace"
        )

        raise RuntimeError(
            f"Supabase status error ({exc.code}): {body[:1000]}"
        )

    except URLError as exc:
        raise RuntimeError(
            f"Supabase connection failed: {exc.reason}"
        )

    try:
        rows = json.loads(raw)
    except Exception:
        raise RuntimeError(
            "Supabase returned invalid JSON."
        )

    if not rows:
        return None

    return rows[0]


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
        )
        self.send_header(
            "Access-Control-Allow-Headers",
            "Authorization, Content-Type"
        )
        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, OPTIONS"
        )
        self.end_headers()

    def do_GET(self):
        try:
            token = extract_bearer_token(self)

            if not token:
                send_json(
                    self,
                    401,
                    {
                        "error": "Authentication required."
                    },
                )
                return

            user = verify_access_token(token)

            if not user:
                send_json(
                    self,
                    401,
                    {
                        "error": "Invalid or expired session."
                    },
                )
                return

            user_id = user.get("id")

            if not user_id:
                send_json(
                    self,
                    401,
                    {
                        "error": "User ID was not found."
                    },
                )
                return

            parsed = urlparse(
                self.path
            )

            params = parse_qs(
                parsed.query
            )

            job_ids = params.get(
                "job_id"
            )

            if not job_ids:
                send_json(
                    self,
                    400,
                    {
                        "error": "job_id is required."
                    },
                )
                return

            job_id = str(
                job_ids[0]
            ).strip()

            if not job_id:
                send_json(
                    self,
                    400,
                    {
                        "error": "job_id is empty."
                    },
                )
                return

            row = get_job(
                job_id,
                user_id
            )

            if row is None:
                send_json(
                    self,
                    404,
                    {
                        "status": "failed",
                        "error": "Analysis job was not found."
                    },
                )
                return

            status = str(
                row.get("status") or ""
            ).lower()

            if status == "completed":
                encoded_result = row.get(
                    "openai_response_id"
                )

                try:
                    result = decode_saved_result(
                        encoded_result
                    )
                except Exception as exc:
                    send_json(
                        self,
                        500,
                        {
                            "status": "failed",
                            "error": (
                                "Saved analysis could not be decoded: "
                                + str(exc)
                            ),
                        },
                    )
                    return

                send_json(
                    self,
                    200,
                    {
                        "status": "completed",
                        "result": result,
                        "job_id": row.get("id"),
                    },
                )
                return

            if status == "failed":
                send_json(
                    self,
                    200,
                    {
                        "status": "failed",
                        "error": (
                            row.get("error_message")
                            or "Analysis failed."
                        ),
                        "job_id": row.get("id"),
                    },
                )
                return

            send_json(
                self,
                200,
                {
                    "status": "in_progress",
                    "job_id": row.get("id"),
                },
            )

        except RuntimeError as exc:
            send_json(
                self,
                500,
                {
                    "status": "failed",
                    "error": str(exc),
                },
            )

        except Exception as exc:
            send_json(
                self,
                500,
                {
                    "status": "failed",
                    "error": (
                        "Analysis status error: "
                        + str(exc)
                    ),
                },
            )
