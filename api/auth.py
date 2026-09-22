import json
import os
import re
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs


# ============================================================
# CONFIGURATION
# ============================================================

def get_supabase_url():
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")

    # Protect against accidentally storing a REST/Auth endpoint
    # instead of the Supabase project URL.
    for suffix in (
        "/rest/v1",
        "/auth/v1",
        "/storage/v1",
    ):
        if url.endswith(suffix):
            url = url[:-len(suffix)]

    return url.rstrip("/")


def get_service_key():
    return os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()


# ============================================================
# HTTP HELPERS
# ============================================================

def send_json(handler, status_code, data):
    body = json.dumps(data).encode("utf-8")

    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()

    if handler.command != "HEAD":
        handler.wfile.write(body)


def read_json_body(handler):
    try:
        content_length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        content_length = 0

    if content_length <= 0:
        return {}

    raw = handler.rfile.read(content_length)

    if not raw:
        return {}

    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def supabase_request(method, endpoint, payload=None, headers=None):
    base_url = get_supabase_url()
    service_key = get_service_key()

    if not base_url:
        raise Exception("SUPABASE_URL is not configured.")

    if not service_key:
        raise Exception("SUPABASE_SERVICE_ROLE_KEY is not configured.")

    url = base_url + endpoint

    request_headers = {
        "apikey": service_key,
        "Authorization": "Bearer " + service_key,
        "Content-Type": "application/json",
    }

    if headers:
        request_headers.update(headers)

    body = None

    if payload is not None:
        body = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        url=url,
        data=body,
        headers=request_headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")

            if not raw:
                return response.status, {}

            try:
                return response.status, json.loads(raw)
            except Exception:
                return response.status, raw

    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")

        try:
            data = json.loads(raw)
        except Exception:
            data = {
                "error": raw
            }

        return error.code, data

    except Exception as error:
        raise Exception(str(error))


# ============================================================
# VALIDATION
# ============================================================

def validate_email(email):
    if not email:
        return False

    email = email.strip()

    if len(email) > 254:
        return False

    return re.match(
        r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
        email
    ) is not None


def validate_password(password):
    return isinstance(password, str) and len(password) >= 6


def clean_string(value):
    if not isinstance(value, str):
        return ""

    return value.strip()


# ============================================================
# PROFILE
# ============================================================

def create_or_update_profile(user_id, full_name, email):
    payload = {
        "id": user_id,
        "full_name": full_name,
        "email": email,
        "daily_analysis_count": 0,
    }

    status, data = supabase_request(
        "POST",
        "/rest/v1/profiles?on_conflict=id",
        payload,
        headers={
            "Prefer": "resolution=merge-duplicates,return=representation"
        },
    )

    if status >= 400:
        # Try updating an existing profile.
        update_payload = {
            "full_name": full_name,
            "email": email,
        }

        status2, data2 = supabase_request(
            "PATCH",
            "/rest/v1/profiles?id=eq." + user_id,
            update_payload,
            headers={
                "Prefer": "return=representation"
            },
        )

        if status2 >= 400:
            raise Exception(
                "Could not create or update the user profile."
            )

        return data2

    return data


def get_profile(user_id):
    status, data = supabase_request(
        "GET",
        "/rest/v1/profiles?id=eq." + user_id + "&select=*",
    )

    if status >= 400:
        raise Exception("Could not retrieve user profile.")

    if isinstance(data, list) and len(data) > 0:
        return data[0]

    return None


# ============================================================
# AUTH ACTIONS
# ============================================================

