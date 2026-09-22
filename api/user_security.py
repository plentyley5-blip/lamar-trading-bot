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


# ---------------------------------------------------------
# ENVIRONMENT
# ---------------------------------------------------------

def get_supabase_url():
    value = os.environ.get(
        "SUPABASE_URL",
        ""
    ).strip()

    if not value:
        raise RuntimeError(
            "SUPABASE_URL is missing in Vercel."
        )

    for suffix in (
        "/rest/v1",
        "/auth/v1",
        "/storage/v1",
    ):
        if value.endswith(suffix):
            value = value[: -len(suffix)]

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


def get_supabase_anon_key():
    value = os.environ.get(
        "SUPABASE_ANON_KEY",
        ""
    ).strip()

    if not value:
        raise RuntimeError(
            "SUPABASE_ANON_KEY is missing in Vercel."
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


# ---------------------------------------------------------
# SUPABASE AUTHENTICATION
# ---------------------------------------------------------

def _get_user_with_access_token(access_token):
    url = (
        get_supabase_url()
        + "/auth/v1/user"
    )

    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            # IMPORTANT:
            # Auth verification uses the publishable/anon key.
            "apikey":
                get_supabase_anon_key(),

            # The user's actual access token goes here.
            "Authorization":
                "Bearer " + access_token,

            "Accept":
                "application/json",
        },
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=20
        ) as response:

            body = (
                response
                .read()
                .decode(
                    "utf-8",
                    errors="replace"
                )
            )

            if not body:
                raise RuntimeError(
                    "Supabase returned an empty authentication response."
                )

            try:
                user = json.loads(body)
            except Exception as exc:
                raise RuntimeError(
                    "Supabase returned an invalid authentication response."
                ) from exc

            if not isinstance(
                user,
                dict
            ):
                raise RuntimeError(
                    "Supabase returned an invalid user object."
                )

            user_id = str(
                user.get(
                    "id",
                    ""
                )
            ).strip()

            if not user_id:
                raise ValueError(
                    "Supabase returned no user ID."
                )

            return user

    except urllib.error.HTTPError as exc:

        error_body = (
            exc.read()
            .decode(
                "utf-8",
                errors="replace"
            )
        )

        if exc.code == 401:
            raise ValueError(
                "Invalid or expired login session."
            ) from exc

        raise RuntimeError(
            "Supabase authentication check failed "
            "(HTTP "
            + str(exc.code)
            + ")."
        ) from exc

    except urllib.error.URLError as exc:

        raise RuntimeError(
            "Unable to connect to Supabase."
        ) from exc


def verify_access_token(access_token):
    token = str(
        access_token or ""
    ).strip()

    if not token:
        raise ValueError(
            "Authorization token is required."
        )

    return _get_user_with_access_token(
        token
    )


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


# ---------------------------------------------------------
# SUPABASE REST HELPERS
# ---------------------------------------------------------

def _supabase_rest_headers():
    key = get_supabase_service_key()

    return {
        "apikey":
            key,
        "Authorization":
            "Bearer " + key,
        "Accept":
            "application/json",
        "Content-Type":
            "application/json",
    }


def _supabase_rpc(
    function_name,
    payload
):
    url = (
        get_supabase_url()
        + "/rest/v1/rpc/"
        + function_name
    )

    body = json.dumps(
        payload,
        separators=(",", ":")
    ).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers=_supabase_rest_headers(),
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=20
        ) as response:

            raw = (
                response
                .read()
                .decode(
                    "utf-8",
                    errors="replace"
                )
            )

            if not raw:
                return None

            try:
                return json.loads(raw)
            except Exception:
                return raw

    except urllib.error.HTTPError as exc:

        error_body = (
            exc.read()
            .decode(
                "utf-8",
                errors="replace"
            )
        )

        raise RuntimeError(
            "Supabase RPC failed for "
            + function_name
            + " (HTTP "
            + str(exc.code)
            + "): "
            + error_body
        ) from exc

    except urllib.error.URLError as exc:

        raise RuntimeError(
            "Unable to connect to Supabase: "
            + str(exc.reason)
        ) from exc


def _scalar_integer(value):
    if isinstance(
        value,
        bool
    ):
        raise ValueError(
            "Invalid numeric response."
        )

    if isinstance(
        value,
        int
    ):
        return value

    if isinstance(
        value,
        float
    ):
        return int(value)

    if isinstance(
        value,
        str
    ):
        return int(
            value.strip()
        )

    if isinstance(
        value,
        list
    ) and len(value) == 1:
        return _scalar_integer(
            value[0]
        )

    raise ValueError(
        "Invalid numeric response from Supabase."
    )


