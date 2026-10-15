
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from html import escape

from api.pesapal_common import process_pesapal_payment


def send_page(handler, status, title, message):
    page = (
        "<!doctype html><html><head>"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>LM Analyzer Payment</title></head>"
        '<body style="font-family:Arial,sans-serif;background:#061A33;'
        'color:white;padding:32px;line-height:1.6">'
        '<main style="max-width:520px;margin:40px auto;'
        'background:#0B2748;padding:24px;border-radius:18px">'
        "<h2>" + escape(title) + "</h2>"
        "<p>" + escape(message) + "</p>"
        "<p>You can now return to the LM Analyzer app.</p>"
        "</main></body></html>"
    ).encode("utf-8")

    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(page)))
    handler.end_headers()
    handler.wfile.write(page)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        tracking_id = params.get("OrderTrackingId", [""])[0]
        merchant_reference = params.get(
            "OrderMerchantReference", [""]
        )[0]

        try:
            result = process_pesapal_payment(
                tracking_id, merchant_reference
            )
            status = result.get("status")

            if status in ("completed", "already_completed"):
                return send_page(
                    self, 200, "Payment confirmed",
                    "Your payment is verified. Your subscription has been activated."
                )

            if status == "pending":
                return send_page(
                    self, 200, "Payment pending",
                    "Pesapal has not confirmed this payment yet. "
                    "Please check your subscription status in the app shortly."
                )

            return send_page(
                self, 200, "Payment not completed",
                "The payment was not confirmed as successful. "
                "If you believe you paid, contact LM Analyzer support."
            )

        except Exception as exc:
            print("Pesapal callback error:", str(exc))
            return send_page(
                self, 500, "Unable to verify payment",
                "We could not verify this transaction right now. "
                "Please check the app again shortly."
            )
