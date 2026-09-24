import json
import os
import urllib.request
import urllib.error
import base64


def response(handler, status, data):
    body = json.dumps(data).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
    handler.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
    handler.end_headers()
    handler.wfile.write(body)


def supabase_url():
    url = os.environ.get("SUPABASE_URL", "").strip()

    if not url:
        raise Exception("SUPABASE_URL is missing")

    url = url.rstrip("/")

    for suffix in ["/rest/v1", "/auth/v1", "/storage/v1"]:
        if url.endswith(suffix):
            url = url[:-len(suffix)]

    return url


def supabase_key():
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()

    if not key:
        raise Exception("SUPABASE_SERVICE_ROLE_KEY is missing")

    return key


def get_token(handler):
    auth = handler.headers.get("Authorization", "")

    if not auth:
        raise Exception("Authorization header is missing")

    if not auth.startswith("Bearer "):
        raise Exception("Authorization header is not Bearer format")

    token = auth[7:].strip()

    if not token:
        raise Exception("Bearer token is empty")

    return token


def verify_user(token):
    url = supabase_url() + "/auth/v1/user"

    req = urllib.request.Request(
        url,
        headers={
            "apikey": supabase_key(),
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw)

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise Exception(
            "Supabase authentication failed: HTTP "
            + str(e.code)
            + " "
            + body[:500]
        )


def handler(event, context):
    return None


class Handler:
    def __init__(self):
        pass

    def do_OPTIONS(self):
        response(self, 204, {})

    def do_GET(self):
        try:
            # STEP 1
            token = get_token(self)

            # STEP 2
            user = verify_user(token)

            user_id = user.get("id")

            if not user_id:
                raise Exception("Supabase user response has no id")

            # STEP 3
            url = (
                supabase_url()
                + "/rest/v1/analysis_jobs"
                + "?user_id=eq."
                + urllib.parse.quote(user_id)
                + "&status=eq.completed"
                + "&order=created_at.desc"
                + "&limit=50"
            )

            req = urllib.request.Request(
                url,
                headers={
                    "apikey": supabase_key(),
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                },
                method="GET",
            )

            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read().decode("utf-8")

            jobs = json.loads(raw)

            # Return only safe diagnostic information for now.
            response(
                self,
                200,
                {
                    "status": "ok",
                    "user_id": user_id,
                    "count": len(jobs),
                    "jobs": jobs,
                },
            )

        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")

            response(
                self,
                500,
                {
                    "status": "error",
                    "step": "HTTPError",
                    "code": e.code,
                    "message": body[:1000],
                },
            )

        except Exception as e:
            response(
                self,
                500,
                {
                    "status": "error",
                    "step": "exception",
                    "type": type(e).__name__,
                    "message": str(e),
                },
            )
