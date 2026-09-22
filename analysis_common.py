import json
import os
import time
import urllib.error
import urllib.request


OPENAI_URL = "https://api.openai.com/v1/responses"

MODEL = (
    os.getenv(
        "OPENAI_MODEL",
        "gpt-5.6-luna",
    ).strip()
    or "gpt-5.6-luna"
)

MAX_IMAGE_DATA_URL_CHARS = 8 * 1024 * 1024


SYSTEM_PROMPT = """
You are the vision analysis engine for LAMAR TRADING BOT.

You analyze two supplied trading-chart screenshots for the selected instrument:

1. A 4H chart for higher-timeframe context.
2. A 15M chart for lower-timeframe confirmation and execution context.

Return ONLY JSON that exactly matches the supplied schema.

CORE RULES

- Use BOTH screenshots together.
- Never make the final decision from only one timeframe.
- Be conservative.
- A valid NO TRADE decision is preferred over a forced setup.
- Never invent an exact price level that is not reasonably visible or inferable from the supplied charts.
- If price digits are unclear, leave exact price fields empty.
- If missing information prevents a reliable setup, return NO TRADE.
- Confidence is an analysis-confidence score, NOT a guaranteed probability of profit.
- For NO TRADE, entry, stop_loss, take_profit_1, take_profit_2, and risk_reward MUST be empty strings.
- For BUY or SELL, provide concrete levels only when they can be reasonably read from the screenshots.
- Explain the decision using visible chart evidence.
- The selected trade focus is context only and must not override chart evidence.

INTERNAL ANALYSIS

Evaluate the chart evidence using these concepts when useful:

- support and resistance
- pure price action
- market structure
- BOS
- CHoCH
- liquidity
- liquidity sweeps
- displacement
- inducement
- mitigation
- invalidation
- Fibonacci retracement
- Fibonacci extension
- premium and discount
- smart-money concepts
- order blocks
- fair value gaps
- higher-timeframe bias
- lower-timeframe confirmation
- visible news or fundamental-risk information

Do not require every concept to agree.

Identify:

- evidence contributing to the setup
- evidence that is weak
- evidence that conflicts
- whether the higher and lower timeframes agree

Do not present internal methods as the main UI trading categories.
Explain them as evidence supporting or weakening the analysis.

TRADE SETUP RULES

For BUY or SELL:

- Make the trade idea clear.
- Use the higher timeframe to establish directional context.
- Use the 15M chart for confirmation and execution context.
- Keep the stop loss beyond the invalidation area.
- TP1 should be the nearer logical objective.
- TP2 should be a further logical objective only when supported by visible structure or liquidity.
- Risk/reward must be consistent with the supplied levels.
- Duration should match the selected trade focus and visible market structure.
- Do not manufacture precision when the chart does not support it.

NO TRADE RULES

For NO TRADE:

- signal must be NO TRADE
- entry must be an empty string
- stop_loss must be an empty string
- take_profit_1 must be an empty string
- take_profit_2 must be an empty string
- risk_reward must be an empty string
- explain clearly why a trade is not justified

IMAGE QUALITY

Mention warnings when:

- chart quality is poor
- price numbers cannot be read clearly
- chart is heavily cropped
- important market context is missing
- the two timeframes conflict
- the visible structure is ambiguous

The final response must be valid JSON matching the schema exactly.
""".strip()


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
    raw = json.dumps(
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
        str(len(raw)),
    )

    if extra_headers:
        for key, value in extra_headers.items():
            handler.send_header(
                key,
                str(value),
            )

    handler.end_headers()

    try:
        handler.wfile.write(raw)
    except Exception:
        pass


def api_key():
    return os.getenv(
        "OPENAI_API_KEY",
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
    if not isinstance(value, str):
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
            f"{label} is too large. "
            "Please use a smaller chart screenshot."
        )

    header, separator, encoded = image.partition(",")

    if not separator or not encoded:
        raise ValueError(
            f"A valid {label} is required."
        )

    supported_headers = (
        "data:image/jpeg;base64",
        "data:image/jpg;base64",
        "data:image/png;base64",
        "data:image/webp;base64",
    )

    if not header.lower().startswith(
        supported_headers
    ):
        raise ValueError(
            f"Unsupported {label} format. "
            "Use JPG, PNG, or WEBP."
        )

    if len(encoded) < 100:
        raise ValueError(
            f"The {label} appears to be empty or invalid."
        )

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

    cleaned = []

    for item in value:
        text = _clean_string(item)

        if text:
            cleaned.append(text)

    return cleaned


