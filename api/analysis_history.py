import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

from user_security import (
    extract_bearer_token,
    verify_access_token,
)


SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get(
    "SUPABASE_SERVICE_ROLE_KEY",
    "",
)

MAX_HISTORY_ITEMS = 50


# ---------------------------------------------------------
# JSON RESPONSE
# ---------------------------------------------------------

def send_json(handler, status_code, payload):
    body = json.dumps(
        payload,
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")

    handler.send_response(status_code)

    handler.send_header(
        "Content-Type",
        "application/json; charset=utf-8",
    )

    handler.send_header(
        "Access-Control-Allow-Origin",
        "*",
    )

    handler.send_header(
        "Access-Control-Allow-Headers",
        "Content-Type, Authorization",
    )

    handler.send_header(
        "Access-Control-Allow-Methods",
        "GET, OPTIONS",
    )

    handler.send_header(
        "Content-Length",
        str(len(body)),
    )

    handler.end_headers()
    handler.wfile.write(body)


# ---------------------------------------------------------
# SUPABASE GET
# ---------------------------------------------------------

def supabase_get(path):
    if not SUPABASE_URL:
        raise RuntimeError(
            "SUPABASE_URL is not configured."
        )

    if not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY is not configured."
        )

    url = SUPABASE_URL + path

    request = urllib.request.Request(
        url,
        headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": (
                "Bearer "
                + SUPABASE_SERVICE_ROLE_KEY
            ),
            "Accept": "application/json",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=20,
        ) as response:

            raw = response.read().decode(
                "utf-8",
                errors="replace",
            )

            if not raw:
                return []

            return json.loads(raw)

    except urllib.error.HTTPError as exc:

        detail = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            "Supabase HTTP "
            + str(exc.code)
            + ": "
            + detail
        )

    except urllib.error.URLError as exc:

        raise RuntimeError(
            "Supabase connection error: "
            + str(exc)
        )


# ---------------------------------------------------------
# AUTHENTICATION
# ---------------------------------------------------------

def authenticate_user(handler):

    authorization = handler.headers.get(
        "Authorization",
        "",
    )

    token = extract_bearer_token(
        authorization
    )

    if not token:
        raise ValueError(
            "Authentication required."
        )

    user = verify_access_token(
        token
    )

    if not isinstance(user, dict):
        raise ValueError(
            "Invalid authentication token."
        )

    user_id = str(
        user.get("id", "")
    ).strip()

    if not user_id:
        raise ValueError(
            "Authenticated user ID is missing."
        )

    return user_id


# ---------------------------------------------------------
# DECODE RESULT
# ---------------------------------------------------------

def decode_saved_result(value):

    if value is None:
        return None

    # Already a JSON object
    if isinstance(value, dict):
        return value

    # Sometimes the database value can already
    # contain a JSON string.
    if isinstance(value, str):

        text = value.strip()

        if not text:
            return None

        try:
            parsed = json.loads(text)

            if isinstance(parsed, dict):
                return parsed

        except Exception:
            pass

        # Try URL-safe base64.
        try:
            padded = text + (
                "=" * (-len(text) % 4)
            )

            decoded = base64.urlsafe_b64decode(
                padded.encode("ascii")
            )

            parsed = json.loads(
                decoded.decode("utf-8")
            )

            if isinstance(parsed, dict):
                return parsed

        except Exception:
            pass

    return None


# ---------------------------------------------------------
# LOAD HISTORY
# ---------------------------------------------------------

def load_history(user_id):

    encoded_user_id = urllib.parse.quote(
        user_id,
        safe="",
    )

    select_fields = (
        "job_nonce,"
        "instrument,"
        "trade_focus,"
        "status,"
        "openai_response_id,"
        "created_at,"
        "error_message"
    )

    path = (
        "/rest/v1/analysis_jobs"
        "?select="
        + select_fields
        + "&user_id=eq."
        + encoded_user_id
        + "&status=eq.completed"
        + "&order=created_at.desc"
        + "&limit="
        + str(MAX_HISTORY_ITEMS)
    )

    rows = supabase_get(path)

    if not isinstance(rows, list):
        return []

    history = []

    for row in rows:

        if not isinstance(row, dict):
            continue

        job_id = str(
            row.get("job_nonce", "")
        ).strip()

        if not job_id:
            continue

        instrument = str(
            row.get("instrument", "")
        ).strip()

        if "|" in instrument:
            instrument = instrument.split(
                "|",
                1,
            )[0]

        instrument = instrument.upper()

        trade_focus = str(
            row.get("trade_focus", "")
        ).strip()

        result = decode_saved_result(
            row.get("openai_response_id")
        )

        # Don't show incomplete/broken records.
        if result is None:
            continue

        history.append(
            {
                "job_id": job_id,
                "instrument": instrument,
                "trade_focus": trade_focus.upper(),
                "created_at": row.get(
                    "created_at"
                ),
                "result": result,
            }
        )

    return history


# ---------------------------------------------------------
# REQUEST HANDLER
# ---------------------------------------------------------

class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):

        send_json(
            self,
            204,
            {},
        )


    def do_GET(self):

        try:

            # 1. Authenticate
            user_id = authenticate_user(
                self
            )

            # 2. Load only this user's history
            history = load_history(
                user_id
            )

            # 3. Return result
            send_json(
                self,
                200,
                {
                    "status": "success",
                    "count": len(history),
                    "history": history,
                },
            )

        except ValueError as exc:

            send_json(
                self,
                401,
                {
                    "status": "error",
                    "error": str(exc),
                },
            )

        except RuntimeError as exc:

            send_json(
                self,
                500,
                {
                    "status": "error",
                    "error": str(exc),
                },
            )

        except Exception as exc:

            # IMPORTANT:
            # Return the real exception so the next
            # Vercel log tells us exactly what failed.
            send_json(
                self,
                500,
                {
                    "status": "error",
                    "error": (
                        "History server error: "
                        + type(exc).__name__
                        + ": "
                        + str(exc)
                    ),
                },
            )
