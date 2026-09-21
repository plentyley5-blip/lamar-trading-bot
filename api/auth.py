import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler


def get_env(name):
    value = os.environ.get(name, "").strip()

    if not value:
        raise RuntimeError(
            name + " is missing from Vercel Environment Variables."
        )

    return value


def supabase_url():
    return get_env("SUPABASE_URL").rstrip("/")


def service_key():
    return get_env("SUPABASE_SERVICE_ROLE_KEY")


def send_request(method, url, payload=None, authorization=None):
    headers = {
        "apikey": service_key(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    if authorization:
        headers["Authorization"] = authorization

    data = None

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers=headers,
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=20
        ) as response:

            body = response.read().decode("utf-8")

            if not body:
                return response.status, {}

            return response.status, json.loads(body)

    except urllib.error.HTTPError as exc:

        body = exc.read().decode(
            "utf-8",
            errors="replace"
        )

        try:
            data = json.loads(body)
        except Exception:
            data = {
                "message": body
            }

        return exc.code, data

    except urllib.error.URLError as exc:

        raise RuntimeError(
            "Unable to connect to Supabase: "
            + str(exc.reason)
        )


def create_profile(user_id, full_name, email):
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

    status, data = send_request(
        "POST",
        url,
        payload
    )

    if status not in (200, 201):
        raise RuntimeError(
            "Account profile could not be created: "
            + json.dumps(data)
        )


def signup(email, password, full_name):

    url = (
        supabase_url()
        + "/auth/v1/signup"
    )

    payload = {
        "email": email,
        "password": password,
        "data": {
            "full_name": full_name
        }
    }

    status, data = send_request(
        "POST",
        url,
        payload
    )

    if status < 200 or status >= 300:

        message = (
            data.get("msg")
            or data.get("message")
            or data.get("error_description")
            or "Unable to create account."
        )

        raise ValueError(
            str(message)
        )

    user = data.get("user") or {}

    user_id = str(
        user.get("id", "")
    ).strip()

    returned_email = str(
        user.get("email", email)
    ).strip()

    if user_id:
        try:
            create_profile(
                user_id,
                full_name,
                returned_email
            )
        except Exception:
            pass

    return data


def login(email, password):

    url = (
        supabase_url()
        + "/auth/v1/token"
        + "?grant_type=password"
    )

    payload = {
        "email": email,
        "password": password
    }

    status, data = send_request(
        "POST",
        url,
        payload
    )

    if status < 200 or status >= 300:

        message = (
            data.get("msg")
            or data.get("message")
            or data.get("error_description")
            or "Invalid email or password."
        )

        raise ValueError(
            str(message)
        )

    return data


class handler(BaseHTTPRequestHandler):

    def send_json(self, status_code, payload):

        body = json.dumps(
            payload,
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

            if content_length <= 0:
                raise ValueError(
                    "Request body is empty."
                )

            if content_length > 1024 * 1024:
                raise ValueError(
                    "Request is too large."
                )

            raw_body = self.rfile.read(
                content_length
            )

            try:
                body = json.loads(
                    raw_body.decode("utf-8")
                )
            except Exception:
                raise ValueError(
                    "Request body must be valid JSON."
                )

            if not isinstance(body, dict):
                raise ValueError(
                    "Request body must be a JSON object."
                )

            action = str(
                body.get("action", "")
            ).strip().lower()

            if action == "signup":

                full_name = str(
                    body.get("full_name", "")
                ).strip()

                email = str(
                    body.get("email", "")
                ).strip().lower()

                password = str(
                    body.get("password", "")
                )

                if not full_name:
                    raise ValueError(
                        "Full name is required."
                    )

                if len(full_name) > 100:
                    raise ValueError(
                        "Full name is too long."
                    )

                if not email or "@" not in email:
                    raise ValueError(
                        "A valid email address is required."
                    )

                if len(password) < 8:
                    raise ValueError(
                        "Password must be at least 8 characters."
                    )

                result = signup(
                    email,
                    password,
                    full_name
                )

                session = result.get(
                    "session"
                )

                if session:

                    self.send_json(
                        200,
                        {
                            "status": "success",
                            "message": "Account created successfully.",
                            "access_token": session.get(
                                "access_token",
                                ""
                            ),
                            "refresh_token": session.get(
                                "refresh_token",
                                ""
                            ),
                            "user": result.get(
                                "user"
                            )
                        }
                    )

                else:

                    self.send_json(
                        200,
                        {
                            "status": "verification_required",
                            "message": (
                                "Account created. "
                                "Please verify your email before logging in."
                            ),
                            "user": result.get(
                                "user"
                            )
                        }
                    )

                return

            if action == "login":

                email = str(
                    body.get("email", "")
                ).strip().lower()

                password = str(
                    body.get("password", "")
                )

                if not email:
                    raise ValueError(
                        "Email is required."
                    )

                if not password:
                    raise ValueError(
                        "Password is required."
                    )

                result = login(
                    email,
                    password
                )

                session = result.get(
                    "session"
                )

                self.send_json(
                    200,
                    {
                        "status": "success",
                        "message": "Login successful.",
                        "access_token": result.get(
                            "access_token",
                            session.get(
                                "access_token",
                                ""
                            ) if session else ""
                        ),
                        "refresh_token": result.get(
                            "refresh_token",
                            session.get(
                                "refresh_token",
                                ""
                            ) if session else ""
                        ),
                        "user": result.get(
                            "user"
                        )
                    }
                )

                return

            raise ValueError(
                "Unknown authentication action."
            )

        except ValueError as exc:

            self.send_json(
                400,
                {
                    "status": "failed",
                    "error": str(exc)
                }
            )

        except Exception as exc:

            self.send_json(
                500,
                {
                    "status": "failed",
                    "error": str(exc)
                }
            )