def _build_analysis_payload(
    instrument,
    trade_focus,
    higher_image,
    lower_image,
):
    return {
        "model": MODEL,

        "background": True,

        "instructions": SYSTEM_PROMPT,

        "input": [
            {
                "role": "user",

                "content": [
                    {
                        "type": "input_text",

                        "text": (
                            f"Instrument: {instrument}.\n"
                            f"Trade focus: {trade_focus}.\n"
                            "Image 1 is the 4H chart.\n"
                            "Image 2 is the 15M chart.\n"
                            "Analyze both together and "
                            "return the requested JSON."
                        ),
                    },

                    {
                        "type": "input_image",
                        "image_url": higher_image,
                        "detail": "high",
                    },

                    {
                        "type": "input_image",
                        "image_url": lower_image,
                        "detail": "high",
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

        "max_output_tokens": 2200,
    }


def _openai_error_message(raw):
    if not raw:
        return ""

    if isinstance(
        raw,
        str,
    ):
        raw = raw.encode("utf-8")

    try:
        data = json.loads(
            raw.decode(
                "utf-8",
                errors="replace",
            )
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

    message = _clean_string(
        error.get("message")
    )

    code = _clean_string(
        error.get("code")
    )

    if message and code:
        return (
            f"{message} ({code})"
        )

    return message or code


def _request_openai(
    payload,
    key,
):
    raw_body = json.dumps(
        payload,
        separators=(",", ":"),
    ).encode("utf-8")

    request = urllib.request.Request(

        OPENAI_URL,

        data=raw_body,

        headers={
            "Authorization": (
                "Bearer " + key
            ),

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
            timeout=60,
        ) as response:

            response_raw = (
                response
                .read()
                .decode("utf-8")
            )

            return json.loads(
                response_raw
            )

    except urllib.error.HTTPError as exc:

        raw = b""

        try:
            raw = exc.read()
        except Exception:
            pass

        message = _openai_error_message(
            raw
        )

        if exc.code == 401:

            raise RuntimeError(
                "The server API credential "
                "was rejected by the AI service."
            ) from exc

        if exc.code == 429:

            if message:

                raise RuntimeError(
                    "The AI service rate or "
                    "usage limit was reached: "
                    + message
                ) from exc

            raise RuntimeError(
                "The AI service rate or usage "
                "limit was reached."
            ) from exc

        if 500 <= exc.code <= 599:

            raise RuntimeError(
                "The AI service is temporarily "
                "unavailable."
            ) from exc

        if message:

            raise RuntimeError(
                "AI analysis could not be started: "
                + message
            ) from exc

        raise RuntimeError(
            "AI analysis could not be started."
        ) from exc

    except (
        urllib.error.URLError,
        TimeoutError,
    ) as exc:

        raise RuntimeError(
            "The AI analysis service could not "
            "be reached."
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
            "OPENAI_API_KEY is missing from Vercel."
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

    payload = _build_analysis_payload(
        instrument=instrument,
        trade_focus=trade_focus,
        higher_image=higher_image,
        lower_image=lower_image,
    )

    return _request_openai(
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
            "The AI service returned an invalid response."
        )

    direct = response_data.get(
        "output_text"
    )

    if (
        isinstance(direct, str)
        and direct.strip()
    ):
        return direct.strip()

    output = response_data.get(
        "output",
        [],
    )

    if isinstance(
        output,
        list,
    ):

        for item in output:

            if not isinstance(
                item,
                dict,
            ):
                continue

            content_items = item.get(
                "content",
                [],
            )

            if not isinstance(
                content_items,
                list,
            ):
                continue

            for content in content_items:

                if not isinstance(
                    content,
                    dict,
                ):
                    continue

                if (
                    content.get("type")
                    != "output_text"
                ):
                    continue

                text = content.get(
                    "text"
                )

                if (
                    isinstance(
                        text,
                        str,
                    )
                    and text.strip()
                ):
                    return text.strip()

    raise RuntimeError(
        "The model returned no analysis text."
    )


def clean_json_text(text):
    cleaned = str(
        text or ""
    ).strip()

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

        cleaned = (
            "\n".join(lines)
            .strip()
        )

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

    try:

        result = json.loads(
            text
        )

    except json.JSONDecodeError as exc:

        raise RuntimeError(
            "The model returned an invalid "
            "analysis format."
        ) from exc

    if not isinstance(
        result,
        dict,
    ):
        raise RuntimeError(
            "The model returned an invalid "
            "analysis object."
        )

    signal = _clean_string(
        result.get("signal")
    ).upper()

    if signal not in {
        "BUY",
        "SELL",
        "NO TRADE",
    }:

        signal = "NO TRADE"

    result["signal"] = signal

    result["instrument"] = (
        _clean_string(
            instrument
        ).upper()
    )

    result["trend"] = (
        _clean_string(
            result.get("trend")
        )
    )

    result["trade_idea"] = (
        _clean_string(
            result.get("trade_idea")
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

    result["entry"] = (
        _clean_string(
            result.get("entry")
        )
    )

    result["stop_loss"] = (
        _clean_string(
            result.get("stop_loss")
        )
    )

    result["take_profit_1"] = (
        _clean_string(
            result.get(
                "take_profit_1"
            )
        )
    )

    result["take_profit_2"] = (
        _clean_string(
            result.get(
                "take_profit_2"
            )
        )
    )

    result["risk_reward"] = (
        _clean_string(
            result.get(
                "risk_reward"
            )
        )
    )

    result["duration"] = (
        _clean_string(
            result.get(
                "duration"
            )
        )
    )

    result["data_analysis"] = (
        _clean_string(
            result.get(
                "data_analysis"
            )
        )
    )

    result["explanation"] = (
        _clean_string(
            result.get(
                "explanation"
            )
        )
    )

    result[
        "news_fundamental_risk"
    ] = _clean_string(
        result.get(
            "news_fundamental_risk"
        )
    )

    try:

        confidence = int(
            result.get(
                "confidence",
                0,
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


def classify_error_body(raw):
    if not raw:
        return "", ""

    if isinstance(
        raw,
        str,
    ):
        raw = raw.encode(
            "utf-8"
        )

    try:

        data = json.loads(
            raw.decode(
                "utf-8",
                errors="replace",
            )
        )

    except Exception:
        return "", ""

    if not isinstance(
        data,
        dict,
    ):
        return "", ""

    error = data.get(
        "error"
    )

    if not isinstance(
        error,
        dict,
    ):
        return "", ""

    return (
        _clean_string(
            error.get("code")
        ),
        _clean_string(
            error.get("message")
        ),
    )
