"""LM Analyzer status endpoint compatible with queued analysis_submit.py jobs.

Current queue jobs use a signed token containing ``job_nonce``. Older clients
may still pass a database UUID, and older releases may use the secure token
format from api.user_security. All database lookups are scoped to the token's
or authenticated session's user ID.
"""

import base64
import hashlib
import hmac
import json
import os
import time
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

JOB_TTL_SECONDS = 24 * 60 * 60


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


def get_job_secret():
    """Match api/analysis_submit.py's signed queue-token key and precedence."""
    secret = os.environ.get("JOB_TOKEN_SECRET", "").strip()
    if not secret:
        secret = os.environ.get("GEMINI_API_KEY", "").strip()
    if not secret:
        raise RuntimeError("JOB_TOKEN_SECRET or GEMINI_API_KEY is not configured.")
    return secret.encode("utf-8")


def extract_job_id(handler):
    values = parse_qs(urlparse(handler.path).query).get("job_id", [""])
    return str(values[0]).strip() if values else ""


def _decode_token_payload(token):
    """Decode only to identify format; no trust is placed in it before signature verification."""
    parts = str(token or "").split(".", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError("Invalid analysis job token.")
    payload_part = parts[0]
    padded = payload_part + ("=" * (-len(payload_part) % 4))
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError("Invalid analysis job token.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Invalid analysis job token.")
    return payload_part, parts[1], payload


def read_queue_job_token(token):
    """Verify tokens created by analysis_submit.py (HMAC over encoded payload)."""
    encoded_payload, supplied_signature, payload = _decode_token_payload(token)
    expected = hmac.new(
        get_job_secret(), encoded_payload.encode("ascii"), hashlib.sha256
    ).digest()
    expected_signature = base64.urlsafe_b64encode(expected).decode("ascii").rstrip("=")
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise ValueError("Invalid analysis job signature.")

    job_nonce = str(payload.get("job_nonce", "")).strip()
    user_id = str(payload.get("user_id", "")).strip()
    instrument = str(payload.get("instrument", "")).strip().upper()
    trade_focus = str(payload.get("trade_focus", "")).strip().upper()
    try:
        created_at = int(payload.get("created_at", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid analysis job token timestamp.") from exc

    now = int(time.time())
    if not job_nonce or not user_id or not instrument or not trade_focus or created_at <= 0:
        raise ValueError("Invalid analysis job token.")
    if created_at > now + 60:
        raise ValueError("Invalid analysis job token timestamp.")
    if now - created_at > JOB_TTL_SECONDS:
        raise ValueError("This analysis job has expired.")

    return {
        "mode": "nonce",
        "job_id": job_nonce,
        "instrument": instrument,
        "trade_focus": trade_focus,
        "user_id": user_id,
    }


def resolve_job_reference(job_id):
    """Resolve current job tokens, legacy signed tokens, or a UUID job ID."""
    if "." not in job_id:
        return {
            "mode": "uuid",
            "job_id": job_id,
            "instrument": "",
            "trade_focus": "",
            "user_id": "",
        }

    # Inspect the payload only to choose the matching verifier. No payload value
    # is trusted until that verifier checks the token's signature.
    _payload_part, _signature, payload = _decode_token_payload(job_id)
    if "job_nonce" in payload:
        return read_queue_job_token(job_id)

    try:
        response_id, instrument, trade_focus, token_user_id = read_secure_job_token(job_id)
    except Exception as exc:
        raise ValueError("Invalid or expired analysis job token.") from exc
    if not token_user_id or not response_id:
        raise ValueError("Invalid legacy analysis job token.")
    return {
        "mode": "legacy_id",
        "job_id": str(response_id).strip(),
        "instrument": str(instrument).strip().upper(),
        "trade_focus": str(trade_focus).strip().upper(),
        "user_id": str(token_user_id).strip(),
    }


def check_optional_session(handler, token_user_id):
    """A signed job token can be polled after session expiry; if a session token
    is supplied and verifies, it must belong to the same user."""
    authorization = str(handler.headers.get("Authorization", "")).strip()
    if not authorization:
        return
    try:
        access_token = extract_bearer_token(handler)
        user = verify_access_token(access_token)
        current_user_id = str(user.get("id", "")).strip() if isinstance(user, dict) else ""
        if current_user_id and current_user_id != token_user_id:
            raise PermissionError("This analysis job does not belong to this user.")
    except PermissionError:
        raise
    except Exception:
        # The signed job token itself still authorizes the scoped lookup if the
        # app's session token has expired or cannot be refreshed in time.
        return


def load_job(job_id, user_id, lookup_mode):
    filters = {
        "select": "id,job_nonce,user_id,instrument,trade_focus,status,openai_response_id,error_message,created_at",
        "user_id": "eq." + user_id,
        "limit": "1",
    }
    if lookup_mode == "nonce":
        filters["job_nonce"] = "eq." + job_id
    else:
        filters["id"] = "eq." + job_id

    query = urllib.parse.urlencode(filters, safe="")
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
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError("Unable to read analysis job: " + detail[:1000]) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Analysis status is temporarily unavailable.") from exc

    try:
        rows = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Invalid response from the analysis database.") from exc
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return None
    return rows[0]


def decode_result(encoded):
    if not encoded or not isinstance(encoded, str):
        raise RuntimeError("Completed analysis has no saved result.")
    try:
        padded = encoded + ("=" * (-len(encoded) % 4))
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        result = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("The saved analysis result is invalid.") from exc
    if not isinstance(result, dict):
        raise RuntimeError("The saved analysis result is invalid.")
    return result


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self):
        try:
            raw_job_id = extract_job_id(self)
            if not raw_job_id:
                raise ValueError("Analysis job ID is missing.")

            reference = resolve_job_reference(raw_job_id)
            user_id = reference["user_id"]
            lookup_mode = reference["mode"]

            if lookup_mode == "uuid":
                access_token = extract_bearer_token(self)
                user = verify_access_token(access_token)
                user_id = str(user.get("id", "")).strip() if isinstance(user, dict) else ""
                if not user_id:
                    raise ValueError("Authenticated user ID is missing.")
                lookup_mode = "id"
            else:
                check_optional_session(self, user_id)
                lookup_mode = "nonce" if lookup_mode == "nonce" else "id"

            job = load_job(reference["job_id"], user_id, lookup_mode)
            if job is None:
                raise LookupError("Analysis job was not found.")

            if str(job.get("user_id", "")).strip() != user_id:
                raise PermissionError("This analysis job does not belong to this user.")

            status = str(job.get("status", "queued")).strip().lower() or "queued"
            if status in {"queued", "pending", "in_progress", "processing"}:
                json_response(self, 200, {
                    "status": "in_progress",
                    "job_id": raw_job_id,
                    "poll_after_seconds": 2,
                })
                return

            if status == "completed":
                result = decode_result(str(job.get("openai_response_id", "")).strip())
                json_response(self, 200, {
                    "status": "completed",
                    "job_id": raw_job_id,
                    "result": result,
                })
                return

            if status in {"failed", "cancelled", "expired", "incomplete"}:
                error = str(job.get("error_message", "")).strip()
                json_response(self, 200, {
                    "status": "failed",
                    "job_id": raw_job_id,
                    "error": error or "The analysis did not complete.",
                })
                return

            json_response(self, 200, {
                "status": "in_progress",
                "job_id": raw_job_id,
                "poll_after_seconds": 3,
            })

        except PermissionError as exc:
            json_response(self, 403, {"error": str(exc)})
        except ValueError as exc:
            json_response(self, 400, {"error": str(exc)})
        except LookupError as exc:
            json_response(self, 404, {"error": str(exc)})
        except RuntimeError as exc:
            json_response(self, 503, {"error": str(exc)})
        except Exception:
            json_response(self, 500, {"error": "Analysis status could not be read."})

    def log_message(self, format, *args):
        return
