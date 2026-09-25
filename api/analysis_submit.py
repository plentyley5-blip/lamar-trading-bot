import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
import uuid
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


GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
)

GEMINI_MODEL = os.environ.get(
    "GEMINI_MODEL",
    "gemini-3.8-flash",
).strip()


SYSTEM_PROMPT = """
You are LM ANALYZER, a professional multi-timeframe trading chart analysis
engine.

Analyze the supplied 4H and 15M charts for the selected instrument and
trade focus: SCALP, DAY TRADE, or SWING.

Return BUY, SELL, or NO TRADE.

Use genuine visible chart evidence including:
- support and resistance
- pure price action
- market structure
- BOS
- CHoCH
- liquidity
- liquidity sweeps
- Smart Money Concepts
- order blocks
- fair value gaps / imbalances
- Fibonacci
- premium / discount
- displacement
- inducement
- mitigation
- invalidation
- higher-timeframe structure
- lower-timeframe confirmation

The 4H chart provides higher-timeframe context.
The 15M chart provides confirmation and execution context.

SCALP emphasizes the 15M chart.
DAY TRADE balances 4H and 15M.
SWING emphasizes 4H structure.

Methods do not need unanimous agreement.

Return NO TRADE when:
- the chart is unclear
- price digits cannot be read reliably
- structure is unresolved
- the two timeframes materially conflict without a clear resolution
- a defensible entry, stop loss and target cannot be established

Never invent exact prices.
Never guarantee profit.

Confidence is an analysis-confidence score, not a probability of profit.

For NO TRADE:
entry=""
stop_loss=""
take_profit_1=""
take_profit_2=""
risk_reward=""

Return valid JSON only.
"""


OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "signal": {
            "type": "string",
            "enum": [
                "BUY",
                "SELL",
                "NO TRADE",
            ],
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

    handler.send_response(
        status_code
    )

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

    try:
        handler.wfile.write(body)
    except Exception:
        pass


def read_json(handler):
    try:
        length = int(
            handler.headers.get(
                "Content-Length",
                "0",
            )
        )
    except ValueError:
        raise ValueError(
            "Invalid request."
        )

    if length <= 0 or length > 25 * 1024 * 1024:
        raise ValueError(
            "Invalid chart request."
        )

    raw = handler.rfile.read(
        length
    )

    try:
        data = json.loads(
            raw.decode("utf-8")
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            "Invalid request JSON."
        ) from exc

    if not isinstance(
        data,
        dict,
    ):
        raise ValueError(
            "Invalid request."
        )

    return data


def validate_instrument(value):
    value = str(
        value or ""
    ).strip().upper()

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
    if not isinstance(
        value,
        str,
    ):
        raise ValueError(
            name + " must be an image."
        )

    if not value.startswith(
        "data:image/",
    ):
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


def split_image_data_url(
    value,
    name,
):
    try:
        header, encoded = value.split(
            ",",
            1,
        )

        mime = (
            header
            .replace(
                "data:",
                "",
                1,
            )
            .split(
                ";",
                1,
            )[0]
            .strip()
        )

        if not mime.startswith(
            "image/",
        ):
            raise ValueError()

        if not encoded:
            raise ValueError()

        return mime, encoded

    except Exception:
        raise ValueError(
            name + " could not be decoded."
        )


def get_gemini_key():
    key = os.environ.get(
        "GEMINI_API_KEY",
        "",
    ).strip()

    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY is missing from Vercel."
        )

    return key


