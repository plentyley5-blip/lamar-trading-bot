import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from api.user_security import (
    extract_bearer_token,
    get_supabase_service_key,
    get_supabase_url,
    read_secure_job_token,
    verify_access_token,
)


# ============================================================
# RESPONSE
# ============================================================

def json_response(handler, status_code, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
    handler.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
    handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except Exception:
        pass


# ============================================================
# JOB ID
# ============================================================

def extract_job_id(handler):
    parsed = urlparse(handler.path)
    values = parse_qs(parsed.query)
    return values.get("job_id", [""])[0].strip()


def is_probably_secure_token(job_id):
    return "." in job_id and len(job_id) > 40


def resolve_job_reference(job_id, authenticated_user_id):
    """
    Current analysis_submit.py returns a UUID stored directly in
    analysis_jobs.id. Older versions returned a signed job token.

    Support both formats so an older installed APK does not suddenly
    become incompatible with the updated backend.
    """
    if is_probably_secure_token(job_id):
        response_id, instrument, trade_focus, token_user_id = read_secure_job_token(job_id)
        if token_user_id != authenticated_user_id:
            raise PermissionError("This analysis job does not belong to this user.")
        return {
            "mode": "token",
            "job_id": response_id,
            "instrument": instrument,
            "trade_focus": trade_focus,
            "user_id": token_user_id,
        }

    return {
        "mode": "uuid",
        "job_id": job_id,
        "instrument": "",
        "trade_focus": "",
        "user_id": authenticated_user_id,
    }


# ============================================================
# SUPABASE
# ============================================================

def load_job(job_id, user_id):
    query_values = {
        "id": "eq." + job_id,
        "user_id": "eq." + user_id,
        "select": "id,user_id,instrument,trade_focus,status,openai_response_id,error_message,created_at",
        "limit": "1",
    }

    query = urllib.parse.urlencode(query_values, safe="")
    url = get_supabase_url().rstrip("/") + "/rest/v1/analysis_jobs?" + query
    key = get_supabase_service_key().strip()

    request = urllib.request.Request(
        url,
        headers={
            "apikey": key,
            "Authorization": "Bearer " + key,
            "Accept": "application/json",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError("Unable to read analysis job: " + detail[:1000]) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Analysis status is temporarily unavailable.") from exc

    try:
        rows = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Invalid response from the analysis database.") from exc

    if not isinstance(rows, list) or not rows:
        return None

    row = rows[0]
    if not isinstance(row, dict):
        return None

    return row


# ============================================================
# RESULT DECODING
# ============================================================

def decode_result(encoded):
    if not encoded:
        raise RuntimeError("Completed analysis has no saved result.")

    try:
        padding = "=" * (-len(encoded) % 4)
        raw = base64.urlsafe_b64decode(encoded + padding)
        result = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("The saved analysis result is invalid.") from exc
    except Exception as exc:
        raise RuntimeError("The saved analysis result could not be decoded.") from exc

    if not isinstance(result, dict):
        raise RuntimeError("The saved analysis result is invalid.")

    return result


# ============================================================
# OPTIONAL SESSION CHECK
# ============================================================

def check_optional_session(handler, token_user_id):
    """
    The analysis job is already scoped to the authenticated user through
    the Supabase query. If Android sends a current Supabase token, verify it
    too. If that session has expired while the user is polling, do not reject
    an already-authorized job solely because of session expiry.
    """
    auth_header = str(handler.headers.get("Authorization", "")).strip()
    if not auth_header:
        return

    try:
        access_token = extract_bearer_token(handler)
        user = verify_access_token(access_token)
        current_user_id = str(user.get("id", "")).strip()

        if current_user_id and current_user_id != token_user_id:
            raise PermissionError("This analysis job does not belong to this user.")
    except PermissionError:
        raise
    except Exception:
        # The database query below still enforces ownership. An expired
        # session alone should not kill a job that is already in progress.
        return


# ============================================================
# HANDLER
# ============================================================

class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        json_response(self, 204, {})

    def do_GET(self):
        try:
            access_token = extract_bearer_token(self)
            user = verify_access_token(access_token)
            authenticated_user_id = str(user.get("id", "")).strip()

            if not authenticated_user_id:
                raise ValueError("Authenticated user ID is missing.")

            raw_job_id = extract_job_id(self)
            if not raw_job_id:
                raise ValueError("Analysis job ID is missing.")

            reference = resolve_job_reference(raw_job_id, authenticated_user_id)
            check_optional_session(self, authenticated_user_id)

            job = load_job(reference["job_id"], authenticated_user_id)

            if job is None:
                # Never reveal whether a job exists for another user.
                raise LookupError("Analysis job was not found.")

            status = str(job.get("status", "queued")).strip().lower() or "queued"

            if status in {"queued", "pending", "in_progress", "processing"}:
                json_response(
                    self,
                    200,
                    {
                        "status": "in_progress",
                        "job_id": raw_job_id,
                        "poll_after_seconds": 2,
                    },
                )
                return

            if status == "completed":
                result = decode_result(str(job.get("openai_response_id", "")).strip())
                json_response(
                    self,
                    200,
                    {
                        "status": "completed",
                        "job_id": raw_job_id,
                        "result": result,
                    },
                )
                return

            if status in {"failed", "cancelled", "expired", "incomplete"}:
                error = str(job.get("error_message", "")).strip()
                json_response(
                    self,
                    200,
                    {
                        "status": "failed",
                        "job_id": raw_job_id,
                        "error": error or "The analysis did not complete.",
                    },
                )
                return

            # Do not expose internal database statuses to the client.
            json_response(
                self,
                200,
                {
                    "status": "in_progress",
                    "job_id": raw_job_id,
                    "poll_after_seconds": 3,
                },
            )

        except PermissionError as exc:
            json_response(self, 403, {"error": str(exc)})
        except ValueError as exc:
            json_response(self, 400, {"error": str(exc)})
        except LookupError as exc:
            json_response(self, 404, {"error": str(exc)})
        except RuntimeError as exc:
            json_response(self, 503, {"error": str(exc)})
        except Exception:
            # Keep internal database/auth details out of the client response.
            json_response(self, 500, {"error": "Analysis status could not be read."})
