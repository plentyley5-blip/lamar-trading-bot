from http.server import BaseHTTPRequestHandler
import json
import os
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse
from typing import Any, Dict, Optional


# =========================================================
# SUPABASE CONFIGURATION
# =========================================================

def get_supabase_url() -> str:
    url = os.environ.get("SUPABASE_URL", "").strip()

    if not url:
        raise RuntimeError("SUPABASE_URL is not configured.")

    url = url.rstrip("/")

    for suffix in (
        "/rest/v1",
        "/auth/v1",
        "/storage/v1",
    ):
        if url.endswith(suffix):
            url = url[:-len(suffix)]

    return url.rstrip("/")


def get_supabase_service_key() -> str:
    key = os.environ.get(
        "SUPABASE_SERVICE_ROLE_KEY",
        ""
    ).strip()

    if not key:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY is not configured."
        )

    return key


# =========================================================
# SUPABASE HTTP REQUEST
# =========================================================

def supabase_request(
    method: str,
    path: str,
    body: Optional[Dict[str, Any]] = None,
    access_token: Optional[str] = None,
):
    base_url = get_supabase_url()

    path = "/" + path.lstrip("/")

    url = base_url + path

    headers = {
        "apikey": get_supabase_service_key(),
        "Content-Type": "application/json",
    }

    if access_token:
        headers["Authorization"] = (
            f"Bearer {access_token}"
        )

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
        with urllib.request.urlopen(
            request,
            timeout=30
        ) as response:

            response_body = (
                response.read()
                .decode("utf-8")
            )

            try:
                parsed = json.loads(
                    response_body
                )
            except json.JSONDecodeError:
                parsed = response_body

            return response.status, parsed

    except urllib.error.HTTPError as error:

        error_body = (
            error.read()
            .decode("utf-8")
        )

        try:
            parsed = json.loads(
                error_body
            )
        except json.JSONDecodeError:
            parsed = {
                "error": (
                    error_body
                    or "Supabase request failed."
                )
            }

        return error.code, parsed

    except urllib.error.URLError as error:

        return 503, {
            "error": (
                "Unable to reach Supabase: "
                f"{error.reason}"
            )
        }

    except Exception as error:

        return 500, {
            "error": (
                "Supabase request failed: "
                f"{str(error)}"
            )
        }


# =========================================================
# VALIDATION
# =========================================================

def validate_email(email: str) -> Optional[str]:

    email = email.strip()

    if not email:
        return "Email is required."

    if (
        "@" not in email
        or "." not in email.split("@")[-1]
    ):
        return "Please enter a valid email address."

    return None


def validate_password(
    password: str
) -> Optional[str]:

    if not password:
        return "Password is required."

    if len(password) < 6:
        return (
            "Password must be at least 6 characters."
        )

    return None


# =========================================================
# AUTHENTICATION
# =========================================================

def get_authenticated_user(
    access_token: str
):

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


# =========================================================
# PROFILE
# =========================================================

