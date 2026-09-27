import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler


PLANS = {
    "monthly": {"name": "Monthly", "amount_ugx": 82797, "duration_months": 1},
    "3_months": {"name": "3 Months", "amount_ugx": 198713, "duration_months": 3},
    "6_months": {"name": "6 Months", "amount_ugx": 248391, "duration_months": 6},
}


def send_json(handler, status, payload):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
    handler.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def bearer(handler):
    value = str(handler.headers.get("Authorization", "")).strip()
    if not value.lower().startswith("bearer "):
        return ""
    return value[7:].strip()


def read_json(handler):
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0 or length > 64 * 1024:
        raise ValueError("Invalid request body.")
    raw = handler.rfile.read(length)
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Invalid request body.")
    return data


def supabase_request(url, headers, method="GET", body=None):
    req = urllib.request.Request(url, headers=headers, method=method, data=body)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                parsed = {"raw": raw}
            return response.status, parsed
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"raw": raw}
        return exc.code, parsed
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError("Supabase is temporarily unavailable.")


def auth_user(token, supabase_url, anon_key):
    code, user = supabase_request(
        supabase_url + "/auth/v1/user",
        {
            "apikey": anon_key,
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
        },
    )
    if code != 200 or not isinstance(user, dict) or not user.get("id"):
        return None
    return user


def create_pending_transaction(user_id, user_email, plan_key, plan, supabase_url, service_key):
    tx_ref = "LM-" + secrets.token_hex(12).upper()
    payload = {
        "user_id": user_id,
        "tx_ref": tx_ref,
        "plan": plan["name"],
        "amount_ugx": plan["amount_ugx"],
        "duration_months": plan["duration_months"],
        "status": "pending",
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    code, result = supabase_request(
        supabase_url + "/rest/v1/payment_transactions",
        {
            "apikey": service_key,
            "Authorization": "Bearer " + service_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Prefer": "return=representation",
        },
        method="POST",
        body=body,
    )
    if code not in (200, 201):
        raise RuntimeError("Payment transaction could not be created.")
    return tx_ref


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        send_json(self, 204, {})

    def do_POST(self):
        token = bearer(self)
        if not token:
            send_json(self, 401, {"error": "Authentication required."})
            return

        try:
            data = read_json(self)
            plan_key = str(data.get("plan", "")).strip().lower()
            if plan_key not in PLANS:
                send_json(self, 400, {"error": "Invalid subscription plan."})
                return

            supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
            anon_key = os.getenv("SUPABASE_ANON_KEY", "").strip()
            service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
            if not supabase_url or not anon_key or not service_key:
                send_json(self, 503, {"error": "Payment service is not configured."})
                return

            user = auth_user(token, supabase_url, anon_key)
            if not user:
                send_json(self, 401, {"error": "Session is invalid or expired."})
                return

            plan = PLANS[plan_key]
            tx_ref = create_pending_transaction(
                str(user["id"]),
                str(user.get("email", "")),
                plan_key,
                plan,
                supabase_url,
                service_key,
            )

            # Do not invent a Boldrails endpoint or payment payload. The provider
            # connection is intentionally disabled until Boldrails supplies the
            # merchant API credentials and the account-specific collection docs.
            send_json(self, 200, {
                "status": "pending_provider_setup",
                "tx_ref": tx_ref,
                "plan": plan["name"],
                "amount_ugx": plan["amount_ugx"],
                "duration_months": plan["duration_months"],
                "message": "Payment order created. Boldrails checkout will be connected after merchant approval and API credentials are available.",
            })
        except ValueError as exc:
            send_json(self, 400, {"error": str(exc)})
        except RuntimeError as exc:
            send_json(self, 503, {"error": str(exc)})
        except Exception:
            send_json(self, 500, {"error": "Payment request could not be processed."})
