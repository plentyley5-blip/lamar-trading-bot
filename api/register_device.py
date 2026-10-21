
import json
import os
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


def response(handler, status, data):
    body = json.dumps(data).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(body)


def read_json(handler):
    try:
        length = int(handler.headers.get("Content-Length", "0"))
        if length < 1 or length > 8192:
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
    if not verified:
        return None

    if isinstance(verified, dict):
        return verified.get("id") or verified.get("sub")

    return None


def supabase_request(url, service_key, method, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as result:
        return result.status


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        user_id = get_user_id(self)
        if not user_id:
            return response(self, 401, {"error": "Authentication required"})

        payload = read_json(self)
        if not payload:
            return response(self, 400, {"error": "Invalid JSON body"})

        device_token = payload.get("token")
        if not isinstance(device_token, str):
            return response(self, 400, {"error": "Missing device token"})

        device_token = device_token.strip()
        if not 50 <= len(device_token) <= 4096:
            return response(self, 400, {"error": "Invalid device token"})

        supabase_url = get_supabase_url()
        service_key = get_supabase_service_key()
        if not supabase_url or not service_key:
            return response(self, 500, {"error": "Database configuration missing"})

        # The database table must be created before this endpoint can work.
        url = supabase_url.rstrip("/") + "/rest/v1/device_tokens"
        try:
            supabase_request(
                url,
                service_key,
                "POST",
                {
                    "user_id": user_id,
                    "device_token": device_token,
                    "platform": "android",
                    "active": True,
                },
            )
            return response(self, 200, {"ok": True, "message": "Device registered"})
        except urllib.error.HTTPError as exc:
            return response(self, 500, {"error": "Could not register device"})
        except Exception:
            return response(self, 500, {"error": "Device registration failed"})

    def do_GET(self):
        return response(self, 405, {"error": "Method not allowed"})

    def do_DELETE(self):
        return response(self, 405, {"error": "Method not allowed"})
