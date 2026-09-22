import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request


JOB_TTL_SECONDS = 15 * 60


def get_supabase_url():
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    if not url:
        raise RuntimeError("SUPABASE_URL is missing from Vercel.")
    for suffix in ("/rest/v1", "/auth/v1", "/storage/v1"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return url


def get_supabase_service_key():
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not key:
        raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY is missing from Vercel.")
    return key


def get_supabase_anon_key():
    key = os.environ.get("SUPABASE_ANON_KEY", "").strip()
    if not key:
        raise RuntimeError("SUPABASE_ANON_KEY is missing from Vercel.")
    return key


def get_openai_key_for_signing():
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is missing from Vercel.")
    return key


def get_owner_email():
    return os.environ.get("OWNER_EMAIL", "").strip().lower()


def is_owner_user(user):
    owner_email = get_owner_email()
    if not owner_email or not isinstance(user, dict):
        return False
    user_email = str(user.get("email", "")).strip().lower()
    return bool(user_email) and hmac.compare_digest(user_email, owner_email)


def _auth_headers(access_token):
    return {
        "apikey": get_supabase_anon_key(),
        "Authorization": "Bearer " + access_token,
        "Accept": "application/json",
    }


def verify_access_token(access_token):
    token = str(access_token or "").strip()
    if not token:
        raise ValueError("Authorization token is required.")

    request = urllib.request.Request(
        get_supabase_url() + "/auth/v1/user",
        headers=_auth_headers(token),
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise ValueError("Invalid or expired session.") from exc
        raise RuntimeError("Unable to verify the user session.") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Unable to verify the user session.") from exc

    if not isinstance(data, dict) or not data.get("id"):
        raise ValueError("Invalid user session.")

    return data


def extract_bearer_token(handler):
    value = str(handler.headers.get("Authorization", "")).strip()
    if not value.lower().startswith("bearer "):
        raise ValueError("Authorization bearer token is required.")
    token = value[7:].strip()
    if not token:
        raise ValueError("Authorization bearer token is required.")
    return token


def _supabase_rest_headers():
    return {
        "apikey": get_supabase_service_key(),
        "Authorization": "Bearer " + get_supabase_service_key(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _supabase_rpc(name, payload):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        get_supabase_url() + "/rest/v1/rpc/" + name,
        data=body,
        headers=_supabase_rest_headers(),
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError("Supabase RPC failed: " + raw) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Supabase is temporarily unavailable.") from exc


def _scalar_integer(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, list) and value:
        return _scalar_integer(value[0])
    if isinstance(value, dict):
        for key in ("result", "count", "daily_analysis_count"):
            if key in value:
                return _scalar_integer(value[key])
    return int(value)


def reserve_analysis_slot(user_id):
    result = _supabase_rpc(
        "reserve_analysis_slot",
        {"p_user_id": str(user_id)},
    )
    return _scalar_integer(result)


def release_analysis_slot(user_id):
    result = _supabase_rpc(
        "release_analysis_slot",
        {"p_user_id": str(user_id)},
    )
    return _scalar_integer(result)


def _now():
    return int(time.time())


def _job_token_key():
    return get_openai_key_for_signing().encode("utf-8")


def create_secure_job_token(response_id, instrument, trade_focus, user_id):
    payload = {
        "response_id": str(response_id),
        "instrument": str(instrument),
        "trade_focus": str(trade_focus),
        "user_id": str(user_id),
        "created_at": _now(),
    }

    raw = json.dumps(
        payload,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    signature = hmac.new(
        _job_token_key(),
        raw,
        hashlib.sha256,
    ).digest()

    encoded_payload = (
        __import__("base64").urlsafe_b64encode(raw)
        .decode("ascii")
        .rstrip("=")
    )

    encoded_signature = (
        __import__("base64").urlsafe_b64encode(signature)
        .decode("ascii")
        .rstrip("=")
    )

    return encoded_payload + "." + encoded_signature


def read_secure_job_token(token):
    import base64

    try:
        parts = str(token or "").split(".", 1)
        if len(parts) != 2:
            raise ValueError("Invalid analysis job.")

        raw_part, signature_part = parts
        raw = base64.urlsafe_b64decode(
            raw_part + "=" * (-len(raw_part) % 4)
        )
        supplied_signature = base64.urlsafe_b64decode(
            signature_part + "=" * (-len(signature_part) % 4)
        )

        expected_signature = hmac.new(
            _job_token_key(),
            raw,
            hashlib.sha256,
        ).digest()

        if not hmac.compare_digest(
            supplied_signature,
            expected_signature,
        ):
            raise ValueError("Invalid analysis job signature.")

        data = json.loads(raw.decode("utf-8"))
        created_at = int(data["created_at"])

        if _now() - created_at > JOB_TTL_SECONDS:
            raise ValueError("Analysis job has expired.")

        response_id = str(data["response_id"]).strip()
        instrument = str(data["instrument"]).strip()
        trade_focus = str(data["trade_focus"]).strip()
        user_id = str(data["user_id"]).strip()

        if not response_id or not user_id:
            raise ValueError("Invalid analysis job.")

        return response_id, instrument, trade_focus, user_id

    except Exception as exc:
        raise ValueError("Invalid analysis job: " + str(exc)) from exc


# Compatibility aliases used by the analysis endpoints.
create_job_token = create_secure_job_token
read_job_token = read_secure_job_token
