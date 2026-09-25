import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from api.user_security import (
    extract_bearer_token,
    get_supabase_service_key,
    get_supabase_url,
    read_secure_job_token,
    verify_access_token,
)


OPENAI_URL = "https://api.openai.com/v1/responses"


def json_response(handler, status_code, payload):

    body = json.dumps(
        payload,
        ensure_ascii=False,
    ).encode("utf-8")

    handler.send_response(status_code)

    handler.send_header(
        "Content-Type",
        "application/json; charset=utf-8",
    )

    handler.send_header(
        "Access-Control-Allow-Origin",
        "*",
    )

    handler.send_header(
        "Access-Control-Allow-Methods",
        "GET, POST, OPTIONS",
    )

    handler.send_header(
        "Access-Control-Allow-Headers",
        "Content-Type, Authorization",
    )

    handler.send_header(
        "Cache-Control",
        "no-store, no-cache, must-revalidate",
    )

    handler.send_header(
        "Content-Length",
        str(len(body)),
    )

    handler.end_headers()

    handler.wfile.write(body)


def get_openai_key():

    key = os.environ.get(
        "OPENAI_API_KEY",
        "",
    ).strip()

    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY is missing from Vercel."
        )

    return key


def retrieve_response(
    response_id,
):

    request = urllib.request.Request(
        f"{OPENAI_URL}/{response_id}",
        headers={
            "Authorization":
                "Bearer " + get_openai_key(),

            "Accept":
                "application/json",
        },
        method="GET",
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=20,
        ) as response:

            return json.loads(
                response.read().decode(
                    "utf-8"
                )
            )

    except urllib.error.HTTPError as exc:

        raw = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        print(
            "OPENAI STATUS ERROR:",
            exc.code,
            raw,
        )

        raise RuntimeError(
            "Analysis status is temporarily unavailable."
        ) from exc

    except (
        urllib.error.URLError,
        TimeoutError,
    ) as exc:

        raise RuntimeError(
            "Analysis status is temporarily unavailable."
        ) from exc


def extract_job_token(handler):

    parsed = urlparse(
        handler.path
    )

    values = parse_qs(
        parsed.query
    )

    return str(
        values.get(
            "job_id",
            [""],
        )[0]
    ).strip()


def extract_output_text(response):

    output = response.get(
        "output"
    )

    if isinstance(
        output,
        list,
    ):

        pieces = []

        for item in output:

            if not isinstance(
                item,
                dict,
            ):
                continue

            content = item.get(
                "content"
            )

            if not isinstance(
                content,
                list,
            ):
                continue

            for part in content:

                if not isinstance(
                    part,
                    dict,
                ):
                    continue

                text = part.get(
                    "text"
                )

                if isinstance(
                    text,
                    str,
                ) and text.strip():

                    pieces.append(
                        text
                    )

        if pieces:
            return "\n".join(
                pieces
            ).strip()

    value = response.get(
        "output_text"
    )

    if isinstance(
        value,
        str,
    ) and value.strip():

        return value.strip()

    raise ValueError(
        "OpenAI completed the analysis but returned no text."
    )


def parse_result(
    response,
    instrument,
    trade_focus,
):

    text = extract_output_text(
        response
    )

    try:

        result = json.loads(
            text
        )

    except json.JSONDecodeError:

        cleaned = text.strip()

        if cleaned.startswith(
            "```"
        ):

            cleaned = cleaned.replace(
                "```json",
                "",
                1,
            )

            cleaned = cleaned.replace(
                "```",
                "",
                1,
            ).strip()

        result = json.loads(
            cleaned
        )

    if not isinstance(
        result,
        dict,
    ):
        raise ValueError(
            "Analysis result is not valid JSON."
        )

    result["instrument"] = instrument
    result["trade_focus"] = trade_focus

    signal = str(
        result.get(
            "signal",
            "",
        )
    ).upper().strip()

    if signal not in {
        "BUY",
        "SELL",
        "NO TRADE",
    }:
        raise ValueError(
            "Analysis returned an invalid signal."
        )

    result["signal"] = signal

    if signal == "NO TRADE":

        result["entry"] = ""
        result["stop_loss"] = ""
        result["take_profit_1"] = ""
        result["take_profit_2"] = ""
        result["risk_reward"] = ""

    return result


