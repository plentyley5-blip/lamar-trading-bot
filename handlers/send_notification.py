
import json
import logging
import os
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler

import firebase_admin
from firebase_admin import credentials, messaging

from api.user_security import (
    extract_bearer_token,
    get_supabase_service_key,
    get_supabase_url,
    is_owner_user,
    verify_access_token,
)

MAX_BODY_BYTES = 4096
logger = logging.getLogger(__name__)


def respond(handler, status, payload):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header(
        "Content-Type", "application/json; charset=utf-8"
    )
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
    handler.wfile.write(body)


def firebase_app():
    if firebase_admin._apps:
        return firebase_admin.get_app()

    raw = os.environ.get(
        "FIREBASE_SERVICE_ACCOUNT_JSON", ""
    ).strip()

    if not raw:
        raise RuntimeError(
            "Firebase service account is not configured."
        )

    service_account = json.loads(raw)

    return firebase_admin.initialize_app(
        credentials.Certificate(service_account)
    )


def registered_tokens(user_id):
    base = get_supabase_url().rstrip("/")
    key = get_supabase_service_key()

    query = urllib.parse.urlencode({
        "user_id": "eq." + user_id,
        "active": "eq.true",
        "select": "device_token",
    })

    request = urllib.request.Request(
        base + "/rest/v1/device_tokens?" + query,
        headers={
            "apikey": key,
            "Authorization": "Bearer " + key,
            "Accept": "application/json",
        },
        method="GET",
    )

    with urllib.request.urlopen(request, timeout=12) as response:
        rows = json.loads(response.read().decode("utf-8"))

    return [
        str(row["device_token"]).strip()
        for row in rows
        if isinstance(row, dict) and row.get("device_token")
    ]


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
            token = extract_bearer_token(self)
            user = verify_access_token(token)

            if not isinstance(user, dict) or not user.get("id"):
                respond(self, 401, {"error": "Invalid session."})
                return

            if not is_owner_user(user):
                respond(
                    self, 403,
                    {"error": "Owner access required."}
                )
                return

            length = int(self.headers.get("Content-Length", "0"))

            if length <= 0 or length > MAX_BODY_BYTES:
                respond(
                    self, 400,
                    {"error": "Invalid request body."}
                )
                return

            data = json.loads(
                self.rfile.read(length).decode("utf-8")
            )

            if not isinstance(data, dict):
                respond(
                    self, 400,
                    {"error": "Invalid request body."}
                )
                return

            title = str(
                data.get("title") or "LM Analyzer"
            ).strip()[:100]

            body = str(
                data.get("body")
                or "Your push notification test succeeded."
            ).strip()[:500]

            if not title or not body:
                respond(
                    self, 400,
                    {"error": "Title and message are required."}
                )
                return

            tokens = registered_tokens(str(user["id"]))

            if not tokens:
                respond(self, 404, {
                    "error": (
                        "No active device tokens found for this account."
                    )
                })
                return

            firebase_app()
            sent = 0
            failed = 0

            for device_token in tokens:
                try:
                    message = messaging.Message(
                        notification=messaging.Notification(
                            title=title,
                            body=body,
                        ),
                        data={"type": "lm_test"},
                        token=device_token,
                        android=messaging.AndroidConfig(
                            priority="high"
                        ),
                    )

                    message_id = messaging.send(message)
                    sent += 1

                    # Log the Firebase message ID, never the device token.
                    logger.info(
                        "LM Analyzer FCM accepted a message: %s",
                        message_id,
                    )

                except Exception as exc:
                    failed += 1

                    # Log diagnostic details without logging the token.
                    logger.exception(
                        "LM Analyzer FCM send failed (%s).",
                        type(exc).__name__,
                    )

            respond(self, 200 if sent else 502, {
                "ok": sent > 0,
                "sent": sent,
                "failed": failed,
                "message": (
                    "Firebase accepted the notification message."
                    if sent
                    else "Firebase could not accept the notification."
                ),
            })

        except ValueError as exc:
            respond(self, 401, {"error": str(exc)})

        except urllib.error.HTTPError as exc:
            logger.error(
                "LM Analyzer notification token lookup failed "
                "with HTTP status %s.",
                exc.code,
            )
            respond(self, 502, {
                "error": "Could not access registered device tokens."
            })

        except Exception as exc:
            logger.exception(
                "LM Analyzer notification endpoint failed (%s).",
                type(exc).__name__,
            )
            respond(self, 500, {
                "error": (
                    "Notification sending failed. Check server logs."
                )
            })

    def do_GET(self):
        respond(self, 405, {"error": "POST required."})
