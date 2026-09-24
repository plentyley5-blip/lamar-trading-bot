import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

from analysis_common import json_response
from user_security import (
    extract_bearer_token,
    verify_access_token,
)


# ---------------------------------------------------------
# CONFIG
# ---------------------------------------------------------

SUPABASE_URL = os.environ.get(
    "SUPABASE_URL",
    ""
).rstrip("/")

SUPABASE_SERVICE_ROLE_KEY = os.environ.get(
    "SUPABASE_SERVICE_ROLE_KEY",
    ""
)

MAX_HISTORY_ITEMS = 50


# ---------------------------------------------------------
# SUPABASE REQUEST
# ---------------------------------------------------------

def _supabase_request(
    method,
    path,
    payload=None,
    timeout=30,
):
    if not SUPABASE_URL:
        raise RuntimeError(
            "SUPABASE_URL is not configured."
        )

    if not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY is not configured."
        )

    url = SUPABASE_URL + path

    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": (
            "Bearer "
            + SUPABASE_SERVICE_ROLE_KEY
        ),
        "Content-Type": "application/json",
    }

    data = None

    if payload is not None:
        data = json.dumps(
            payload,
            ensure_ascii=False,
        ).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout,
        ) as response:

            raw = response.read().decode(
                "utf-8",
                errors="replace",
            )

            if not raw:
                return None

            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw

    except urllib.error.HTTPError as exc:

        detail = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            "Supabase request failed "
            f"({exc.code}): {detail}"
        )

    except urllib.error.URLError as exc:

        raise RuntimeError(
            "Supabase connection failed: "
            + str(exc)
        )


# ---------------------------------------------------------
# DECODE SAVED RESULT
# ---------------------------------------------------------

def _decode_result(encoded_result):
    if not encoded_result:
        return None

    if not isinstance(
        encoded_result,
        str,
    ):
        return None

    try:

        padded = encoded_result + (
            "=" * (
                -len(encoded_result) % 4
            )
        )

        raw = base64.urlsafe_b64decode(
            padded.encode("ascii")
        )

        result = json.loads(
            raw.decode("utf-8")
        )

        if not isinstance(
            result,
            dict,
        ):
            return None

        return result

    except Exception:
        return None


# ---------------------------------------------------------
# LOAD USER HISTORY
# ---------------------------------------------------------

def _load_history(user_id):
    encoded_user_id = urllib.parse.quote(
        user_id,
        safe="",
    )

    path = (
        "/rest/v1/analysis_jobs"
        "?select="
        "job_nonce,"
        "instrument,"
        "trade_focus,"
        "status,"
        "openai_response_id,"
        "created_at,"
        "error_message"
        "&user_id=eq."
        + encoded_user_id
        + "&status=eq.completed"
        "&order=created_at.desc"
        "&limit="
        + str(MAX_HISTORY_ITEMS)
    )

    rows = _supabase_request(
        "GET",
        path,
        timeout=30,
    )

    if not rows:
        return []

    if not isinstance(
        rows,
        list,
    ):
        return []

    history = []

    for row in rows:

        if not isinstance(
            row,
            dict,
        ):
            continue

        job_nonce = str(
            row.get(
                "job_nonce",
                "",
            )
        ).strip()

        raw_instrument = str(
            row.get(
                "instrument",
                "",
            )
        ).strip()

        # The current submit system stores:
        # instrument|job_nonce
        instrument = raw_instrument

        if "|" in raw_instrument:
            instrument = raw_instrument.split(
                "|",
                1,
            )[0]

        instrument = instrument.upper()

        trade_focus = str(
            row.get(
                "trade_focus",
                "",
            )
        ).strip().upper()

        created_at = row.get(
            "created_at"
        )

        result = _decode_result(
            row.get(
                "openai_response_id"
            )
        )

        # Do not expose broken records.
        if not job_nonce:
            continue

        if result is None:
            continue

        history.append(
            {
                "job_id": job_nonce,
                "instrument": instrument,
                "trade_focus": trade_focus,
                "created_at": created_at,
                "result": result,
            }
        )

    return history


# ---------------------------------------------------------
# AUTHENTICATE
# ---------------------------------------------------------

def _authenticate_user(handler):
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

    if not isinstance(
        user,
        dict,
    ):
        raise ValueError(
            "Invalid authentication."
        )

    user_id = str(
        user.get(
            "id",
            "",
        )
    ).strip()

    if not user_id:
        raise ValueError(
            "Invalid authenticated user."
        )

    return user_id


# ---------------------------------------------------------
# HANDLER
# ---------------------------------------------------------

class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(204)

        self.send_header(
            "Access-Control-Allow-Origin",
            "*",
        )

        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization",
        )

        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, OPTIONS",
        )

        self.end_headers()


    def do_GET(self):

        try:

            # ---------------------------------------------
            # AUTHENTICATE USER
            # ---------------------------------------------

            user_id = _authenticate_user(
                self
            )


            # ---------------------------------------------
            # LOAD ONLY THAT USER'S HISTORY
            # ---------------------------------------------

            history = _load_history(
                user_id
            )


            # ---------------------------------------------
            # RESPONSE
            # ---------------------------------------------

            json_response(
                self,
                200,
                {
                    "status": "success",
                    "count": len(history),
                    "history": history,
                },
            )

        except ValueError as exc:

            json_response(
                self,
                401,
                {
                    "error": str(exc)
                },
            )

        except RuntimeError as exc:

            json_response(
                self,
                500,
                {
                    "error": str(exc)
                },
            )

        except Exception:

            json_response(
                self,
                500,
                {
                    "error": (
                        "Unable to load "
                        "analysis history."
                    )
                },
            )