def encode_result(result):

    raw = json.dumps(
        result,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    return (
        base64.urlsafe_b64encode(
            raw
        )
        .decode("ascii")
        .rstrip("=")
    )


def save_completed_history(
    user_id,
    response_id,
    result,
):

    encoded = encode_result(
        result
    )

    url = (
        get_supabase_url()
        + "/rest/v1/analysis_jobs?"
        + "user_id=eq."
        + urllib.parse.quote(
            str(user_id),
            safe="",
        )
        + "&openai_response_id=eq."
        + urllib.parse.quote(
            str(response_id),
            safe="",
        )
    )

    payload = {
        "status":
            "completed",

        "openai_response_id":
            encoded,

        "error_message":
            None,
    }

    request = urllib.request.Request(
        url,
        data=json.dumps(
            payload,
            separators=(",", ":"),
        ).encode("utf-8"),
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

            "Prefer":
                "return=minimal",
        },
        method="PATCH",
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=20,
        ) as response:

            response.read()

        return True

    except Exception as exc:

        print(
            "COMPLETED HISTORY SAVE FAILED:",
            repr(exc),
        )

        return False


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):

        json_response(
            self,
            204,
            {},
        )

    def do_GET(self):

        try:

            access_token = extract_bearer_token(
                self
            )

            user = verify_access_token(
                access_token
            )

            current_user_id = str(
                user["id"]
            )

            token = extract_job_token(
                self
            )

            if not token:
                raise ValueError(
                    "Analysis job ID is required."
                )

            (
                response_id,
                instrument,
                trade_focus,
                token_user_id,
            ) = read_secure_job_token(
                token
            )

            if (
                token_user_id
                != current_user_id
            ):

                json_response(
                    self,
                    403,
                    {
                        "error":
                            "This analysis job does not belong to this user."
                    },
                )

                return

            response = retrieve_response(
                response_id
            )

            status = str(
                response.get(
                    "status",
                    "queued",
                )
            ).lower()

            if status in {
                "queued",
                "in_progress",
            }:

                json_response(
                    self,
                    200,
                    {
                        "status":
                            status,

                        "poll_after_seconds":
                            2,
                    },
                )

                return

            if status == "completed":

                result = parse_result(
                    response,
                    instrument,
                    trade_focus,
                )

                history_saved = (
                    save_completed_history(
                        current_user_id,
                        response_id,
                        result,
                    )
                )

                json_response(
                    self,
                    200,
                    {
                        "status":
                            "completed",

                        "result":
                            result,

                        "history_saved":
                            history_saved,
                    },
                )

                return

            if status in {
                "failed",
                "cancelled",
                "incomplete",
                "expired",
            }:

                json_response(
                    self,
                    200,
                    {
                        "status":
                            "failed",

                        "error":
                            "The analysis did not complete.",
                    },
                )

                return

            json_response(
                self,
                200,
                {
                    "status":
                        "in_progress",

                    "poll_after_seconds":
                        3,
                },
            )

        except ValueError as exc:

            json_response(
                self,
                400,
                {
                    "error":
                        str(exc),
                },
            )

        except RuntimeError as exc:

            json_response(
                self,
                503,
                {
                    "error":
                        str(exc),
                },
            )

        except Exception as exc:

            print(
                "ANALYSIS STATUS CRASH:",
                repr(exc),
            )

            json_response(
                self,
                500,
                {
                    "error":
                        "Analysis status failed.",
                    "details":
                        str(exc),
                },
            )
