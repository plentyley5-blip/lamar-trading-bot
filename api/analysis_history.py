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


SUPABASE_URL = os.environ.get(
    "SUPABASE_URL",
    ""
).strip().rstrip("/")

SUPABASE_SERVICE_ROLE_KEY = os.environ.get(
    "SUPABASE_SERVICE_ROLE_KEY",
    ""
).strip()


def response(handler, status, data):
    body = json.dumps(
        data,
        ensure_ascii=False,
        default=str,
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


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        response(
            self,
            204,
            {}
        )

    def do_GET(self):

        try:

            # -----------------------------------------
            # STEP 1 — CHECK ENVIRONMENT
            # -----------------------------------------

            if not SUPABASE_URL:
                raise RuntimeError(
                    "STEP 1 FAILED: SUPABASE_URL is missing."
                )

            if not SUPABASE_SERVICE_ROLE_KEY:
                raise RuntimeError(
                    "STEP 1 FAILED: SUPABASE_SERVICE_ROLE_KEY is missing."
                )


            # -----------------------------------------
            # STEP 2 — GET TOKEN
            # -----------------------------------------

            token = extract_bearer_token(
                self
            )


            # -----------------------------------------
            # STEP 3 — VERIFY USER
            # -----------------------------------------

            user = verify_access_token(
                token
            )

            if not isinstance(user, dict):
                raise RuntimeError(
                    "STEP 3 FAILED: Invalid user response."
                )

            user_id = str(
                user.get("id", "")
            ).strip()

            if not user_id:
                raise RuntimeError(
                    "STEP 3 FAILED: User ID is missing."
                )


            # -----------------------------------------
            # STEP 4 — TEST SUPABASE DATABASE
            # -----------------------------------------

            encoded_user_id = urllib.parse.quote(
                user_id,
                safe=""
            )

            url = (
                SUPABASE_URL
                + "/rest/v1/analysis_jobs"
                + "?select=*"
                + "&user_id=eq."
                + encoded_user_id
                + "&limit=1"
            )

            request = urllib.request.Request(
                url,
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

            with urllib.request.urlopen(
                request,
                timeout=20,
            ) as supabase_response:

                raw = (
                    supabase_response
                    .read()
                    .decode(
                        "utf-8",
                        errors="replace"
                    )
                )

            rows = json.loads(raw)


            # -----------------------------------------
            # SUCCESS
            # -----------------------------------------

            response(
                self,
                200,
                {
                    "status": "success",
                    "message": "History backend test passed.",
                    "user_id": user_id,
                    "database_rows": rows,
                }
            )

        except urllib.error.HTTPError as exc:

            detail = (
                exc.read()
                .decode(
                    "utf-8",
                    errors="replace"
                )
            )

            response(
                self,
                500,
                {
                    "status": "error",
                    "stage": "SUPABASE",
                    "error": (
                        "HTTP "
                        + str(exc.code)
                        + ": "
                        + detail
                    ),
                }
            )

        except Exception as exc:

            response(
                self,
                500,
                {
                    "status": "error",
                    "error_type":
                        type(exc).__name__,
                    "error":
                        str(exc),
                }
            )
