import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler


def supabase_url():
    value = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")

    for suffix in (
        "/rest/v1",
        "/auth/v1",
        "/storage/v1",
    ):
        if value.endswith(suffix):
            value = value[: -len(suffix)]

    return value


def supabase_key():
    return os.environ.get(
        "SUPABASE_SERVICE_ROLE_KEY",
        ""
    ).strip()


def request_json(
    method,
    url,
    body=None,
    headers=None,
):
    request_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    if headers:
        request_headers.update(headers)

    data = None

    if body is not None:
        data = json.dumps(body).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers=request_headers,
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=30,
        ) as response:

            raw = response.read().decode(
                "utf-8",
                errors="replace",
            )

            if not raw:
                return {}, response.status

            try:
                return json.loads(raw), response.status
            except json.JSONDecodeError:
                return {
                    "raw": raw
                }, response.status

    except urllib.error.HTTPError as exc:

        raw = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        try:
            payload = json.loads(raw)
        except Exception:
            payload = {
                "error": raw
            }

        return payload, exc.code

    except urllib.error.URLError as exc:

        raise RuntimeError(
            "Unable to connect to Supabase: "
            + str(exc.reason)
        )


def supabase_headers():
    key = supabase_key()

    if not key:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY is missing."
        )

    return {
        "apikey": key,
        "Authorization": "Bearer " + key,
    }


def json_response(
    handler,
    status,
    payload,
):
    body = json.dumps(
        payload,
        ensure_ascii=False,
    ).encode("utf-8")

    handler.send_response(status)

    handler.send_header(
        "Content-Type",
        "application/json; charset=utf-8",
    )

    handler.send_header(
        "Access-Control-Allow-Origin",
        "*",
    )

    handler.send_header(
        "Access-Control-Allow-Methods",
        "GET, POST, OPTIONS",
    )

    handler.send_header(
        "Access-Control-Allow-Headers",
        "Content-Type, Authorization",
    )

    handler.send_header(
        "Cache-Control",
        "no-store",
    )

    handler.send_header(
        "Content-Length",
        str(len(body)),
    )

    handler.end_headers()

    handler.wfile.write(body)


def read_body(handler):
    length = int(
        handler.headers.get(
            "Content-Length",
            "0",
        )
    )

    if length <= 0:
        return {}

    raw = handler.rfile.read(length)

    try:
        return json.loads(
            raw.decode("utf-8")
        )
    except Exception:
        raise ValueError(
            "Invalid JSON request."
        )


def get_user_from_token(access_token):
    url = (
        supabase_url()
        + "/auth/v1/user"
    )

    payload, status = request_json(
        "GET",
        url,
        headers={
            "apikey": supabase_key(),
            "Authorization": (
                "Bearer "
                + access_token
            ),
        },
    )

    if status != 200:
        return None

    if not isinstance(payload, dict):
        return None

    return payload


def create_profile(
    user_id,
    full_name,
    email,
):
    url = (
        supabase_url()
        + "/rest/v1/profiles"
    )

    payload = {
        "id": user_id,
        "full_name": full_name,
        "email": email,
        "daily_analysis_count": 0,
    }

    result, status = request_json(
        "POST",
        url,
        body=payload,
        headers={
            **supabase_headers(),
            "Prefer": "resolution=merge-duplicates",
        },
    )

    if status not in (200, 201, 204):
        raise RuntimeError(
            "Profile creation failed: "
            + json.dumps(result)
        )

    return result


def get_profile(user_id):
    url = (
        supabase_url()
        + "/rest/v1/profiles"
        + "?id=eq."
        + user_id
        + "&select=id,full_name,email,"
        + "daily_analysis_count,"
        + "last_analysis_date"
    )

    result, status = request_json(
        "GET",
        url,
        headers=supabase_headers(),
    )

    if status != 200:
        return None

    if not isinstance(result, list):
        return None

    if not result:
        return None

    return result[0]


