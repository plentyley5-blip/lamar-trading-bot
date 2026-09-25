import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

from api.user_security import (
    extract_bearer_token,
    get_supabase_service_key,
    get_supabase_url,
    verify_access_token,
)


def json_response(handler, status_code, payload):
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
        "Access-Control-Allow-Origin",
        "*"
    )
    handler.send_header(
        "Access-Control-Allow-Headers",
        "Content-Type, Authorization"
    )
    handler.send_header(
        "Access-Control-Allow-Methods",
        "GET, POST, OPTIONS"
    )
    handler.send_header(
        "Cache-Control",
        "no-store, no-cache, must-revalidate"
    )
    handler.send_header(
        "Content-Length",
        str(len(body))
    )
    handler.end_headers()

    try:
        handler.wfile.write(body)
    except Exception:
        pass


def get_job_id(handler):
    parsed = urllib.parse.urlparse(
        handler.path
    )

    params = urllib.parse.parse_qs(
        parsed.query
    )

    return str(
        params.get(
            "job_id",
            [""]
        )[0]
    ).strip()


def get_job(job_id, user_id):
    url = (
        get_supabase_url().rstrip("/")
        + "/rest/v1/analysis_jobs"
        + "?select="
        + "id,user_id,instrument,trade_focus,"
        + "created_at,status,openai_response_id,"
        + "error_message"
        + "&id=eq."
        + urllib.parse.quote(
            job_id,
            safe=""
        )
        + "&user_id=eq."
        + urllib.parse.quote(
            user_id,
            safe=""
        )
        + "&limit=1"
    )

    service_key = (
        get_supabase_service_key()
        .strip()
    )

    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "apikey": service_key,
            "Authorization": (
                "Bearer " + service_key
            ),
            "Accept": "application/json"
        }
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=20
        ) as response:

            raw = response.read().decode(
                "utf-8",
                errors="replace"
            )

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(
            "utf-8",
            errors="replace"
        )

        raise RuntimeError(
            "Supabase status error: "
            + detail[:2000]
        ) from exc

    except (
        urllib.error.URLError,
        TimeoutError
    ) as exc:
        raise RuntimeError(
            "Supabase connection error: "
            + str(exc)
        ) from exc

    try:
        rows = json.loads(
            raw
        )
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Supabase returned invalid JSON."
        ) from exc

    if not isinstance(
        rows,
        list
    ) or not rows:
        return None

    return rows[0]


def decode_result(encoded):
    encoded = str(
        encoded or ""
    ).strip()

    if not encoded:
        raise RuntimeError(
            "The saved analysis result is empty."
        )

    try:
        raw = base64.urlsafe_b64decode(
            encoded
            + "=" * (
                -len(encoded) % 4
            )
        )

        result = json.loads(
            raw.decode("utf-8")
        )

    except Exception as exc:
        raise RuntimeError(
            "The saved analysis result is invalid."
        ) from exc

    if not isinstance(
        result,
        dict
    ):
        raise RuntimeError(
            "The saved analysis result is invalid."
        )

    return result


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
        )
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization"
        )
        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, OPTIONS"
        )
        self.end_headers()

    def do_GET(self):
        try:
            access_token = (
                extract_bearer_token(self)
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
                    "Authenticated user ID is missing."
                )

            job_id = get_job_id(
                self
            )

            if not job_id:
                json_response(
                    self,
                    400,
                    {
                        "error": "job_id is required."
                    }
                )
                return

            row = get_job(
                job_id,
                user_id
            )

            if row is None:
                json_response(
                    self,
                    404,
                    {
                        "status": "failed",
                        "error": "Analysis job not found."
                    }
                )
                return

            status = str(
                row.get(
                    "status",
                    ""
                )
            ).strip().lower()

            if status == "completed":
                result = decode_result(
                    row.get(
                        "openai_response_id"
                    )
                )

                json_response(
                    self,
                    200,
                    {
                        "status": "completed",
                        "job_id": row.get("id"),
                        "result": result
                    }
                )
                return

            if status == "failed":
                json_response(
                    self,
                    200,
                    {
                        "status": "failed",
                        "job_id": row.get("id"),
                        "error": (
                            row.get(
                                "error_message"
                            )
                            or "The analysis failed."
                        )
                    }
                )
                return

            json_response(
                self,
                200,
                {
                    "status": "in_progress",
                    "job_id": row.get("id"),
                    "poll_after_seconds": 2
                }
            )

        except ValueError as exc:
            json_response(
                self,
                401,
                {
                    "error": str(exc)
                }
            )

        except RuntimeError as exc:
            json_response(
                self,
                502,
                {
                    "error": str(exc)
                }
            )

        except Exception as exc:
            json_response(
                self,
                500,
                {
                    "error": (
                        "Analysis status backend error: "
                        + str(exc)
                    )
                }
            )
