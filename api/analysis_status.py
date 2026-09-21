import os
import json
import time
import hmac
import hashlib
import base64
import urllib.request
import urllib.error
from urllib.parse import parse_qs


OPENAI_URL = "https://api.openai.com/v1/responses"
JOB_TTL_SECONDS = 86400


def make_response(status, data):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        },
        "body": json.dumps(data),
    }


def get_token_from_request(request):
    try:
        query = getattr(request, "query", None)

        if isinstance(query, dict):
            value = query.get("job_id")

            if isinstance(value, list):
                return value[0] if value else None

            if value:
                return value

        url = getattr(request, "url", "")

        if url and "?" in url:
            query_string = url.split("?", 1)[1]
            values = parse_qs(query_string).get("job_id")

            if values:
                return values[0]

    except Exception as e:
        raise Exception(
            "Could not read job_id: " + str(e)
        )

    return None


def get_secret():
    secret = os.environ.get("JOB_TOKEN_SECRET")

    if not secret:
        secret = os.environ.get("OPENAI_API_KEY")

    if not secret:
        raise Exception(
            "JOB_TOKEN_SECRET and OPENAI_API_KEY are both missing"
        )

    return secret.encode("utf-8")


def verify_token(token):
    if not token:
        raise Exception(
            "No job_id was supplied"
        )

    if "." not in token:
        raise Exception(
            "Invalid job token format"
        )

    encoded_payload, supplied_signature = token.rsplit(".", 1)

    expected_signature = hmac.new(
        get_secret(),
        encoded_payload.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(
        supplied_signature,
        expected_signature
    ):
        raise Exception(
            "Invalid job token signature"
        )

    try:
        padding = "=" * (
            -len(encoded_payload) % 4
        )

        decoded = base64.urlsafe_b64decode(
            encoded_payload + padding
        )

        payload = json.loads(
            decoded.decode("utf-8")
        )

    except Exception as e:
        raise Exception(
            "Could not decode job token: " + str(e)
        )

    response_id = payload.get("r")
    created_at = payload.get("t")
    instrument = payload.get("i", "")
    trade_focus = payload.get("f", "")

    if not response_id:
        raise Exception(
            "Job token does not contain response ID"
        )

    if not created_at:
        raise Exception(
            "Job token does not contain timestamp"
        )

    try:
        created_at = int(created_at)
    except Exception:
        raise Exception(
            "Job token timestamp is invalid"
        )

    if time.time() - created_at > JOB_TTL_SECONDS:
        raise Exception(
            "Analysis job has expired"
        )

    return (
        response_id,
        instrument,
        trade_focus
    )


def retrieve_openai_response(response_id):
    api_key = os.environ.get("OPENAI_API_KEY")

    if not api_key:
        raise Exception(
            "OPENAI_API_KEY is missing from Vercel"
        )

    response_url = (
        OPENAI_URL
        + "/"
        + response_id
    )

    request = urllib.request.Request(
        response_url,
        method="GET",
        headers={
            "Authorization":
                "Bearer " + api_key,

            "Content-Type":
                "application/json",

            "Accept":
                "application/json",
        }
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=30
        ) as response:

            raw = response.read().decode(
                "utf-8"
            )

            if not raw:
                raise Exception(
                    "OpenAI returned an empty response"
                )

            try:
                return json.loads(raw)

            except Exception as e:
                raise Exception(
                    "OpenAI returned invalid JSON: "
                    + str(e)
                )

    except urllib.error.HTTPError as e:

        try:
            body = e.read().decode(
                "utf-8"
            )
        except Exception:
            body = ""

        if e.code == 404:
            raise Exception(
                "OpenAI could not find analysis response "
                + response_id
                + ". HTTP 404. "
                + (
                    body[:1500]
                    if body
                    else "No response body."
                )
            )

        raise Exception(
            "OpenAI HTTP "
            + str(e.code)
            + ": "
            + (
                body[:1500]
                if body
                else "No response body."
            )
        )

    except urllib.error.URLError as e:

        raise Exception(
            "OpenAI connection error: "
            + str(
                getattr(
                    e,
                    "reason",
                    "unknown"
                )
            )
        )


def extract_text(data):
    output_text = data.get(
        "output_text"
    )

    if (
        isinstance(output_text, str)
        and output_text.strip()
    ):
        return output_text.strip()

    output = data.get(
        "output",
        []
    )

    if not isinstance(output, list):
        return ""

    pieces = []

    for item in output:

        if not isinstance(item, dict):
            continue

        content = item.get(
            "content",
            []
        )

        if not isinstance(content, list):
            continue

        for part in content:

            if not isinstance(part, dict):
                continue

            text_value = part.get(
                "text"
            )

            if isinstance(
                text_value,
                str
            ):
                pieces.append(
                    text_value
                )

    return "".join(
        pieces
    ).strip()


def parse_result(
    data,
    instrument,
    trade_focus
):
    text = extract_text(data)

    if not text:
        raise Exception(
            "OpenAI completed but no analysis text was found"
        )

    try:
        result = json.loads(text)

    except Exception as e:
        raise Exception(
            "Analysis JSON could not be parsed: "
            + str(e)
        )

    if not isinstance(result, dict):
        raise Exception(
            "Analysis result is not an object"
        )

    result["instrument"] = (
        result.get("instrument")
        or instrument
    )

    result["trade_focus"] = (
        result.get("trade_focus")
        or trade_focus
    )

    signal = result.get(
        "signal"
    )

    if signal not in [
        "BUY",
        "SELL",
        "NO TRADE"
    ]:
        raise Exception(
            "Invalid signal returned: "
            + str(signal)
        )

    try:
        confidence = int(
            result.get(
                "confidence",
                0
            )
        )
    except Exception:
        confidence = 0

    result["confidence"] = max(
        0,
        min(
            100,
            confidence
        )
    )

    return result


def handler(request):

    try:

        method = getattr(
            request,
            "method",
            "GET"
        )

        if method == "OPTIONS":
            return make_response(
                204,
                {}
            )

        if method != "GET":
            return make_response(
                405,
                {
                    "status": "failed",
                    "error": "GET required"
                }
            )

        token = get_token_from_request(
            request
        )

        if not token:
            return make_response(
                400,
                {
                    "status": "failed",
                    "error": "Missing job_id"
                }
            )

        (
            response_id,
            instrument,
            trade_focus
        ) = verify_token(token)

        print(
            "LM ANALYZER STATUS CHECK"
        )

        print(
            "RESPONSE ID:",
            response_id
        )

        print(
            "INSTRUMENT:",
            instrument
        )

        print(
            "TRADE FOCUS:",
            trade_focus
        )

        data = retrieve_openai_response(
            response_id
        )

        if not isinstance(
            data,
            dict
        ):
            raise Exception(
                "OpenAI response is not an object"
            )

        openai_status = data.get(
            "status"
        )

        print(
            "OPENAI STATUS:",
            openai_status
        )

        # BACKGROUND RESPONSE STILL RUNNING

        if openai_status in [
            "queued",
            "in_progress",
            "processing"
        ]:

            return make_response(
                200,
                {
                    "status":
                        "in_progress",

                    "poll_after_seconds":
                        3
                }
            )

        # COMPLETED

        if openai_status == "completed":

            result = parse_result(
                data,
                instrument,
                trade_focus
            )

            return make_response(
                200,
                {
                    "status":
                        "completed",

                    "result":
                        result
                }
            )

        # TERMINAL FAILURE

        if openai_status in [
            "failed",
            "cancelled",
            "canceled",
            "expired",
            "incomplete"
        ]:

            details = (
                data.get("error")
                or data.get("incomplete_details")
                or data.get("status_details")
            )

            print(
                "OPENAI TERMINAL RESPONSE:"
            )

            print(
                json.dumps(data)
            )

            return make_response(
                200,
                {
                    "status":
                        "failed",

                    "error":
                        "OpenAI analysis failed",

                    "openai_status":
                        openai_status,

                    "details":
                        details
                }
            )

        # UNKNOWN STATUS

        return make_response(
            200,
            {
                "status":
                    "failed",

                "error":
                    "Unknown OpenAI status",

                "openai_status":
                    openai_status,

                "response_id":
                    response_id
            }
        )

    except Exception as e:

        print(
            "LM ANALYZER STATUS ERROR:"
        )

        print(
            repr(e)
        )

        return make_response(
            200,
            {
                "status":
                    "failed",

                "error":
                    str(e)
            }
        )
