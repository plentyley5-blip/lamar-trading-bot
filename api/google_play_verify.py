import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

from api.user_security import extract_bearer_token, verify_access_token
from google_play_common import (
    activate_subscription,
    google_play_subscription,
    subscription_details,
    update_subscription_status,
)


def _send(handler, code, payload):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
    handler.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_body(handler):
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0 or length > 64 * 1024:
        raise ValueError("Invalid purchase verification request.")
    data = json.loads(handler.rfile.read(length).decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Invalid purchase verification request.")
    return data


def _account_hash(user_id):
    normalized = str(user_id or "").strip().lower()
    if not normalized:
        raise ValueError("Your LM Analyzer account ID is missing.")
    return hashlib.sha256(("lm-analyzer-account:" + normalized).encode("utf-8")).hexdigest()


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        _send(self, 204, {})

    def do_POST(self):
        try:
            access_token = extract_bearer_token(self)
            user = verify_access_token(access_token)
            user_id = str(user.get("id", "")).strip()
            if not user_id:
                _send(self, 401, {"error": "Your LM Analyzer session is invalid."})
                return

            request_data = _read_body(self)
            product_id = str(request_data.get("product_id", "")).strip()
            purchase_token = str(request_data.get("purchase_token", "")).strip()
            expected_product = os.getenv("GOOGLE_PLAY_SUBSCRIPTION_PRODUCT_ID", "lm_analyzer_premium").strip()
            if product_id != expected_product or not purchase_token or len(purchase_token) > 8192:
                _send(self, 400, {"error": "Invalid Google Play purchase details."})
                return

            purchase = google_play_subscription(purchase_token)
            external = purchase.get("externalAccountIdentifiers") or {}
            supplied_hash = str(external.get("obfuscatedExternalAccountId", "")).strip()
            expected_hash = _account_hash(user_id)
            if not supplied_hash or not hmac.compare_digest(supplied_hash, expected_hash):
                _send(self, 403, {"error": "This Google Play purchase is not linked to the signed-in LM Analyzer account."})
                return

            state = str(purchase.get("subscriptionState", "")).strip()
            if state in {"SUBSCRIPTION_STATE_PENDING", "SUBSCRIPTION_STATE_PENDING_PURCHASE_CANCELED"}:
                pending_status = "pending" if state == "SUBSCRIPTION_STATE_PENDING" else "expired"
                # A pending purchase has no startTime yet. Do not attempt to activate it.
                update_subscription_status(purchase_token, pending_status)
                _send(self, 409, {
                    "active": False,
                    "status": pending_status,
                    "error": "Google Play has not completed this payment. LM Analyzer has not activated the subscription."
                })
                return

            cancel_context = purchase.get("canceledStateContext") or {}
            if state == "SUBSCRIPTION_STATE_CANCELED" and cancel_context.get("replacementCancellation") is not None:
                update_subscription_status(purchase_token, "replaced")
                _send(self, 409, {"active": False, "status": "replaced", "error": "This Google Play subscription has been replaced by another plan."})
                return

            details = subscription_details(purchase)
            now = datetime.now(timezone.utc)
            expiry_dt = details["expiry_dt"]
            access_states = {
                "SUBSCRIPTION_STATE_ACTIVE",
                "SUBSCRIPTION_STATE_IN_GRACE_PERIOD",
                "SUBSCRIPTION_STATE_CANCELED",
            }
            if state not in access_states or expiry_dt <= now:
                status_map = {
                    "SUBSCRIPTION_STATE_PAUSED": "paused",
                    "SUBSCRIPTION_STATE_ON_HOLD": "on_hold",
                    "SUBSCRIPTION_STATE_EXPIRED": "expired",
                    "SUBSCRIPTION_STATE_PENDING": "pending",
                    "SUBSCRIPTION_STATE_PENDING_PURCHASE_CANCELED": "expired",
                }
                local_status = status_map.get(state, "inactive")
                update_subscription_status(purchase_token, local_status, details["expires_at"])
                _send(self, 409, {"active": False, "status": local_status, "error": "Google Play has not granted an active subscription entitlement."})
                return

            saved = activate_subscription(user_id, purchase_token, details)
            if saved.get("status") != "active":
                _send(self, 409, {"active": False, "error": "This Google Play purchase is already linked to another LM Analyzer account."})
                return

            _send(self, 200, {
                "active": True,
                "status": "active",
                "plan": details["plan"],
                "starts_at": details["starts_at"],
                "expires_at": details["expires_at"],
                "message": "Google Play purchase verified and LM Analyzer subscription activated.",
            })
        except ValueError as exc:
            _send(self, 400, {"error": str(exc)[:220]})
        except RuntimeError as exc:
            _send(self, 503, {"error": str(exc)[:220]})
        except Exception:
            _send(self, 500, {"error": "Google Play purchase verification could not be completed."})

    def log_message(self, format, *args):
        return
