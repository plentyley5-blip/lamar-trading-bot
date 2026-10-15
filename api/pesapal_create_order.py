
import json
import os
import uuid
import urllib.parse
from http.server import BaseHTTPRequestHandler

from api.user_security import extract_bearer_token, verify_access_token
from api.pesapal_common import (
    pesapal_api,
    supabase_rest,
)


PLANS = {
    "monthly": {
        "name": "Monthly",
        "amount": 21.23,
        "months": 1,
    },
    "three_months": {
        "name": "3 Months",
        "amount": 63.69,
        "months": 3,
    },
    "six_months": {
        "name": "6 Months",
        "amount": 127.38,
        "months": 6,
    },
}

PLAN_ALIASES = {
    "monthly": "monthly",
    "1_month": "monthly",
    "1 month": "monthly",
    "three_months": "three_months",
    "3_months": "three_months",
    "3 months": "three_months",
    "quarterly": "three_months",
    "six_months": "six_months",
    "6_months": "six_months",
    "6 months": "six_months",
    "half_yearly": "six_months",
}


def send_json(handler, status, data):
    body = json.dumps(data).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            token = extract_bearer_token(self)
            user = verify_access_token(token) if token else None

            if not isinstance(user, dict) or not user.get("id"):
                return send_json(
                    self, 401, {"error": "Please sign in again"}
                )

            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 20000:
                return send_json(
                    self, 400, {"error": "Invalid request body"}
                )

            body = json.loads(self.rfile.read(length).decode("utf-8"))
            requested_plan = str(body.get("plan", "")).strip().lower()
            plan_key = PLAN_ALIASES.get(requested_plan)

            if not plan_key:
                return send_json(
                    self, 400,
                    {
                        "error": "Invalid plan",
                        "valid_plans": list(PLANS.keys()),
                    },
                )

            ipn_id = os.environ.get("PESAPAL_IPN_ID", "").strip()
            callback_url = os.environ.get(
                "PESAPAL_CALLBACK_URL", ""
            ).strip()

            if not ipn_id or not callback_url:
                return send_json(
                    self, 503,
                    {
                        "error": (
                            "Pesapal setup is incomplete. "
                            "Configure PESAPAL_IPN_ID and callback URL."
                        )
                    },
                )

            plan = PLANS[plan_key]
            merchant_reference = "LM-" + uuid.uuid4().hex

            # Create the pending order before sending it to Pesapal.
            created = supabase_rest(
                "payment_orders",
                method="POST",
                payload={
                    "user_id": user["id"],
                    "plan": plan["name"],
                    "amount": plan["amount"],
                    "currency": "USD",
                    "amount_ugx": None,
                    "duration_months": plan["months"],
                    "merchant_reference": merchant_reference,
                    "status": "pending",
                },
                prefer="return=representation",
            )

            if not isinstance(created, list) or not created:
                raise RuntimeError("Could not save the payment order")

            metadata = user.get("user_metadata") or {}
            email = user.get("email") or ""
            full_name = str(
                metadata.get("full_name")
                or metadata.get("name")
                or ""
            ).strip()
            name_parts = full_name.split(maxsplit=1)

            first_name = name_parts[0] if name_parts else "LM"
            last_name = name_parts[1] if len(name_parts) > 1 else "Analyzer"
            phone = str(
                metadata.get("phone_number")
                or user.get("phone")
                or ""
            ).strip()

            order_payload = {
                "id": merchant_reference,
                "currency": "USD",
                "amount": plan["amount"],
                "description": "LM Analyzer - " + plan["name"],
                "callback_url": callback_url,
                "notification_id": ipn_id,
                "billing_address": {
                    "email_address": email,
                    "phone_number": phone,
                    "country_code": "UG",
                    "first_name": first_name[:50],
                    "middle_name": "",
                    "last_name": last_name[:50],
                    "line_1": "",
                    "line_2": "",
                    "city": "",
                    "state": "",
                    "postal_code": "",
                    "zip_code": "",
                },
            }

            result = pesapal_api(
                "/api/Transactions/SubmitOrderRequest",
                method="POST",
                payload=order_payload,
            )

            tracking_id = result.get("order_tracking_id")
            redirect_url = result.get("redirect_url")

            if not tracking_id or not redirect_url:
                raise RuntimeError(
                    "Pesapal did not return a payment redirect URL"
                )

            query = urllib.parse.urlencode({
                "merchant_reference": "eq." + merchant_reference
            })
            supabase_rest(
                "payment_orders?" + query,
                method="PATCH",
                payload={
                    "pesapal_order_tracking_id": tracking_id
                },
                prefer="return=minimal",
            )

            return send_json(
                self, 200,
                {
                    "success": True,
                    "plan": plan["name"],
                    "amount": plan["amount"],
                    "currency": "USD",
                    "merchant_reference": merchant_reference,
                    "order_tracking_id": tracking_id,
                    "redirect_url": redirect_url,
                },
            )

        except Exception as exc:
            # Details are sent to Vercel logs, not exposed to the app.
            print("Pesapal create-order error:", str(exc))
            return send_json(
                self, 500,
                {"error": "Could not create the payment. Please try again."},
            )

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()
