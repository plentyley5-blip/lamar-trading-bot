import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

from api.user_security import (
    create_secure_job_token,
    extract_bearer_token,
    get_supabase_service_key,
    get_supabase_url,
    is_owner_user,
    release_analysis_slot,
    reserve_analysis_slot,
    verify_access_token,
)


OPENAI_URL = "https://api.openai.com/v1/responses"
MODEL = os.environ.get(
    "OPENAI_MODEL",
    "gpt-5.6-luna",
).strip()


SYSTEM_PROMPT = """
You are LM ANALYZER, a professional chart-analysis engine.

Analyze the supplied 4H and 15M charts for the supplied instrument and trade
focus: SCALP, DAY TRADE, or SWING.

Return BUY, SELL, or NO TRADE.

Use genuine multi-timeframe technical analysis:
support/resistance, pure price action, market structure, BOS, CHoCH,
liquidity, liquidity sweeps, SMC, order blocks, fair value gaps, Fibonacci,
premium/discount, displacement, inducement, mitigation/invalidation,
higher-timeframe context, lower-timeframe confirmation.

The 4H chart provides higher-timeframe context.
The 15M chart provides confirmation and execution context.

SCALP emphasizes 15M confirmation.
DAY TRADE balances 4H and 15M.
SWING emphasizes 4H structure.

Methods do not need unanimous agreement.

Return NO TRADE when the charts are unclear, unreadable, structurally
conflicting without resolution, or when a defensible entry, SL and target
cannot be established.

Never invent exact prices.
Never guarantee profit.
Confidence is an analysis-confidence score, not a probability of profit.

For NO TRADE:
entry=""
stop_loss=""
take_profit_1=""
take_profit_2=""
risk_reward=""

Only return the requested JSON.
"""


OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "signal": {
            "type": "string",
            "enum": ["BUY", "SELL", "NO TRADE"],
        },
        "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 100,
        },
        "strength": {
            "type": "string",
            "enum": [
                "VERY STRONG",
                "STRONG",
                "MODERATE",
                "WEAK",
            ],
        },
        "instrument": {
            "type": "string",
        },
        "trade_focus": {
            "type": "string",
            "enum": [
                "SCALP",
                "DAY TRADE",
                "SWING",
            ],
        },
        "trend": {
            "type": "string",
            "enum": [
                "BULLISH",
                "BEARISH",
                "RANGE",
                "UNCLEAR",
            ],
        },
        "trade_idea": {
            "type": "string",
        },
        "entry": {
            "type": "string",
        },
        "stop_loss": {
            "type": "string",
        },
        "take_profit_1": {
            "type": "string",
        },
        "take_profit_2": {
            "type": "string",
        },
        "risk_reward": {
            "type": "string",
        },
        "duration": {
            "type": "string",
        },
        "higher_timeframe_context": {
            "type": "string",
        },
        "lower_timeframe_confirmation": {
            "type": "string",
        },
        "data_analysis": {
            "type": "string",
        },
        "explanation": {
            "type": "string",
        },
        "contributing_methods": {
            "type": "array",
            "items": {
                "type": "string",
            },
        },
        "weak_methods": {
            "type": "array",
            "items": {
                "type": "string",
            },
        },
        "conflicting_methods": {
            "type": "array",
            "items": {
                "type": "string",
            },
        },
        "news_fundamental_risk": {
            "type": "string",
        },
        "warnings": {
            "type": "array",
            "items": {
                "type": "string",
            },
        },
    },
    "required": [
        "signal",
        "confidence",
        "strength",
        "instrument",
        "trade_focus",
        "trend",
        "trade_idea",
        "entry",
        "stop_loss",
        "take_profit_1",
        "take_profit_2",
        "risk_reward",
        "duration",
        "higher_timeframe_context",
        "lower_timeframe_confirmation",
        "data_analysis",
        "explanation",
        "contributing_methods",
        "weak_methods",
        "conflicting_methods",
        "news_fundamental_risk",
        "warnings",
    ],
}


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


def read_json(handler):
    try:
        length = int(
            handler.headers.get(
                "Content-Length",
                "0",
            )
        )
    except ValueError:
        raise ValueError("Invalid request.")

    if length <= 0 or length > 25 * 1024 * 1024:
        raise ValueError("Invalid chart request.")

    raw = handler.rfile.read(length)

    try:
        data = json.loads(
            raw.decode("utf-8")
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            "Invalid request JSON."
        ) from exc

    if not isinstance(data, dict):
        raise ValueError("Invalid request.")

    return data


