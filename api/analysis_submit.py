import base64
import json
import os
import urllib.error
import urllib.request

from http.server import BaseHTTPRequestHandler

from analysis_common import (
    json_response,
    validate_focus,
    validate_image_data_url,
    validate_instrument,
)

from api.user_security import (
    create_secure_job_token,
    extract_bearer_token,
    is_owner_user,
    release_analysis_slot,
    reserve_analysis_slot,
    verify_access_token,
)


# ============================================================
# GEMINI CONFIGURATION
# ============================================================

GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/interactions"
)

# HARD-CODED.
# This endpoint cannot read GEMINI_MODEL from Vercel.
GEMINI_MODEL = "gemini-3.8-flash"


# ============================================================
# ANALYSIS INSTRUCTIONS
# ============================================================

SYSTEM_PROMPT = """
You are LAMAR TRADING BOT's professional chart-analysis engine.

You receive two trading screenshots:

1. 4H chart
2. 15M chart

Analyze both charts and return exactly one of:

BUY
SELL
NO TRADE

Do not automatically choose NO TRADE.

Do not require every method to agree.

Do not require the 4H and 15M charts to be identical.

For SCALP:

- 15M is the primary execution timeframe.
- 4H is broader context.
- A valid BUY or SELL is allowed when the 15M provides a sufficiently
  clear setup and the 4H does not clearly invalidate it.

For DAY TRADE:

- 4H provides directional context.
- 15M provides confirmation and execution.

For SWING:

- 4H is the primary structural timeframe.
- 15M may be used for timing.

Analyze whichever visible concepts are useful:

- support and resistance
- price action
- market structure
- swing highs and lows
- BOS
- CHoCH
- liquidity
- equal highs
- equal lows
- liquidity sweeps
- displacement
- inducement
- mitigation
- invalidation
- Fibonacci
- premium and discount
- smart money concepts
- order blocks
- fair value gaps

Not every method is required.

A BUY is allowed when the overall visible evidence is sufficiently bullish.

A SELL is allowed when the overall visible evidence is sufficiently bearish.

Use NO TRADE only when the evidence is genuinely unclear, materially
conflicting, too weak, or the image quality prevents reliable analysis.

Do not invent current news.

Do not invent prices.

When price digits are readable, use them.

When an exact price cannot responsibly be determined, leave the field empty.

For NO TRADE:

entry = ""
stop_loss = ""
take_profit_1 = ""
take_profit_2 = ""
risk_reward = ""

Confidence is confidence in the quality of the analysis, not a guarantee
of profit.

Return ONLY valid JSON matching the supplied schema.
""".strip()


# ============================================================
# OUTPUT SCHEMA
# ============================================================

OUTPUT_SCHEMA = {
    "type": "object",
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
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
        },

        "instrument": {
            "type": "string",
        },

        "trend": {
            "type": "string",
        },

        "trade_idea": {
            "type": "string",
        },

        "higher_timeframe_context": {
            "type": "string",
        },

        "lower_timeframe_confirmation": {
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
        "instrument",
        "trend",
        "trade_idea",
        "higher_timeframe_context",
        "lower_timeframe_confirmation",
        "entry",
        "stop_loss",
        "take_profit_1",
        "take_profit_2",
        "risk_reward",
        "duration",
        "data_analysis",
        "explanation",
        "contributing_methods",
        "weak_methods",
        "conflicting_methods",
        "news_fundamental_risk",
        "warnings",
    ],
}


# ============================================================
# REQUEST READER
# ============================================================

