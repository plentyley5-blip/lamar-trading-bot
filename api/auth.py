import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, Optional


# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------

def get_supabase_url() -> str:
    url = os.environ.get("SUPABASE_URL", "").strip()

    if not url:
        raise RuntimeError("SUPABASE_URL is not configured.")

    # Remove accidental trailing slashes.
    url = url.rstrip("/")

    # Prevent common configuration mistakes.
    for suffix in (
        "/rest/v1",
        "/auth/v1",
        "/storage/v1",
    ):
        if url.endswith(suffix):
            url = url[: -len(suffix)]

    return url.rstrip("/")


def get_supabase_service_key() -> str:
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()

    if not key:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY is not configured."
        )

    return key


# ---------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------

def supabase_request(
    method: str,
    path: str,
    body: Optional[Dict[str, Any]] = None,
    access_token: Optional[str] = None,
):
    base_url = get_supabase_url()

    # Always make sure the path begins with exactly one slash.
    path = "/" + path.lstrip("/")

    url = base_url + path

    headers = {
        "apikey": get_supabase_service_key(),
        "Content-Type": "application/json",
    }

    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"

    data = None

    if body is not None:
        data = json.dumps(body).encode("utf-8")

    request = urllib.request.Request(
        url=url,
        data=data,
        headers=headers,
        method=method.upper(),
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response_body = response.read().decode("utf-8")

            try:
                parsed = json.loads(response_body)
            except json.JSONDecodeError:
                parsed = response_body

            return response.status, parsed

    except urllib.error.HTTPError as error:
        error_body = error.read().decode("utf-8")

        try:
            parsed = json.loads(error_body)
        except json.JSONDecodeError:
            parsed = {
                "error": error_body or "Supabase request failed."
            }

        return error.code, parsed

    except urllib.error.URLError as error:
        return 503, {
            "error": f"Unable to reach Supabase: {error.reason}"
        }

    except Exception as error:
        return 500, {
            "error": f"Supabase request failed: {str(error)}"
        }


# ---------------------------------------------------------
# Response helpers
# ---------------------------------------------------------

def json_response(
    body: Dict[str, Any],
    status: int = 200,
):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        },
        "body": json.dumps(body),
    }


# ---------------------------------------------------------
# Validation
# ---------------------------------------------------------

def validate_email(email: str) -> Optional[str]:
    email = email.strip()

    if not email:
        return "Email is required."

    if "@" not in email or "." not in email.split("@")[-1]:
        return "Please enter a valid email address."

    return None


def validate_password(password: str) -> Optional[str]:
    if not password:
        return "Password is required."

    if len(password) < 6:
        return "Password must be at least 6 characters."

    return None


# ---------------------------------------------------------
# Authentication helpers
# ---------------------------------------------------------

def extract_bearer_token(request) -> Optional[str]:
    headers = getattr(request, "headers", {}) or {}

    authorization = (
        headers.get("Authorization")
        or headers.get("authorization")
        or ""
    ).strip()

    if not authorization:
        return None

    if not authorization.lower().startswith("bearer "):
        return None

    token = authorization[7:].strip()

    return token if token else None


def get_authenticated_user(access_token: str):
    status, result = supabase_request(
        "GET",
        "/auth/v1/user",
        access_token=access_token,
    )

    if status != 200:
        return None

    if not isinstance(result, dict):
        return None

    return result


# ---------------------------------------------------------
# Profiles
# ---------------------------------------------------------

def get_profile(user_id: str):
    status, result = supabase_request(
        "GET",
        f"/rest/v1/profiles?id=eq.{user_id}&select=*",
    )

    if status != 200:
        return None

    if not isinstance(result, list):
        return None

    if not result:
        return None

    return result[0]


def create_profile(
    user_id: str,
    full_name: str,
    email: str,
):
    body = {
        "id": user_id,
        "full_name": full_name.strip(),
        "email": email.strip().lower(),
    }

    status, result = supabase_request(
        "POST",
        "/rest/v1/profiles",
        body=body,
    )

    if status not in (200, 201):
        return False, result

    return True, result


# ---------------------------------------------------------
# Signup
# ---------------------------------------------------------

def signup(
    full_name: str,
    email: str,
    password: str,
):
    full_name = full_name.strip()
    email = email.strip().lower()

    if not full_name:
        return json_response(
            {"error": "Full name is required."},
            400,
        )

    email_error = validate_email(email)

    if email_error:
        return json_response(
            {"error": email_error},
            400,
        )

    password_error = validate_password(password)

    if password_error:
        return json_response(
            {"error": password_error},
            400,
        )

    # Create the Supabase Auth account.
    status, result = supabase_request(
        "POST",
        "/auth/v1/signup",
        body={
            "email": email,
            "password": password,
            "data": {
                "full_name": full_name,
            },
        },
    )

    if status not in (200, 201):
        error_message = "Account creation failed."

        if isinstance(result, dict):
            error_message = (
                result.get("msg")
                or result.get("message")
                or result.get("error_description")
                or result.get("error")
                or error_message
            )

        return json_response(
            {"error": error_message},
            status,
        )

    if not isinstance(result, dict):
        return json_response(
            {"error": "Invalid response from authentication server."},
            500,
        )

    user = result.get("user") or {}

    user_id = user.get("id")

    if not user_id:
        return json_response(
            {
                "error": (
                    "Account was created, but the user ID "
                    "was not returned."
                )
            },
            500,
        )

    # Create the application profile.
    profile_ok, profile_result = create_profile(
        user_id=user_id,
        full_name=full_name,
        email=email,
    )

    if not profile_ok:
        return json_response(
            {
                "error": (
                    "Account was created, but the profile "
                    "could not be created."
                ),
                "details": profile_result,
            },
            500,
        )

    access_token = result.get("access_token")
    refresh_token = result.get("refresh_token")

    return json_response(
        {
            "success": True,
            "message": "Account created successfully.",
            "user": {
                "id": user_id,
                "email": email,
                "full_name": full_name,
            },
            "access_token": access_token,
            "refresh_token": refresh_token,
            "session": {
                "access_token": access_token,
                "refresh_token": refresh_token,
            }
            if access_token
            else None,
        },
        200,
    )


