import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler


def _json(handler, status, payload):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _headers(token=None, service=False):
    url_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY" if service else "SUPABASE_ANON_KEY", "").strip()
    headers = {"apikey": url_key, "Accept": "application/json"}
    if service:
        headers["Authorization"] = "Bearer " + url_key
    elif token:
        headers["Authorization"] = "Bearer " + token
    return headers


def _request(url, headers):
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except Exception:
            data = {"error": raw}
        return exc.code, data


def _bearer(handler):
    value = str(handler.headers.get("Authorization", "")).strip()
    if not value.lower().startswith("bearer "):
        return ""
    return value[7:].strip()


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        _json(self, 204, {})

    def do_GET(self):
        token = _bearer(self)
        if not token:
            _json(self, 401, {"error": "Authentication required."})
            return

        supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
        anon_key = os.getenv("SUPABASE_ANON_KEY", "").strip()
        service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        if not supabase_url or not anon_key or not service_key:
            _json(self, 503, {"error": "Subscription service is not configured."})
            return

        # Validate the user's access token with Supabase Auth.
        code, user = _request(
            supabase_url + "/auth/v1/user",
            {"apikey": anon_key, "Authorization": "Bearer " + token, "Accept": "application/json"},
        )
        if code != 200 or not isinstance(user, dict) or not user.get("id"):
            _json(self, 401, {"error": "Session is invalid or expired."})
            return

        user_id = str(user["id"])
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        query = (
            supabase_url
            + "/rest/v1/subscriptions?"
            + urllib.parse.urlencode({
                "user_id": "eq." + user_id,
                "status": "eq.active",
                "expires_at": "gt." + now,
                "select": "plan,amount_ugx,duration_months,starts_at,expires_at,status",
                "order": "expires_at.desc",
                "limit": "1",
            })
        )
        code, rows = _request(query, _headers(service=True))
        if code != 200 or not isinstance(rows, list):
            _json(self, 503, {"error": "Subscription status could not be read."})
            return

        subscription = rows[0] if rows else None
        _json(self, 200, {
            "active": subscription is not None,
            "subscription": subscription,
        })