def call_gemini(
    instrument,
    trade_focus,
    higher_image,
    lower_image,
):
    key = get_gemini_key()

    h4_mime, h4_base64 = (
        split_image_data_url(
            higher_image,
            "4H chart",
        )
    )

    m15_mime, m15_base64 = (
        split_image_data_url(
            lower_image,
            "15M chart",
        )
    )

    prompt = (
        SYSTEM_PROMPT
        + "\n\nInstrument: "
        + instrument
        + "\nTrade focus: "
        + trade_focus
        + "\n\n"
        + "Image 1 = 4H chart.\n"
        + "Image 2 = 15M chart.\n"
        + "Analyze both charts together.\n"
        + "Use only visible evidence.\n"
        + "Do not invent prices.\n"
    )

    url = (
        GEMINI_URL
        + GEMINI_MODEL
        + ":generateContent"
    )

    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text":
                            prompt,
                    },
                    {
                        "inlineData": {
                            "mimeType":
                                h4_mime,
                            "data":
                                h4_base64,
                        },
                    },
                    {
                        "inlineData": {
                            "mimeType":
                                m15_mime,
                            "data":
                                m15_base64,
                        },
                    },
                ],
            },
        ],
        "generationConfig": {
            "temperature":
                0.2,

            "responseMimeType":
                "application/json",

            "responseSchema":
                OUTPUT_SCHEMA,

            "maxOutputTokens":
                5000,
        },
    }

    request = urllib.request.Request(
        url,
        data=json.dumps(
            payload,
            separators=(",", ":"),
        ).encode("utf-8"),
        headers={
            "x-goog-api-key":
                key,

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
            timeout=85,
        ) as response:

            raw = response.read().decode(
                "utf-8"
            )

            data = json.loads(
                raw
            )

            if not isinstance(
                data,
                dict,
            ):
                raise RuntimeError(
                    "Gemini returned an invalid response."
                )

            return data

    except urllib.error.HTTPError as exc:

        raw = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        print(
            "GEMINI HTTP ERROR:",
            exc.code,
            raw,
        )

        try:
            error_data = json.loads(
                raw
            )
        except Exception:
            error_data = {}

        message = ""

        error_obj = (
            error_data.get(
                "error"
            )
        )

        if isinstance(
            error_obj,
            dict,
        ):
            message = str(
                error_obj.get(
                    "message",
                    "",
                )
            ).strip()

        if not message:
            message = raw[:1200]

        raise RuntimeError(
            "Gemini error: "
            + message
        ) from exc

    except urllib.error.URLError as exc:

        raise RuntimeError(
            "Unable to connect to Gemini: "
            + str(exc.reason)
        ) from exc

    except TimeoutError as exc:

        raise RuntimeError(
            "Gemini analysis timed out."
        ) from exc


def extract_gemini_text(
    data,
):
    candidates = data.get(
        "candidates"
    )

    if not isinstance(
        candidates,
        list,
    ) or not candidates:
        raise RuntimeError(
            "Gemini returned no analysis candidate."
        )

    candidate = candidates[0]

    if not isinstance(
        candidate,
        dict,
    ):
        raise RuntimeError(
            "Gemini returned an invalid analysis candidate."
        )

    content = candidate.get(
        "content"
    )

    if not isinstance(
        content,
        dict,
    ):
        raise RuntimeError(
            "Gemini returned no analysis content."
        )

    parts = content.get(
        "parts"
    )

    if not isinstance(
        parts,
        list,
    ):
        raise RuntimeError(
            "Gemini returned no analysis parts."
        )

    result_text = []

    for part in parts:

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

            result_text.append(
                text.strip()
            )

    if not result_text:

        raise RuntimeError(
            "Gemini returned no analysis text."
        )

    return "\n".join(
        result_text
    ).strip()


def parse_gemini_result(
    data,
    instrument,
    trade_focus,
):
    text = extract_gemini_text(
        data
    )

    cleaned = text.strip()

    if cleaned.startswith(
        "```"
    ):
        lines = cleaned.splitlines()

        if lines:
            lines = lines[1:]

        if (
            lines
            and lines[-1].strip()
            == "```"
        ):
            lines = lines[:-1]

        cleaned = "\n".join(
            lines
        ).strip()

    try:

        result = json.loads(
            cleaned
        )

    except json.JSONDecodeError as exc:

        print(
            "GEMINI INVALID JSON:",
            cleaned[:3000],
        )

        raise RuntimeError(
            "Gemini returned an invalid analysis format."
        ) from exc

    if not isinstance(
        result,
        dict,
    ):
        raise RuntimeError(
            "Gemini returned an invalid analysis object."
        )

    result["instrument"] = (
        instrument
    )

    result["trade_focus"] = (
        trade_focus
    )

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
        signal = "NO TRADE"

    result["signal"] = signal

    try:
        confidence = int(
            float(
                result.get(
                    "confidence",
                    0,
                )
            )
        )
    except (
        TypeError,
        ValueError,
    ):
        confidence = 0

    result["confidence"] = max(
        0,
        min(
            100,
            confidence,
        ),
    )

    if signal == "NO TRADE":

        result["entry"] = ""
        result["stop_loss"] = ""
        result["take_profit_1"] = ""
        result["take_profit_2"] = ""
        result["risk_reward"] = ""

    return result


def encode_result(
    result,
):
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
    job_uuid,
    user_id,
    instrument,
    trade_focus,
    result,
):
    encoded_result = (
        encode_result(
            result
        )
    )

    payload = {
        "id":
            job_uuid,

        "user_id":
            str(user_id),

        "instrument":
            str(instrument),

        "trade_focus":
            str(trade_focus),

        "created_at":
            __import__(
                "datetime"
            ).datetime.now(
                __import__(
                    "datetime"
                ).timezone.utc
            ).isoformat(),

        "status":
            "completed",

        "openai_response_id":
            encoded_result,
    }

    url = (
        get_supabase_url()
        + "/rest/v1/analysis_jobs"
    )

    service_key = (
        get_supabase_service_key()
    )

    request = urllib.request.Request(
        url,

        data=json.dumps(
            payload,
            separators=(",", ":"),
        ).encode("utf-8"),

        headers={
            "apikey":
                service_key,

            "Authorization":
                "Bearer "
                + service_key,

            "Content-Type":
                "application/json",

            "Accept":
                "application/json",

            "Prefer":
                "return=minimal",
        },

        method="POST",
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=20,
        ) as response:

            response.read()

        return True

    except urllib.error.HTTPError as exc:

        raw = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        print(
            "HISTORY SAVE ERROR:",
            raw,
        )

        return False

    except Exception as exc:

        print(
            "HISTORY SAVE ERROR:",
            repr(exc),
        )

        return False


