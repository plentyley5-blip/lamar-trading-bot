import base64
import json
import os
import urllib.error
import urllib.request


GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/interactions"
)

MODEL = (
    os.getenv(
        "GEMINI_MODEL",
        "gemini-3.5-flash",
    ).strip()
    or "gemini-3.5-flash"
)

MAX_IMAGE_DATA_URL_CHARS = 8 * 1024 * 1024


SYSTEM_PROMPT = """
You are the professional chart-analysis engine for LAMAR TRADING BOT.

You receive two trading-chart screenshots:

1. 4H chart
2. 15M chart

Your job is to determine whether the chart evidence supports:

BUY
SELL
NO TRADE

==================================================
IMPORTANT DECISION RULE
==================================================

Do NOT automatically choose NO TRADE.

Do NOT require every trading method to agree.

Do NOT require perfect agreement between the 4H and 15M charts.

Choose the strongest direction supported by the overall visible evidence.

Choose BUY when the evidence is sufficiently bullish.

Choose SELL when the evidence is sufficiently bearish.

Choose NO TRADE only when the evidence is genuinely too unclear,
too conflicting, or too weak to justify a directional setup.

==================================================
TIMEFRAME LOGIC
==================================================

SCALP:

The 15M chart is the primary execution timeframe.

The 4H chart provides broader context.

A valid scalp BUY or SELL may be produced from a sufficiently clear
15M setup even when the 4H chart is neutral, provided the 4H context
does not clearly invalidate the setup.

DAY TRADE:

Use the 4H chart for broader directional context.

Use the 15M chart for confirmation and execution.

SWING:

Use the 4H chart as the primary structure.

Use the 15M chart for timing when useful.

==================================================
ANALYSIS
==================================================

Use whichever visible concepts are useful:

support and resistance
pure price action
market structure
swing highs
swing lows
BOS
CHoCH
liquidity
equal highs
equal lows
liquidity sweep
stop hunt
displacement
inducement
mitigation
invalidation
Fibonacci retracement
Fibonacci extension
premium and discount
smart money concepts
order block
fair value gap
higher timeframe bias
lower timeframe confirmation

Do not require all concepts.

Identify:

- contributing evidence
- weak evidence
- conflicting evidence

==================================================
TRADE SETUP
==================================================

For BUY:

Provide a logical entry area when reasonably visible.

Provide stop loss beyond logical invalidation.

Provide TP1.

Provide TP2 when supported by visible structure or liquidity.

Provide a consistent risk/reward value.

For SELL:

Provide a logical entry area when reasonably visible.

Provide stop loss beyond logical invalidation.

Provide TP1.

Provide TP2 when supported by visible structure or liquidity.

Provide a consistent risk/reward value.

If the exact price digits are not clearly readable, do not invent
false precision.

==================================================
NO TRADE
==================================================

Use NO TRADE only when:

- directional evidence is genuinely unclear
- bullish and bearish evidence are materially balanced
- the setup is too weak
- the chart is too poor to analyze
- important chart information is missing
- the proposed trade is materially invalidated

Do not use NO TRADE simply because one method is weak.

==================================================
NEWS
==================================================

Do not invent current news.

Use visible or reliably supplied news information only.

If current news cannot be verified, say so in the news/fundamental
risk field.

==================================================
CONFIDENCE
==================================================

Confidence represents confidence in the quality of the chart analysis.

It is NOT a guarantee of profit.

It is NOT a guaranteed probability of winning.

Higher confidence requires stronger visible evidence.

==================================================
OUTPUT
==================================================

Return ONLY valid JSON.

No Markdown.

No code fences.

No explanation outside the JSON object.
""".strip()


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
    return os.getenv(
        "GEMINI_API_KEY",
        "",
    ).strip()


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

    supported = (
        "data:image/jpeg;base64",
        "data:image/jpg;base64",
        "data:image/png;base64",
        "data:image/webp;base64",
    )

    if not header.lower().startswith(
        supported
    ):
        raise ValueError(
            f"Unsupported {label} format. "
            "Use JPG, PNG, or WEBP."
        )

    if len(encoded.strip()) < 100:
        raise ValueError(
            f"The {label} appears to be empty or invalid."
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


def _clean_string(value):
    if value is None:
        return ""

    return str(value).strip()


def _clean_string_list(value):
    if not isinstance(
        value,
        list,
    ):
        return []

    result = []

    for item in value:

        cleaned = _clean_string(
            item
        )

        if cleaned:
            result.append(
                cleaned
            )

    return result


def _image_data(
    data_url,
):
    header, _, encoded = (
        data_url.partition(",")
    )

    if header.lower().startswith(
        "data:image/png"
    ):
        mime_type = "image/png"
    elif header.lower().startswith(
        "data:image/webp"
    ):
        mime_type = "image/webp"
    else:
        mime_type = "image/jpeg"

    return {
        "type": "image",
        "mime_type": mime_type,
        "data": encoded,
    }


def _build_gemini_payload(
    instrument,
    trade_focus,
    higher_image,
    lower_image,
):
    user_text = (
        "Perform the chart analysis now.\n\n"
        f"Instrument: {instrument}\n"
        f"Trade focus: {trade_focus}\n\n"
        "Image 1 = 4H chart.\n"
        "Image 2 = 15M chart.\n\n"
        "SCALP uses the 15M chart as the primary execution "
        "timeframe while the 4H chart provides context.\n"
        "Do not force NO TRADE simply because the 4H and 15M "
        "are not identical.\n"
        "Return the strongest justified decision: "
        "BUY, SELL, or NO TRADE."
    )

    return {
        "model": MODEL,

        "input": [
            {
                "type": "text",
                "text": user_text,
            },

            _image_data(
                higher_image
            ),

            _image_data(
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

        "background": True,

        "store": True,

        "generation_config": {
            "thinking_level": "low",
            "max_output_tokens": 3000,
        },
    }


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

        message = _clean_string(
            error.get(
                "message"
            )
        )

        status = _clean_string(
            error.get(
                "status"
            )
        )

        if message and status:
            return (
                f"{message} ({status})"
            )

        return (
            message or status
        )

    return ""


def _request_gemini(
    payload,
    key,
):
    body = json.dumps(
        payload,
        separators=(",", ":"),
    ).encode("utf-8")

    request = urllib.request.Request(

        GEMINI_URL,

        data=body,

        headers={
            "x-goog-api-key":
                key,

            "Content-Type":
                "application/json",

            "Accept":
                "application/json",

            "Api-Revision":
                "2026-05-20",
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
                "The Gemini model or endpoint was not found: "
                + (
                    message
                    or "not found"
                )
            ) from exc

        if exc.code == 429:

            raise RuntimeError(
                "Gemini rate limit or quota reached: "
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
            "Gemini analysis could not be started: "
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


def create_background_response(
    instrument,
    trade_focus,
    higher_image,
    lower_image,
):
    key = api_key()

    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY is missing from Vercel."
        )

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

    payload = _build_gemini_payload(
        instrument=instrument,
        trade_focus=trade_focus,
        higher_image=higher_image,
        lower_image=lower_image,
    )

    return _request_gemini(
        payload,
        key,
    )


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

    direct = response_data.get(
        "output_text"
    )

    if (
        isinstance(
            direct,
            str,
        )
        and direct.strip()
    ):
        return direct.strip()

    steps = response_data.get(
        "steps",
        [],
    )

    if not isinstance(
        steps,
        list,
    ):
        steps = []

    for step in steps:

        if not isinstance(
            step,
            dict,
        ):
            continue

        if step.get(
            "type"
        ) != "model_output":
            continue

        content = step.get(
            "content",
            [],
        )

        if not isinstance(
            content,
            list,
        ):
            continue

        pieces = []

        for part in content:

            if not isinstance(
                part,
                dict,
            ):
                continue

            if part.get(
                "type"
            ) != "text":
                continue

            text = part.get(
                "text",
                "",
            )

            if isinstance(
                text,
                str,
            ):
                pieces.append(
                    text
                )

        if pieces:

            return "\n".join(
                pieces
            ).strip()

    return ""


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

    if not text:

        errors = response_data.get(
            "errors",
            [],
        )

        if isinstance(
            errors,
            list,
        ):

            for error in errors:

                if isinstance(
                    error,
                    dict,
                ):

                    message = _clean_string(
                        error.get(
                            "message"
                        )
                    )

                    if message:
                        raise RuntimeError(
                            "Gemini: " + message
                        )

        raise RuntimeError(
            "Gemini returned no analysis text."
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
            int(
                result.get(
                    "confidence",
                    0,
                )
            )
            if str(
                result.get(
                    "confidence",
                    "0",
                )
            ).strip().lstrip("-").isdigit()
            else 0,
        ),
    )

    result["instrument"] = (
        _clean_string(
            instrument
        ).upper()
    )

    result["trend"] = _clean_string(
        result.get(
            "trend"
        )
    )

    result["trade_idea"] = _clean_string(
        result.get(
            "trade_idea"
        )
    )

    result[
        "higher_timeframe_context"
    ] = _clean_string(
        result.get(
            "higher_timeframe_context"
        )
    )

    result[
        "lower_timeframe_confirmation"
    ] = _clean_string(
        result.get(
            "lower_timeframe_confirmation"
        )
    )

    result["entry"] = _clean_string(
        result.get(
            "entry"
        )
    )

    result["stop_loss"] = _clean_string(
        result.get(
            "stop_loss"
        )
    )

    result["take_profit_1"] = _clean_string(
        result.get(
            "take_profit_1"
        )
    )

    result["take_profit_2"] = _clean_string(
        result.get(
            "take_profit_2"
        )
    )

    result["risk_reward"] = _clean_string(
        result.get(
            "risk_reward"
        )
    )

    result["duration"] = _clean_string(
        result.get(
            "duration"
        )
    )

    result["data_analysis"] = _clean_string(
        result.get(
            "data_analysis"
        )
    )

    result["explanation"] = _clean_string(
        result.get(
            "explanation"
        )
    )

    result[
        "contributing_methods"
    ] = _clean_string_list(
        result.get(
            "contributing_methods"
        )
    )

    result[
        "weak_methods"
    ] = _clean_string_list(
        result.get(
            "weak_methods"
        )
    )

    result[
        "conflicting_methods"
    ] = _clean_string_list(
        result.get(
            "conflicting_methods"
        )
    )

    result[
        "news_fundamental_risk"
    ] = _clean_string(
        result.get(
            "news_fundamental_risk"
        )
    )

    result[
        "warnings"
    ] = _clean_string_list(
        result.get(
            "warnings"
        )
    )

    if signal == "NO TRADE":

        result["entry"] = ""
        result["stop_loss"] = ""
        result["take_profit_1"] = ""
        result["take_profit_2"] = ""
        result["risk_reward"] = ""

    return result
