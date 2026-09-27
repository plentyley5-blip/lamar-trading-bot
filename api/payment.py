import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler


FLUTTERWAVE_URL = "https://api.flutterwave.com/v3"


PLANS = {
    "monthly": {
        "name": "Monthly",
        "amount": 82797,
        "months": 1,
    },
    "three_months": {
        "name": "3 Months - 20% OFF",
        "amount": 198713,
        "months": 3,
    },
    "six_months": {
        "name": "6 Months - 50% OFF",
        "amount": 248391,
        "months": 6,
    },
}


def env(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(name + " is not configured.")
    return value


def supabase_url():
    return env("SUPABASE_URL").rstrip("/")


def supabase_service_key():
    return env("SUPABASE_SERVICE_ROLE_KEY")


def flutterwave_secret_key():
    return env("FLW_SECRET_KEY")


def flutterwave_secret_hash():
    return env("FLW_SECRET_HASH")


def json_response(handler, status_code, payload):
    body = json.dumps(
        payload,
        ensure_ascii=False
    ).encode("utf-8")

    handler.send_response(status_code)
    handler.send_header(
        "Content-Type",
        "application/json; charset=utf-8"
    )
    handler.send_header(
        "Access-Control-Allow-Origin",
        "*"
    )
    handler.send_header(
        "Access-Control-Allow-Headers",
        "Content-Type, Authorization"
    )
    handler.send_header(
        "Access-Control-Allow-Methods",
        "GET, POST, OPTIONS"
    )
    handler.send_header(
        "Cache-Control",
        "no-store, no-cache, must-revalidate"
    )
    handler.send_header(
        "Content-Length",
        str(len(body))
    )
    handler.end_headers()

    try:
        handler.wfile.write(body)
    except Exception:
        pass


def html_response(handler, status_code, html):
    body = html.encode("utf-8")

    handler.send_response(status_code)
    handler.send_header(
        "Content-Type",
        "text/html; charset=utf-8"
    )
    handler.send_header(
        "Cache-Control",
        "no-store"
    )
    handler.send_header(
        "Content-Length",
        str(len(body))
    )
    handler.end_headers()

    try:
        handler.wfile.write(body)
    except Exception:
        pass


def request_json(
    url,
    method="GET",
    payload=None,
    headers=None,
    timeout=30
):
    body = None

    request_headers = {
        "Accept": "application/json",
    }

    if headers:
        request_headers.update(headers)

    if payload is not None:
        body = json.dumps(
            payload,
            separators=(",", ":")
        ).encode("utf-8")

        request_headers["Content-Type"] = (
            "application/json"
        )

    request = urllib.request.Request(
        url,
        data=body,
        headers=request_headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout
        ) as response:

            raw = response.read().decode(
                "utf-8"
            )

            return (
                json.loads(raw)
                if raw
                else {}
            )

    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(
            "utf-8",
            errors="replace"
        )

        try:
            detail = json.loads(raw)
        except Exception:
            detail = {
                "error": raw
            }

        raise RuntimeError(
            json.dumps(
                detail,
                ensure_ascii=False
            )
        ) from exc

    except (
        urllib.error.URLError,
        TimeoutError
    ) as exc:

        raise RuntimeError(
            "The payment service is temporarily unavailable."
        ) from exc


def supabase_headers():
    key = supabase_service_key()

    return {
        "apikey": key,
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
    }


def verify_supabase_user(access_token):
    if not access_token:
        raise ValueError(
            "Authentication is required."
        )

    url = (
        supabase_url()
        + "/auth/v1/user"
    )

    key = env(
        "SUPABASE_ANON_KEY"
    )

    return request_json(
        url,
        headers={
            "apikey": key,
            "Authorization": (
                "Bearer " + access_token
            ),
        },
    )


def read_json(handler):
    try:
        length = int(
            handler.headers.get(
                "Content-Length",
                "0"
            )
        )
    except ValueError:
        raise ValueError(
            "Invalid request."
        )

    if length <= 0 or length > 128 * 1024:
        raise ValueError(
            "Invalid request."
        )

    try:
        data = json.loads(
            handler.rfile.read(
                length
            ).decode("utf-8")
        )
    except Exception as exc:
        raise ValueError(
            "Invalid request JSON."
        ) from exc

    if not isinstance(data, dict):
        raise ValueError(
            "Invalid request."
        )

    return data


def get_access_token(handler):
    authorization = str(
        handler.headers.get(
            "Authorization",
            ""
        )
    ).strip()

    if not authorization.lower().startswith(
        "bearer "
    ):
        return ""

    return authorization[7:].strip()


def load_profile(user_id):
    query = urllib.parse.urlencode({
        "id": "eq." + str(user_id),
        "select": "id,full_name,email",
        "limit": "1",
    })

    url = (
        supabase_url()
        + "/rest/v1/profiles?"
        + query
    )

    rows = request_json(
        url,
        headers=supabase_headers()
    )

    if (
        isinstance(rows, list)
        and rows
    ):
        return rows[0]

    return {}


def create_payment_record(
    user_id,
    tx_ref,
    plan,
    amount,
    months
):
    payload = {
        "user_id": user_id,
        "tx_ref": tx_ref,
        "plan": plan,
        "amount_ugx": amount,
        "duration_months": months,
        "status": "pending",
    }

    url = (
        supabase_url()
        + "/rest/v1/payment_transactions"
    )

    headers = supabase_headers()

    headers["Prefer"] = (
        "return=representation"
    )

    result = request_json(
        url,
        method="POST",
        payload=payload,
        headers=headers,
    )

    if (
        not isinstance(result, list)
        or not result
    ):
        raise RuntimeError(
            "The payment record could not be created."
        )

    return result[0]


def find_payment(tx_ref):
    query = urllib.parse.urlencode({
        "tx_ref": "eq." + tx_ref,
        "select": "*",
        "limit": "1",
    })

    url = (
        supabase_url()
        + "/rest/v1/payment_transactions?"
        + query
    )

    rows = request_json(
        url,
        headers=supabase_headers()
    )

    if (
        isinstance(rows, list)
        and rows
    ):
        return rows[0]

    return None


def update_payment(
    tx_ref,
    status,
    transaction_id=None,
    flutterwave_reference=None
):
    payload = {
        "status": status,
        "updated_at": datetime.now(
            timezone.utc
        ).isoformat(),
    }

    if transaction_id:
        payload[
            "flutterwave_transaction_id"
        ] = str(transaction_id)

    if flutterwave_reference:
        payload[
            "flutterwave_reference"
        ] = str(
            flutterwave_reference
        )

    query = urllib.parse.urlencode({
        "tx_ref": "eq." + tx_ref,
    })

    url = (
        supabase_url()
        + "/rest/v1/payment_transactions?"
        + query
    )

    headers = supabase_headers()

    headers["Prefer"] = (
        "return=minimal"
    )

    request_json(
        url,
        method="PATCH",
        payload=payload,
        headers=headers,
    )


def activate_subscription(
    payment,
    transaction
):
    user_id = str(
        payment["user_id"]
    )

    plan = str(
        payment["plan"]
    )

    months = int(
        payment["duration_months"]
    )

    amount = int(
        payment["amount_ugx"]
    )

    transaction_id = str(
        transaction.get("id", "")
    )

    flutterwave_reference = str(
        transaction.get(
            "flw_ref",
            ""
        )
    )

    now = datetime.now(
        timezone.utc
    )

    query = urllib.parse.urlencode({
        "user_id": "eq." + user_id,
        "select": "*",
        "limit": "1",
    })

    existing_url = (
        supabase_url()
        + "/rest/v1/subscriptions?"
        + query
    )

    existing_rows = request_json(
        existing_url,
        headers=supabase_headers()
    )

    existing = (
        existing_rows[0]
        if (
            isinstance(
                existing_rows,
                list
            )
            and existing_rows
        )
        else None
    )

    if existing:
        existing_expiry = (
            existing.get("expires_at")
        )

        try:
            existing_date = datetime.fromisoformat(
                str(
                    existing_expiry
                ).replace(
                    "Z",
                    "+00:00"
                )
            )
        except Exception:
            existing_date = now

        start = (
            existing_date
            if existing_date > now
            else now
        )

    else:
        start = now

    expires = start + timedelta(
        days=30 * months
    )

    payload = {
        "user_id": user_id,
        "plan": plan,
        "amount_ugx": amount,
        "duration_months": months,
        "status": "active",
        "starts_at": start.isoformat(),
        "expires_at": expires.isoformat(),
        "flutterwave_tx_ref": str(
            payment["tx_ref"]
        ),
        "flutterwave_transaction_id": (
            transaction_id
        ),
        "updated_at": now.isoformat(),
    }

    url = (
        supabase_url()
        + "/rest/v1/subscriptions"
    )

    headers = supabase_headers()

    headers["Prefer"] = (
        "resolution=merge-duplicates,"
        "return=representation"
    )

    result = request_json(
        url,
        method="POST",
        payload=payload,
        headers=headers,
    )

    if (
        not isinstance(result, list)
        or not result
    ):
        raise RuntimeError(
            "The subscription could not be activated."
        )

    return result[0]


def verify_transaction(
    transaction_id
):
    url = (
        FLUTTERWAVE_URL
        + "/transactions/"
        + urllib.parse.quote(
            str(transaction_id),
            safe=""
        )
        + "/verify"
    )

    response = request_json(
        url,
        headers={
            "Authorization": (
                "Bearer "
                + flutterwave_secret_key()
            ),
            "Content-Type": (
                "application/json"
            ),
        },
    )

    data = response.get(
        "data"
    )

    if not isinstance(
        data,
        dict
    ):
        raise RuntimeError(
            "Flutterwave returned an invalid verification response."
        )

    return data


def verify_and_activate(
    transaction_id,
    expected_tx_ref
):
    transaction = verify_transaction(
        transaction_id
    )

    status = str(
        transaction.get(
            "status",
            ""
        )
    ).lower()

    tx_ref = str(
        transaction.get(
            "tx_ref",
            ""
        )
    )

    currency = str(
        transaction.get(
            "currency",
            ""
        )
    ).upper()

    payment = find_payment(
        expected_tx_ref
    )

    if not payment:
        raise RuntimeError(
            "Payment record was not found."
        )

    expected_amount = int(
        payment["amount_ugx"]
    )

    actual_amount = int(
        transaction.get(
            "amount",
            transaction.get(
                "charged_amount",
                0
            )
        )
        or 0
    )

    if tx_ref != expected_tx_ref:
        raise RuntimeError(
            "Transaction reference mismatch."
        )

    if status != "successful":
        raise RuntimeError(
            "Payment has not been completed."
        )

    if currency != "UGX":
        raise RuntimeError(
            "Payment currency mismatch."
        )

    if actual_amount < expected_amount:
        raise RuntimeError(
            "The amount paid is lower than the plan price."
        )

    activate_subscription(
        payment,
        transaction
    )

    update_payment(
        expected_tx_ref,
        "successful",
        transaction_id=transaction_id,
        flutterwave_reference=transaction.get(
            "flw_ref"
        ),
    )

    return payment, transaction


def create_checkout(user):
    user_id = str(
        user.get("id", "")
    ).strip()

    email = str(
        user.get("email", "")
    ).strip()

    metadata = user.get(
        "user_metadata",
        {}
    )

    if not isinstance(
        metadata,
        dict
    ):
        metadata = {}

    name = str(
        metadata.get(
            "full_name",
            email.split("@")[0]
        )
    ).strip()

    if not user_id or not email:
        raise ValueError(
            "Your account information is incomplete."
        )

    return (
        user_id,
        email,
        name
    )


def handle_create(
    handler,
    data
):
    access_token = get_access_token(
        handler
    )

    user = verify_supabase_user(
        access_token
    )

    (
        user_id,
        email,
        name
    ) = create_checkout(user)

    plan = str(
        data.get(
            "plan",
            ""
        )
    ).strip().lower()

    if plan not in PLANS:
        raise ValueError(
            "Invalid subscription plan."
        )

    details = PLANS[plan]

    tx_ref = (
        "LM-"
        + plan.upper()
        + "-"
        + secrets.token_hex(12)
    )

    create_payment_record(
        user_id=user_id,
        tx_ref=tx_ref,
        plan=plan,
        amount=details["amount"],
        months=details["months"],
    )

    callback_url = (
        "https://lamar-trading-bot.vercel.app"
        "/api/payment"
        "?action=callback"
    )

    checkout_payload = {
        "tx_ref": tx_ref,
        "amount": details["amount"],
        "currency": "UGX",
        "redirect_url": callback_url,
        "payment_options": (
            "card,mobilemoneyuganda"
        ),
        "customer": {
            "email": email,
            "name": name,
        },
        "customizations": {
            "title": "LM Analyzer",
            "description": (
                details["name"]
                + " subscription"
            ),
        },
        "meta": {
            "user_id": user_id,
            "plan": plan,
        },
        "configuration": {
            "session_duration": 30,
            "max_retry_attempt": 5,
        },
    }

    response = request_json(
        FLUTTERWAVE_URL
        + "/payments",
        method="POST",
        payload=checkout_payload,
        headers={
            "Authorization": (
                "Bearer "
                + flutterwave_secret_key()
            ),
            "Content-Type": (
                "application/json"
            ),
        },
    )

    link = (
        response.get(
            "data",
            {}
        ).get(
            "link",
            ""
        )
    )

    if not link:
        raise RuntimeError(
            "Flutterwave did not return a checkout link."
        )

    return {
        "status": "success",
        "plan": plan,
        "amount_ugx": details["amount"],
        "tx_ref": tx_ref,
        "checkout_url": link,
    }


def handle_callback(
    handler,
    query
):
    status = str(
        query.get(
            "status",
            [""]
        )[0]
    ).lower()

    tx_ref = str(
        query.get(
            "tx_ref",
            [""]
        )[0]
    ).strip()

    transaction_id = str(
        query.get(
            "transaction_id",
            [""]
        )[0]
    ).strip()

    if (
        not tx_ref
        or not transaction_id
    ):
        html_response(
            handler,
            400,
            """
            <html>
            <body style="
                font-family:sans-serif;
                text-align:center;
                padding:40px
            ">
            <h2>Payment information missing</h2>
            <p>
            Please return to LM Analyzer and try again.
            </p>
            </body>
            </html>
            """
        )
        return

    try:
        if status != "successful":
            raise RuntimeError(
                "The payment was not completed."
            )

        verify_and_activate(
            transaction_id,
            tx_ref
        )

        html_response(
            handler,
            200,
            """
            <!doctype html>
            <html>
            <head>
            <meta name="viewport"
                  content="width=device-width,initial-scale=1">
            <title>LM Analyzer</title>
            </head>

            <body style="
                margin:0;
                background:#050914;
                color:white;
                font-family:sans-serif;
                text-align:center;
                padding:50px 20px;
            ">

            <h1>Payment Successful</h1>

            <p>
            Your LM Analyzer subscription is now active.
            </p>

            <p>
            You can return to the app.
            </p>

            <script>
            setTimeout(function() {
                window.location.href =
                    "lmanalyzer://payment-success";
            }, 1200);
            </script>

            </body>
            </html>
            """
        )

    except Exception:
        html_response(
            handler,
            400,
            """
            <!doctype html>
            <html>
            <head>
            <meta name="viewport"
                  content="width=device-width,initial-scale=1">
            <title>LM Analyzer</title>
            </head>

            <body style="
                margin:0;
                background:#050914;
                color:white;
                font-family:sans-serif;
                text-align:center;
                padding:50px 20px;
            ">

            <h1>Payment Not Confirmed</h1>

            <p>
            We could not confirm this payment yet.
            </p>

            <p>
            Please return to LM Analyzer and check your subscription.
            </p>

            </body>
            </html>
            """
        )


def handle_webhook(handler):
    supplied_hash = str(
        handler.headers.get(
            "verif-hash",
            ""
        )
    ).strip()

    expected_hash = (
        flutterwave_secret_hash()
    )

    if (
        not supplied_hash
        or not secrets.compare_digest(
            supplied_hash,
            expected_hash
        )
    ):
        json_response(
            handler,
            401,
            {
                "error":
                "Invalid webhook signature."
            }
        )
        return

    data = read_json(
        handler
    )

    event_data = data.get(
        "data"
    )

    if not isinstance(
        event_data,
        dict
    ):
        json_response(
            handler,
            200,
            {
                "status":
                "ignored"
            }
        )
        return

    transaction_id = str(
        event_data.get(
            "id",
            ""
        )
    ).strip()

    tx_ref = str(
        event_data.get(
            "tx_ref",
            ""
        )
    ).strip()

    if (
        not transaction_id
        or not tx_ref
    ):
        json_response(
            handler,
            200,
            {
                "status":
                "ignored"
            }
        )
        return

    payment = find_payment(
        tx_ref
    )

    if not payment:
        json_response(
            handler,
            200,
            {
                "status":
                "ignored"
            }
        )
        return

    if payment.get(
        "status"
    ) == "successful":

        json_response(
            handler,
            200,
            {
                "status":
                "already_processed"
            }
        )
        return

    try:
        verify_and_activate(
            transaction_id,
            tx_ref
        )

    except Exception:
        json_response(
            handler,
            200,
            {
                "status":
                "pending_verification"
            }
        )
        return

    json_response(
        handler,
        200,
        {
            "status":
            "processed"
        }
    )


class handler(
    BaseHTTPRequestHandler
):

    def do_OPTIONS(self):
        json_response(
            self,
            204,
            {}
        )

    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(
                self.path
            )

            query = urllib.parse.parse_qs(
                parsed.query
            )

            action = str(
                query.get(
                    "action",
                    [""]
                )[0]
            ).lower()

            if action == "callback":
                handle_callback(
                    self,
                    query
                )
                return

            json_response(
                self,
                400,
                {
                    "error":
                    "Invalid payment request."
                }
            )

        except Exception as exc:
            json_response(
                self,
                500,
                {
                    "error":
                    str(exc)
                }
            )

    def do_POST(self):
        try:
            parsed = urllib.parse.urlparse(
                self.path
            )

            query = urllib.parse.parse_qs(
                parsed.query
            )

            action = str(
                query.get(
                    "action",
                    [""]
                )[0]
            ).lower()

            if action == "webhook":
                handle_webhook(
                    self
                )
                return

            data = read_json(
                self
            )

            action = str(
                data.get(
                    "action",
                    "create"
                )
            ).strip().lower()

            if action == "create":
                result = handle_create(
                    self,
                    data
                )

                json_response(
                    self,
                    200,
                    result
                )
                return

            raise ValueError(
                "Unknown payment action."
            )

        except ValueError as exc:
            json_response(
                self,
                400,
                {
                    "error":
                    str(exc)
                }
            )

        except RuntimeError as exc:
            json_response(
                self,
                503,
                {
                    "error":
                    str(exc)
                }
            )

        except Exception:
            json_response(
                self,
                500,
                {
                    "error":
                    "The payment could not be started."
                }
            )
