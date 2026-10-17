
import json
import os
import urllib.parse
import urllib.request
import urllib.error

from api.user_security import (
    get_supabase_service_key,
    get_supabase_url,
)


# ============================================================
# HTTP / JSON REQUESTS
# ============================================================

def json_request(url, method="GET", payload=None, headers=None):
    data = (
        None
        if payload is None
        else json.dumps(payload).encode("utf-8")
    )

    request_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    if headers:
        request_headers.update(headers)

    request = urllib.request.Request(
        url,
        data=data,
        headers=request_headers,
        method=method,
    )

    parsed_url = urllib.parse.urlparse(url)
    host = parsed_url.netloc
    path = parsed_url.path

    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            raw = response.read().decode(
                "utf-8",
                errors="replace",
            )

            if not raw.strip():
                return {}

            try:
                result = json.loads(raw)
            except json.JSONDecodeError:
                print(
                    "Remote API returned invalid JSON:",
                    "host=" + host,
                    "path=" + path,
                    "status=" + str(response.status),
                    "response_length=" + str(len(raw)),
                )
                raise RuntimeError(
                    "Remote API returned invalid JSON"
                ) from None

            if not isinstance(result, (dict, list)):
                print(
                    "Remote API returned unexpected JSON type:",
                    "host=" + host,
                    "path=" + path,
                    "type=" + type(result).__name__,
                )
                raise RuntimeError(
                    "Remote API returned an unexpected response"
                )

            return result

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        # Do not log request headers, access tokens, or credentials.
        print(
            "Remote API HTTP error:",
            "host=" + host,
            "path=" + path,
            "status=" + str(exc.code),
            "response=" + detail[:1000],
        )

        raise RuntimeError(
            "Remote API returned HTTP "
            + str(exc.code)
            + ": "
            + detail[:500]
        ) from None

    except urllib.error.URLError as exc:
        reason = str(exc.reason)

        print(
            "Remote API connection error:",
            "host=" + host,
            "path=" + path,
            "reason=" + reason[:300],
        )

        raise RuntimeError(
            "Could not connect to remote API: "
            + reason[:300]
        ) from None

    except TimeoutError:
        print(
            "Remote API request timed out:",
            "host=" + host,
            "path=" + path,
        )
        raise RuntimeError(
            "Remote API request timed out"
        ) from None


# ============================================================
# PESAPAL CONFIGURATION
# ============================================================

def pesapal_base():
    base = os.environ.get(
        "PESAPAL_BASE_URL",
        "https://pay.pesapal.com/v3",
    ).strip().rstrip("/")

    if not base.startswith("https://"):
        raise RuntimeError(
            "PESAPAL_BASE_URL must use HTTPS"
        )

    return base


# ============================================================
# PESAPAL AUTHENTICATION
# ============================================================

def pesapal_token():
    consumer_key = os.environ.get(
        "PESAPAL_CONSUMER_KEY",
        "",
    ).strip()

    consumer_secret = os.environ.get(
        "PESAPAL_CONSUMER_SECRET",
        "",
    ).strip()

    if not consumer_key or not consumer_secret:
        raise RuntimeError(
            "Pesapal credentials are not configured"
        )

    result = json_request(
        pesapal_base() + "/api/Auth/RequestToken",
        method="POST",
        payload={
            "consumer_key": consumer_key,
            "consumer_secret": consumer_secret,
        },
    )

    if not isinstance(result, dict):
        raise RuntimeError(
            "Pesapal returned an invalid authentication response"
        )

    token = result.get("token")

    if not isinstance(token, str) or not token.strip():
        print(
            "Pesapal authentication response keys:",
            sorted(str(key) for key in result.keys()),
        )

        # Do not log the full response because it may contain
        # sensitive authentication information.
        raise RuntimeError(
            "Pesapal did not return an access token"
        )

    return token.strip()


# ============================================================
# PESAPAL API
# ============================================================

def pesapal_api(path, method="GET", payload=None):
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError(
            "Pesapal API path must begin with /"
        )

    token = pesapal_token()

    result = json_request(
        pesapal_base() + path,
        method=method,
        payload=payload,
        headers={
            "Authorization": "Bearer " + token,
        },
    )

    return result


# ============================================================
# SUPABASE REST API
# ============================================================

def supabase_rest(path, method="GET", payload=None, prefer=None):
    base = get_supabase_url().rstrip("/")
    key = get_supabase_service_key().strip()

    if not base or not key:
        raise RuntimeError(
            "Supabase service configuration is missing"
        )

    if not base.startswith("https://"):
        raise RuntimeError(
            "Supabase URL must use HTTPS"
        )

    headers = {
        "apikey": key,
        "Authorization": "Bearer " + key,
    }

    if prefer:
        headers["Prefer"] = prefer

    return json_request(
        base + "/rest/v1/" + path.lstrip("/"),
        method=method,
        payload=payload,
        headers=headers,
    )


