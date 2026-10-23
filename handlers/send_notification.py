"""Authenticated LM Analyzer remote push test endpoint."""
import json
from http.server import BaseHTTPRequestHandler

from api.user_security import extract_bearer_token, verify_access_token
from api.lm_push import notify_user_ids


def _send(handler, status, payload):
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
    handler.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    if status != 204:
        handler.wfile.write(raw)


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        _send(self, 204, {})

    def do_POST(self):
        try:
            token = extract_bearer_token(self)
            user = verify_access_token(token)
            user_id = str(user.get("id", "")).strip() if isinstance(user, dict) else ""
            if not user_id:
                _send(self, 401, {"error": "A valid signed-in session is required."})
                return
            result = notify_user_ids(
                [user_id],
                title="LM Analyzer Remote Test",
                body="Your LM Analyzer remote push notification is working.",
                data={"type": "remote_test"},
            )
            sent = int(result.get("sent", 0))
            failed = int(result.get("failed", 0))
            if sent > 0:
                _send(self, 200, {"success": True, "sent": sent, "failed": failed, "message": "Firebase accepted the push request."})
            elif failed > 0:
                _send(self, 502, {"success": False, "sent": 0, "failed": failed, "error": "Firebase rejected all registered device tokens. Reopen the app while signed in to refresh registration, then check the Firebase service account and device token status."})
            else:
                _send(self, 200, {"success": False, "sent": 0, "failed": 0, "message": "No active registered device token was found for this account. Open LM Analyzer on the phone while signed in, allow notifications, wait a few seconds, then retry."})
        except Exception as exc:
            # Do not return exception text because it can contain infrastructure details.
            print("remote push test failed; error_type={}".format(type(exc).__name__))
            _send(self, 503, {"success": False, "sent": 0, "failed": 0, "error": "Remote push setup failed. Verify Firebase service-account environment variables, firebase-admin dependency, and Supabase server credentials in Vercel."})
