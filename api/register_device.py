
import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

from api.user_security import (
    extract_bearer_token,
    get_supabase_service_key,
    get_supabase_url,
    verify_access_token,
)


def send_json(handler, status, payload):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            # Authenticate the signed-in user.
            token = extract_bearer_token(self)
            if not token:
                send_json(self, 401, {"error": "Authentication required"})
                return

            user = verify_access_token(token)
            if not isinstance(user, dict) or not user.get("id"):
                send_json(self, 401, {"error": "Invalid session"})
                return

            user_id = str(user["id"])

            # Read and validate the device token.
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 16384:
                send_json(self, 400, {"error": "Invalid request body"})
                return

            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                send_json(self, 400, {"error": "Invalid request body"})
                return

            device_token = str(
                payload.get("device_token") or payload.get("token") or ""
            ).strip()

            if not device_token or len(device_token) > 8192:
                send_json(self, 400, {"error": "Invalid device token"})
                return

            # Validate server configuration before contacting Supabase.
            supabase_url = get_supabase_url().rstrip("/")
            service_key = get_supabase_service_key().strip()

            if not supabase_url or not service_key:
                send_json(self, 503, {"error": "Database configuration missing"})
                return

            url = (
                supabase_url
                + "/rest/v1/device_tokens"
                + "?on_conflict=device_token"
            )

            record = {
                "user_id": user_id,
                "device_token": device_token,
                "platform": "android",
                "active": True,
            }

            request = urllib.request.Request(
                url,
                data=json.dumps(record).encode("utf-8"),
                headers={
                    "apikey": service_key,
                    "Authorization": "Bearer " + service_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Prefer": "resolution=merge-duplicates,return=minimal",
                },
                method="POST",
            )

            with urllib.request.urlopen(request, timeout=12) as response:
                if response.status not in (200, 201, 204):
                    send_json(self, 502, {"error": "Device registration failed"})
                    return

            send_json(self, 200, {"ok": True, "message": "Device registered"})

        except (ValueError, json.JSONDecodeError):
            send_json(self, 400, {"error": "Invalid request"})
        except PermissionError:
            send_json(self, 401, {"error": "Authentication failed"})
        except urllib.error.HTTPError as exc:
            # Log database details in Vercel, never expose credentials.
            detail = exc.read().decode("utf-8", errors="replace")
            print("Device registration database error:", exc.code, detail[:1000])
            send_json(self, 502, {"error": "Device registration failed"})
        except Exception as exc:
            print("Device registration exception:", type(exc).__name__, str(exc))
            send_json(self, 500, {"error": "Device registration failed"})

    def do_GET(self):
        send_json(self, 405, {"error": "Method not allowed"})

    def do_PUT(self):
        send_json(self, 405, {"error": "Method not allowed"})

    def do_DELETE(self):
        send_json(self, 405, {"error": "Method not allowed"})
