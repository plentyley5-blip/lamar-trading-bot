import json
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from analysis_common import (
    OPENAI_URL,
    api_key,
    json_response,
    parse_completed_response,
)
from api.user_security import (
    extract_bearer_token,
    read_secure_job_token,
    verify_access_token,
)


def retrieve_response(response_id, key):
    request = urllib.request.Request(
        f"{OPENAI_URL}/{response_id}",
        headers={
            "Authorization": "Bearer " + key,
            "Accept": "application/json",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError("Analysis status is temporarily unavailable.") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Analysis status is temporarily unavailable.") from exc


def extract_job_token(handler):
    parsed = urlparse(handler.path)
    values = parse_qs(parsed.query)
    return values.get("job_id", [""])[0]


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        json_response(self, 204, {})

    def do_GET(self):
        try:
            key = api_key()
            if not key:
                json_response(self, 500, {"error": "Server configuration is incomplete."})
                return

            access_token = extract_bearer_token(self)
            user = verify_access_token(access_token)
            current_user_id = str(user["id"])

            token = extract_job_token(self)
            response_id, instrument, trade_focus, token_user_id = read_secure_job_token(token)

            if token_user_id != current_user_id:
                json_response(self, 403, {"error": "This analysis job does not belong to this user."})
                return

            response = retrieve_response(response_id, key)
            status = str(response.get("status", "queued"))

            if status in {"queued", "in_progress"}:
                json_response(
                    self,
                    200,
                    {"status": status, "poll_after_seconds": 2},
                )
                return

            if status == "completed":
                result = parse_completed_response(response, instrument, trade_focus)
                json_response(
                    self,
                    200,
                    {"status": "completed", "result": result},
                )
                return

            if status in {"failed", "cancelled", "incomplete", "expired"}:
                json_response(
                    self,
                    200,
                    {"status": "failed", "error": "The analysis did not complete."},
                )
                return

            json_response(
                self,
                200,
                {"status": "in_progress", "poll_after_seconds": 3},
            )

        except ValueError as exc:
            json_response(self, 400, {"error": str(exc)})
        except RuntimeError as exc:
            json_response(self, 200, {"status": "in_progress", "poll_after_seconds": 4, "message": str(exc)})
        except Exception:
            json_response(self, 200, {"status": "in_progress", "poll_after_seconds": 4})