# ---------------------------------------------------------
# DAILY ANALYSIS LIMIT
# ---------------------------------------------------------

def reserve_analysis_slot(user_id):
    user_id = str(
        user_id or ""
    ).strip()

    if not user_id:
        raise ValueError(
            "User ID is required."
        )

    result = _supabase_rpc(
        "reserve_analysis_slot",
        {
            "p_user_id":
                user_id
        }
    )

    count = _scalar_integer(
        result
    )

    return count


def release_analysis_slot(user_id):
    user_id = str(
        user_id or ""
    ).strip()

    if not user_id:
        raise ValueError(
            "User ID is required."
        )

    result = _supabase_rpc(
        "release_analysis_slot",
        {
            "p_user_id":
                user_id
        }
    )

    count = _scalar_integer(
        result
    )

    return count


# ---------------------------------------------------------
# SECURE ANALYSIS JOB TOKEN
# ---------------------------------------------------------

def _now():
    return int(
        time.time()
    )


def _job_token_key():
    return get_openai_key_for_signing()


def create_secure_job_token(
    response_id,
    instrument,
    trade_focus,
    user_id
):
    response_id = str(
        response_id or ""
    ).strip()

    instrument = str(
        instrument or ""
    ).strip()

    trade_focus = str(
        trade_focus or ""
    ).strip()

    user_id = str(
        user_id or ""
    ).strip()

    if not response_id:
        raise ValueError(
            "Response ID is required."
        )

    if not instrument:
        raise ValueError(
            "Instrument is required."
        )

    if not trade_focus:
        raise ValueError(
            "Trade focus is required."
        )

    if not user_id:
        raise ValueError(
            "User ID is required."
        )

    payload = {
        "response_id":
            response_id,
        "instrument":
            instrument,
        "trade_focus":
            trade_focus,
        "user_id":
            user_id,
        "created_at":
            _now()
    }

    raw = json.dumps(
        payload,
        separators=(",", ":"),
        sort_keys=True
    ).encode("utf-8")

    signature = hmac.new(
        _job_token_key(),
        raw,
        hashlib.sha256
    ).digest()

    encoded_payload = (
        base64
        .urlsafe_b64encode(raw)
        .decode("ascii")
        .rstrip("=")
    )

    encoded_signature = (
        base64
        .urlsafe_b64encode(signature)
        .decode("ascii")
        .rstrip("=")
    )

    return (
        encoded_payload
        + "."
        + encoded_signature
    )


def read_secure_job_token(token):
    token = str(
        token or ""
    ).strip()

    if not token:
        raise ValueError(
            "Analysis job token is required."
        )

    try:

        parts = token.split(
            ".",
            1
        )

        if len(parts) != 2:
            raise ValueError(
                "Invalid job token."
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
            base64
            .urlsafe_b64decode(
                signature_part
                + "=" * (
                    -len(signature_part) % 4
                )
            )
        )

        expected_signature = hmac.new(
            _job_token_key(),
            raw,
            hashlib.sha256
        ).digest()

        if not hmac.compare_digest(
            supplied_signature,
            expected_signature
        ):
            raise ValueError(
                "Invalid job token signature."
            )

        data = json.loads(
            raw.decode("utf-8")
        )

        created_at = int(
            data["created_at"]
        )

        age = _now() - created_at

        if age < 0:
            raise ValueError(
                "Invalid analysis job timestamp."
            )

        if age > JOB_TTL_SECONDS:
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
                "Job response ID is missing."
            )

        if not instrument:
            raise ValueError(
                "Job instrument is missing."
            )

        if not trade_focus:
            raise ValueError(
                "Job trade focus is missing."
            )

        if not user_id:
            raise ValueError(
                "Job user ID is missing."
            )

        return (
            response_id,
            instrument,
            trade_focus,
            user_id
        )

    except ValueError:
        raise

    except Exception as exc:
        raise ValueError(
            "Invalid analysis job: "
            + str(exc)
        ) from exc


# ---------------------------------------------------------
# COMPATIBILITY ALIASES
# ---------------------------------------------------------
# These keep older backend code from breaking if one of
# the old function names is still referenced.

create_job_token = create_secure_job_token
read_job_token = read_secure_job_token
