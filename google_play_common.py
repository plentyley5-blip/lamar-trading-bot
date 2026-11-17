"""Shared Google Play subscription verification helpers for LM Analyzer."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone

GOOGLE_PLAY_SCOPE = "https://www.googleapis.com/auth/androidpublisher"
DEFAULT_PRODUCT_ID = "lm_analyzer_premium"
PLAN_BY_BASE_ID = {
    "weekly": {"plan": "Weekly", "duration_days": 7, "duration_months": 0},
    "monthly": {"plan": "Monthly", "duration_days": 0, "duration_months": 1},
    "three-months": {"plan": "3 Months", "duration_days": 0, "duration_months": 3},
    "six-months": {"plan": "6 Months", "duration_days": 0, "duration_months": 6},
    # Accept underscore aliases if a Play Console base plan was created that way.
    "three_months": {"plan": "3 Months", "duration_days": 0, "duration_months": 3},
    "six_months": {"plan": "6 Months", "duration_days": 0, "duration_months": 6},
}


def supabase_config():
    url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not service_key:
        raise RuntimeError("Supabase service configuration is missing.")
    for suffix in ("/rest/v1", "/auth/v1", "/storage/v1"):
        if url.endswith(suffix):
            url = url[:-len(suffix)]
    return url, service_key


def service_json(method, path, payload=None, timeout=20):
    base, key = supabase_config()
    headers = {
        "apikey": key,
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {"error": raw[:500]}
        return exc.code, body
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Supabase is temporarily unavailable.") from exc


def _credentials():
    raw = os.getenv("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        raise RuntimeError("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON is missing from Vercel.")
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON is not valid JSON.") from exc
    if not isinstance(info, dict) or not info.get("client_email") or not info.get("private_key"):
        raise RuntimeError("The Google Play service-account JSON is incomplete.")
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request
        credentials = service_account.Credentials.from_service_account_info(
            info, scopes=[GOOGLE_PLAY_SCOPE]
        )
        credentials.refresh(Request())
        return credentials.token
    except ImportError as exc:
        raise RuntimeError("Install google-auth[requests] in the Vercel Python requirements.") from exc
    except Exception as exc:
        raise RuntimeError("Google Play service-account authentication failed.") from exc


def google_play_subscription(purchase_token):
    package_name = os.getenv("GOOGLE_PLAY_PACKAGE_NAME", "com.lamanalyzer.app").strip()
    if package_name != "com.lamanalyzer.app":
        raise RuntimeError("GOOGLE_PLAY_PACKAGE_NAME must match com.lamanalyzer.app.")
    access_token = _credentials()
    url = (
        "https://androidpublisher.googleapis.com/androidpublisher/v3/applications/"
        + urllib.parse.quote(package_name, safe="")
        + "/purchases/subscriptionsv2/tokens/"
        + urllib.parse.quote(str(purchase_token), safe="")
    )
    request = urllib.request.Request(
        url,
        headers={"Authorization": "Bearer " + access_token, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            raw = response.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 404, 410):
            raise ValueError("Google Play could not find a valid subscription for this purchase token.") from exc
        raise RuntimeError("Google Play subscription verification is temporarily unavailable.") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Google Play subscription verification is temporarily unavailable.") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Google Play returned an invalid subscription response.")
    return data


def subscription_details(data):
    product_id = os.getenv("GOOGLE_PLAY_SUBSCRIPTION_PRODUCT_ID", DEFAULT_PRODUCT_ID).strip()
    line_items = data.get("lineItems")
    if not isinstance(line_items, list):
        raise ValueError("Google Play returned no subscription line items.")
    line = next((item for item in line_items if isinstance(item, dict) and item.get("productId") == product_id), None)
    if not line:
        raise ValueError("The Google Play purchase does not belong to the LM Analyzer subscription product.")
    offer = line.get("offerDetails") or {}
    base_plan_id = str(offer.get("basePlanId", "")).strip().lower()
    plan = PLAN_BY_BASE_ID.get(base_plan_id)
    if not plan:
        raise ValueError("The Google Play purchase uses an unrecognised LM Analyzer base plan.")
    expiry = str(line.get("expiryTime", "")).strip()
    start = str(data.get("startTime", "")).strip()
    if not expiry or not start:
        raise ValueError("Google Play did not return valid subscription start and expiry times.")
    try:
        expiry_dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        if expiry_dt.tzinfo is None: expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
        if start_dt.tzinfo is None: start_dt = start_dt.replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError("Google Play returned an invalid subscription timestamp.") from exc

    auto = line.get("autoRenewingPlan")
    if not isinstance(auto, dict):
        raise ValueError("This LM Analyzer product is not configured as an auto-renewing Google Play base plan.")
    money = auto.get("recurringPrice") or {}
    currency = str(money.get("currencyCode", "")).strip().upper()
    amount = None
    if currency:
        try:
            amount = Decimal(str(money.get("units", "0"))) + (Decimal(int(money.get("nanos", 0))) / Decimal(1_000_000_000))
        except (InvalidOperation, ValueError, TypeError):
            amount = None
    if amount is None or amount <= 0:
        raise ValueError("Google Play did not return a valid recurring subscription price.")
    if expiry_dt <= start_dt:
        raise ValueError("Google Play returned an invalid subscription period.")
    return {
        "product_id": product_id,
        "base_plan_id": base_plan_id,
        "plan": plan["plan"],
        "duration_days": plan["duration_days"],
        "duration_months": plan["duration_months"],
        "starts_at": start_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "expires_at": expiry_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "expiry_dt": expiry_dt.astimezone(timezone.utc),
        "currency": currency or "USD",
        "amount": str(amount) if amount is not None else None,
        "order_id": str(line.get("latestSuccessfulOrderId") or data.get("latestOrderId") or "").strip(),
        "linked_purchase_token": str(data.get("linkedPurchaseToken") or "").strip(),
    }


def activate_subscription(user_id, purchase_token, details):
    payload = {
        "p_user_id": str(user_id),
        "p_plan": details["plan"],
        "p_amount": details["amount"],
        "p_currency": details["currency"],
        "p_duration_months": details["duration_months"],
        "p_duration_days": details["duration_days"],
        "p_starts_at": details["starts_at"],
        "p_expires_at": details["expires_at"],
        "p_purchase_token": str(purchase_token),
        "p_product_id": details["product_id"],
        "p_base_plan_id": details["base_plan_id"],
        "p_payment_reference": details["order_id"] or None,
        "p_linked_purchase_token": details["linked_purchase_token"] or None,
    }
    status, result = service_json(
        "POST", "/rest/v1/rpc/activate_google_play_subscription", payload, timeout=20
    )
    if status != 200 or not isinstance(result, dict):
        raise RuntimeError("LM Analyzer could not save the verified Google Play subscription.")
    return result


def get_subscription_by_token(purchase_token):
    query = "/rest/v1/google_play_subscription_tokens?" + urllib.parse.urlencode({
        "purchase_token": "eq." + str(purchase_token),
        "select": "id,user_id,subscription_id,plan,status,expires_at",
        "limit": "1",
    })
    status, rows = service_json("GET", query)
    if status != 200 or not isinstance(rows, list):
        raise RuntimeError("LM Analyzer could not look up the Google Play subscription token.")
    return rows[0] if rows else None


def update_subscription_status(purchase_token, status_value, expires_at=None):
    token_row = get_subscription_by_token(purchase_token)
    if not token_row:
        return False

    now = datetime.now(timezone.utc).isoformat()
    token_query = "/rest/v1/google_play_subscription_tokens?purchase_token=eq." + urllib.parse.quote(str(purchase_token), safe="")
    token_payload = {"status": str(status_value), "updated_at": now}
    if expires_at:
        token_payload["expires_at"] = expires_at
    token_status, _ = service_json("PATCH", token_query, token_payload)
    if token_status not in (200, 204):
        raise RuntimeError("LM Analyzer could not update the Google Play token lifecycle state.")

    subscription_id = str(token_row.get("subscription_id", "")).strip()
    if subscription_id:
        sub_query = "/rest/v1/subscriptions?id=eq." + urllib.parse.quote(subscription_id, safe="")
        sub_payload = {"status": str(status_value), "updated_at": now}
        if expires_at:
            sub_payload["expires_at"] = expires_at
        sub_status, _ = service_json("PATCH", sub_query, sub_payload)
        if sub_status not in (200, 204):
            raise RuntimeError("LM Analyzer could not update the subscription lifecycle state.")
    return True

