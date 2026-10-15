
import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from api.pesapal_common import process_pesapal_payment


def send_json(handler, status, data):
    body = json.dumps(data).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class handler(BaseHTTPRequestHandler):
    def _handle_notification(self, tracking_id, merchant_reference,
                             notification_type="IPNCHANGE"):
        try:
            result = process_pesapal_payment(
                tracking_id, merchant_reference
            )
            print("Pesapal IPN:", result.get("status"))

            return send_json(
                self, 200,
                {
                    "orderNotificationType": notification_type,
                    "orderTrackingId": tracking_id,
                    "orderMerchantReference": merchant_reference,
                    "status": 200,
                },
            )
        except Exception as exc:
            print("Pesapal IPN error:", str(exc))
            return send_json(
                self, 500,
                {
                    "orderNotificationType": notification_type,
                    "orderTrackingId": tracking_id,
                    "orderMerchantReference": merchant_reference,
                    "status": 500,
                },
            )

    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        tracking_id = params.get("OrderTrackingId", [""])[0]
        merchant_reference = params.get(
            "OrderMerchantReference", [""]
        )[0]
        notification_type = params.get(
            "OrderNotificationType", ["IPNCHANGE"]
        )[0]

        return self._handle_notification(
            tracking_id, merchant_reference, notification_type
        )

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(
                self.rfile.read(length).decode("utf-8")
            ) if length else {}

            return self._handle_notification(
                body.get("OrderTrackingId", ""),
                body.get("OrderMerchantReference", ""),
                body.get("OrderNotificationType", "IPNCHANGE"),
            )
        except Exception as exc:
            print("Pesapal IPN request error:", str(exc))
            return send_json(self, 400, {"status": 500})
