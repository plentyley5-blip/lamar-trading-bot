
import json
import os
import urllib.parse
import urllib.request
import urllib.error

from api.user_security import (
    get_supabase_service_key,
    get_supabase_url,
)


def json_request(url, method="GET", payload=None, headers=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if headers:
        request_headers.update(headers)

    request = urllib.request.Request(
        url, data=data, headers=request_headers, method=method
    )

    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            "Remote API returned HTTP " + str(exc.code) + ": " + detail[:500]
        )


def pesapal_base():
    base = os.environ.get(
        "PESAPAL_BASE_URL", "https://pay.pesapal.com/v3"
    ).strip().rstrip("/")
    if not base.startswith("https://"):
        raise RuntimeError("PESAPAL_BASE_URL must use HTTPS")
    return base


def pesapal_token():
    key = os.environ.get("PESAPAL_CONSUMER_KEY", "").strip()
    secret = os.environ.get("PESAPAL_CONSUMER_SECRET", "").strip()

    if not key or not secret:
        raise RuntimeError("Pesapal credentials are not configured")

    result = json_request(
        pesapal_base() + "/api/Auth/RequestToken",
        method="POST",
        payload={"consumer_key": key, "consumer_secret": secret},
    )
    token = result.get("token")
    if not token:
        raise RuntimeError("Pesapal did not return an access token")
    return token


def pesapal_api(path, method="GET", payload=None):
    token = pesapal_token()
    return json_request(
        pesapal_base() + path,
        method=method,
        payload=payload,
        headers={"Authorization": "Bearer " + token},
    )


def supabase_rest(path, method="GET", payload=None, prefer=None):
    base = get_supabase_url().rstrip("/")
    key = get_supabase_service_key().strip()

    if not base or not key:
        raise RuntimeError("Supabase service configuration is missing")

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


def find_payment_order(merchant_reference):
    query = urllib.parse.urlencode({
        "merchant_reference": "eq." + merchant_reference,
        "select": (
            "id,user_id,plan,amount,currency,duration_months,status,"
            "merchant_reference,pesapal_order_tracking_id"
        ),
        "limit": "1",
    })
    rows = supabase_rest("payment_orders?" + query)
    return rows[0] if isinstance(rows, list) and rows else None


def process_pesapal_payment(tracking_id, merchant_reference):
    if not tracking_id or not merchant_reference:
        raise ValueError("Missing Pesapal tracking ID or merchant reference")

    order = find_payment_order(merchant_reference)
    if not order:
        raise ValueError("Payment order was not found")

    if order.get("status") == "completed":
        return {"status": "already_completed"}

    if order.get("status") != "pending":
        return {"status": "not_pending"}

    saved_tracking_id = order.get("pesapal_order_tracking_id")
    if saved_tracking_id and saved_tracking_id != tracking_id:
        raise ValueError("Pesapal tracking ID does not match the order")

    tracking_query = urllib.parse.urlencode({
        "orderTrackingId": tracking_id
    })
    payment = pesapal_api(
        "/api/Transactions/GetTransactionStatus?" + tracking_query
    )

    returned_reference = str(payment.get("merchant_reference", ""))
    if returned_reference != merchant_reference:
        raise ValueError("Pesapal merchant reference does not match")

    status = str(
        payment.get("payment_status_description", "")
    ).strip().upper()

    if status in ("FAILED", "REVERSED", "INVALID"):
        new_status = "reversed" if status == "REVERSED" else "failed"
        reference_query = urllib.parse.urlencode({
            "merchant_reference": "eq." + merchant_reference,
            "status": "eq.pending",
        })
        supabase_rest(
            "payment_orders?" + reference_query,
            method="PATCH",
            payload={"status": new_status},
            prefer="return=minimal",
        )
        return {"status": status.lower()}

    if status != "COMPLETED":
        return {"status": "pending"}

    try:
        paid_amount = round(float(payment.get("amount")), 2)
        expected_amount = round(float(order.get("amount")), 2)
    except (TypeError, ValueError):
        raise ValueError("Pesapal returned an invalid amount")

    paid_currency = str(payment.get("currency", "")).upper()
    expected_currency = str(order.get("currency", "")).upper()

    if paid_amount != expected_amount:
        raise ValueError("Verified payment amount does not match the order")

    if paid_currency != "USD" or expected_currency != "USD":
        raise ValueError("Verified payment currency is not USD")

    confirmation_code = payment.get("confirmation_code") or tracking_id

    result = supabase_rest(
        "rpc/complete_pesapal_payment",
        method="POST",
        payload={
            "p_merchant_reference": merchant_reference,
            "p_tracking_id": tracking_id,
            "p_amount": paid_amount,
            "p_currency": paid_currency,
            "p_confirmation_code": str(confirmation_code),
        },
    )

    if isinstance(result, dict):
        activation_status = result.get("status")
        if activation_status in ("completed", "already_completed"):
            return {"status": activation_status}

        raise RuntimeError(
            "Subscription activation was not completed: "
            + str(activation_status)
        )

    raise RuntimeError("Unexpected response from subscription activation")