def _read_json(handler):
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

    if (
        length <= 0
        or length > 25 * 1024 * 1024
    ):
        raise ValueError(
            "Invalid chart request."
        )

    raw = handler.rfile.read(
        length
    )

    try:
        data = json.loads(
            raw.decode(
                "utf-8"
            )
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


# ============================================================
# GEMINI ERROR
# ============================================================

def _gemini_error_message(
    raw,
):
    if not raw:
        return ""

    try:

        if isinstance(
            raw,
            bytes,
        ):
            raw = raw.decode(
                "utf-8",
                errors="replace",
            )

        data = json.loads(
            raw
        )

    except Exception:
        return ""

    if not isinstance(
        data,
        dict,
    ):
        return ""

    error = data.get(
        "error"
    )

    if isinstance(
        error,
        dict,
    ):

        message = str(
            error.get(
                "message",
                "",
            )
        ).strip()

        status = str(
            error.get(
                "status",
                "",
            )
        ).strip()

        if message and status:
            return (
                f"{message} ({status})"
            )

        return (
            message or status
        )

    return ""


# ============================================================
# GEMINI REQUEST
# ============================================================

def _gemini_request(
    key,
    instrument,
    trade_focus,
    higher_image,
    lower_image,
):

    def split_image(
        data_url
    ):
        header, separator, encoded = (
            data_url.partition(",")
        )

        if not separator:
            raise ValueError(
                "Invalid chart image."
            )

        if "image/png" in header.lower():
            mime = "image/png"
        elif "image/webp" in header.lower():
            mime = "image/webp"
        else:
            mime = "image/jpeg"

        # Verify base64.
        base64.b64decode(
            encoded,
            validate=True,
        )

        return {
            "type": "image",
            "mime_type": mime,
            "data": encoded,
        }

    prompt = (
        "Analyze this trading setup.\n\n"
        f"Instrument: {instrument}\n"
        f"Trade focus: {trade_focus}\n\n"
        "IMAGE 1 = 4H chart.\n"
        "IMAGE 2 = 15M chart.\n\n"
        "For SCALP, use the 15M as the primary execution chart "
        "while using the 4H for context.\n"
        "Do not force NO TRADE simply because the 4H and 15M "
        "do not show identical structures.\n"
        "Return the strongest justified decision: BUY, SELL, or NO TRADE."
    )

    payload = {
        "model": GEMINI_MODEL,

        "input": [
            {
                "type": "text",
                "text": prompt,
            },

            split_image(
                higher_image
            ),

            split_image(
                lower_image
            ),
        ],

        "system_instruction":
            SYSTEM_PROMPT,

        "response_format": {
            "type": "text",
            "mime_type": "application/json",
            "schema": OUTPUT_SCHEMA,
        },

        "generation_config": {
            "thinking_level": "low",
            "max_output_tokens": 3000,
        },

        "background": True,

        "store": True,
    }

    body = json.dumps(
        payload,
        separators=(",", ":"),
    ).encode(
        "utf-8"
    )

    request = urllib.request.Request(

        GEMINI_API_URL,

        data=body,

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
            timeout=90,
        ) as response:

            raw = (
                response
                .read()
                .decode(
                    "utf-8"
                )
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

        raw = b""

        try:
            raw = exc.read()
        except Exception:
            pass

        message = _gemini_error_message(
            raw
        )

        # Include the hard-coded model in the
        # error so there is no ambiguity about
        # which model the deployed endpoint used.
        model_note = (
            f" [Server model: {GEMINI_MODEL}]"
        )

        if exc.code == 400:

            raise RuntimeError(
                "Gemini rejected the request: "
                + (
                    message
                    or "invalid request"
                )
                + model_note
            ) from exc

        if exc.code in {
            401,
            403,
        }:

            raise RuntimeError(
                "The Gemini API key was rejected: "
                + (
                    message
                    or "access denied"
                )
                + model_note
            ) from exc

        if exc.code == 404:

            raise RuntimeError(
                "Gemini model or endpoint not found: "
                + (
                    message
                    or "not found"
                )
                + model_note
            ) from exc

        if exc.code == 429:

            raise RuntimeError(
                "Gemini quota or rate limit reached: "
                + (
                    message
                    or "quota exceeded"
                )
                + model_note
            ) from exc

        raise RuntimeError(
            "Gemini analysis failed: "
            + (
                message
                or "unknown error"
            )
            + model_note
        ) from exc

    except (
        urllib.error.URLError,
        TimeoutError,
    ) as exc:

        raise RuntimeError(
            "Gemini could not be reached."
        ) from exc

    except json.JSONDecodeError as exc:

        raise RuntimeError(
            "Gemini returned invalid JSON."
        ) from exc


# ============================================================
# HANDLER
# ============================================================

class handler(
    BaseHTTPRequestHandler
):

    def do_OPTIONS(
        self
    ):
        json_response(
            self,
            204,
            {},
        )

    def do_POST(
        self
    ):

        reserved = False
        user = None

        try:

            key = os.getenv(
                "GEMINI_API_KEY",
                "",
            ).strip()

            if not key:

                json_response(
                    self,
                    500,
                    {
                        "error":
                            "GEMINI_API_KEY is missing from Vercel."
                    },
                )

                return

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

            data = _read_json(
                self
            )

            instrument = (
                validate_instrument(
                    data.get(
                        "instrument"
                    )
                )
            )

            trade_focus = validate_focus(
                data.get(
                    "trade_focus",
                    "DAY TRADE",
                )
            )

            higher_image = (
                validate_image_data_url(
                    data.get(
                        "higher_timeframe_image"
                    ),
                    "4H chart",
                )
            )

            lower_image = (
                validate_image_data_url(
                    data.get(
                        "lower_timeframe_image"
                    ),
                    "15M chart",
                )
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

                response = _gemini_request(

                    key=key,

                    instrument=instrument,

                    trade_focus=trade_focus,

                    higher_image=higher_image,

                    lower_image=lower_image,
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

            interaction_id = str(
                response.get(
                    "id",
                    "",
                )
            ).strip()

            status = str(
                response.get(
                    "status",
                    "queued",
                )
            ).strip()

            if not interaction_id:

                if reserved:

                    try:

                        release_analysis_slot(
                            user_id
                        )

                    except Exception:
                        pass

                raise RuntimeError(
                    "Gemini returned no interaction id."
                )

            job_id = (
                create_secure_job_token(
                    interaction_id,
                    instrument,
                    trade_focus,
                    user_id,
                )
            )

            json_response(
                self,
                202,
                {
                    "status":
                        status or "queued",

                    "job_id":
                        job_id,

                    "poll_after_seconds":
                        2,

                    "is_owner":
                        owner,

                    "daily_limit":
                        None if owner else 4,

                    "remaining":
                        remaining,

                    "model":
                        GEMINI_MODEL,
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

            json_response(
                self,
                500,
                {
                    "error":
                        "The analysis could not be started: "
                        + str(exc)
                },
            )