# ---------------------------------------------------------
# Login
# ---------------------------------------------------------

def login(
    email: str,
    password: str,
):
    email = email.strip().lower()

    email_error = validate_email(email)

    if email_error:
        return json_response(
            {"error": email_error},
            400,
        )

    if not password:
        return json_response(
            {"error": "Password is required."},
            400,
        )

    status, result = supabase_request(
        "POST",
        "/auth/v1/token?grant_type=password",
        body={
            "email": email,
            "password": password,
        },
    )

    if status != 200:
        error_message = "Login failed."

        if isinstance(result, dict):
            error_message = (
                result.get("msg")
                or result.get("message")
                or result.get("error_description")
                or result.get("error")
                or error_message
            )

        return json_response(
            {"error": error_message},
            status,
        )

    user = result.get("user") or {}

    user_id = user.get("id")

    if not user_id:
        return json_response(
            {"error": "Login succeeded but user information is missing."},
            500,
        )

    profile = get_profile(user_id)

    full_name = ""

    if profile:
        full_name = profile.get("full_name", "")

    return json_response(
        {
            "success": True,
            "message": "Login successful.",
            "user": {
                "id": user_id,
                "email": user.get("email", email),
                "full_name": full_name,
            },
            "access_token": result.get("access_token"),
            "refresh_token": result.get("refresh_token"),
            "expires_in": result.get("expires_in"),
            "token_type": result.get("token_type"),
            "session": {
                "access_token": result.get("access_token"),
                "refresh_token": result.get("refresh_token"),
            },
        },
        200,
    )


# ---------------------------------------------------------
# Refresh session
# ---------------------------------------------------------

def refresh_session(refresh_token: str):
    if not refresh_token:
        return json_response(
            {"error": "Refresh token is required."},
            400,
        )

    status, result = supabase_request(
        "POST",
        "/auth/v1/token?grant_type=refresh_token",
        body={
            "refresh_token": refresh_token,
        },
    )

    if status != 200:
        error_message = "Session refresh failed."

        if isinstance(result, dict):
            error_message = (
                result.get("msg")
                or result.get("message")
                or result.get("error_description")
                or result.get("error")
                or error_message
            )

        return json_response(
            {"error": error_message},
            status,
        )

    user = result.get("user") or {}
    user_id = user.get("id")

    full_name = ""

    if user_id:
        profile = get_profile(user_id)

        if profile:
            full_name = profile.get("full_name", "")

    return json_response(
        {
            "success": True,
            "access_token": result.get("access_token"),
            "refresh_token": result.get("refresh_token"),
            "expires_in": result.get("expires_in"),
            "user": {
                "id": user_id,
                "email": user.get("email", ""),
                "full_name": full_name,
            },
        },
        200,
    )


# ---------------------------------------------------------
# Profile endpoint
# ---------------------------------------------------------

def profile(request):
    access_token = extract_bearer_token(request)

    if not access_token:
        return json_response(
            {"error": "Authorization token is required."},
            401,
        )

    user = get_authenticated_user(access_token)

    if not user:
        return json_response(
            {"error": "Invalid or expired session."},
            401,
        )

    user_id = user.get("id")

    if not user_id:
        return json_response(
            {"error": "User information is missing."},
            401,
        )

    profile_data = get_profile(user_id)

    if not profile_data:
        return json_response(
            {"error": "User profile not found."},
            404,
        )

    return json_response(
        {
            "success": True,
            "user": {
                "id": user_id,
                "email": user.get("email", ""),
                "full_name": profile_data.get("full_name", ""),
            },
            "profile": profile_data,
        },
        200,
    )


# ---------------------------------------------------------
# Request body
# ---------------------------------------------------------

def get_json_body(request):
    try:
        body = request.get_json(silent=True)

        if isinstance(body, dict):
            return body
    except Exception:
        pass

    try:
        raw_body = request.body

        if isinstance(raw_body, bytes):
            raw_body = raw_body.decode("utf-8")

        if isinstance(raw_body, str) and raw_body.strip():
            parsed = json.loads(raw_body)

            if isinstance(parsed, dict):
                return parsed
    except Exception:
        pass

    return {}


# ---------------------------------------------------------
# Main Vercel handler
# ---------------------------------------------------------

def handler(request):
    method = getattr(request, "method", "GET").upper()

    # CORS preflight.
    if method == "OPTIONS":
        return json_response(
            {"success": True},
            200,
        )

    body = get_json_body(request)

    # Also support action through query string.
    action = body.get("action")

    if not action:
        try:
            action = request.args.get("action")
        except Exception:
            action = None

    action = (action or "").strip().lower()

    try:
        if action == "signup":
            return signup(
                full_name=str(body.get("full_name", "")),
                email=str(body.get("email", "")),
                password=str(body.get("password", "")),
            )

        if action == "login":
            return login(
                email=str(body.get("email", "")),
                password=str(body.get("password", "")),
            )

        if action == "refresh":
            return refresh_session(
                refresh_token=str(
                    body.get("refresh_token", "")
                )
            )

        if action == "profile":
            return profile(request)

        return json_response(
            {
                "error": (
                    "Invalid action. "
                    "Use signup, login, refresh, or profile."
                )
            },
            400,
        )

    except Exception as error:
        return json_response(
            {
                "error": "Authentication server error.",
                "details": str(error),
            },
            500,
        )