def signup(data):
    full_name = clean_string(data.get("full_name"))
    email = clean_string(data.get("email")).lower()
    password = data.get("password")

    if not full_name:
        return {
            "success": False,
            "message": "Full name is required."
        }, 400

    if len(full_name) > 100:
        return {
            "success": False,
            "message": "Full name is too long."
        }, 400

    if not validate_email(email):
        return {
            "success": False,
            "message": "Please enter a valid email address."
        }, 400

    if not validate_password(password):
        return {
            "success": False,
            "message": "Password must contain at least 6 characters."
        }, 400

    # Create Supabase Auth user.
    status, auth_data = supabase_request(
        "POST",
        "/auth/v1/signup",
        {
            "email": email,
            "password": password,
            "data": {
                "full_name": full_name
            }
        },
    )

    if status >= 400:
        message = "Could not create account."

        if isinstance(auth_data, dict):
            message = (
                auth_data.get("msg")
                or auth_data.get("message")
                or auth_data.get("error_description")
                or auth_data.get("error")
                or message
            )

        return {
            "success": False,
            "message": message
        }, status

    user = auth_data.get("user") if isinstance(auth_data, dict) else None
    session = auth_data.get("session") if isinstance(auth_data, dict) else None

    # Some Supabase configurations require email confirmation.
    # The user may therefore exist even when session is null.
    if not isinstance(user, dict):
        user = {}

    user_id = user.get("id", "")
    returned_email = user.get("email") or email

    if user_id:
        try:
            create_or_update_profile(
                user_id,
                full_name,
                returned_email,
            )
        except Exception as error:
            # Do not pretend signup failed if the Auth account was
            # already created successfully.
            print("Profile creation warning:", str(error))

    access_token = None
    refresh_token = None

    if isinstance(session, dict):
        access_token = session.get("access_token")
        refresh_token = session.get("refresh_token")

    # Account created but email confirmation is required.
    if not access_token:
        return {
            "success": True,
            "message": (
                "Account created successfully. "
                "Please check your email to confirm your account."
            ),
            "user": {
                "id": user_id,
                "email": returned_email,
                "full_name": full_name,
            },
            "access_token": None,
            "refresh_token": None,
            "session": None,
            "email_confirmation_required": True,
        }, 200

    return {
        "success": True,
        "message": "Account created successfully.",
        "user": {
            "id": user_id,
            "email": returned_email,
            "full_name": full_name,
        },
        "access_token": access_token,
        "refresh_token": refresh_token,
        "session": session,
        "email_confirmation_required": False,
    }, 200


def login(data):
    email = clean_string(data.get("email")).lower()
    password = data.get("password")

    if not validate_email(email):
        return {
            "success": False,
            "message": "Please enter a valid email address."
        }, 400

    if not isinstance(password, str) or not password:
        return {
            "success": False,
            "message": "Password is required."
        }, 400

    status, auth_data = supabase_request(
        "POST",
        "/auth/v1/token?grant_type=password",
        {
            "email": email,
            "password": password,
        },
    )

    if status >= 400:
        message = "Invalid email or password."

        if isinstance(auth_data, dict):
            message = (
                auth_data.get("msg")
                or auth_data.get("message")
                or auth_data.get("error_description")
                or message
            )

        return {
            "success": False,
            "message": message
        }, 401

    user = auth_data.get("user") if isinstance(auth_data, dict) else None
    session = auth_data if isinstance(auth_data, dict) else {}

    if not isinstance(user, dict):
        user = {}

    user_id = user.get("id", "")
    returned_email = user.get("email") or email

    full_name = ""

    if user_id:
        profile = get_profile(user_id)

        if isinstance(profile, dict):
            full_name = profile.get("full_name") or ""

        # If an older account has no profile, create one from
        # the Auth metadata.
        if not full_name:
            metadata = user.get("user_metadata")

            if isinstance(metadata, dict):
                full_name = (
                    metadata.get("full_name")
                    or metadata.get("name")
                    or ""
                )

        if not full_name:
            full_name = returned_email.split("@")[0]

        try:
            create_or_update_profile(
                user_id,
                full_name,
                returned_email,
            )
        except Exception as error:
            print("Profile update warning:", str(error))

    access_token = session.get("access_token")
    refresh_token = session.get("refresh_token")

    if not access_token:
        return {
            "success": False,
            "message": "Login succeeded but no access token was returned."
        }, 500

    return {
        "success": True,
        "message": "Login successful.",
        "user": {
            "id": user_id,
            "email": returned_email,
            "full_name": full_name,
        },
        "access_token": access_token,
        "refresh_token": refresh_token,
        "session": session,
    }, 200


