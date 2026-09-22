import json
import os
import urllib.error
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler

from api.user_security import (
    get_supabase_anon_key,
    get_supabase_service_key,
    get_supabase_url,
    is_owner_user,
    verify_access_token,
)


def json_response(handler, status_code, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except Exception:
        pass


def _request(url, method="GET", payload=None, headers=None, timeout=20):
    body = None
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request_headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        url,
        data=body,
        headers=request_headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except Exception:
            data = {"error": raw}
        raise RuntimeError(json.dumps(data, ensure_ascii=False)) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("The authentication service is temporarily unavailable.") from exc


def _service_headers():
    key = get_supabase_service_key()
    return {
        "apikey": key,
        "Authorization": "Bearer " + key,
    }


def _anon_headers():
    key = get_supabase_anon_key()
    return {
        "apikey": key,
    }


def _profile_row(user_id):
    url = (
        get_supabase_url()
        + "/rest/v1/profiles?id=eq."
        + urllib.parse.quote(str(user_id), safe="")
        + "&select=id,full_name,email,daily_analysis_count,last_analysis_date,created_at"
        + "&limit=1"
    )
    data = _request(url, headers=_service_headers())
    if isinstance(data, list) and data:
        return data[0]
    return None


def _ensure_profile(user):
    user_id = str(user.get("id", "")).strip()
    email = str(user.get("email", "")).strip().lower()
    if not user_id or not email:
        raise RuntimeError("The authenticated user is incomplete.")

    existing = _profile_row(user_id)
    if existing:
        return existing

    metadata = user.get("user_metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    full_name = str(metadata.get("full_name", "")).strip() or email.split("@", 1)[0]

    url = get_supabase_url() + "/rest/v1/profiles"
    payload = {
        "id": user_id,
        "full_name": full_name,
        "email": email,
        "daily_analysis_count": 0,
    }
    headers = _service_headers()
    headers["Prefer"] = "return=representation"
    data = _request(url, method="POST", payload=payload, headers=headers)
    if isinstance(data, list) and data:
        return data[0]
    return payload


def _profile_payload(user, profile):
    owner = is_owner_user(user)
    count = int(profile.get("daily_analysis_count", 0) or 0)
    return {
        "user": {
            "id": str(user.get("id", "")),
            "email": str(user.get("email", "")),
            "full_name": str(profile.get("full_name", "")),
        },
        "profile": profile,
        "is_owner": owner,
        "daily_analysis_count": count,
        "daily_limit": None if owner else 4,
        "remaining": None if owner else max(0, 4 - count),
    }


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        json_response(self, 204, {})

    def do_GET(self):
        try:
            user = verify_access_token(self.headers.get("Authorization", "").split(" ", 1)[1] if " " in str(self.headers.get("Authorization", "")) else "")
            profile = _ensure_profile(user)
            json_response(self, 200, _profile_payload(user, profile))
        except ValueError as exc:
            json_response(self, 401, {"error": str(exc)})
        except RuntimeError as exc:
            json_response(self, 503, {"error": str(exc)})
        except Exception:
            json_response(self, 500, {"error": "Profile request failed."})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 128 * 1024:
                json_response(self, 400, {"error": "Invalid request."})
                return
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            action = str(data.get("action", "")).strip().lower()

            if action == "signup":
                email = str(data.get("email", "")).strip().lower()
                password = str(data.get("password", ""))
                full_name = str(data.get("full_name", "")).strip()
                confirm_password = str(data.get("confirm_password", ""))

                if not full_name:
                    raise ValueError("Full name is required.")
                if not email or "@" not in email:
                    raise ValueError("A valid email is required.")
                if len(password) < 6:
                    raise ValueError("Password must contain at least 6 characters.")
                if password != confirm_password:
                    raise ValueError("Passwords do not match.")

                signup_url = get_supabase_url() + "/auth/v1/signup"
                user = _request(
                    signup_url,
                    method="POST",
                    payload={
                        "email": email,
                        "password": password,
                        "data": {"full_name": full_name},
                    },
                    headers=_anon_headers(),
                )

                returned_user = user.get("user") if isinstance(user, dict) else None
                if isinstance(returned_user, dict) and returned_user.get("id"):
                    _ensure_profile(returned_user)

                session = user.get("session") if isinstance(user, dict) else None
                if isinstance(session, dict) and session.get("access_token"):
                    json_response(
                        self,
                        200,
                        {
                            "user": returned_user,
                            "access_token": session.get("access_token"),
                            "refresh_token": session.get("refresh_token", ""),
                        },
                    )
                else:
                    json_response(
                        self,
                        200,
                        {
                            "message": "Account created. Please log in.",
                            "user": returned_user,
                        },
                    )
                return

            if action == "login":
                email = str(data.get("email", "")).strip().lower()
                password = str(data.get("password", ""))
                if not email or not password:
                    raise ValueError("Email and password are required.")

                login_url = get_supabase_url() + "/auth/v1/token?grant_type=password"
                session = _request(
                    login_url,
                    method="POST",
                    payload={"email": email, "password": password},
                    headers=_anon_headers(),
                )
                user = session.get("user")
                if not isinstance(user, dict) or not user.get("id"):
                    raise RuntimeError("Login succeeded without a valid user session.")
                _ensure_profile(user)

                json_response(
                    self,
                    200,
                    {
                        "user": user,
                        "access_token": session.get("access_token", ""),
                        "refresh_token": session.get("refresh_token", ""),
                        "expires_in": session.get("expires_in"),
                    },
                )
                return

            if action == "refresh":
                refresh_token = str(data.get("refresh_token", "")).strip()
                if not refresh_token:
                    raise ValueError("Refresh token is required.")

                refresh_url = get_supabase_url() + "/auth/v1/token?grant_type=refresh_token"
                session = _request(
                    refresh_url,
                    method="POST",
                    payload={"refresh_token": refresh_token},
                    headers=_anon_headers(),
                )
                user = session.get("user")
                if isinstance(user, dict) and user.get("id"):
                    _ensure_profile(user)

                json_response(
                    self,
                    200,
                    {
                        "user": user,
                        "access_token": session.get("access_token", ""),
                        "refresh_token": session.get("refresh_token", refresh_token),
                        "expires_in": session.get("expires_in"),
                    },
                )
                return

            if action == "profile":
                auth = str(self.headers.get("Authorization", ""))
                token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
                user = verify_access_token(token)
                profile = _ensure_profile(user)
                json_response(self, 200, _profile_payload(user, profile))
                return

            raise ValueError("Unknown authentication action.")

        except json.JSONDecodeError:
            json_response(self, 400, {"error": "Invalid request JSON."})
        except ValueError as exc:
            json_response(self, 400, {"error": str(exc)})
        except RuntimeError as exc:
            message = str(exc)
            if "Invalid or expired session" in message:
                json_response(self, 401, {"error": message})
            else:
                json_response(self, 503, {"error": message})
        except Exception:
            json_response(self, 500, {"error": "Authentication request failed."})
