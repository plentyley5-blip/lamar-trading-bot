import base64
import json
import os
import urllib.error
import urllib.request


# ============================================================
# GEMINI CONFIGURATION
# ============================================================

GEMINI_MODEL = "gemini-3.5-flash-lite"

GEMINI_GENERATE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/"
    "models/gemini-3.5-flash-lite:generateContent"
)

MAX_IMAGE_DATA_URL_CHARS = 8 * 1024 * 1024


# ============================================================
# SYSTEM PROMPT
# ============================================================

SYSTEM_PROMPT = """
You are the professional chart-analysis engine for LAMAR TRADING BOT.

You receive exactly two trading screenshots:

IMAGE 1 = 4H chart
IMAGE 2 = 15M chart

The selected instrument and trade focus are supplied separately.

Your task is to analyze both charts and produce exactly one decision:

BUY
SELL
NO TRADE

==================================================
CORE DECISION RULE
==================================================

Do NOT automatically choose NO TRADE.

Do NOT require every trading method to agree.

Do NOT require the 4H and 15M charts to show identical structures.

Use the strongest overall visible evidence.

A BUY is allowed when the overall evidence is sufficiently bullish.

A SELL is allowed when the overall evidence is sufficiently bearish.

Use NO TRADE only when the evidence is genuinely too unclear,
too weak, materially conflicting, or the chart quality prevents
a responsible directional setup.

==================================================
TIMEFRAME LOGIC
==================================================

SCALP:

- 15M is the primary execution timeframe.
- 4H provides broader context.
- A valid scalp BUY or SELL can exist from a sufficiently clear
  15M setup even if the 4H chart is neutral.
- The 4H chart should not clearly invalidate the setup.

DAY TRADE:

- 4H provides broader market context.
- 15M provides confirmation and execution.

SWING:

- 4H is the primary structural timeframe.
- 15M may assist with entry timing.

==================================================
ANALYSIS METHODS
==================================================

Use whichever are supported by visible chart evidence:

- support and resistance
- pure price action
- market structure
- swing highs
- swing lows
- break of structure
- change of character
- liquidity
- equal highs
- equal lows
- liquidity sweeps
- stop hunts
- displacement
- inducement
- mitigation
- invalidation
- Fibonacci retracement
- Fibonacci extension
- premium and discount
- smart money concepts
- order blocks
- fair value gaps
- higher-timeframe bias
- lower-timeframe confirmation

Do NOT require every method.

Identify:

- contributing evidence
- weak evidence
- conflicting evidence

==================================================
BUY
==================================================

A BUY may be selected when a meaningful combination of bullish evidence
is visible.

Examples include:

- bullish market structure
- bullish break of structure
- bullish change of character
- support reaction
- bullish liquidity sweep
- bullish displacement
- bullish order block
- bullish fair value gap
- discount positioning
- bullish price action
- lower-timeframe bullish confirmation

Not all are required.

==================================================
SELL
==================================================

A SELL may be selected when a meaningful combination of bearish evidence
is visible.

Examples include:

- bearish market structure
- bearish break of structure
- bearish change of character
- resistance reaction
- bearish liquidity sweep
- bearish displacement
- bearish order block
- bearish fair value gap
- premium positioning
- bearish price action
- lower-timeframe bearish confirmation

Not all are required.

==================================================
ENTRY
==================================================

For BUY or SELL:

Provide a logical entry level or entry area when the chart supports it.

Do not invent false precision.

If exact price digits are clearly visible, use them.

If exact digits are not clearly readable, use a reasonable level or zone
when that can be inferred responsibly.

==================================================
STOP LOSS
==================================================

Place the stop beyond the logical invalidation area.

==================================================
TAKE PROFITS
==================================================

TP1 should be the nearer logical objective.

TP2 should be a further logical objective when supported by visible
structure, liquidity, or another clear price objective.

==================================================
RISK / REWARD
==================================================

Keep the risk/reward description consistent with the proposed entry,
stop loss, and targets.

==================================================
NO TRADE
==================================================

Use NO TRADE when:

- directional evidence is genuinely unclear
- bullish and bearish evidence are materially balanced
- the setup is too weak
- the chart quality prevents reliable analysis
- important information is missing
- the proposed idea is materially invalidated

Do NOT use NO TRADE merely because one method is weak.

==================================================
NEWS
==================================================

Do not invent current news.

Only discuss news or fundamentals that are visible or reliably supplied.

If current news cannot be verified, say so.

==================================================
CONFIDENCE
==================================================

Confidence is confidence in the quality of the chart analysis.

It is NOT:

- guaranteed win rate
- guaranteed probability of profit
- guarantee of success

==================================================
OUTPUT
==================================================

Return ONLY valid JSON matching the supplied schema.

No Markdown.
No code fences.
No commentary outside JSON.
""".strip()


# ============================================================
# OUTPUT SCHEMA
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
# ALLOWED VALUES
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


# ============================================================
# JSON RESPONSE
# ============================================================

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


# ============================================================
# GEMINI API KEY
# ============================================================

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


# ============================================================
# VALIDATION
# ============================================================

def validate_instrument(
    value,
):
    instrument = str(
        value or ""
    ).strip().upper()

    if instrument not in ALLOWED_INSTRUMENTS:

        raise ValueError(
            "Invalid instrument. Select a supported instrument."
        )

    return instrument