def validate_instrument(value):
    value = str(
        value or ""
    ).strip()

    if not value:
        raise ValueError(
            "Instrument is required."
        )

    if len(value) > 50:
        raise ValueError(
            "Instrument name is too long."
        )

    return value


def validate_focus(value):
    value = str(
        value or ""
    ).strip().upper()

    if value not in {
        "SCALP",
        "DAY TRADE",
        "SWING",
    }:
        raise ValueError(
            "Trade focus must be SCALP, DAY TRADE, or SWING."
        )

    return value


def validate_image(value, name):
    if not isinstance(value, str):
        raise ValueError(
            name + " must be an image."
        )

    if not value.startswith("data:image/"):
        raise ValueError(
            name + " is not a valid image."
        )

    if ";base64," not in value:
        raise ValueError(
            name + " is not base64 encoded."
        )

    if len(value) > 12 * 1024 * 1024:
        raise ValueError(
            name + " is too large."
        )

    return value


def create_openai_background_job(
    instrument,
    trade_focus,
    higher_image,
    lower_image,
):
    api_key = os.environ.get(
        "OPENAI_API_KEY",
        "",
    ).strip()

    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is missing from Vercel."
        )

    payload = {
        "model": MODEL,
        "background": True,
        "store": True,
        "instructions": SYSTEM_PROMPT,
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "Instrument: "
                            + instrument
                            + "\nTrade focus: "
                            + trade_focus
                            + "\n\n"
                            "Analyze the supplied charts.\n"
                            "First image: 4H.\n"
                            "Second image: 15M.\n"
                            "Use only visible chart evidence.\n"
                            "Do not invent exact prices."
                        ),
                    },
                    {
                        "type": "input_image",
                        "image_url": higher_image,
                        "detail": "low",
                    },
                    {
                        "type": "input_image",
                        "image_url": lower_image,
                        "detail": "low",
                    },
                ],
            }
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "lamar_trade_analysis",
                "strict": True,
                "schema": OUTPUT_SCHEMA,
            }
        },
        "max_output_tokens": 1200,
    }

    request = urllib.request.Request(
        OPENAI_URL,
        data=json.dumps(
            payload,
            separators=(",", ":"),
        ).encode("utf-8"),
        headers={
            "Authorization":
                "Bearer " + api_key,
            "Content-Type":
                "application/json",
            "Accept":
                "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=55,
        ) as response:

            raw = response.read().decode(
                "utf-8"
            )

            data = json.loads(raw)

            if not isinstance(data, dict):
                raise RuntimeError(
                    "OpenAI returned an invalid response."
                )

            return data

    except urllib.error.HTTPError as exc:

        raw = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        print(
            "OPENAI CREATE ERROR:",
            exc.code,
            raw,
        )

        try:
            data = json.loads(raw)
        except Exception:
            data = {}

        message = ""

        if isinstance(
            data.get("error"),
            dict,
        ):
            message = str(
                data["error"].get(
                    "message",
                    "",
                )
            )

        if not message:
            message = str(
                data.get(
                    "message",
                    "",
                )
            )

        if not message:
            message = raw[:1000]

        raise RuntimeError(
            "OpenAI error: " + message
        ) from exc

    except urllib.error.URLError as exc:

        raise RuntimeError(
            "Unable to connect to OpenAI: "
            + str(exc.reason)
        ) from exc

    except TimeoutError as exc:

        raise RuntimeError(
            "OpenAI request timed out."
        ) from exc


def extract_openai_id(response):
    if not isinstance(
        response,
        dict,
    ):
        return ""

    for key in (
        "id",
        "response_id",
    ):
        value = str(
            response.get(
                key,
                "",
            )
        ).strip()

        if value:
            return value

    return ""


def save_job(
    user_id,
    instrument,
    trade_focus,
    response_id,
    higher_image,
    lower_image,
    status,
):
    """
    History persistence is deliberately non-fatal.
    Analysis must continue even if this database write fails.
    """

    base = {
        "user_id":
            str(user_id),

        "instrument":
            str(instrument),

        "trade_focus":
            str(trade_focus),

        "status":
            str(status or "queued"),

        "openai_response_id":
            str(response_id),
    }

    payloads = [
        {
            **base,
            "higher_timeframe_image":
                higher_image,
            "lower_timeframe_image":
                lower_image,
        },
        base,
    ]

    for payload in payloads:

        try:
            request = urllib.request.Request(
                get_supabase_url()
                + "/rest/v1/analysis_jobs",
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
                method="POST",
            )

            with urllib.request.urlopen(
                request,
                timeout=20,
            ) as response:

                response.read()

            return True

        except Exception as exc:

            print(
                "HISTORY SAVE ATTEMPT FAILED:",
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

    def do_POST(self):

        reserved = False
        user = None

        try:
            access_token = extract_bearer_token(
                self
            )

            user = verify_access_token(
                access_token
            )

            user_id = str(
                user["id"]
            )

            owner = is_owner_user(
                user
            )

            data = read_json(
                self
            )

            instrument = validate_instrument(
                data.get(
                    "instrument"
                )
            )

            trade_focus = validate_focus(
                data.get(
                    "trade_focus",
                    "DAY TRADE",
                )
            )

            higher_image = validate_image(
                data.get(
                    "higher_timeframe_image"
                ),
                "4H chart",
            )

            lower_image = validate_image(
                data.get(
                    "lower_timeframe_image"
                ),
                "15M chart",
            )

            if not owner:

                slot = reserve_analysis_slot(
                    user_id
                )

                if slot == -1:

                    json_response(
                        self,
                        429,
                        {
                            "error":
                                "Daily analysis limit reached.",
                            "daily_limit":
                                4,
                            "remaining":
                                0,
                            "is_owner":
                                False,
                        },
                    )

                    return

                reserved = True

                remaining = max(
                    0,
                    4 - slot,
                )

            else:
                remaining = None

            try:

                openai_response = (
                    create_openai_background_job(
                        instrument,
                        trade_focus,
                        higher_image,
                        lower_image,
                    )
                )

            except Exception:

                if reserved:
                    try:
                        release_analysis_slot(
                            user_id
                        )
                    except Exception:
                        pass

                raise

            print(
                "OPENAI RESPONSE:",
                json.dumps(
                    openai_response,
                    ensure_ascii=False,
                ),
            )

            response_id = extract_openai_id(
                openai_response
            )

            if not response_id:

                if reserved:
                    try:
                        release_analysis_slot(
                            user_id
                        )
                    except Exception:
                        pass

                json_response(
                    self,
                    503,
                    {
                        "error":
                            "OpenAI did not return a response ID.",
                        "openai_response":
                            openai_response,
                    },
                )

                return

            status = str(
                openai_response.get(
                    "status",
                    "queued",
                )
            ).strip() or "queued"

            history_saved = save_job(
                user_id=user_id,
                instrument=instrument,
                trade_focus=trade_focus,
                response_id=response_id,
                higher_image=higher_image,
                lower_image=lower_image,
                status=status,
            )

            job_id = create_secure_job_token(
                response_id,
                instrument,
                trade_focus,
                user_id,
            )

            json_response(
                self,
                202,
                {
                    "status":
                        status,

                    "job_id":
                        job_id,

                    "poll_after_seconds":
                        2,

                    "is_owner":
                        owner,

                    "daily_limit":
                        None
                        if owner
                        else 4,

                    "remaining":
                        remaining,

                    "history_saved":
                        history_saved,
                },
            )

        except ValueError as exc:

            json_response(
                self,
                400,
                {
                    "error":
                        str(exc)
                },
            )

        except RuntimeError as exc:

            if reserved and user:
                try:
                    release_analysis_slot(
                        str(user["id"])
                    )
                except Exception:
                    pass

            json_response(
                self,
                503,
                {
                    "error":
                        str(exc)
                },
            )

        except Exception as exc:

            if reserved and user:
                try:
                    release_analysis_slot(
                        str(user["id"])
                    )
                except Exception:
                    pass

            print(
                "ANALYSIS SUBMIT CRASH:",
                repr(exc),
            )

            json_response(
                self,
                500,
                {
                    "error":
                        "Analysis submit failed.",
                    "details":
                        str(exc),
                },
            )
