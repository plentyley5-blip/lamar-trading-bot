
import os
import hmac
import json
from http.server import BaseHTTPRequestHandler

from api.pesapal_common import pesapal_api


def send_json(handler, status, data):
    body = json.dumps(data).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        expected = os.environ.get("PESAPAL_SETUP_SECRET", "")
        supplied = self.headers.get("X-Setup-Secret", "")

        if not expected or not supplied or not hmac.compare_digest(
            expected, supplied
        ):
            return send_json(self, 401, {"error": "Unauthorized"})

        ipn_url = os.environ.get("PESAPAL_IPN_URL", "").strip()
        if not ipn_url.startswith("https://"):
            return send_json(
                self, 400, {"error": "Configure a valid HTTPS PESAPAL_IPN_URL"}
            )

        try:
            result = pesapal_api(
                "/api/URLSetup/RegisterIPN",
                method="POST",
                payload={
                    "url": ipn_url,
                    "ipn_notification_type": "GET",
                },
            )

            ipn_id = result.get("ipn_id") or result.get("ipnId")
            if not ipn_id:
                print("Pesapal IPN registration response:", result)
                return send_json(
                    self, 502,
                    {"error": "Pesapal did not return an IPN ID"}
                )

            return send_json(
                self, 200,
                {
                    "success": True,
                    "ipn_id": ipn_id,
                    "message": (
                        "Save this IPN ID in Vercel as PESAPAL_IPN_ID."
                    ),
                },
            )

        except Exception as exc:
            print("Pesapal IPN registration error:", str(exc))
            return send_json(
                self, 502,
                {"error": "IPN registration failed; check Vercel logs"}
            )