def validate_focus(
    value,
):
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

    if (
        len(image)
        > MAX_IMAGE_DATA_URL_CHARS
    ):

        raise ValueError(
            f"{label} is too large."
        )

    header, separator, encoded = (
        image.partition(",")
    )

    if (
        not separator
        or not encoded
    ):

        raise ValueError(
            f"A valid {label} is required."
        )

    allowed_headers = (
        "data:image/jpeg;base64",
        "data:image/jpg;base64",
        "data:image/png;base64",
        "data:image/webp;base64",
    )

    if not header.lower().startswith(
        allowed_headers
    ):

        raise ValueError(
            f"Unsupported {label} format. "
            "Use JPG, PNG, or WEBP."
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
# GEMINI ERROR PARSING
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

    if not isinstance(
        error,
        dict,
    ):
        return ""

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

    return message or status


# ============================================================
# STANDARD GEMINI GENERATECONTENT
# ============================================================

def create_background_response(
    instrument,
    trade_focus,
    higher_image,
    lower_image,
):
    """
    Compatibility function name.

    Despite the old name, this no longer uses a background
    Interactions/Agent request.

    It uses the standard Gemini generateContent API directly.
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
        image_data_url,
    ):
        header, separator, encoded = (
            image_data_url.partition(",")
        )

        if not separator:
            raise ValueError(
                "Invalid chart image."
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
        "Analyze the supplied trading charts.\n\n"
        f"Instrument: {instrument}\n"
        f"Trade focus: {trade_focus}\n\n"
        "IMAGE 1 = 4H chart.\n"
        "IMAGE 2 = 15M chart.\n\n"
        "For SCALP, make the 15M chart the primary execution "
        "timeframe and use the 4H chart as context.\n"
        "For DAY TRADE, combine 4H context with 15M confirmation.\n"
        "For SWING, make the 4H chart the primary structural timeframe.\n\n"
        "Do not automatically return NO TRADE.\n"
        "Return the strongest justified decision: BUY, SELL, or NO TRADE."
    )

    payload = {

        "contents": [
            {
                "role": "user",

                "parts": [

                    {
                        "text":
                            SYSTEM_PROMPT
                            + "\n\n"
                            + user_prompt,
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
                2400,
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

        message = _gemini_error_message(
            raw
        )

        if exc.code == 400:

            raise RuntimeError(
                "Gemini rejected the request: "
                + (
                    message
                    or "invalid request"
                )
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
            ) from exc

        if exc.code == 404:

            raise RuntimeError(
                "The Gemini model was not found: "
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
                or "unknown error"
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

    candidate = candidates[0]

    if not isinstance(
        candidate,
        dict,
    ):

        raise RuntimeError(
            "Gemini returned an invalid analysis candidate."
        )

    finish_reason = str(
        candidate.get(
            "finishReason",
            "",
        )
    ).strip()

    content = candidate.get(
        "content",
        {},
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
        [],
    )

    if not isinstance(
        parts,
        list,
    ):

        raise RuntimeError(
            "Gemini returned no analysis parts."
        )

    pieces = []

    for part in parts:

        if not isinstance(
            part,
            dict,
        ):
            continue

        text = part.get(
            "text",
            "",
        )

        if (
            isinstance(
                text,
                str,
            )
            and text.strip()
        ):

            pieces.append(
                text.strip()
            )

    if not pieces:

        if finish_reason:
            raise RuntimeError(
                "Gemini returned no analysis text. "
                f"Finish reason: {finish_reason}"
            )

        raise RuntimeError(
            "Gemini returned no analysis text."
        )

    return "\n".join(
        pieces
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


def _clean_string_list(
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

    confidence = _safe_int(
        result.get(
            "confidence",
            0,
        )
    )

    result = {

        "signal":
            signal,

        "confidence":
            max(
                0,
                min(
                    100,
                    confidence,
                ),
            ),

        "instrument":
            _clean_string(
                instrument
            ).upper(),

        "trend":
            _clean_string(
                result.get(
                    "trend"
                )
            ),

        "trade_idea":
            _clean_string(
                result.get(
                    "trade_idea"
                )
            ),

        "higher_timeframe_context":
            _clean_string(
                result.get(
                    "higher_timeframe_context"
                )
            ),

        "lower_timeframe_confirmation":
            _clean_string(
                result.get(
                    "lower_timeframe_confirmation"
                )
            ),

        "entry":
            _clean_string(
                result.get(
                    "entry"
                )
            ),

        "stop_loss":
            _clean_string(
                result.get(
                    "stop_loss"
                )
            ),

        "take_profit_1":
            _clean_string(
                result.get(
                    "take_profit_1"
                )
            ),

        "take_profit_2":
            _clean_string(
                result.get(
                    "take_profit_2"
                )
            ),

        "risk_reward":
            _clean_string(
                result.get(
                    "risk_reward"
                )
            ),

        "duration":
            _clean_string(
                result.get(
                    "duration"
                )
            ),

        "data_analysis":
            _clean_string(
                result.get(
                    "data_analysis"
                )
            ),

        "explanation":
            _clean_string(
                result.get(
                    "explanation"
                )
            ),

        "contributing_methods":
            _clean_string_list(
                result.get(
                    "contributing_methods"
                )
            ),

        "weak_methods":
            _clean_string_list(
                result.get(
                    "weak_methods"
                )
            ),

        "conflicting_methods":
            _clean_string_list(
                result.get(
                    "conflicting_methods"
                )
            ),

        "news_fundamental_risk":
            _clean_string(
                result.get(
                    "news_fundamental_risk"
                )
            ),

        "warnings":
            _clean_string_list(
                result.get(
                    "warnings"
                )
            ),
    }

    # For NO TRADE, price fields remain empty.
    if signal == "NO TRADE":

        result["entry"] = ""
        result["stop_loss"] = ""
        result["take_profit_1"] = ""
        result["take_profit_2"] = ""
        result["risk_reward"] = ""

    return result
