
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
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
    response_body = json.dumps(payload).encode("utf-8")
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
    handler.send_header("Content-Length", str(len(response_body)))
    handler.end_headers()
    handler.wfile.write(response_body)


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

    if not isinstance(rows, list):
        raise RuntimeError(
            "Device-token lookup returned an unexpected response."
        )

    # Deduplicate tokens without logging or exposing their values.
    tokens = []
    seen = set()

    for row in rows:
        if not isinstance(row, dict):
            continue

        device_token = str(row.get("device_token") or "").strip()

        if device_token and device_token not in seen:
            seen.add(device_token)
            tokens.append(device_token)

    return tokens


def is_permanently_invalid_token(exc):
    """
    Return True only for Firebase errors indicating that the token
    cannot be used by this Firebase project.

    Do not deactivate tokens for timeouts, quota errors, or general
    server/network failures.
    """
    error_name = type(exc).__name__

    if error_name in {
        "UnregisteredError",
        "SenderIdMismatchError",
    }:
        return True

    error_code = str(
        getattr(exc, "code", "") or ""
    ).strip().upper()

    if error_code in {
        "UNREGISTERED",
        "SENDER_ID_MISMATCH",
    }:
        return True

    return False


def deactivate_token(user_id, device_token):
    """
    Mark only the matching user's invalid token inactive.
    Uses the existing device_tokens table and active column.
    """
    base = get_supabase_url().rstrip("/")
    key = get_supabase_service_key()

    query = urllib.parse.urlencode({
        "user_id": "eq." + user_id,
        "device_token": "eq." + device_token,
    })

    request = urllib.request.Request(
        base + "/rest/v1/device_tokens?" + query,
        data=json.dumps({"active": False}).encode("utf-8"),
        headers={
            "apikey": key,
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Prefer": "return=minimal",
        },
        method="PATCH",
    )

    with urllib.request.urlopen(request, timeout=12) as response:
        # A successful Supabase PATCH confirms the update request
        # was accepted. The token itself is never logged.
        return response.status in (200, 204)


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
            access_token = extract_bearer_token(self)
            user = verify_access_token(access_token)

            if not isinstance(user, dict) or not user.get("id"):
                respond(self, 401, {"error": "Invalid session."})
                return

            if not is_owner_user(user):
                respond(self, 403, {
                    "error": "Owner access required."
                })
                return

            try:
                length = int(
                    self.headers.get("Content-Length", "0")
                )
            except (TypeError, ValueError):
                length = 0

            if length <= 0 or length > MAX_BODY_BYTES:
                respond(self, 400, {
                    "error": "Invalid request body."
                })
                return

            try:
                data = json.loads(
                    self.rfile.read(length).decode("utf-8")
                )
            except (UnicodeDecodeError, json.JSONDecodeError):
                respond(self, 400, {
                    "error": "Request body must be valid JSON."
                })
                return

            if not isinstance(data, dict):
                respond(self, 400, {
                    "error": "Invalid request body."
                })
                return

            title = str(
                data.get("title") or "LM Analyzer"
            ).strip()[:100]

            body = str(
                data.get("body")
                or "Your push notification test succeeded."
            ).strip()[:500]

            if not title or not body:
                respond(self, 400, {
                    "error": "Title and message are required."
                })
                return

            user_id = str(user["id"])
            tokens = registered_tokens(user_id)

            if not tokens:
                respond(self, 404, {
                    "ok": False,
                    "sent": 0,
                    "failed": 0,
                    "deactivated": 0,
                    "error": (
                        "No active device tokens found for this account."
                    ),
                })
                return

            firebase_app()

            sent = 0
            failed = 0
            deactivated = 0

            for device_token in tokens:
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

                try:
                    message_id = messaging.send(message)
                    sent += 1

                    # Never log device tokens or credentials.
                    logger.info(
                        "LM Analyzer FCM accepted a message."
                    )

                except Exception as exc:
                    failed += 1
                    error_name = type(exc).__name__

                    logger.warning(
                        "LM Analyzer FCM send failed; error type: %s",
                        error_name,
                    )

                    if is_permanently_invalid_token(exc):
                        try:
                            if deactivate_token(user_id, device_token):
                                deactivated += 1
                                logger.info(
                                    "An invalid LM Analyzer device token "
                                    "was deactivated."
                                )
                            else:
                                logger.error(
                                    "Invalid device token could not be "
                                    "confirmed inactive."
                                )
                        except Exception as cleanup_exc:
                            logger.error(
                                "Could not deactivate invalid device token; "
                                "error type: %s",
                                type(cleanup_exc).__name__,
                            )

            respond(self, 200 if sent > 0 else 502, {
                "ok": sent > 0,
                "sent": sent,
                "failed": failed,
                "deactivated": deactivated,
                "message": (
                    "Firebase accepted the notification message."
                    if sent
                    else (
                        "No notification was accepted. Check the "
                        "failure details in server logs."
                    )
                ),
            })

        except ValueError as exc:
            respond(self, 401, {"error": str(exc)})

        except urllib.error.HTTPError as exc:
            logger.error(
                "LM Analyzer notification token lookup/update "
                "failed with HTTP status %s.",
                exc.code,
            )
            respond(self, 502, {
                "error": (
                    "Could not access or update registered device tokens."
                )
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