class handler(
    BaseHTTPRequestHandler
):

    def do_OPTIONS(
        self
    ):
        json_response(
            self,
            204,
            {}
        )

    def do_POST(
        self
    ):

        reserved = False
        user = None

        try:

            # Authentication
            access_token = (
                extract_bearer_token(
                    self
                )
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

            # Request
            data = read_json(
                self
            )

            instrument = (
                validate_instrument(
                    data.get(
                        "instrument"
                    )
                )
            )

            trade_focus = (
                validate_focus(
                    data.get(
                        "trade_focus",
                        "DAY TRADE",
                    )
                )
            )

            higher_image = (
                validate_image(
                    data.get(
                        "higher_timeframe_image"
                    ),
                    "4H chart",
                )
            )

            lower_image = (
                validate_image(
                    data.get(
                        "lower_timeframe_image"
                    ),
                    "15M chart",
                )
            )

            # Daily limit
            if not owner:

                slot = (
                    reserve_analysis_slot(
                        user_id
                    )
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

            # Gemini analysis
            try:

                gemini_response = (
                    call_gemini(
                        instrument,
                        trade_focus,
                        higher_image,
                        lower_image,
                    )
                )

                result = (
                    parse_gemini_result(
                        gemini_response,
                        instrument,
                        trade_focus,
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

            # Create database UUID
            database_job_id = str(
                uuid.uuid4()
            )

            # Save completed analysis
            history_saved = (
                save_completed_history(
                    database_job_id,
                    user_id,
                    instrument,
                    trade_focus,
                    result,
                )
            )

            # IMPORTANT:
            # Put database UUID inside the existing secure
            # job-token format. analysis_status.py will read it
            # as response_id and use it as the database job ID.
            job_id = (
                create_secure_job_token(
                    database_job_id,
                    instrument,
                    trade_focus,
                    user_id,
                )
            )

            # Analysis is already completed when submit returns.
            json_response(
                self,
                202,
                {
                    "status":
                        "completed",

                    "job_id":
                        job_id,

                    "poll_after_seconds":
                        1,

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
                        str(exc),
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
                        str(exc),
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
                "GEMINI SUBMIT ERROR:",
                repr(exc),
            )

            json_response(
                self,
                500,
                {
                    "error":
                        "Gemini analysis failed.",

                    "details":
                        str(exc),
                },
            )