def signup(
    full_name,
    email,
    password,
):
    if len(full_name) < 2:
        raise ValueError(
            "Please enter your full name."
        )

    if len(email) < 5 or "@" not in email:
        raise ValueError(
            "Please enter a valid email address."
        )

    if len(password) < 6:
        raise ValueError(
            "Password must be at least 6 characters."
        )

    url = (
        supabase_url()
        + "/auth/v1/signup"
    )

    result, status = request_json(
        "POST",
        url,
        body={
            "email": email,
            "password": password,
            "data": {
                "full_name": full_name,
            },
        },
        headers={
            "apikey": supabase_key(),
        },
    )

    if status not in (200, 201):
        message = (
            result.get("msg")
            or result.get("message")
            or result.get("error_description")
            or result.get("error")
            or "Account creation failed."
        )

        raise RuntimeError(
            str(message)
        )

    user = result.get("user")

    if not user:
        user = result

    user_id = user.get("id")

    if not user_id:
        raise RuntimeError(
            "Supabase did not return a user."
        )

    create_profile(
        user_id=user_id,
        full_name=full_name,
        email=email,
    )

    session = result.get("session")

    access_token = None
    refresh_token = None

    if isinstance(session, dict):
        access_token = session.get(
            "access_token"
        )
        refresh_token = session.get(
            "refresh_token"
        )

    return {
        "id": user_id,
        "email": email,
        "full_name": full_name,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "email_confirmation_required": (
            access_token is None
        ),
    }


def login(
    email,
    password,
):
    if not email or "@" not in email:
        raise ValueError(
            "Please enter a valid email address."
        )

    if not password:
        raise ValueError(
            "Please enter your password."
        )

    url = (
        supabase_url()
        + "/auth/v1/token"
        + "?grant_type=password"
    )

    result, status = request_json(
        "POST",
        url,
        body={
            "email": email,
            "password": password,
        },
        headers={
            "apikey": supabase_key(),
        },
    )

    if status != 200:
        message = (
            result.get("msg")
            or result.get("message")
            or result.get("error_description")
            or result.get("error")
            or "Login failed."
        )

        raise RuntimeError(
            str(message)
        )

    access_token = result.get(
        "access_token"
    )

    refresh_token = result.get(
        "refresh_token"
    )

    user = result.get("user")

    if not access_token or not user:
        raise RuntimeError(
            "Login succeeded but Supabase returned no session."
        )

    user_id = user.get("id")

    profile = None

    if user_id:
        profile = get_profile(
            user_id
        )

    if not profile:
        metadata = user.get(
            "user_metadata"
        ) or {}

        full_name = (
            metadata.get("full_name")
            or email.split("@")[0]
        )

        create_profile(
            user_id=user_id,
            full_name=full_name,
            email=email,
        )

        profile = get_profile(
            user_id
        )

    full_name = (
        profile.get("full_name")
        if profile
        else email.split("@")[0]
    )

    daily_count = (
        profile.get(
            "daily_analysis_count",
            0,
        )
        if profile
        else 0
    )

    return {
        "id": user_id,
        "email": email,
        "full_name": full_name,
        "daily_analysis_count": daily_count,
        "access_token": access_token,
        "refresh_token": refresh_token,
    }


def refresh(refresh_token):
    if not refresh_token:
        raise ValueError(
            "Refresh token is required."
        )

    url = (
        supabase_url()
        + "/auth/v1/token"
        + "?grant_type=refresh_token"
    )

    result, status = request_json(
        "POST",
        url,
        body={
            "refresh_token": refresh_token,
        },
        headers={
            "apikey": supabase_key(),
        },
    )

    if status != 200:
        message = (
            result.get("msg")
            or result.get("message")
            or result.get("error_description")
            or result.get("error")
            or "Session refresh failed."
        )

        raise RuntimeError(
            str(message)
        )

    return {
        "access_token": result.get(
            "access_token"
        ),
        "refresh_token": result.get(
            "refresh_token"
        ),
        "user": result.get("user"),
    }