def get_profile(user_id: str):

    status, result = supabase_request(
        "GET",
        (
            "/rest/v1/profiles"
            f"?id=eq.{user_id}"
            "&select=*"
        ),
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


# =========================================================
# SIGNUP
# =========================================================

def signup(
    full_name: str,
    email: str,
    password: str,
):

    full_name = full_name.strip()
    email = email.strip().lower()

    if not full_name:
        return {
            "error": "Full name is required."
        }, 400

    email_error = validate_email(email)

    if email_error:
        return {
            "error": email_error
        }, 400

    password_error = validate_password(password)

    if password_error:
        return {
            "error": password_error
        }, 400

    status, result = supabase_request(
        "POST",
        "/auth/v1/signup",
        body={
            "email": email,
            "password": password,
            "data": {
                "full_name": full_name
            },
        },
    )

    if status not in (200, 201):

        error_message = (
            "Account creation failed."
        )

        if isinstance(result, dict):
            error_message = (
                result.get("msg")
                or result.get("message")
                or result.get(
                    "error_description"
                )
                or result.get("error")
                or error_message
            )

        return {
            "error": error_message
        }, status

    if not isinstance(result, dict):

        return {
            "error": (
                "Invalid response from "
                "authentication server."
            )
        }, 500

    user = result.get("user") or {}

    user_id = user.get("id")

    if not user_id:

        return {
            "error": (
                "Account was created, but "
                "the user ID was not returned."
            )
        }, 500

    profile_ok, profile_result = create_profile(
        user_id=user_id,
        full_name=full_name,
        email=email,
    )

    if not profile_ok:

        return {
            "error": (
                "Account was created, but "
                "the profile could not be created."
            ),
            "details": profile_result,
        }, 500

    access_token = result.get(
        "access_token"
    )

    refresh_token = result.get(
        "refresh_token"
    )

    return {
        "success": True,
        "message": (
            "Account created successfully."
        ),
        "user": {
            "id": user_id,
            "email": email,
            "full_name": full_name,
        },
        "access_token": access_token,
        "refresh_token": refresh_token,
        "session": (
            {
                "access_token": access_token,
                "refresh_token": refresh_token,
            }
            if access_token
            else None
        ),
    }, 200


# =========================================================
# LOGIN
# =========================================================

def login(
    email: str,
    password: str,
):

    email = email.strip().lower()

    email_error = validate_email(email)

    if email_error:
        return {
            "error": email_error
        }, 400

    if not password:
        return {
            "error": "Password is required."
        }, 400

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
                or result.get(
                    "error_description"
                )
                or result.get("error")
                or error_message
            )

        return {
            "error": error_message
        }, status

    user = result.get("user") or {}

    user_id = user.get("id")

    if not user_id:

        return {
            "error": (
                "Login succeeded but user "
                "information is missing."
            )
        }, 500

    profile = get_profile(user_id)

    full_name = ""

    if profile:
        full_name = profile.get(
            "full_name",
            ""
        )

    return {
        "success": True,
        "message": "Login successful.",
        "user": {
            "id": user_id,
            "email": user.get(
                "email",
                email
            ),
            "full_name": full_name,
        },
        "access_token": result.get(
            "access_token"
        ),
        "refresh_token": result.get(
            "refresh_token"
        ),
        "expires_in": result.get(
            "expires_in"
        ),
        "token_type": result.get(
            "token_type"
        ),
        "session": {
            "access_token": result.get(
                "access_token"
            ),
            "refresh_token": result.get(
                "refresh_token"
            ),
        },
    }, 200


# =========================================================
# REFRESH SESSION
# =========================================================

def refresh_session(
    refresh_token: str
):

    if not refresh_token:

        return {
            "error": (
                "Refresh token is required."
            )
        }, 400

    status, result = supabase_request(
        "POST",
        (
            "/auth/v1/token"
            "?grant_type=refresh_token"
        ),
        body={
            "refresh_token": refresh_token
        },
    )

    if status != 200:

        error_message = (
            "Session refresh failed."
        )

        if isinstance(result, dict):
            error_message = (
                result.get("msg")
                or result.get("message")
                or result.get(
                    "error_description"
                )
                or result.get("error")
                or error_message
            )

        return {
            "error": error_message
        }, status

    user = result.get("user") or {}

    user_id = user.get("id")

    full_name = ""

    if user_id:

        profile = get_profile(user_id)

        if profile:
            full_name = profile.get(
                "full_name",
                ""
            )

    return {
        "success": True,
        "access_token": result.get(
            "access_token"
        ),
        "refresh_token": result.get(
            "refresh_token"
        ),
        "expires_in": result.get(
            "expires_in"
        ),
        "user": {
            "id": user_id,
            "email": user.get(
                "email",
                ""
            ),
            "full_name": full_name,
        },
    }, 200


# =========================================================
# PROFILE ENDPOINT
# =========================================================

def profile(
    access_token: Optional[str]
):

    if not access_token:

        return {
            "error": (
                "Authorization token is required."
            )
        }, 401

    user = get_authenticated_user(
        access_token
    )

    if not user:

        return {
            "error": (
                "Invalid or expired session."
            )
        }, 401

    user_id = user.get("id")

    if not user_id:

        return {
            "error": (
                "User information is missing."
            )
        }, 401

    profile_data = get_profile(
        user_id
    )

    if not profile_data:

        return {
            "error": "User profile not found."
        }, 404

    return {
        "success": True,
        "user": {
            "id": user_id,
            "email": user.get(
                "email",
                ""
            ),
            "full_name": profile_data.get(
                "full_name",
                ""
            ),
        },
        "profile": profile_data,
    }, 200


