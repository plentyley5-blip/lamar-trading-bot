
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

from api.user_security import (
    get_supabase_service_key,
    get_supabase_url,
    verify_access_token,
)

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 16384
MAX_TOKEN_LENGTH = 4096


def respond(handler, status, payload):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header(
        "Access-Control-Allow-Headers", "Content-Type, Authorization"
    )
    handler.send_header(
        "Access-Control-Allow-Methods", "POST, OPTIONS"
    )
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()

    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        logger.warning("Client disconnected before receiving the response.")


def authenticated_user_id(handler):
    authorization = str(
        handler.headers.get("Authorization", "")
    ).strip()

    parts = authorization.split(None, 1)

    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise ValueError("Authentication required.")

    user = verify_access_token(parts[1].strip())
    user_id = user.get("id") if isinstance(user, dict) else None

    if not user_id:
        raise ValueError("Invalid user session.")

    return str(user_id)


def read_body(handler):
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid request length.") from exc

    if length <= 0 or length > MAX_BODY_BYTES:
        raise ValueError("Invalid request body.")

    try:
        raw = handler.rfile.read(length).decode("utf-8")
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid JSON body.") from exc

    if not isinstance(data, dict):
        raise ValueError("Invalid JSON body.")

    return data


def save_device(user_id, token, platform):
    base_url = get_supabase_url().strip().rstrip("/")
    service_key = get_supabase_service_key().strip()

    if not base_url or not service_key:
        raise RuntimeError("Supabase configuration is incomplete.")

    query = urllib.parse.urlencode({
        "on_conflict": "device_token"
    })

    url = base_url + "/rest/v1/device_tokens?" + query

    payload = json.dumps({
        "user_id": user_id,
        "device_token": token,
        "platform": platform,
        "active": True,
    }).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "apikey": service_key,
            "Authorization": "Bearer " + service_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read()
            return response.status

    except urllib.error.HTTPError as exc:
        # Record the database error, but never log the submitted token
        # or the Supabase service-role key.
        detail = exc.read().decode("utf-8", errors="replace")

        logger.error(
            "Device registration Supabase HTTP error: status=%s detail=%s",
            exc.code,
            detail[:1000],
        )

        raise

    except (urllib.error.URLError, TimeoutError) as exc:
        logger.error(
            "Device registration could not reach Supabase: %s",
            type(exc).__name__,
        )
        raise


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Allow-Headers", "Content-Type, Authorization"
        )
        self.send_header(
            "Access-Control-Allow-Methods", "POST, OPTIONS"
        )
        self.end_headers()

    def do_POST(self):
        try:
            user_id = authenticated_user_id(self)
            data = read_body(self)

            token = str(
                data.get("device_token") or data.get("token") or ""
            ).strip()

            if not token or len(token) > MAX_TOKEN_LENGTH:
                respond(self, 400, {
                    "error": "Invalid device token."
                })
                return

            platform = str(data.get("platform") or "android").strip().lower()

            if platform not in {"android", "ios", "web"}:
                respond(self, 400, {
                    "error": "Unsupported device platform."
                })
                return

            status = save_device(user_id, token, platform)

            logger.info(
                "Device token registered successfully for an authenticated user."
            )

            respond(self, 200, {
                "ok": True,
                "message": "Device registered.",
                "database_status": status,
            })

        except ValueError as exc:
            respond(self, 401, {
                "error": str(exc)
            })

        except urllib.error.HTTPError as exc:
            respond(self, 502, {
                "error": "Device storage rejected the registration.",
                "status": exc.code,
            })

        except Exception as exc:
            logger.exception(
                "Device registration failed (%s).",
                type(exc).__name__,
            )

            respond(self, 500, {
                "error": "Device registration failed.",
                "reason": type(exc).__name__,
            })

    def do_GET(self):
        respond(self, 405, {
            "error": "Method not allowed."
        })

    def do_PUT(self):
        respond(self, 405, {
            "error": "Method not allowed."
        })

    def do_DELETE(self):
        respond(self, 405, {
            "error": "Method not allowed."
        })
