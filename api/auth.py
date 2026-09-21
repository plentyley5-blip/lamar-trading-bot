import json
import os
import urllib.error
import urllib.request

from http.server import BaseHTTPRequestHandler


def get_supabase_url():
    value = os.environ.get("SUPABASE_URL", "").strip()

    if not value:
        raise RuntimeError(
            "SUPABASE_URL is missing from Vercel Production."
        )

    return value.rstrip("/")


def get_supabase_service_key():
    value = os.environ.get(
        "SUPABASE_SERVICE_ROLE_KEY",
        ""
    ).strip()

    if not value:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY is missing from Vercel Production."
        )

    return value


def supabase_request(
    method,
    path,
    body=None,
    authorization=None
):
    url = get_supabase_url() + path

    headers = {
        "apikey": get_supabase_service_key(),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    if authorization:
        headers["Authorization"] = authorization

    data = None

    if body is not None:
        data = json.dumps(body).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers=headers
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=20
        ) as response:

            raw = response.read().decode(
                "utf-8"
            )

            if not raw:
                return response.status, {}

            return response.status, json.loads(raw)

    except urllib.error.HTTPError as exc:

        raw = exc.read().decode(
            "utf-8",
            errors="replace"
        )

        try:
            data = json.loads(raw)
        except Exception:
            data = {
                "message": raw
            }

        return exc.code, data


def validate_email(value):

    email = str(value or "").strip().lower()

    if not email:
        raise ValueError(
            "Email is required."
        )

    if "@" not in email or "." not in email.split("@")[-1]:
        raise ValueError(
            "Please enter a valid email address."
        )

    if len(email) > 254:
        raise ValueError(
            "Email address is too long."
        )

    return email


def validate_password(value):

    password = str(value or "")

    if len(password) < 8:
        raise ValueError(
            "Password must be at least 8 characters."
        )

    if len(password) > 128:
        raise ValueError(
            "Password is too long."
        )

    return password


def validate_full_name(value):

    name = " ".join(
        str(value or "").strip().split()
    )

    if len(name) < 2:
        raise ValueError(
            "Please enter your full name."
        )

    if len(name) > 100:
        raise ValueError(
            "Full name is too long."
        )

    return name


def create_profile(
    user_id,
    full_name,
    email
):

    url = (
        get_supabase_url()
        + "/rest/v1/profiles"
    )

    body = json.dumps({
        "id": user_id,
        "full_name": full_name,
        "email": email
    }).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "apikey":
                get_supabase_service_key(),
            "Authorization":
                "Bearer "
                + get_supabase_service_key(),
            "Content-Type":
                "application/json",
            "Prefer":
                "resolution=merge-duplicates",
            "Accept":
                "application/json"
        }
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=20
        ) as response:

            response.read()

    except Exception:
        pass


def extract_bearer_token(headers):

    value = headers.get(
        "Authorization",
        ""
    ).strip()

    if not value:
        raise ValueError(
            "Authorization header is required."
        )

    parts = value.split(None, 1)

    if len(parts) != 2:
        raise ValueError(
            "Invalid Authorization header."
        )

    if parts[0].lower() != "bearer":
        raise ValueError(
            "Authorization must use Bearer token."
        )

    token = parts[1].strip()

    if not token:
        raise ValueError(
            "Authorization token is empty."
        )

    return token


def get_authenticated_user(token):

    status, data = supabase_request(
        "GET",
        "/auth/v1/user",
        authorization="Bearer " + token
    )

    if status != 200:

        raise ValueError(
            "Your login session is invalid or expired."
        )

    user_id = str(
        data.get("id", "")
    ).strip()

    if not user_id:
        raise ValueError(
            "Your account could not be identified."
        )

    return data


def get_profile(user_id):

    path = (
        "/rest/v1/profiles"
        "?id=eq."
        + user_id
        + "&select=id,full_name,email,"
          "daily_analysis_count,last_analysis_date"
    )

    status, data = supabase_request(
        "GET",
        path
    )

    if status != 200:
        raise RuntimeError(
            "Unable to load your profile."
        )

    if not isinstance(data, list) or not data:

        raise RuntimeError(
            "Your account profile was not found."
        )

    profile = data[0]

    from datetime import date

    today = date.today().isoformat()

    last_date = str(
        profile.get(
            "last_analysis_date",
            ""
        )
    )

    count = int(
        profile.get(
            "daily_analysis_count",
            0
        )
    )

    if last_date != today:
        count = 0

    return {
        "full_name": str(
            profile.get(
                "full_name",
                ""
            )
        ),
        "email": str(
            profile.get(
                "email",
                ""
            )
        ),
        "analyses_today": count,
        "daily_limit": 4
    }


def signup(
    email,
    password,
    full_name
):

    email = validate_email(email)
    password = validate_password(password)
    full_name = validate_full_name(full_name)

    status, data = supabase_request(
        "POST",
        "/auth/v1/signup",
        body={
            "email": email,
            "password": password,
            "data": {
                "full_name": full_name
            }
        }
    )

    if status not in (200, 201):

        message = (
            data.get("msg")
            or data.get("message")
            or data.get("error_description")
            or data.get("error")
            or "Unable to create account."
        )

        raise ValueError(
            str(message)
        )

    user = data.get("user") or {}

    user_id = str(
        user.get("id", "")
    ).strip()

    if user_id:
        create_profile(
            user_id,
            full_name,
            email
        )

    session = data.get(
        "session"
    )

    if not session:

        return {
            "status": "verification_required",
            "message":
                "Account created. Please verify your email before logging in."
        }

    access_token = str(
        session.get(
            "access_token",
            ""
        )
    )

    refresh_token = str(
        session.get(
            "refresh_token",
            ""
        )
    )

    profile = get_profile(
        user_id
    )

    return {
        "status": "success",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "user": {
            "id": user_id,
            "email": email,
            "full_name": full_name
        },
        "profile": profile
    }


