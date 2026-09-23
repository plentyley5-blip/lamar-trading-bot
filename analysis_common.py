import base64
import json
import os
import urllib.error
import urllib.request


# ============================================================
# GEMINI STANDARD GENERATECONTENT API
# ============================================================

GEMINI_MODEL = "gemini-3.8-flash"

GEMINI_GENERATE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/"
    "models/gemini-3.8-flash:generateContent"
)

MAX_IMAGE_DATA_URL_CHARS = 8 * 1024 * 1024


# ============================================================
# ANALYSIS PROMPT
# ============================================================

SYSTEM_PROMPT = """
You are the professional chart-analysis engine for LAMAR TRADING BOT.

You receive two trading-chart screenshots:

1. 4H chart
2. 15M chart

Your job is to determine whether the evidence supports:

BUY
SELL
NO TRADE

IMPORTANT:

Do not automatically choose NO TRADE.

Do not require every trading method to agree.

Do not require perfect agreement between the 4H and 15M charts.

Use the selected trade focus appropriately.

SCALP:
- 15M is the primary execution timeframe.
- 4H provides broader context.
- A valid scalp BUY or SELL can exist when the 15M setup is sufficiently
  clear and the 4H does not clearly invalidate it.

DAY TRADE:
- 4H provides broader directional context.
- 15M provides confirmation and execution.

SWING:
- 4H is the primary structural timeframe.
- 15M may assist with timing.

Analyze whichever visible evidence is useful:

- support and resistance
- price action
- market structure
- swing highs
- swing lows
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
- higher timeframe bias
- lower timeframe confirmation

Not every method is required.

Choose BUY when the overall visible evidence is sufficiently bullish.

Choose SELL when the overall visible evidence is sufficiently bearish.

Choose NO TRADE only when the evidence is genuinely unclear, too weak,
materially conflicting, or the chart quality prevents a responsible setup.

Do not force a trade merely to provide a signal.

ENTRY:

For BUY or SELL, identify a logical entry area.

STOP LOSS:

Place it beyond the logical invalidation area.

TP1:

Use the nearer logical target.

TP2:

Use a further logical target only when supported by visible structure,
liquidity, or price objectives.

RISK/REWARD:

Make it consistent with the entry, stop loss, and targets.

PRICE PRECISION:

Use exact prices only when they are reasonably visible or inferable.

Do not invent false precision.

If the exact numerical value cannot responsibly be determined, leave
that individual field empty.

NO TRADE:

For NO TRADE, entry, stop loss, TP1, TP2, and risk/reward must be empty.

NEWS:

Do not invent current news.

If current news cannot be verified, state that limitation.

CONFIDENCE:

Confidence measures confidence in the quality of the analysis.

It is NOT a guaranteed probability of winning.

It is NOT a guarantee of profit.

Return only JSON matching the supplied schema.

Do not return Markdown.
Do not return code fences.
Do not return commentary outside JSON.
""".strip()


# ============================================================
# JSON SCHEMA
# ============================================================

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
# VALIDATION
# ============================================================

ALLOWED_INSTRUMENTS = {
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "XAUUSD",
    "GBPJPY",
    "AUDUSD",
    "USDCAD",
    "USDCHF",
    "EURGBP",
    "BTCUSD",
}

ALLOWED_FOCUSES = {
    "SCALP",
    "DAY TRADE",
    "SWING",
}


def json_response(
    handler,
    status,
    payload,
    extra_headers=None,
):
    body = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    handler.send_response(status)

    handler.send_header(
        "Content-Type",
        "application/json; charset=utf-8",
    )

    handler.send_header(
        "Access-Control-Allow-Origin",
        "*",
    )

    handler.send_header(
        "Access-Control-Allow-Headers",
        "Content-Type, Authorization",
    )

    handler.send_header(
        "Access-Control-Allow-Methods",
        "GET, POST, OPTIONS",
    )

    handler.send_header(
        "Cache-Control",
        "no-store, no-cache, must-revalidate",
    )

    handler.send_header(
        "Content-Length",
        str(len(body)),
    )

    if extra_headers:
        for key, value in extra_headers.items():
            handler.send_header(
                key,
                str(value),
            )

    handler.end_headers()

    try:
        handler.wfile.write(body)
    except Exception:
        pass


def api_key():
    key = os.getenv(
        "GEMINI_API_KEY",
        "",
    ).strip()

    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY is missing from Vercel."
        )

    return key


