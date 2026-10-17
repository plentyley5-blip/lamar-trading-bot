
import json
import os
import hmac
import uuid
import urllib.parse
from http.server import BaseHTTPRequestHandler

from api.user_security import (
    extract_bearer_token,
    verify_access_token,
)
from pesapal_common import (
    pesapal_api,
    supabase_rest,
    process_pesapal_payment,
)


PLANS = {
    "monthly": {"name": "Monthly", "amount": 21.23, "months": 1},
    "three_months": {"name": "3 Months", "amount": 63.69, "months": 3},
    "six_months": {"name": "6 Months", "amount": 127.38, "months": 6},
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


def send_page(handler, status, title, message):
    from html import escape

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


def get_query_params(handler):
    return urllib.parse.parse_qs(
        urllib.parse.urlparse(handler.path).query
    )


def safe_pesapal_message(result):
    """Extract a useful error without exposing credentials."""
    if not isinstance(result, dict):
        return "Pesapal returned an unexpected response."

    error = result.get("error")

    if isinstance(error, dict):
        parts = [
            str(error.get(key, "")).strip()
            for key in ("code", "type", "message")
            if error.get(key)
        ]
        if parts:
            return "Pesapal rejected the request: " + " - ".join(parts)[:700]

    if isinstance(error, str) and error.strip():
        return "Pesapal rejected the request: " + error.strip()[:700]

    message = result.get("message")
    if message:
        return "Pesapal response: " + str(message)[:700]

    return "Pesapal did not return a valid checkout URL."


def register_ipn():
    ipn_url = os.environ.get("PESAPAL_IPN_URL", "").strip()

    if not ipn_url.startswith("https://"):
        raise RuntimeError(
            "Configure HTTPS PESAPAL_IPN_URL in Vercel."
        )

    return pesapal_api(
        "/api/URLSetup/RegisterIPN",
        method="POST",
        payload={
            "url": ipn_url,
            "ipn_notification_type": "GET",
        },
    )


def create_order(user, body):
    requested_plan = str(body.get("plan", "")).strip().lower()
    plan_key = PLAN_ALIASES.get(requested_plan)

    if not plan_key:
        raise ValueError(
            "Invalid plan. Use monthly, three_months, or six_months."
        )

    ipn_id = os.environ.get("PESAPAL_IPN_ID", "").strip()
    callback_url = os.environ.get("PESAPAL_CALLBACK_URL", "").strip()

    if not ipn_id:
        raise RuntimeError("PESAPAL_IPN_ID is missing in Vercel.")

    if not callback_url.startswith("https://"):
        raise RuntimeError(
            "PESAPAL_CALLBACK_URL must be a valid HTTPS URL."
        )

    plan = PLANS[plan_key]
    merchant_reference = "LM-" + uuid.uuid4().hex

    metadata = user.get("user_metadata") or {}
    email = str(user.get("email") or "").strip()

    if not email:
        raise ValueError(
            "Your account has no email address. Please sign in again."
        )

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

    # Save the order before contacting Pesapal.
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
        raise RuntimeError(
            "Supabase did not confirm that the payment order was saved."
        )

    payload = {
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

    # Submit the order to Pesapal.
    result = pesapal_api(
        "/api/Transactions/SubmitOrderRequest",
        method="POST",
        payload=payload,
    )

    if not isinstance(result, dict):
        print(
            "Pesapal checkout failure: unexpected response type:",
            type(result).__name__,
        )
        raise RuntimeError(
            "Pesapal returned an unexpected response format."
        )

    tracking_id = (
        result.get("order_tracking_id")
        or result.get("orderTrackingId")
    )

    redirect_url = (
        result.get("redirect_url")
        or result.get("redirectUrl")
    )

    # A response containing an error is not a successful order.
    if (
        result.get("error")
        and not redirect_url
    ) or not tracking_id or not redirect_url:
        diagnostic_message = safe_pesapal_message(result)

        # Do not print credentials, authorization headers, or tokens.
        print(
            "Pesapal checkout failure:",
            json.dumps({
                "merchant_reference": merchant_reference,
                "response_keys": sorted(
                    str(key) for key in result.keys()
                ),
                "error": (
                    result.get("error")
                    if isinstance(result.get("error"), (str, int, float, type(None)))
                    else {
                        key: result["error"].get(key)
                        for key in ("type", "code", "message")
                        if key in result["error"]
                    }
                    if isinstance(result.get("error"), dict)
                    else "unrecognized error format"
                ),
                "message": result.get("message"),
                "status": result.get("status"),
            }, default=str)[:2000],
        )

        raise RuntimeError(diagnostic_message)

    if not isinstance(redirect_url, str):
        raise RuntimeError(
            "Pesapal returned an invalid checkout URL."
        )

    parsed_redirect = urllib.parse.urlparse(redirect_url)

    if (
        parsed_redirect.scheme != "https"
        or not parsed_redirect.netloc
    ):
        raise RuntimeError(
            "Pesapal returned an invalid HTTPS checkout URL."
        )

    # Save the tracking ID before returning the checkout URL.
    query = urllib.parse.urlencode({
        "merchant_reference": "eq." + merchant_reference
    })

    supabase_rest(
        "payment_orders?" + query,
        method="PATCH",
        payload={
            "pesapal_order_tracking_id": tracking_id,
        },
        prefer="return=minimal",
    )

    return {
        "success": True,
        "plan": plan["name"],
        "amount": plan["amount"],
        "currency": "USD",
        "merchant_reference": merchant_reference,
        "order_tracking_id": tracking_id,
        "redirect_url": redirect_url,
    }


def handle_ipn(handler, params):
    tracking_id = params.get("OrderTrackingId", [""])[0]
    merchant_reference = params.get(
        "OrderMerchantReference", [""]
    )[0]
    notification_type = params.get(
        "OrderNotificationType", ["IPNCHANGE"]
    )[0]

    try:
        result = process_pesapal_payment(
            tracking_id,
            merchant_reference,
        )

        print("Pesapal IPN result:", result.get("status"))

        return send_json(
            handler,
            200,
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
            handler,
            500,
            {
                "orderNotificationType": notification_type,
                "orderTrackingId": tracking_id,
                "orderMerchantReference": merchant_reference,
                "status": 500,
            },
        )


def handle_callback(handler, params):
    tracking_id = params.get("OrderTrackingId", [""])[0]
    merchant_reference = params.get(
        "OrderMerchantReference", [""]
    )[0]

    try:
        result = process_pesapal_payment(
            tracking_id,
            merchant_reference,
        )

        status = result.get("status")

        if status in ("completed", "already_completed"):
            return send_page(
                handler,
                200,
                "Payment confirmed",
                "Your payment has been verified. Check your subscription in the app.",
            )

        if status == "pending":
            return send_page(
                handler,
                200,
                "Payment pending",
                "Pesapal has not confirmed the payment yet. Check the app again shortly.",
            )

        return send_page(
            handler,
            200,
            "Payment not confirmed",
            "The payment has not been confirmed as successful. Contact support if you paid.",
        )

    except Exception as exc:
        print("Pesapal callback error:", str(exc))

        return send_page(
            handler,
            500,
            "Unable to verify payment",
            "We could not verify this transaction right now. Check the app again shortly.",
        )


class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        params = get_query_params(self)
        action = params.get("action", ["callback"])[0].lower()

        if action == "ipn":
            return handle_ipn(self, params)

        if action == "callback":
            return handle_callback(self, params)

        return send_json(
            self,
            400,
            {"error": "Unknown action"},
        )

    def do_POST(self):
        params = get_query_params(self)
        action = params.get("action", ["create"])[0].lower()

        if action == "register":
            expected = os.environ.get(
                "PESAPAL_SETUP_SECRET", ""
            )
            supplied = self.headers.get(
                "X-Setup-Secret", ""
            )

            if (
                not expected
                or not supplied
                or not hmac.compare_digest(expected, supplied)
            ):
                return send_json(
                    self,
                    401,
                    {"error": "Unauthorized"},
                )

            try:
                result = register_ipn()

                ipn_id = (
                    result.get("ipn_id")
                    or result.get("ipnId")
                ) if isinstance(result, dict) else None

                if not ipn_id:
                    print(
                        "Pesapal IPN registration failed:",
                        safe_pesapal_message(result),
                    )

                    return send_json(
                        self,
                        502,
                        {
                            "error": safe_pesapal_message(result),
                        },
                    )

                return send_json(
                    self,
                    200,
                    {
                        "success": True,
                        "ipn_id": ipn_id,
                        "message": (
                            "Save this value as PESAPAL_IPN_ID in Vercel."
                        ),
                    },
                )

            except Exception as exc:
                print("Pesapal registration error:", str(exc))

                return send_json(
                    self,
                    502,
                    {
                        "error": (
                            "IPN registration failed. "
                            "Check the server logs."
                        ),
                    },
                )

        if action != "create":
            return send_json(
                self,
                400,
                {"error": "Unknown action"},
            )

        try:
            token = extract_bearer_token(self)
            user = verify_access_token(token) if token else None

            if not isinstance(user, dict) or not user.get("id"):
                return send_json(
                    self,
                    401,
                    {"error": "Please sign in again"},
                )

            length = int(
                self.headers.get("Content-Length", "0")
            )

            if length < 1 or length > 20000:
                return send_json(
                    self,
                    400,
                    {"error": "Invalid request body"},
                )

            body = json.loads(
                self.rfile.read(length).decode("utf-8")
            )

            if not isinstance(body, dict):
                return send_json(
                    self,
                    400,
                    {"error": "Invalid request body"},
                )

            result = create_order(user, body)

            return send_json(
                self,
                200,
                result,
            )

        except ValueError as exc:
            return send_json(
                self,
                400,
                {"error": str(exc)},
            )

        except Exception as exc:
            # Keep the detailed reason in server logs.
            print(
                "Pesapal create-order error:",
                type(exc).__name__,
                str(exc)[:1000],
            )

            # Return the useful error to the client for diagnosis.
            # Never return request headers, tokens, or credentials.
            return send_json(
                self,
                502,
                {
                    "success": False,
                    "error": str(exc)[:700],
                    "message": "Payment checkout could not be created.",
                },
            )

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()
