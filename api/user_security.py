import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request


JOB_TTL_SECONDS = 15 * 60


def get_supabase_url():
    value = os.environ.get(
        "SUPABASE_URL",
        ""
    ).strip()

    if not value:
        raise RuntimeError(
            "SUPABASE_URL is missing in Vercel."
        )

    return value.rstrip("/")


def get_supabase_service_key():
    value = os.environ.get(
        "SUPABASE_SERVICE_ROLE_KEY",
        ""
    ).strip()

    if not value:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY is missing in Vercel."
        )

    return value


def get_openai_key_for_signing():
    value = os.environ.get(
        "OPENAI_API_KEY",
        ""
    ).strip()

    if not value:
        raise RuntimeError(
            "OPENAI_API_KEY is missing in Vercel."
        )

    return value.encode("utf-8")


def _get_user_with_access_token(access_token):

    url = (
        get_supabase_url()
        + "/auth/v1/user"
    )

    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization":
                "Bearer " + access_token,

            "apikey":
                get_supabase_service_key(),

            "Accept":
                "application/json",
        },
    )

    with urllib.request.urlopen(
        request,
        timeout=20
    ) as response:

        body = response.read().decode(
            "utf-8"
        )

        user = json.loads(body)

        user_id = str(
            user.get("id", "")
        ).strip()

        if not user_id:
            raise ValueError(
                "Supabase returned no user ID."
            )

        return user


def _refresh_access_token(refresh_token):

    url = (
        get_supabase_url()
        + "/auth/v1/token"
        + "?grant_type=refresh_token"
    )

    payload = urllib.parse.urlencode(
        {
            "refresh_token":
                refresh_token
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "apikey":
                get_supabase_service_key(),

            "Content-Type":
                "application/x-www-form-urlencoded",

            "Accept":
                "application/json",
        },
    )

    with urllib.request.urlopen(
        request,
        timeout=20
    ) as response:

        body = response.read().decode(
            "utf-8"
        )

        data = json.loads(body)

        access_token = str(
            data.get(
                "access_token",
                ""
            )
        ).strip()

        if not access_token:
            raise ValueError(
                "Supabase returned no refreshed access token."
            )

        return _get_user_with_access_token(
            access_token
        )


def verify_access_token(access_token):

    token = str(
        access_token or ""
    ).strip()

    if not token:
        raise ValueError(
            "Authorization token is required."
        )

    # Normal Supabase access token.
    try:

        return _get_user_with_access_token(
            token
        )

    except urllib.error.HTTPError as access_error:

        if access_error.code != 401:
            raise RuntimeError(
                "Unable to verify your login session."
            ) from access_error

        # If the supplied value is not a valid access
        # token, try treating it as a refresh token.
        try:

            return _refresh_access_token(
                token
            )

        except Exception as refresh_error:

            raise ValueError(
                "Invalid or expired login session."
            ) from refresh_error

    except urllib.error.URLError as exc:

        raise RuntimeError(
            "Unable to connect to Supabase."
        ) from exc


def extract_bearer_token(headers):

    value = headers.get(
        "Authorization",
        ""
    ).strip()

    if not value:
        raise ValueError(
            "Authorization header is required."
        )

    parts = value.split(
        None,
        1
    )

    if len(parts) != 2:
        raise ValueError(
            "Invalid Authorization header."
        )

    scheme = parts[0].lower()

    if scheme != "bearer":
        raise ValueError(
            "Authorization must use Bearer token."
        )

    token = parts[1].strip()

    if not token:
        raise ValueError(
            "Authorization token is empty."
        )

    return token


def reserve_analysis_slot(user_id):

    url = (
        get_supabase_url()
        + "/rest/v1/rpc/reserve_analysis_slot"
    )

    payload = json.dumps(
        {
            "p_user_id": user_id
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "apikey":
                get_supabase_service_key(),

            "Authorization":
                "Bearer "
                + get_supabase_service_key(),

            "Content-Type":
                "application/json",

            "Accept":
                "application/json",
        },
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=20
        ) as response:

            body = response.read().decode(
                "utf-8"
            )

            return int(
                json.loads(body)
            )

    except urllib.error.HTTPError as exc:

        body = exc.read().decode(
            "utf-8",
            errors="replace"
        )

        raise RuntimeError(
            "Unable to reserve analysis slot: "
            + body
        ) from exc

    except urllib.error.URLError as exc:

        raise RuntimeError(
            "Unable to connect to Supabase."
        ) from exc


def release_analysis_slot(user_id):

    url = (
        get_supabase_url()
        + "/rest/v1/rpc/release_analysis_slot"
    )

    payload = json.dumps(
        {
            "p_user_id": user_id
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "apikey":
                get_supabase_service_key(),

            "Authorization":
                "Bearer "
                + get_supabase_service_key(),

            "Content-Type":
                "application/json",

            "Accept":
                "application/json",
        },
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=20
        ) as response:

            body = response.read().decode(
                "utf-8"
            )

            return int(
                json.loads(body)
            )

    except Exception:

        return None


def create_secure_job_token(
    response_id,
    instrument,
    trade_focus,
    user_id
):

    payload = {
        "response_id":
            str(response_id).strip(),

        "instrument":
            str(instrument).strip(),

        "trade_focus":
            str(trade_focus).strip(),

        "user_id":
            str(user_id).strip(),

        "created_at":
            int(time.time()),
    }

    raw = json.dumps(
        payload,
        separators=(
            ",",
            ":"
        ),
        sort_keys=True
    ).encode("utf-8")

    signature = hmac.new(
        get_openai_key_for_signing(),
        raw,
        hashlib.sha256
    ).digest()

    encoded_payload = (
        base64.urlsafe_b64encode(raw)
        .decode("ascii")
        .rstrip("=")
    )

    encoded_signature = (
        base64.urlsafe_b64encode(signature)
        .decode("ascii")
        .rstrip("=")
    )

    return (
        encoded_payload
        + "."
        + encoded_signature
    )


def read_secure_job_token(token):

    try:

        parts = str(token).split(
            ".",
            1
        )

        if len(parts) != 2:
            raise ValueError(
                "Invalid analysis job."
            )

        raw_part = parts[0]
        signature_part = parts[1]

        raw = base64.urlsafe_b64decode(
            raw_part
            + "=" * (
                -len(raw_part) % 4
            )
        )

        supplied_signature = (
            base64.urlsafe_b64decode(
                signature_part
                + "=" * (
                    -len(signature_part) % 4
                )
            )
        )

        expected_signature = hmac.new(
            get_openai_key_for_signing(),
            raw,
            hashlib.sha256
        ).digest()

        if not hmac.compare_digest(
            supplied_signature,
            expected_signature
        ):
            raise ValueError(
                "Invalid analysis job signature."
            )

        data = json.loads(
            raw.decode("utf-8")
        )

        created_at = int(
            data["created_at"]
        )

        if (
            int(time.time())
            - created_at
            > JOB_TTL_SECONDS
        ):
            raise ValueError(
                "Analysis job has expired."
            )

        response_id = str(
            data["response_id"]
        ).strip()

        instrument = str(
            data["instrument"]
        ).strip()

        trade_focus = str(
            data["trade_focus"]
        ).strip()

        user_id = str(
            data["user_id"]
        ).strip()

        if not response_id:
            raise ValueError(
                "Analysis job has no response ID."
            )

        if not user_id:
            raise ValueError(
                "Analysis job has no user ID."
            )

        return (
            response_id,
            instrument,
            trade_focus,
            user_id
        )

    except Exception as exc:

        raise ValueError(
            "Invalid analysis job: "
            + str(exc)
        )