def validate_instrument(value):
    instrument = str(
        value or ""
    ).strip().upper()

    if instrument not in ALLOWED_INSTRUMENTS:
        raise ValueError(
            "Invalid instrument. Select a supported instrument."
        )

    return instrument


def validate_focus(value):
    focus = str(
        value or "DAY TRADE"
    ).strip().upper()

    if focus not in ALLOWED_FOCUSES:
        raise ValueError(
            "Invalid trade focus."
        )

    return focus


def validate_image_data_url(
    value,
    label,
):
    if not isinstance(
        value,
        str,
    ):
        raise ValueError(
            f"A valid {label} is required."
        )

    image = value.strip()

    if not image.startswith(
        "data:image/"
    ):
        raise ValueError(
            f"A valid {label} is required."
        )

    if len(image) > MAX_IMAGE_DATA_URL_CHARS:
        raise ValueError(
            f"{label} is too large."
        )

    header, separator, encoded = (
        image.partition(",")
    )

    if not separator or not encoded:
        raise ValueError(
            f"A valid {label} is required."
        )

    allowed = (
        "data:image/jpeg;base64",
        "data:image/jpg;base64",
        "data:image/png;base64",
        "data:image/webp;base64",
    )

    if not header.lower().startswith(
        allowed
    ):
        raise ValueError(
            f"Unsupported {label} format."
        )

    encoded = encoded.strip()

    if len(encoded) < 100:
        raise ValueError(
            f"The {label} appears to be empty."
        )

    try:
        base64.b64decode(
            encoded,
            validate=True,
        )
    except Exception as exc:
        raise ValueError(
            f"The {label} contains invalid image data."
        ) from exc

    return image


# ============================================================
# GEMINI REQUEST
# ============================================================

