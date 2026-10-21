
import json
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

from api.user_security import (
    get_supabase_service_key,
    get_supabase_url,
    verify_access_token,
)


def respond(handler, status, payload):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header(
        "Access-Control-Allow-Headers", "Content-Type, Authorization"
    )
    handler.send_header(
        "Access-Control-Allow-Methods", "POST, OPTIONS"
    )
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def authenticated_user_id(handler):
    authorization = handler.headers.get("Authorization", "")
    parts = authorization.split(None, 1)

    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise ValueError("Authentication required.")

    user = verify_access_token(parts[1].strip())
    user_id = user.get("id") if isinstance(user, dict) else None

    if not user_id:
        raise ValueError("Invalid user session.")

    return str(user_id)


def read_body(handler):
    length = int(handler.headers.get("Content-Length", "0"))

    if length <= 0 or length > 16384:
        raise ValueError("Invalid request body.")

    data = json.loads(handler.rfile.read(length).decode("utf-8"))

    if not isinstance(data, dict):
        raise ValueError("Invalid JSON body.")

    return data


def save_device(user_id, token):
    base_url = get_supabase_url().rstrip("/")
    service_key = get_supabase_service_key()

    query = urllib.parse.urlencode({"on_conflict": "device_token"})
    url = base_url + "/rest/v1/device_tokens?" + query

    payload = json.dumps({
        "user_id": user_id,
        "device_token": token,
        "platform": "android",
        "active": True,
    }).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "apikey": service_key,
            "Authorization": "Bearer " + service_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
    )

    with urllib.request.urlopen(request, timeout=15) as response:
        return response.status


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Allow-Headers", "Content-Type, Authorization"
        )
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.end_headers()

    def do_POST(self):
        try:
            user_id = authenticated_user_id(self)
            data = read_body(self)
            token = str(
                data.get("device_token") or data.get("token") or ""
            ).strip()

            if not token or len(token) > 4096:
                respond(self, 400, {"error": "Invalid device token."})
                return

            save_device(user_id, token)
            respond(self, 200, {
                "ok": True,
                "message": "Device registered.",
            })

        except ValueError as exc:
            respond(self, 401, {"error": str(exc)})

        except urllib.error.HTTPError as exc:
            try:
                exc.read()
            except Exception:
                pass
            respond(self, 502, {
                "error": "Device storage rejected the registration."
            })

        except Exception:
            respond(self, 500, {
                "error": "Device registration failed."
            })

    def do_GET(self):
        respond(self, 405, {"error": "Method not allowed."})

    def do_PUT(self):
        respond(self, 405, {"error": "Method not allowed."})

    def do_DELETE(self):
        respond(self, 405, {"error": "Method not allowed."})
