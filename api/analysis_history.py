import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from http.server import BaseHTTPRequestHandler


SUPABASE_URL = os.environ.get(
    "SUPABASE_URL",
    ""
).strip().rstrip("/")

SUPABASE_ANON_KEY = os.environ.get(
    "SUPABASE_ANON_KEY",
    ""
).strip()

SUPABASE_SERVICE_ROLE_KEY = os.environ.get(
    "SUPABASE_SERVICE_ROLE_KEY",
    ""
).strip()


def send_json(
    handler,
    status,
    data,
):

    body = json.dumps(
        data,
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")

    handler.send_response(
        status
    )

    handler.send_header(
        "Content-Type",
        "application/json; charset=utf-8",
    )

    handler.send_header(
        "Content-Length",
        str(len(body)),
    )

    handler.send_header(
        "Cache-Control",
        "no-store",
    )

    handler.send_header(
        "Access-Control-Allow-Origin",
        "*",
    )

    handler.send_header(
        "Access-Control-Allow-Headers",
        "Authorization, Content-Type",
    )

    handler.send_header(
        "Access-Control-Allow-Methods",
        "GET, OPTIONS",
    )

    handler.end_headers()

    handler.wfile.write(
        body
    )


def get_user(
    handler,
):

    authorization = str(
        handler.headers.get(
            "Authorization",
            "",
        )
    ).strip()

    if not authorization.lower().startswith(
        "bearer "
    ):

        raise ValueError(
            "Authorization bearer token is required."
        )

    token = authorization[
        7:
    ].strip()

    if not token:

        raise ValueError(
            "Authorization bearer token is required."
        )

    if not SUPABASE_ANON_KEY:

        raise RuntimeError(
            "SUPABASE_ANON_KEY is missing."
        )

    request = urllib.request.Request(

        SUPABASE_URL
        + "/auth/v1/user",

        headers={
            "apikey":
                SUPABASE_ANON_KEY,

            "Authorization":
                "Bearer "
                + token,

            "Accept":
                "application/json",
        },

        method="GET",
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=15,
        ) as response:

            user = json.loads(
                response.read()
                .decode(
                    "utf-8"
                )
            )

    except urllib.error.HTTPError as exc:

        detail = (
            exc.read()
            .decode(
                "utf-8",
                errors="replace",
            )
        )

        raise ValueError(
            "Session verification failed: "
            + detail[:500]
        )

    user_id = str(
        user.get(
            "id",
            "",
        )
    ).strip()

    if not user_id:

        raise ValueError(
            "Invalid user session."
        )

    return user_id


def load_rows(
    user_id,
):

    if not SUPABASE_SERVICE_ROLE_KEY:

        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY is missing."
        )

    encoded_user = (
        urllib.parse.quote(
            user_id,
            safe="",
        )
    )

    path = (
        "/rest/v1/analysis_jobs"
        "?select=id,instrument,trade_focus,created_at,status,openai_response_id,error_message"
        "&user_id=eq."
        + encoded_user
        + "&status=eq.completed"
        "&order=created_at.desc"
        "&limit=50"
    )

    request = urllib.request.Request(

        SUPABASE_URL
        + path,

        headers={
            "apikey":
                SUPABASE_SERVICE_ROLE_KEY,

            "Authorization":
                "Bearer "
                + SUPABASE_SERVICE_ROLE_KEY,

            "Accept":
                "application/json",
        },

        method="GET",
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=20,
        ) as response:

            raw = (
                response.read()
                .decode(
                    "utf-8",
                    errors="replace",
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
                errors="replace",
            )
        )

        raise RuntimeError(
            "Supabase history query failed: "
            + detail[:1000]
        )

    if not isinstance(
        rows,
        list,
    ):

        return []

    return rows


def decode_result(
    value,
):

    if not isinstance(
        value,
        str,
    ) or not value:

        return None

    if value in {
        "pending",
        "failed",
    }:

        return None

    try:

        padded = (
            value
            + "="
            * (-len(value) % 4)
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
            dict,
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
            "*",
        )

        self.send_header(
            "Access-Control-Allow-Headers",
            "Authorization, Content-Type",
        )

        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, OPTIONS",
        )

        self.end_headers()


    def do_GET(self):

        try:

            # -----------------------------------------
            # USER
            # -----------------------------------------

            user_id = get_user(
                self
            )


            # -----------------------------------------
            # DATABASE
            # -----------------------------------------

            rows = load_rows(
                user_id
            )

            history = []


            # -----------------------------------------
            # BUILD ANDROID-FRIENDLY HISTORY
            # -----------------------------------------

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
                                    "",
                                )
                            ),

                        "instrument":
                            str(
                                row.get(
                                    "instrument",
                                    "",
                                )
                            ),

                        "trade_focus":
                            str(
                                row.get(
                                    "trade_focus",
                                    "",
                                )
                            ),

                        "created_at":
                            str(
                                row.get(
                                    "created_at",
                                    "",
                                )
                            ),

                        # IMPORTANT:
                        # MainActivity expects result
                        # to be an object here.
                        "result":
                            result,
                    }
                )


            # -----------------------------------------
            # RESPONSE
            # -----------------------------------------

            send_json(
                self,
                200,
                {
                    "status":
                        "ok",

                    "count":
                        len(history),

                    "history":
                        history,
                },
            )

        except ValueError as exc:

            send_json(
                self,
                401,
                {
                    "error":
                        str(exc),
                },
            )

        except RuntimeError as exc:

            send_json(
                self,
                500,
                {
                    "error":
                        str(exc),
                },
            )

        except Exception as exc:

            send_json(
                self,
                500,
                {
                    "error":
                        str(exc),
                },
            )