def create_background_response(
    instrument,
    trade_focus,
    higher_image,
    lower_image,
):
    """
    Compatibility name retained because analysis_submit.py
    already calls this function.

    This no longer uses the Interactions API or any Agent.
    It uses the standard Gemini generateContent endpoint.
    """

    key = api_key()

    instrument = validate_instrument(
        instrument
    )

    trade_focus = validate_focus(
        trade_focus
    )

    higher_image = validate_image_data_url(
        higher_image,
        "4H chart",
    )

    lower_image = validate_image_data_url(
        lower_image,
        "15M chart",
    )

    def image_part(
        data_url
    ):
        header, separator, encoded = (
            data_url.partition(",")
        )

        if not separator:
            raise ValueError(
                "Invalid image data."
            )

        header_lower = header.lower()

        if "image/png" in header_lower:
            mime_type = "image/png"

        elif "image/webp" in header_lower:
            mime_type = "image/webp"

        else:
            mime_type = "image/jpeg"

        return {
            "inline_data": {
                "mime_type":
                    mime_type,

                "data":
                    encoded,
            }
        }

    user_prompt = (
        "Analyze these trading charts.\n\n"
        f"Instrument: {instrument}\n"
        f"Trade focus: {trade_focus}\n\n"
        "The first image is the 4H chart.\n"
        "The second image is the 15M chart.\n\n"
        "Use the strongest visible evidence.\n"
        "For SCALP, give the 15M setup primary execution importance "
        "while using the 4H chart for context.\n"
        "Do not automatically return NO TRADE merely because the "
        "4H and 15M are not identical.\n\n"
        "Return BUY, SELL, or NO TRADE with the complete JSON schema."
    )

    payload = {
        "system_instruction": {
            "parts": [
                {
                    "text":
                        SYSTEM_PROMPT,
                }
            ]
        },

        "contents": [
            {
                "role": "user",

                "parts": [

                    {
                        "text":
                            user_prompt,
                    },

                    image_part(
                        higher_image
                    ),

                    image_part(
                        lower_image
                    ),
                ],
            }
        ],

        "generationConfig": {
            "responseMimeType":
                "application/json",

            "responseJsonSchema":
                OUTPUT_SCHEMA,

            "thinkingConfig": {
                "thinkingLevel":
                    "low",
            },

            "maxOutputTokens":
                3000,
        },
    }

    body = json.dumps(
        payload,
        separators=(",", ":"),
    ).encode("utf-8")

    request = urllib.request.Request(

        GEMINI_GENERATE_URL,

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
            timeout=120,
        ) as response:

            raw = (
                response
                .read()
                .decode("utf-8")
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

        message = ""

        try:

            decoded = raw.decode(
                "utf-8",
                errors="replace",
            )

            error_data = json.loads(
                decoded
            )

            error = error_data.get(
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

        except Exception:
            pass

        if exc.code == 400:
            raise RuntimeError(
                "Gemini rejected the request: "
                + (
                    message
                    or "invalid request"
                )
            ) from exc

        if exc.code in {401, 403}:
            raise RuntimeError(
                "The Gemini API key was rejected: "
                + (
                    message
                    or "access denied"
                )
            ) from exc

        if exc.code == 404:
            raise RuntimeError(
                "Gemini model not found: "
                + (
                    message
                    or GEMINI_MODEL
                )
            ) from exc

        if exc.code == 429:
            raise RuntimeError(
                "Gemini quota or rate limit reached: "
                + (
                    message
                    or "quota exceeded"
                )
            ) from exc

        if 500 <= exc.code <= 599:
            raise RuntimeError(
                "Gemini is temporarily unavailable."
            ) from exc

        raise RuntimeError(
            "Gemini request failed: "
            + (
                message
                or str(exc)
            )
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
# RESPONSE PARSING
# ============================================================

def extract_output_text(
    response_data,
):
    if not isinstance(
        response_data,
        dict,
    ):
        raise RuntimeError(
            "Gemini returned an invalid response."
        )

    candidates = response_data.get(
        "candidates",
        [],
    )

    if not isinstance(
        candidates,
        list,
    ) or not candidates:
        raise RuntimeError(
            "Gemini returned no analysis candidate."
        )

    first = candidates[0]

    if not isinstance(
        first,
        dict,
    ):
        raise RuntimeError(
            "Gemini returned an invalid analysis candidate."
        )

    content = first.get(
        "content",
        {}
    )

    if not isinstance(
        content,
        dict,
    ):
        raise RuntimeError(
            "Gemini returned no analysis content."
        )

    parts = content.get(
        "parts",
        []
    )

    if not isinstance(
        parts,
        list,
    ):
        raise RuntimeError(
            "Gemini returned no analysis text."
        )

    texts = []

    for part in parts:

        if not isinstance(
            part,
            dict,
        ):
            continue

        text = part.get(
            "text",
            ""
        )

        if isinstance(
            text,
            str,
        ) and text.strip():

            texts.append(
                text.strip()
            )

    if not texts:
        raise RuntimeError(
            "Gemini returned no analysis text."
        )

    return "\n".join(
        texts
    ).strip()


def clean_json_text(
    text,
):
    cleaned = str(
        text or ""
    ).strip()

    if not cleaned:
        return ""

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

    return cleaned


def _safe_int(
    value,
):
    try:
        return int(value)
    except (
        TypeError,
        ValueError,
    ):
        return 0


def _clean_string(
    value,
):
    if value is None:
        return ""

    return str(
        value
    ).strip()


def _clean_list(
    value,
):
    if not isinstance(
        value,
        list,
    ):
        return []

    result = []

    for item in value:

        text = _clean_string(
            item
        )

        if text:
            result.append(
                text
            )

    return result


def parse_completed_response(
    response_data,
    instrument,
    trade_focus="DAY TRADE",
):
    text = clean_json_text(
        extract_output_text(
            response_data
        )
    )

    try:

        result = json.loads(
            text
        )

    except json.JSONDecodeError as exc:

        raise RuntimeError(
            "Gemini returned invalid analysis JSON."
        ) from exc

    if not isinstance(
        result,
        dict,
    ):
        raise RuntimeError(
            "Gemini returned an invalid analysis object."
        )

    signal = (
        _clean_string(
            result.get(
                "signal"
            )
        )
        .upper()
    )

    if signal not in {
        "BUY",
        "SELL",
        "NO TRADE",
    }:
        signal = "NO TRADE"

    result["signal"] = signal

    result["confidence"] = max(
        0,
        min(
            100,
            _safe_int(
                result.get(
                    "confidence",
                    0,
                )
            ),
        ),
    )

    result["instrument"] = (
        _clean_string(
            instrument
        ).upper()
    )

    text_fields = [
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
        "news_fundamental_risk",
    ]

    for field in text_fields:

        result[field] = _clean_string(
            result.get(
                field
            )
        )

    list_fields = [
        "contributing_methods",
        "weak_methods",
        "conflicting_methods",
        "warnings",
    ]

    for field in list_fields:

        result[field] = _clean_list(
            result.get(
                field
            )
        )

    if signal == "NO TRADE":

        result["entry"] = ""
        result["stop_loss"] = ""
        result["take_profit_1"] = ""
        result["take_profit_2"] = ""
        result["risk_reward"] = ""

    return result
