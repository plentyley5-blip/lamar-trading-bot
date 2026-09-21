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


def response_json(status_code, data):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        },
        "body": json.dumps(data),
    }


def get_secret():
    secret = os.environ.get("JOB_TOKEN_SECRET")

    if not secret:
        secret = os.environ.get("OPENAI_API_KEY")

    if not secret:
        raise Exception("Server configuration error: job token secret is missing")

    return secret.encode("utf-8")


def verify_job_token(token):
    if not token:
        raise Exception("Missing analysis job")

    if "." not in token:
        raise Exception("Invalid analysis job")

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
        raise Exception("Invalid analysis job")

    try:
        padding = "=" * (-len(encoded_payload) % 4)

        decoded = base64.urlsafe_b64decode(
            encoded_payload + padding
        )

        payload = json.loads(
            decoded.decode("utf-8")
        )

    except Exception:
        raise Exception("Invalid analysis job token")

    response_id = payload.get("r")
    created_at = payload.get("t")
    instrument = payload.get("i", "")
    trade_focus = payload.get("f", "")

    if not response_id:
        raise Exception("Analysis response ID missing")

    if not created_at:
        raise Exception("Analysis job timestamp missing")

    try:
        created_at = int(created_at)
    except Exception:
        raise Exception("Invalid analysis job timestamp")

    if time.time() - created_at > JOB_TTL_SECONDS:
        raise Exception("Analysis job expired")

    return response_id, instrument, trade_focus


def get_openai_response(response_id):
    api_key = os.environ.get("OPENAI_API_KEY")

    if not api_key:
        raise Exception("OpenAI API key is not configured")

    request = urllib.request.Request(
        OPENAI_URL + "/" + response_id,
        method="GET",
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=45
        ) as response:

            raw = response.read().decode("utf-8")

            if not raw:
                raise Exception(
                    "OpenAI returned an empty response"
                )

            try:
                return json.loads(raw)
            except Exception:
                raise Exception(
                    "OpenAI returned invalid JSON"
                )

    except urllib.error.HTTPError as error:

        try:
            error_body = error.read().decode("utf-8")
        except Exception:
            error_body = ""

        raise Exception(
            "OpenAI HTTP "
            + str(error.code)
            + ": "
            + error_body[:1000]
        )

    except urllib.error.URLError as error:

        reason = getattr(
            error,
            "reason",
            "unknown connection error"
        )

        raise Exception(
            "OpenAI connection error: "
            + str(reason)
        )

    except TimeoutError:
        raise Exception(
            "OpenAI request timed out"
        )


def extract_output_text(data):
    output = data.get("output", [])

    if not isinstance(output, list):
        return ""

    parts = []

    for item in output:

        if not isinstance(item, dict):
            continue

        content = item.get("content", [])

        if not isinstance(content, list):
            continue

        for content_item in content:

            if not isinstance(content_item, dict):
                continue

            text_value = content_item.get("text")

            if isinstance(text_value, str):
                parts.append(text_value)

    return "".join(parts).strip()


def parse_completed_result(
    data,
    instrument,
    trade_focus
):
    text = extract_output_text(data)

    if not text:

        # Some Responses API responses can expose
        # output_text directly.
        output_text = data.get("output_text")

        if isinstance(output_text, str):
            text = output_text.strip()

    if not text:
        raise Exception(
            "OpenAI completed without returning analysis data"
        )

    try:
        result = json.loads(text)

    except Exception as error:
        raise Exception(
            "OpenAI returned invalid JSON: "
            + str(error)
        )

    if not isinstance(result, dict):
        raise Exception(
            "OpenAI returned an invalid analysis object"
        )

    if not result.get("instrument"):
        result["instrument"] = instrument

    if not result.get("trade_focus"):
        result["trade_focus"] = trade_focus

    signal = result.get("signal")

    if signal not in [
        "BUY",
        "SELL",
        "NO TRADE"
    ]:
        raise Exception(
            "Analysis returned an invalid signal"
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


def get_query_value(request, name):
    """
    Works with Vercel-style request objects and
    also tolerates query-string dictionaries.
    """

    query = getattr(
        request,
        "query",
        None
    )

    if isinstance(query, dict):

        value = query.get(name)

        if isinstance(value, list):

            if value:
                return value[0]

            return None

        return value

    url = getattr(
        request,
        "url",
        ""
    )

    if url:

        parsed = parse_qs(
            url.split("?", 1)[1]
            if "?" in url
            else ""
        )

        values = parsed.get(name)

        if values:
            return values[0]

    return None


def handler(request):
    try:

        method = getattr(
            request,
            "method",
            "GET"
        )

        if method == "OPTIONS":

            return response_json(
                204,
                {}
            )

        if method != "GET":

            return response_json(
                405,
                {
                    "status": "failed",
                    "error": "Method not allowed"
                }
            )

        token = get_query_value(
            request,
            "job_id"
        )

        if not token:

            return response_json(
                400,
                {
                    "status": "failed",
                    "error": "Missing analysis job"
                }
            )

        response_id, instrument, trade_focus = verify_job_token(
            token
        )

        data = get_openai_response(
            response_id
        )

        if not isinstance(data, dict):

            return response_json(
                502,
                {
                    "status": "failed",
                    "error": "OpenAI returned an invalid response"
                }
            )

        openai_status = data.get(
            "status"
        )

        if openai_status in [
            "queued",
            "in_progress",
            "processing"
        ]:

            return response_json(
                200,
                {
                    "status": "in_progress",
                    "poll_after_seconds": 3
                }
            )

        if openai_status == "completed":

            result = parse_completed_result(
                data,
                instrument,
                trade_focus
            )

            return response_json(
                200,
                {
                    "status": "completed",
                    "result": result
                }
            )

        if openai_status in [
            "failed",
            "cancelled",
            "canceled",
            "expired",
            "incomplete"
        ]:

            error_details = data.get(
                "error"
            )

            if error_details is None:
                error_details = data.get(
                    "incomplete_details"
                )

            if error_details is None:
                error_details = data.get(
                    "status_details"
                )

            print(
                "OPENAI TERMINAL RESPONSE:",
                json.dumps(data)
            )

            return response_json(
                200,
                {
                    "status": "failed",
                    "error": "OpenAI analysis failed",
                    "openai_status": openai_status,
                    "details": error_details
                }
            )

        return response_json(
            200,
            {
                "status": "failed",
                "error": "Unknown OpenAI analysis status",
                "openai_status": openai_status
            }
        )

    except Exception as error:

        print(
            "ANALYSIS STATUS ERROR:",
            repr(error)
        )

        return response_json(
            200,
            {
                "status": "failed",
                "error": str(error)
            }
        )
