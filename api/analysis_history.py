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

    handler.wfile.write(body)


def get_supabase_url():
    if not SUPABASE_URL:
        raise RuntimeError(
            "SUPABASE_URL is missing."
        )

    return SUPABASE_URL


def get_user_from_token(
    handler,
):
    authorization = handler.headers.get(
        "Authorization",
        "",
    ).strip()

    if not authorization.startswith(
        "Bearer "
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
        get_supabase_url()
        + "/auth/v1/user",
        headers={
            "apikey":
                SUPABASE_ANON_KEY,
            "Authorization":
                "Bearer " + token,
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
                response.read().decode(
                    "utf-8"
                )
            )

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        raise ValueError(
            "Session verification failed: "
            + detail[:500]
        )

    if not isinstance(
        user,
        dict,
    ) or not user.get("id"):

        raise ValueError(
            "Invalid user session."
        )

    return (
        str(user["id"]).strip(),
        token,
    )


def load_history(
    user_id,
):
    if not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY is missing."
        )

    encoded_user = urllib.parse.quote(
        user_id,
        safe="",
    )

    path = (
        "/rest/v1/analysis_jobs"
        "?user_id=eq."
        + encoded_user
        + "&status=eq.completed"
        + "&order=created_at.desc"
        + "&limit=50"
    )

    request = urllib.request.Request(
        get_supabase_url()
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
            timeout=15,
        ) as response:

            raw = response.read().decode(
                "utf-8",
                errors="replace",
            )

            return json.loads(raw)

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            "Supabase history query failed: "
            + detail[:1000]
        )


def decode_result(
    encoded_result,
):
    if not isinstance(
        encoded_result,
        str,
    ) or not encoded_result:
        return {}

    try:
        padded = (
            encoded_result
            + "="
            * (-len(encoded_result) % 4)
        )

        raw = base64.urlsafe_b64decode(
            padded.encode("ascii")
        )

        result = json.loads(
            raw.decode("utf-8")
        )

        return (
            result
            if isinstance(result, dict)
            else {}
        )

    except Exception:
        return {}


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(204)

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
            # AUTHENTICATED USER
            # -----------------------------------------
            user_id, token = (
                get_user_from_token(
                    self
                )
            )

            # -----------------------------------------
            # HISTORY
            # -----------------------------------------
            rows = load_history(
                user_id
            )

            if not isinstance(
                rows,
                list,
            ):
                rows = []

            history = []

            for row in rows:

                result = decode_result(
                    row.get(
                        "openai_response_id"
                    )
                )

                item = {
                    "job_id":
                        row.get(
                            "job_nonce"
                        ),

                    "trade_focus":
                        row.get(
                            "trade_focus"
                        ),

                    "created_at":
                        row.get(
                            "created_at"
                        ),

                    "status":
                        "completed",
                }

                # Put the saved analysis
                # fields directly into the item.
                if result:
                    item.update(
                        result
                    )

                # Always preserve the database
                # instrument as fallback.
                if not item.get(
                    "instrument"
                ):
                    item[
                        "instrument"
                    ] = str(
                        row.get(
                            "instrument",
                            "",
                        )
                    ).split(
                        "|",
                        1
                    )[0]

                history.append(
                    item
                )

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
                    "analyses":
                        history,
                },
            )

        except ValueError as exc:

            send_json(
                self,
                401,
                {
                    "status":
                        "error",
                    "error":
                        str(exc),
                },
            )

        except RuntimeError as exc:

            send_json(
                self,
                500,
                {
                    "status":
                        "error",
                    "error":
                        str(exc),
                },
            )

        except Exception as exc:

            send_json(
                self,
                500,
                {
                    "status":
                        "error",
                    "error":
                        str(exc),
                },
            )
