
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


MAX_BODY_BYTES = 8192
MIN_TOKEN_LENGTH = 50
MAX_TOKEN_LENGTH = 4096


def response(handler, status, data):
    body = json.dumps(data).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(body)


def read_json(handler):
    try:
        length = int(handler.headers.get("Content-Length", "0"))

        if length < 1 or length > MAX_BODY_BYTES:
            return None

        data = json.loads(handler.rfile.read(length))

        return data if isinstance(data, dict) else None

    except (ValueError, json.JSONDecodeError):
        return None


def get_user_id(handler):
    token = extract_bearer_token(handler.headers)

    if not token:
        return None

    verified = verify_access_token(token)

    if not verified or not isinstance(verified, dict):
        return None

    user_id = verified.get("id") or verified.get("sub")

    if not isinstance(user_id, str) or not user_id.strip():
        return None

    return user_id.strip()


def supabase_request(url, service_key, method, payload=None):
    data = (
        None
        if payload is None
        else json.dumps(payload).encode("utf-8")
    )

    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }

    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers=headers,
    )

    with urllib.request.urlopen(request, timeout=15) as result:
        return result.status


class handler(BaseHTTPRequestHandler):

    def do_POST(self):
        # Authenticate the signed-in LM Analyzer user.
        user_id = get_user_id(self)

        if not user_id:
            return response(
                self,
                401,
                {"ok": False, "error": "Authentication required"},
            )

        # Validate request body.
        payload = read_json(self)

        if payload is None:
            return response(
                self,
                400,
                {"ok": False, "error": "Invalid JSON body"},
            )

        # Android sends "device_token".
        # Accept "token" as an alternative for compatibility.
        device_token = (
            payload.get("device_token")
            or payload.get("token")
        )

        if not isinstance(device_token, str):
            return response(
                self,
                400,
                {"ok": False, "error": "Missing device token"},
            )

        device_token = device_token.strip()

        if not MIN_TOKEN_LENGTH <= len(device_token) <= MAX_TOKEN_LENGTH:
            return response(
                self,
                400,
                {"ok": False, "error": "Invalid device token"},
            )

        # Only accept the supported Android platform.
        platform = payload.get("platform", "android")

        if platform != "android":
            return response(
                self,
                400,
                {"ok": False, "error": "Unsupported platform"},
            )

        # Verify server-side database configuration.
        supabase_url = get_supabase_url()
        service_key = get_supabase_service_key()

        if not supabase_url or not service_key:
            return response(
                self,
                500,
                {"ok": False, "error": "Database configuration missing"},
            )

        # Upsert using the device token's primary key.
        # A repeated registration updates the existing record.
        query = urllib.parse.urlencode(
            {"on_conflict": "device_token"}
        )

        url = (
            supabase_url.rstrip("/")
            + "/rest/v1/device_tokens?"
            + query
        )

        record = {
            "user_id": user_id,
            "device_token": device_token,
            "platform": "android",
            "active": True,
            "updated_at": "now()",
        }

        try:
            supabase_request(
                url,
                service_key,
                "POST",
                record,
            )

            return response(
                self,
                200,
                {
                    "ok": True,
                    "message": "Device registered",
                },
            )

        except urllib.error.HTTPError as exc:
            # Do not expose database details or credentials.
            try:
                exc.close()
            except Exception:
                pass

            return response(
                self,
                500,
                {
                    "ok": False,
                    "error": "Could not register device",
                },
            )

        except Exception:
            return response(
                self,
                500,
                {
                    "ok": False,
                    "error": "Device registration failed",
                },
            )

    def do_GET(self):
        return response(
            self,
            405,
            {"ok": False, "error": "Method not allowed"},
        )

    def do_DELETE(self):
        return response(
            self,
            405,
            {"ok": False, "error": "Method not allowed"},
        )

    def do_PUT(self):
        return response(
            self,
            405,
            {"ok": False, "error": "Method not allowed"},
        )