# =========================================================
# HTTP HELPERS
# =========================================================

def read_request_body(handler):

    try:

        content_length = int(
            handler.headers.get(
                "Content-Length",
                "0"
            )
        )

    except (TypeError, ValueError):

        content_length = 0

    if content_length <= 0:
        return {}

    raw = handler.rfile.read(
        content_length
    )

    if not raw:
        return {}

    try:

        parsed = json.loads(
            raw.decode("utf-8")
        )

        if isinstance(parsed, dict):
            return parsed

    except Exception:
        pass

    return {}


def get_bearer_token(handler):

    authorization = (
        handler.headers.get(
            "Authorization",
            ""
        )
        .strip()
    )

    if not authorization:
        return None

    if not authorization.lower().startswith(
        "bearer "
    ):
        return None

    token = authorization[7:].strip()

    return token if token else None


def send_json(
    handler,
    body: Dict[str, Any],
    status: int = 200,
):

    payload = json.dumps(
        body
    ).encode("utf-8")

    handler.send_response(status)

    handler.send_header(
        "Content-Type",
        "application/json"
    )

    handler.send_header(
        "Access-Control-Allow-Origin",
        "*"
    )

    handler.send_header(
        "Access-Control-Allow-Headers",
        "Content-Type, Authorization"
    )

    handler.send_header(
        "Access-Control-Allow-Methods",
        "GET, POST, OPTIONS"
    )

    handler.send_header(
        "Cache-Control",
        "no-store"
    )

    handler.send_header(
        "Content-Length",
        str(len(payload))
    )

    handler.end_headers()

    handler.wfile.write(payload)


# =========================================================
# MAIN VERCEL HANDLER
# =========================================================

class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):

        self.send_response(204)

        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
        )

        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, POST, OPTIONS"
        )

        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization"
        )

        self.send_header(
            "Cache-Control",
            "no-store"
        )

        self.end_headers()


    def do_GET(self):

        try:

            parsed_url = urlparse(
                self.path
            )

            query = parse_qs(
                parsed_url.query
            )

            action = query.get(
                "action",
                [""]
            )[0].strip().lower()

            if action == "profile":

                access_token = (
                    get_bearer_token(self)
                )

                body, status = profile(
                    access_token
                )

                send_json(
                    self,
                    body,
                    status
                )

                return

            send_json(
                self,
                {
                    "error": (
                        "Invalid action. "
                        "Use signup, login, "
                        "refresh, or profile."
                    )
                },
                400
            )

        except Exception as error:

            send_json(
                self,
                {
                    "error": (
                        "Authentication "
                        "server error."
                    ),
                    "details": str(error)
                },
                500
            )


    def do_POST(self):

        try:

            body = read_request_body(
                self
            )

            action = str(
                body.get(
                    "action",
                    ""
                )
            ).strip().lower()

            if action == "signup":

                response_body, status = signup(
                    full_name=str(
                        body.get(
                            "full_name",
                            ""
                        )
                    ),
                    email=str(
                        body.get(
                            "email",
                            ""
                        )
                    ),
                    password=str(
                        body.get(
                            "password",
                            ""
                        )
                    ),
                )

                send_json(
                    self,
                    response_body,
                    status
                )

                return


            if action == "login":

                response_body, status = login(
                    email=str(
                        body.get(
                            "email",
                            ""
                        )
                    ),
                    password=str(
                        body.get(
                            "password",
                            ""
                        )
                    ),
                )

                send_json(
                    self,
                    response_body,
                    status
                )

                return


            if action == "refresh":

                response_body, status = (
                    refresh_session(
                        refresh_token=str(
                            body.get(
                                "refresh_token",
                                ""
                            )
                        )
                    )
                )

                send_json(
                    self,
                    response_body,
                    status
                )

                return


            if action == "profile":

                access_token = (
                    get_bearer_token(self)
                )

                response_body, status = (
                    profile(
                        access_token
                    )
                )

                send_json(
                    self,
                    response_body,
                    status
                )

                return


            send_json(
                self,
                {
                    "error": (
                        "Invalid action. "
                        "Use signup, login, "
                        "refresh, or profile."
                    )
                },
                400
            )


        except Exception as error:

            send_json(
                self,
                {
                    "error": (
                        "Authentication "
                        "server error."
                    ),
                    "details": str(error)
                },
                500
            )
