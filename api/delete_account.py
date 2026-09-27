import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler


def send_json(handler, status, payload):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
    handler.send_header("Access-Control-Allow-Methods", "DELETE, OPTIONS")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def bearer(handler):
    value = str(handler.headers.get("Authorization", "")).strip()
    if not value.lower().startswith("bearer "):
        return ""
    return value[7:].strip()


def supabase_request(url, headers, method="GET", body=None):
    request = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return exc.code, raw
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError("Supabase is temporarily unavailable.")


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        send_json(self, 204, {})

    def do_DELETE(self):
        token = bearer(self)
        if not token:
            send_json(self, 401, {"error": "Authentication required."})
            return

        supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
        anon_key = os.getenv("SUPABASE_ANON_KEY", "").strip()
        service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

        if not supabase_url or not anon_key or not service_key:
            send_json(self, 503, {"error": "Account deletion is not configured on the server."})
            return

        # First validate the caller's access token and obtain the exact user ID.
        auth_headers = {
            "apikey": anon_key,
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
        }
        code, raw = supabase_request(
            supabase_url + "/auth/v1/user",
            auth_headers,
            "GET",
        )
        if code != 200:
            send_json(self, 401, {"error": "Session is invalid or expired."})
            return

        try:
            user = json.loads(raw)
        except json.JSONDecodeError:
            send_json(self, 502, {"error": "Unable to verify the account."})
            return

        user_id = str(user.get("id", "")).strip() if isinstance(user, dict) else ""
        if not user_id:
            send_json(self, 401, {"error": "Authenticated user ID is missing."})
            return

        # Delete the Auth user with the service-role key. Any application tables
        # that reference auth.users with ON DELETE CASCADE are removed by Postgres.
        admin_headers = {
            "apikey": service_key,
            "Authorization": "Bearer " + service_key,
            "Accept": "application/json",
        }
        delete_url = supabase_url + "/auth/v1/admin/users/" + user_id
        delete_code, delete_raw = supabase_request(
            delete_url,
            admin_headers,
            "DELETE",
        )

        if delete_code not in (200, 204):
            detail = delete_raw[:500] if delete_raw else ""
            send_json(self, 502, {
                "error": "Account deletion could not be completed." + ((" " + detail) if detail else "")
            })
            return

        send_json(self, 200, {"deleted": True, "message": "Account deleted successfully."})