def login(
    email,
    password
):

    email = validate_email(email)
    password = validate_password(password)

    status, data = supabase_request(
        "POST",
        "/auth/v1/token?grant_type=password",
        body={
            "email": email,
            "password": password
        }
    )

    if status != 200:

        message = (
            data.get("msg")
            or data.get("message")
            or data.get("error_description")
            or "Incorrect email or password."
        )

        raise ValueError(
            str(message)
        )

    access_token = str(
        data.get(
            "access_token",
            ""
        )
    )

    refresh_token = str(
        data.get(
            "refresh_token",
            ""
        )
    )

    user = data.get(
        "user"
    ) or {}

    user_id = str(
        user.get(
            "id",
            ""
        )
    ).strip()

    if not access_token or not user_id:
        raise ValueError(
            "Login succeeded but no valid session was returned."
        )

    profile = get_profile(
        user_id
    )

    return {
        "status": "success",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "user": {
            "id": user_id,
            "email": str(
                user.get(
                    "email",
                    email
                )
            ),
            "full_name":
                profile.get(
                    "full_name",
                    ""
                )
        },
        "profile": profile
    }


def refresh_session(
    refresh_token
):

    refresh_token = str(
        refresh_token or ""
    ).strip()

    if not refresh_token:
        raise ValueError(
            "Refresh token is required."
        )

    status, data = supabase_request(
        "POST",
        "/auth/v1/token?grant_type=refresh_token",
        body={
            "refresh_token":
                refresh_token
        }
    )

    if status != 200:

        raise ValueError(
            "Your session has expired. Please log in again."
        )

    access_token = str(
        data.get(
            "access_token",
            ""
        )
    )

    new_refresh_token = str(
        data.get(
            "refresh_token",
            refresh_token
        )
    )

    user = data.get(
        "user"
    ) or {}

    user_id = str(
        user.get(
            "id",
            ""
        )
    ).strip()

    if not access_token or not user_id:
        raise ValueError(
            "Unable to refresh your session."
        )

    profile = get_profile(
        user_id
    )

    return {
        "status": "success",
        "access_token": access_token,
        "refresh_token":
            new_refresh_token,
        "user": {
            "id": user_id,
            "email": str(
                user.get(
                    "email",
                    ""
                )
            ),
            "full_name":
                profile.get(
                    "full_name",
                    ""
                )
        },
        "profile": profile
    }


class handler(BaseHTTPRequestHandler):

    def send_json(
        self,
        status_code,
        data
    ):

        body = json.dumps(
            data,
            ensure_ascii=False
        ).encode("utf-8")

        self.send_response(
            status_code
        )

        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8"
        )

        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
        )

        self.send_header(
            "Access-Control-Allow-Methods",
            "POST, OPTIONS"
        )

        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization"
        )

        self.send_header(
            "Cache-Control",
            "no-store"
        )

        self.send_header(
            "Content-Length",
            str(len(body))
        )

        self.end_headers()

        self.wfile.write(body)

    def do_OPTIONS(self):

        self.send_response(204)

        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
        )

        self.send_header(
            "Access-Control-Allow-Methods",
            "POST, OPTIONS"
        )

        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization"
        )

        self.end_headers()

    def do_POST(self):

        try:

            content_length = int(
                self.headers.get(
                    "Content-Length",
                    "0"
                )
            )

            raw = self.rfile.read(
                content_length
            )

            body = json.loads(
                raw.decode("utf-8")
            )

            action = str(
                body.get(
                    "action",
                    ""
                )
            ).strip().lower()

            if action == "signup":

                result = signup(
                    email=body.get("email"),
                    password=body.get("password"),
                    full_name=body.get("full_name")
                )

                self.send_json(
                    200,
                    result
                )

                return

            if action == "login":

                result = login(
                    email=body.get("email"),
                    password=body.get("password")
                )

                self.send_json(
                    200,
                    result
                )

                return

            if action == "refresh":

                result = refresh_session(
                    body.get(
                        "refresh_token"
                    )
                )

                self.send_json(
                    200,
                    result
                )

                return

            if action == "profile":

                token = extract_bearer_token(
                    self.headers
                )

                user = get_authenticated_user(
                    token
                )

                user_id = str(
                    user.get(
                        "id",
                        ""
                    )
                ).strip()

                profile = get_profile(
                    user_id
                )

                self.send_json(
                    200,
                    {
                        "status": "success",
                        "user": {
                            "id": user_id,
                            "email": str(
                                user.get(
                                    "email",
                                    ""
                                )
                            )
                        },
                        "profile": profile
                    }
                )

                return

            raise ValueError(
                "Invalid authentication action."
            )

        except Exception as exc:

            self.send_json(
                400,
                {
                    "status": "failed",
                    "error": str(exc)
                }
            )