# ============================================================
# PAYMENT ORDER LOOKUP
# ============================================================

def find_payment_order(merchant_reference):
    if not merchant_reference:
        raise ValueError(
            "Missing merchant reference"
        )

    query = urllib.parse.urlencode({
        "merchant_reference": "eq." + merchant_reference,
        "select": (
            "id,user_id,plan,amount,currency,duration_months,status,"
            "merchant_reference,pesapal_order_tracking_id"
        ),
        "limit": "1",
    })

    rows = supabase_rest(
        "payment_orders?" + query
    )

    if not isinstance(rows, list):
        raise RuntimeError(
            "Unexpected response when looking up payment order"
        )

    return rows[0] if rows else None


# ============================================================
# PAYMENT VERIFICATION AND SUBSCRIPTION ACTIVATION
# ============================================================

def process_pesapal_payment(
    tracking_id,
    merchant_reference,
):
    if not tracking_id or not merchant_reference:
        raise ValueError(
            "Missing Pesapal tracking ID or merchant reference"
        )

    order = find_payment_order(
        merchant_reference
    )

    if not order:
        raise ValueError(
            "Payment order was not found"
        )

    if order.get("status") == "completed":
        return {
            "status": "already_completed",
        }

    if order.get("status") != "pending":
        return {
            "status": "not_pending",
        }

    saved_tracking_id = order.get(
        "pesapal_order_tracking_id"
    )

    if (
        saved_tracking_id
        and saved_tracking_id != tracking_id
    ):
        raise ValueError(
            "Pesapal tracking ID does not match the order"
        )

    tracking_query = urllib.parse.urlencode({
        "orderTrackingId": tracking_id,
    })

    payment = pesapal_api(
        "/api/Transactions/GetTransactionStatus?"
        + tracking_query
    )

    if not isinstance(payment, dict):
        raise RuntimeError(
            "Pesapal returned an invalid transaction status response"
        )

    returned_reference = str(
        payment.get("merchant_reference", "")
    )

    if returned_reference != merchant_reference:
        raise ValueError(
            "Pesapal merchant reference does not match"
        )

    payment_status = str(
        payment.get(
            "payment_status_description",
            "",
        )
    ).strip().upper()

    if payment_status in (
        "FAILED",
        "REVERSED",
        "INVALID",
    ):
        new_status = (
            "reversed"
            if payment_status == "REVERSED"
            else "failed"
        )

        reference_query = urllib.parse.urlencode({
            "merchant_reference": "eq." + merchant_reference,
            "status": "eq.pending",
        })

        supabase_rest(
            "payment_orders?" + reference_query,
            method="PATCH",
            payload={
                "status": new_status,
            },
            prefer="return=minimal",
        )

        return {
            "status": payment_status.lower(),
        }

    if payment_status != "COMPLETED":
        return {
            "status": "pending",
        }

    try:
        paid_amount = round(
            float(payment.get("amount")),
            2,
        )

        expected_amount = round(
            float(order.get("amount")),
            2,
        )

    except (TypeError, ValueError):
        raise ValueError(
            "Pesapal returned an invalid amount"
        ) from None

    paid_currency = str(
        payment.get("currency", "")
    ).upper()

    expected_currency = str(
        order.get("currency", "")
    ).upper()

    if paid_amount != expected_amount:
        raise ValueError(
            "Verified payment amount does not match the order"
        )

    if (
        paid_currency != "USD"
        or expected_currency != "USD"
    ):
        raise ValueError(
            "Verified payment currency is not USD"
        )

    confirmation_code = (
        payment.get("confirmation_code")
        or tracking_id
    )

    result = supabase_rest(
        "rpc/complete_pesapal_payment",
        method="POST",
        payload={
            "p_merchant_reference": merchant_reference,
            "p_tracking_id": tracking_id,
            "p_amount": paid_amount,
            "p_currency": paid_currency,
            "p_confirmation_code": str(
                confirmation_code
            ),
        },
    )

    if isinstance(result, dict):
        activation_status = result.get("status")

        if activation_status in (
            "completed",
            "already_completed",
        ):
            return {
                "status": activation_status,
            }

        raise RuntimeError(
            "Subscription activation was not completed: "
            + str(activation_status)
        )

    raise RuntimeError(
        "Unexpected response from subscription activation"
    )
