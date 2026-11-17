import base64
import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

from google_play_common import (
    activate_subscription,
    get_subscription_by_token,
    google_play_subscription,
    subscription_details,
    update_subscription_status,
)


def _send(handler, code, payload):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _verify_pubsub_oidc(handler):
    header = str(handler.headers.get("Authorization", "")).strip()
    if not header.lower().startswith("bearer "):
        raise ValueError("Missing authenticated Pub/Sub identity.")
    audience = os.getenv("GOOGLE_PLAY_RTDN_AUDIENCE", "").strip()
    expected_email = os.getenv("GOOGLE_PLAY_RTDN_SERVICE_ACCOUNT_EMAIL", "").strip().lower()
    if not audience or not expected_email:
        raise RuntimeError("Google Play RTDN authentication is not configured.")
    try:
        from google.oauth2 import id_token
        from google.auth.transport.requests import Request
        claims = id_token.verify_oauth2_token(header[7:].strip(), Request(), audience=audience)
    except Exception as exc:
        raise ValueError("Pub/Sub identity token is invalid.") from exc
    actual_email = str(claims.get("email", "")).strip().lower()
    if actual_email != expected_email or str(claims.get("email_verified", "")).lower() != "true":
        raise ValueError("Pub/Sub service account is not authorised for this endpoint.")


def _read_push(handler):
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0 or length > 256 * 1024:
        raise ValueError("Invalid Pub/Sub notification envelope.")
    envelope = json.loads(handler.rfile.read(length).decode("utf-8"))
    message = envelope.get("message") if isinstance(envelope, dict) else None
    encoded = message.get("data") if isinstance(message, dict) else None
    if not encoded:
        raise ValueError("Pub/Sub notification has no message data.")
    raw = base64.b64decode(encoded, validate=True)
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Invalid Google Play notification payload.")
    return payload


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            _verify_pubsub_oidc(self)
            notification = _read_push(self)
            expected_package = os.getenv("GOOGLE_PLAY_PACKAGE_NAME", "com.lamanalyzer.app").strip()
            if str(notification.get("packageName", "")) != expected_package:
                _send(self, 400, {"error": "Notification package does not match LM Analyzer."})
                return
            if notification.get("testNotification"):
                _send(self, 200, {"ok": True, "message": "Google Play RTDN test notification received."})
                return

            subscription_event = notification.get("subscriptionNotification") or {}
            voided_event = notification.get("voidedPurchaseNotification") or {}
            purchase_token = str(subscription_event.get("purchaseToken") or voided_event.get("purchaseToken") or "").strip()
            if not purchase_token:
                _send(self, 200, {"ok": True, "ignored": True})
                return

            existing = get_subscription_by_token(purchase_token)
            if voided_event:
                if existing:
                    update_subscription_status(purchase_token, "revoked")
                _send(self, 200, {"ok": True, "status": "revoked" if existing else "unlinked_voided_purchase"})
                return

            if not subscription_event:
                _send(self, 200, {"ok": True, "ignored": True})
                return

            purchase = google_play_subscription(purchase_token)
            state = str(purchase.get("subscriptionState", "")).strip()

            # Pending purchases do not have a startTime yet and must never grant access.
            if state in {"SUBSCRIPTION_STATE_PENDING", "SUBSCRIPTION_STATE_PENDING_PURCHASE_CANCELED"}:
                local_status = "pending" if state == "SUBSCRIPTION_STATE_PENDING" else "expired"
                if existing:
                    update_subscription_status(purchase_token, local_status)
                _send(self, 200, {"ok": True, "status": local_status})
                return

            cancel_context = purchase.get("canceledStateContext") or {}
            if state == "SUBSCRIPTION_STATE_CANCELED" and cancel_context.get("replacementCancellation") is not None:
                if existing:
                    update_subscription_status(purchase_token, "replaced")
                _send(self, 200, {"ok": True, "status": "replaced"})
                return

            details = subscription_details(purchase)
            now = datetime.now(timezone.utc)

            # A replacement purchase can arrive before the app's purchase callback.
            if not existing and details["linked_purchase_token"]:
                existing = get_subscription_by_token(details["linked_purchase_token"])
            if not existing:
                # The app verification endpoint will claim the purchase once the signed-in account is available.
                _send(self, 200, {"ok": True, "deferred": True})
                return

            if state in {
                "SUBSCRIPTION_STATE_ACTIVE",
                "SUBSCRIPTION_STATE_IN_GRACE_PERIOD",
                "SUBSCRIPTION_STATE_CANCELED",
            } and details["expiry_dt"] > now:
                result = activate_subscription(existing["user_id"], purchase_token, details)
                _send(self, 200, {"ok": True, "status": result.get("status", "active")})
                return

            state_map = {
                "SUBSCRIPTION_STATE_PAUSED": "paused",
                "SUBSCRIPTION_STATE_ON_HOLD": "on_hold",
                "SUBSCRIPTION_STATE_EXPIRED": "expired",
                "SUBSCRIPTION_STATE_PENDING": "pending",
                "SUBSCRIPTION_STATE_PENDING_PURCHASE_CANCELED": "expired",
            }
            local_status = state_map.get(state, "inactive")
            update_subscription_status(purchase_token, local_status, details.get("expires_at"))
            _send(self, 200, {"ok": True, "status": local_status})
        except ValueError as exc:
            _send(self, 401, {"error": str(exc)[:180]})
        except RuntimeError as exc:
            _send(self, 503, {"error": str(exc)[:180]})
        except Exception:
            _send(self, 500, {"error": "Google Play notification could not be processed."})

    def log_message(self, format, *args):
        return