def refresh(data):
    refresh_token = clean_string(data.get("refresh_token"))

    if not refresh_token:
        return {
            "success": False,
            "message": "Refresh token is required."
        }, 400

    status, auth_data = supabase_request(
        "POST",
        "/auth/v1/token?grant_type=refresh_token",
        {
            "refresh_token": refresh_token
        },
    )

    if status >= 400:
        return {
            "success": False,
            "message": "Session expired. Please log in again."
        }, 401

    user = auth_data.get("user") if isinstance(auth_data, dict) else None

    if not isinstance(user, dict):
        user = {}

    user_id = user.get("id", "")
    email = user.get("email", "")

    full_name = ""

    if user_id:
        profile = get_profile(user_id)

        if isinstance(profile, dict):
            full_name = profile.get("full_name") or ""

    return {
        "success": True,
        "message": "Session refreshed.",
        "user": {
            "id": user_id,
            "email": email,
            "full_name": full_name,
        },
        "access_token": auth_data.get("access_token"),
        "refresh_token": auth_data.get("refresh_token"),
        "session": auth_data,
    }, 200


def profile(handler):
    auth_header = handler.headers.get("Authorization", "")

    if not auth_header.startswith("Bearer "):
        return {
            "success": False,
            "message": "Authorization token is required."
        }, 401

    token = auth_header[7:].strip()

    if not token:
        return {
            "success": False,
            "message": "Authorization token is required."
        }, 401

    # Ask Supabase Auth who owns this access token.
    status, user_data = supabase_request(
        "GET",
        "/auth/v1/user",
        headers={
            "Authorization": "Bearer " + token,
        },
    )

    if status >= 400 or not isinstance(user_data, dict):
        return {
            "success": False,
            "message": "Invalid or expired session."
        }, 401

    user_id = user_data.get("id", "")
    email = user_data.get("email", "")

    if not user_id:
        return {
            "success": False,
            "message": "Invalid user session."
        }, 401

    profile_data = get_profile(user_id)

    if not isinstance(profile_data, dict):
        metadata = user_data.get("user_metadata")

        full_name = ""

        if isinstance(metadata, dict):
            full_name = (
                metadata.get("full_name")
                or metadata.get("name")
                or ""
            )

        if not full_name:
            full_name = email.split("@")[0]

        try:
            create_or_update_profile(
                user_id,
                full_name,
                email,
            )
        except Exception as error:
            print("Profile creation warning:", str(error))

        profile_data = {
            "id": user_id,
            "full_name": full_name,
            "email": email,
            "daily_analysis_count": 0,
        }

    return {
        "success": True,
        "user": {
            "id": user_id,
            "email": profile_data.get("email") or email,
            "full_name": profile_data.get("full_name") or "",
        },
        "profile": profile_data,
    }, 200


# ============================================================
# VERCEL FUNCTION
# ============================================================

class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        send_json(
            self,
            200,
            {
                "success": True
            }
        )

    def do_GET(self):
        self.process_request()

    def do_POST(self):
        self.process_request()

    def process_request(self):

        try:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)

            action = ""

            if "action" in query and query["action"]:
                action = query["action"][0].strip().lower()

            data = {}

            if self.command == "POST":
                body = read_json_body(self)

                if body is None:
                    send_json(
                        self,
                        400,
                        {
                            "success": False,
                            "message": "Invalid JSON request."
                        }
                    )
                    return

                if isinstance(body, dict):
                    data = body

                # Allow the action to be supplied inside JSON.
                if not action:
                    action = clean_string(
                        data.get("action")
                    ).lower()

            # GET /api/auth without an action should NOT be a 404.
            # It returns a useful API response so we can verify
            # that Vercel is actually running this function.
            if not action:
                send_json(
                    self,
                    200,
                    {
                        "success": False,
                        "message": (
                            "Invalid action. "
                            "Use signup, login, refresh, or profile."
                        )
                    }
                )
                return

            if action == "signup":
                result, status = signup(data)

            elif action == "login":
                result, status = login(data)

            elif action == "refresh":
                result, status = refresh(data)

            elif action == "profile":
                result, status = profile(self)

            else:
                result = {
                    "success": False,
                    "message": (
                        "Invalid action. "
                        "Use signup, login, refresh, or profile."
                    )
                }
                status = 400

            send_json(
                self,
                status,
                result
            )

        except Exception as error:
            print("AUTH FUNCTION ERROR:", str(error))

            send_json(
                self,
                500,
                {
                    "success": False,
                    "message": (
                        "Authentication server error. "
                        "Please try again."
                    )
                }
            )
