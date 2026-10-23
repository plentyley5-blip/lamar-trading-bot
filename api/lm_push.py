"""Private Firebase push helper for LM Analyzer Vercel Python endpoints.

Requires firebase-admin (already used by the existing notification sender) and
FIREBASE_SERVICE_ACCOUNT_JSON in Vercel, containing the service-account JSON.
Never expose this credential to Android or client responses.
"""
import json
import os
import urllib.parse
import urllib.request
import urllib.error

import firebase_admin
from firebase_admin import credentials, messaging


def _supabase_request(method, path, payload=None, prefer=None):
    base = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not base or not key:
        raise RuntimeError("Supabase server credentials are not configured")
    for suffix in ("/rest/v1", "/auth/v1", "/storage/v1"):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
    headers = {
        "apikey": key,
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            raw = response.read().decode("utf-8", "replace")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raise RuntimeError("Supabase request failed (HTTP %s)" % exc.code) from exc


def _firebase_app():
    try:
        return firebase_admin.get_app()
    except ValueError:
        raw = (os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON") or
               os.environ.get("FIREBASE_SERVICE_ACCOUNT") or "").strip()
        if not raw:
            raise RuntimeError("Firebase service account is not configured")
        account = json.loads(raw)
        return firebase_admin.initialize_app(credentials.Certificate(account))


def _deactivate_token(token):
    params = urllib.parse.urlencode({"device_token": "eq." + token})
    _supabase_request(
        "PATCH", "/rest/v1/device_tokens?" + params,
        {"active": False}, prefer="return=minimal",
    )


def notify_user_ids(user_ids, title, body, data=None):
    """Send FCM notifications to active registered devices for the given users."""
    ids = list(dict.fromkeys(str(x).strip() for x in user_ids if str(x).strip()))
    if not ids:
        return {"sent": 0, "failed": 0}
    _firebase_app()
    query = urllib.parse.urlencode({
        "user_id": "in.({})".format(",".join(ids)),
        "active": "eq.true",
        "select": "user_id,device_token",
        "limit": "1000",
    })
    rows = _supabase_request("GET", "/rest/v1/device_tokens?" + query)
    rows = rows if isinstance(rows, list) else []
    sent = failed = 0
    failures = {}
    extra_data = {str(k): str(v) for k, v in (data or {}).items() if v is not None}
    for row in rows:
        token = str(row.get("device_token", "")).strip()
        if not token:
            continue
        message = messaging.Message(
            token=token,
            notification=messaging.Notification(title=str(title)[:100], body=str(body)[:240]),
            data=extra_data,
            android=messaging.AndroidConfig(priority="high"),
        )
        try:
            messaging.send(message)
            sent += 1
        except Exception as exc:
            failed += 1
            # Return only a safe Firebase error class, never the device token or credentials.
            error_type = type(exc).__name__
            failures[error_type] = failures.get(error_type, 0) + 1
            error_name = error_type.lower()
            error_text = str(exc).lower()
            if "unregistered" in error_name or "unregistered" in error_text or "registration-token-not-registered" in error_text:
                try:
                    _deactivate_token(token)
                except Exception:
                    pass
    return {"sent": sent, "failed": failed, "failure_types": failures}
