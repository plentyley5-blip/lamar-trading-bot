import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from http.server import BaseHTTPRequestHandler

from analysis_common import json_response

from api.user_security import (
    extract_bearer_token,
    verify_access_token,
)


SUPABASE_URL = os.environ.get(
    "SUPABASE_URL",
    ""
).strip().rstrip("/")

SUPABASE_SERVICE_ROLE_KEY = os.environ.get(
    "SUPABASE_SERVICE_ROLE_KEY",
    ""
).strip()


def get_current_user(
    handler
):

    token = extract_bearer_token(
        handler
    )

    user = verify_access_token(
        token
    )

    user_id = str(
        user.get(
            "id",
            ""
        )
    ).strip()

    if not user_id:

        raise ValueError(
            "Invalid user session."
        )

    return user_id


def load_history(
    user_id
):

    if not SUPABASE_URL:

        raise RuntimeError(
            "SUPABASE_URL is missing."
        )

    if not SUPABASE_SERVICE_ROLE_KEY:

        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY is missing."
        )

    encoded_user = urllib.parse.quote(
        user_id,
        safe=""
    )

    path = (
        "/rest/v1/analysis_jobs"
        "?select="
        "id,"
        "user_id,"
        "instrument,"
        "trade_focus,"
        "created_at,"
        "status,"
        "openai_response_id,"
        "error_message"
        "&user_id=eq."
        + encoded_user
        + "&status=eq.completed"
        + "&order=created_at.desc"
        + "&limit=50"
    )

    request = urllib.request.Request(

        SUPABASE_URL + path,

        headers={
            "apikey":
                SUPABASE_SERVICE_ROLE_KEY,

            "Authorization":
                "Bearer "
                + SUPABASE_SERVICE_ROLE_KEY,

            "Accept":
                "application/json"
        },

        method="GET"
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=20
        ) as response:

            raw = (
                response.read()
                .decode(
                    "utf-8",
                    errors="replace"
                )
            )

            rows = json.loads(
                raw
            )

    except urllib.error.HTTPError as exc:

        detail = (
            exc.read()
            .decode(
                "utf-8",
                errors="replace"
            )
        )

        raise RuntimeError(
            "Supabase history query failed: "
            + detail[:1000]
        )

    if not isinstance(
        rows,
        list
    ):

        return []

    return rows


def decode_result(
    encoded
):

    if not isinstance(
        encoded,
        str
    ):

        return None

    encoded = encoded.strip()

    if not encoded:

        return None

    if encoded in {
        "pending",
        "failed"
    }:

        return None

    try:

        padded = (
            encoded
            + "="
            * (-len(encoded) % 4)
        )

        raw = (
            base64.urlsafe_b64decode(
                padded
            )
        )

        result = json.loads(
            raw.decode(
                "utf-8"
            )
        )

        if isinstance(
            result,
            dict
        ):

            return result

    except Exception:

        return None

    return None


class handler(
    BaseHTTPRequestHandler
):

    def do_OPTIONS(self):

        self.send_response(
            204
        )

        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
        )

        self.send_header(
            "Access-Control-Allow-Headers",
            "Authorization, Content-Type"
        )

        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, OPTIONS"
        )

        self.end_headers()


    def do_GET(self):

        try:

            # =========================================
            # AUTHENTICATED USER
            # =========================================

            user_id = get_current_user(
                self
            )


            # =========================================
            # DATABASE
            # =========================================

            rows = load_history(
                user_id
            )

            history = []


            # =========================================
            # BUILD ANDROID FORMAT
            # =========================================

            for row in rows:

                result = decode_result(
                    row.get(
                        "openai_response_id"
                    )
                )

                if result is None:

                    continue


                history.append(
                    {
                        "job_id":
                            str(
                                row.get(
                                    "id",
                                    ""
                                )
                            ),

                        "instrument":
                            str(
                                row.get(
                                    "instrument",
                                    ""
                                )
                            ),

                        "trade_focus":
                            str(
                                row.get(
                                    "trade_focus",
                                    ""
                                )
                            ),

                        "created_at":
                            str(
                                row.get(
                                    "created_at",
                                    ""
                                )
                            ),

                        "result":
                            result
                    }
                )


            # =========================================
            # RESPONSE
            # =========================================

            json_response(
                self,
                200,
                {
                    "status":
                        "ok",

                    "count":
                        len(history),

                    "history":
                        history
                }
            )

        except ValueError as exc:

            json_response(
                self,
                401,
                {
                    "error":
                        str(exc)
                }
            )

        except RuntimeError as exc:

            json_response(
                self,
                500,
                {
                    "error":
                        str(exc)
                }
            )

        except Exception as exc:

            json_response(
                self,
                500,
                {
                    "error":
                        str(exc)
                }
            )
