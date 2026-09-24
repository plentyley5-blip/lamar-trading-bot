import json
import os
import urllib.request
import urllib.error
import urllib.parse


def send_json(handler, status, data):
    body = json.dumps(data).encode("utf-8")

    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header(
        "Access-Control-Allow-Headers",
        "Authorization, Content-Type"
    )
    handler.send_header(
        "Access-Control-Allow-Methods",
        "GET, OPTIONS"
    )
    handler.end_headers()

    handler.wfile.write(body)


def get_supabase_url():
    url = os.environ.get("SUPABASE_URL", "").strip()

    if not url:
        raise Exception("SUPABASE_URL is missing")

    url = url.rstrip("/")

    for suffix in [
        "/rest/v1",
        "/auth/v1",
        "/storage/v1"
    ]:
        if url.endswith(suffix):
            url = url[:-len(suffix)]

    return url


def get_supabase_key():
    key = os.environ.get(
        "SUPABASE_SERVICE_ROLE_KEY",
        ""
    ).strip()

    if not key:
        raise Exception(
            "SUPABASE_SERVICE_ROLE_KEY is missing"
        )

    return key


def get_bearer_token(handler):
    authorization = handler.headers.get(
        "Authorization",
        ""
    ).strip()

    if not authorization:
        raise Exception(
            "Authorization header is missing"
        )

    if not authorization.startswith("Bearer "):
        raise Exception(
            "Authorization header is not Bearer format"
        )

    token = authorization[7:].strip()

    if not token:
        raise Exception(
            "Bearer token is empty"
        )

    return token


def verify_user(token):
    url = get_supabase_url() + "/auth/v1/user"

    request = urllib.request.Request(
        url,
        headers={
            "apikey": get_supabase_key(),
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=15
        ) as result:

            raw = result.read().decode(
                "utf-8",
                errors="replace"
            )

            return json.loads(raw)

    except urllib.error.HTTPError as error:
        error_body = error.read().decode(
            "utf-8",
            errors="replace"
        )

        raise Exception(
            "Supabase authentication failed: HTTP "
            + str(error.code)
            + " "
            + error_body[:500]
        )


def get_history(user_id, token):
    encoded_user_id = urllib.parse.quote(
        user_id,
        safe=""
    )

    url = (
        get_supabase_url()
        + "/rest/v1/analysis_jobs"
        + "?user_id=eq."
        + encoded_user_id
        + "&status=eq.completed"
        + "&order=created_at.desc"
        + "&limit=50"
    )

    request = urllib.request.Request(
        url,
        headers={
            "apikey": get_supabase_key(),
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=15
        ) as result:

            raw = result.read().decode(
                "utf-8",
                errors="replace"
            )

            return json.loads(raw)

    except urllib.error.HTTPError as error:
        error_body = error.read().decode(
            "utf-8",
            errors="replace"
        )

        raise Exception(
            "Supabase history query failed: HTTP "
            + str(error.code)
            + " "
            + error_body[:1000]
        )


class Handler:

    def do_OPTIONS(self):
        send_json(self, 204, {})

    def do_GET(self):
        try:
            # 1. Get login token
            token = get_bearer_token(self)

            # 2. Verify logged-in user
            user = verify_user(token)

            user_id = user.get("id")

            if not user_id:
                raise Exception(
                    "Supabase user response has no id"
                )

            # 3. Get only this user's completed analyses
            jobs = get_history(
                user_id,
                token
            )

            # 4. Return history
            send_json(
                self,
                200,
                {
                    "status": "ok",
                    "count": len(jobs),
                    "history": jobs,
                }
            )

        except urllib.error.HTTPError as error:

            body = error.read().decode(
                "utf-8",
                errors="replace"
            )

            send_json(
                self,
                500,
                {
                    "status": "error",
                    "type": "HTTPError",
                    "code": error.code,
                    "message": body[:1000],
                }
            )

        except Exception as error:

            send_json(
                self,
                500,
                {
                    "status": "error",
                    "type": type(error).__name__,
                    "message": str(error),
                }
            )


# Vercel Python entry point
handler = Handler()