def profile(access_token):
    if not access_token:
        raise ValueError(
            "Access token is required."
        )

    user = get_user_from_token(
        access_token
    )

    if not user:
        raise RuntimeError(
            "Your session has expired."
        )

    user_id = user.get("id")

    if not user_id:
        raise RuntimeError(
            "Invalid user session."
        )

    data = get_profile(
        user_id
    )

    if not data:
        metadata = user.get(
            "user_metadata"
        ) or {}

        full_name = (
            metadata.get("full_name")
            or user.get("email", "").split("@")[0]
        )

        create_profile(
            user_id=user_id,
            full_name=full_name,
            email=user.get("email", ""),
        )

        data = get_profile(
            user_id
        )

    return {
        "id": user_id,
        "email": user.get(
            "email",
            "",
        ),
        "full_name": (
            data.get("full_name")
            if data
            else user.get(
                "email",
                "",
            ).split("@")[0]
        ),
        "daily_analysis_count": (
            data.get(
                "daily_analysis_count",
                0,
            )
            if data
            else 0
        ),
    }


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        json_response(
            self,
            204,
            {},
        )

    def do_GET(self):
        json_response(
            self,
            200,
            {
                "success": True,
                "service": "LM Analyzer authentication",
                "message": "Authentication API is online.",
            },
        )

    def do_POST(self):

        try:
            body = read_body(self)

            if not isinstance(body, dict):
                raise ValueError(
                    "Request body must be JSON."
                )

            action = str(
                body.get("action", "")
            ).strip().lower()

            if action == "signup":

                result = signup(
                    full_name=str(
                        body.get(
                            "full_name",
                            "",
                        )
                    ).strip(),
                    email=str(
                        body.get(
                            "email",
                            "",
                        )
                    ).strip().lower(),
                    password=str(
                        body.get(
                            "password",
                            "",
                        )
                    ),
                )

                json_response(
                    self,
                    200,
                    {
                        "success": True,
                        "message": (
                            "Account created successfully."
                        ),
                        "user": {
                            "id": result["id"],
                            "email": result["email"],
                            "full_name": result["full_name"],
                        },
                        "access_token": result[
                            "access_token"
                        ],
                        "refresh_token": result[
                            "refresh_token"
                        ],
                        "email_confirmation_required": (
                            result[
                                "email_confirmation_required"
                            ]
                        ),
                    },
                )
                return

            if action == "login":

                result = login(
                    email=str(
                        body.get(
                            "email",
                            "",
                        )
                    ).strip().lower(),
                    password=str(
                        body.get(
                            "password",
                            "",
                        )
                    ),
                )

                json_response(
                    self,
                    200,
                    {
                        "success": True,
                        "message": "Login successful.",
                        "user": {
                            "id": result["id"],
                            "email": result["email"],
                            "full_name": result["full_name"],
                            "daily_analysis_count": result[
                                "daily_analysis_count"
                            ],
                        },
                        "access_token": result[
                            "access_token"
                        ],
                        "refresh_token": result[
                            "refresh_token"
                        ],
                    },
                )
                return

            if action == "refresh":

                result = refresh(
                    str(
                        body.get(
                            "refresh_token",
                            "",
                        )
                    ).strip()
                )

                json_response(
                    self,
                    200,
                    {
                        "success": True,
                        "access_token": result[
                            "access_token"
                        ],
                        "refresh_token": result[
                            "refresh_token"
                        ],
                        "user": result["user"],
                    },
                )
                return

            if action == "profile":

                token = str(
                    body.get(
                        "access_token",
                        "",
                    )
                ).strip()

                result = profile(
                    token
                )

                json_response(
                    self,
                    200,
                    {
                        "success": True,
                        "user": result,
                    },
                )
                return

            json_response(
                self,
                400,
                {
                    "success": False,
                    "error": (
                        "Invalid action. "
                        "Use signup, login, "
                        "refresh, or profile."
                    ),
                },
            )

        except ValueError as exc:

            json_response(
                self,
                400,
                {
                    "success": False,
                    "error": str(exc),
                },
            )

        except RuntimeError as exc:

            json_response(
                self,
                400,
                {
                    "success": False,
                    "error": str(exc),
                },
            )

        except Exception as exc:

            json_response(
                self,
                500,
                {
                    "success": False,
                    "error": (
                        "Authentication server error: "
                        + str(exc)
                    ),
                },
            )
