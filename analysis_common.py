import json
import os
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
You are the professional chart-analysis engine for LAMAR TRADING BOT.

You receive two trading-chart screenshots:

1. 4H chart
2. 15M chart

Your job is to determine whether the chart evidence supports:

- BUY
- SELL
- NO TRADE

IMPORTANT:

Do NOT automatically choose NO TRADE simply because every method does not agree.

Do NOT require perfect agreement between the 4H and 15M charts.

A trade is allowed when the overall evidence provides a sufficiently clear and logical setup.

Use professional judgment.

==================================================
TIMEFRAME LOGIC
==================================================

SCALP:

- The 15M chart is the main execution timeframe.
- The 4H chart provides broader context.
- A valid scalp BUY or SELL may be produced when the 15M structure,
  price action, liquidity, and/or SMC evidence provide a sufficiently
  clear setup.
- The 4H chart does NOT have to show an identical entry pattern.
- A neutral 4H chart does not automatically mean NO TRADE.
- A clear 15M setup with acceptable higher-timeframe context can qualify.

DAY TRADE:

- Use the 4H chart for directional context.
- Use the 15M chart for confirmation and execution.
- The timeframes should generally support the same directional idea,
  but they do not need to be visually identical.

SWING:

- The 4H chart is the primary decision timeframe.
- The 15M chart can be used for timing and confirmation.

==================================================
TRADE DECISION
==================================================

Choose BUY when the evidence is sufficiently bullish.

Choose SELL when the evidence is sufficiently bearish.

Choose NO TRADE only when:

- the directional evidence is genuinely unclear,
- bullish and bearish evidence are materially conflicting,
- the setup is too weak to justify a directional decision,
- the chart quality prevents meaningful analysis,
- or there is no reasonable setup visible.

Do NOT use NO TRADE simply because one individual method is weak.

Do NOT require all analysis concepts to agree.

The strongest overall evidence should determine the direction.

==================================================
INTERNAL ANALYSIS
==================================================

Analyze whichever of these concepts are useful and visible:

- support and resistance
- pure price action
- market structure
- swing highs
- swing lows
- BOS
- CHoCH
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

You do NOT need every concept.

Identify which evidence:

- contributes to the setup
- is weak
- conflicts with the setup

==================================================
ENTRY LOGIC
==================================================

When a BUY setup is sufficiently clear:

- identify the logical entry area
- identify invalidation
- identify stop loss
- identify TP1
- identify TP2 when supported
- describe the trade idea
- calculate or describe risk/reward consistently

When a SELL setup is sufficiently clear:

- identify the logical entry area
- identify invalidation
- identify stop loss
- identify TP1
- identify TP2 when supported
- describe the trade idea
- calculate or describe risk/reward consistently

If exact price digits are visible enough, use them.

If the exact price digits are not readable:

- do not invent fake precision
- you may describe an entry area using a clear price zone or structure
  when that can reasonably be inferred from the chart
- if an exact numerical field cannot be stated responsibly, leave that
  individual field empty rather than inventing a number

Do NOT turn an otherwise valid trade into NO TRADE solely because a
single exact price digit is difficult to read.

==================================================
BUY CONDITIONS
==================================================

A BUY can be considered when the evidence contains a meaningful
combination such as:

- bullish structure
- bullish BOS or CHoCH
- support reaction
- bullish liquidity sweep
- bullish displacement
- bullish order block
- bullish fair value gap
- discount positioning
- bullish price action
- lower-timeframe bullish confirmation

Not all of these are necessary.

==================================================
SELL CONDITIONS
==================================================

A SELL can be considered when the evidence contains a meaningful
combination such as:

- bearish structure
- bearish BOS or CHoCH
- resistance reaction
- bearish liquidity sweep
- bearish displacement
- bearish order block
- bearish fair value gap
- premium positioning
- bearish price action
- lower-timeframe bearish confirmation

Not all of these are necessary.

==================================================
NO TRADE CONDITIONS
==================================================

Use NO TRADE when the chart genuinely does not provide enough
directional evidence.

Examples:

- price is clearly ranging with no meaningful confirmation
- bullish and bearish evidence are strongly balanced
- the 15M setup is too unclear for the selected focus
- the 4H context materially invalidates the proposed idea
- the screenshot is too poor to analyze
- important chart information is missing

Do not use NO TRADE just because some methods disagree.

==================================================
CONFIDENCE
==================================================

Confidence is the confidence in the QUALITY OF THE ANALYSIS.

It is NOT:

- a guaranteed win rate
- a guarantee of profit
- a guaranteed probability of success

Use a lower confidence score when evidence is weak or conflicting.

Use a higher confidence score when the setup is well supported by
multiple visible pieces of evidence.

==================================================
NEWS AND FUNDAMENTALS
==================================================

Do not invent current news.

Use only visible or reliably supplied news/fundamental information.

If current news cannot be verified from the chart or supplied data,
state that limitation.

News uncertainty by itself does not automatically require NO TRADE.

Instead, describe it under news_fundamental_risk when relevant.

==================================================
IMAGE QUALITY
==================================================

Warn about:

- blurry images
- unreadable price labels
- heavy cropping
- missing candles
- unclear timeframe
- missing structure
- conflicting evidence

==================================================
OUTPUT
==================================================

Return ONLY valid JSON.

Do not return Markdown.

Do not return code fences.

Do not return commentary outside the JSON.

Return exactly the requested JSON schema.
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
            f"{label} is too large. "
            "Please use a smaller chart screenshot."
        )

    header, separator, encoded = image.partition(",")

    if not separator or not encoded:
        raise ValueError(
            f"A valid {label} is required."
        )

    header_lower = header.lower()

    supported = (
        "data:image/jpeg;base64",
        "data:image/jpg;base64",
        "data:image/png;base64",
        "data:image/webp;base64",
    )

    if not header_lower.startswith(
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
        text = _clean_string(
            item
        )

        if text:
            cleaned.append(
                text
            )

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
                            "Perform a professional chart analysis.\n\n"
                            f"Instrument: {instrument}\n"
                            f"Trade focus: {trade_focus}\n\n"
                            "Image 1 = 4H chart.\n"
                            "Image 2 = 15M chart.\n\n"
                            "IMPORTANT:\n"
                            "Do not default to NO TRADE merely because "
                            "the two timeframes are not identical.\n"
                            "For SCALP, give the 15M setup primary "
                            "execution importance while using 4H for context.\n"
                            "For DAY TRADE, combine 4H context with 15M confirmation.\n"
                            "For SWING, give the 4H structure primary importance.\n\n"
                            "Return the strongest justified decision: "
                            "BUY, SELL, or NO TRADE."
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

        "reasoning": {
            "effort": "low",
        },

        "max_output_tokens": 3000,
    }


def _openai_error_message(
    raw,
):
    if not raw:
        return ""

    try:

        if isinstance(
            raw,
            bytes,
        ):
            text = raw.decode(
                "utf-8",
                errors="replace",
            )
        else:
            text = str(raw)

        data = json.loads(
            text
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
        error.get(
            "message"
        )
    )

    code = _clean_string(
        error.get(
            "code"
        )
    )

    if message and code:
        return (
            f"{message} ({code})"
        )

    return (
        message or code
    )


def _request_openai(
    payload,
    key,
):
    body = json.dumps(
        payload,
        separators=(",", ":"),
    ).encode("utf-8")

    request = urllib.request.Request(

        OPENAI_URL,

        data=body,

        headers={
            "Authorization":
                "Bearer " + key,

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
                    "The AI service returned an invalid response."
                )

            return data

    except urllib.error.HTTPError as exc:

        raw = b""

        try:
            raw = exc.read()
        except Exception:
            pass

        message = _openai_error_message(
            raw
        )

        if exc.code == 400:

            raise RuntimeError(
                "The AI request was rejected: "
                + (
                    message
                    or "invalid request"
                )
            ) from exc

        if exc.code == 401:

            raise RuntimeError(
                "The server AI API key was rejected."
            ) from exc

        if exc.code == 403:

            raise RuntimeError(
                "The AI service refused the request: "
                + (
                    message
                    or "access denied"
                )
            ) from exc

        if exc.code == 404:

            raise RuntimeError(
                "The selected AI model or endpoint was not found: "
                + (
                    message
                    or "not found"
                )
            ) from exc

        if exc.code == 429:

            raise RuntimeError(
                "The AI service rate or usage limit was reached: "
                + (
                    message
                    or "rate limit"
                )
            ) from exc

        if 500 <= exc.code <= 599:

            raise RuntimeError(
                "The AI service is temporarily unavailable."
            ) from exc

        raise RuntimeError(
            "AI analysis could not be started: "
            + (
                message
                or "unknown AI error"
            )
        ) from exc

    except (
        urllib.error.URLError,
        TimeoutError,
    ) as exc:

        raise RuntimeError(
            "The AI analysis service could not be reached."
        ) from exc

    except json.JSONDecodeError as exc:

        raise RuntimeError(
            "The AI service returned invalid JSON."
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

    output_text = response_data.get(
        "output_text"
    )

    if (
        isinstance(
            output_text,
            str,
        )
        and output_text.strip()
    ):
        return output_text.strip()

    output = response_data.get(
        "output",
        [],
    )

    if not isinstance(
        output,
        list,
    ):
        raise RuntimeError(
            "The AI service returned no analysis output."
        )

    for item in output:

        if not isinstance(
            item,
            dict,
        ):
            continue

        content = item.get(
            "content",
            [],
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

            part_type = str(
                part.get(
                    "type",
                    "",
                )
            )

            if part_type not in {
                "output_text",
                "text",
            }:
                continue

            text = part.get(
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

    try:

        result = json.loads(
            text
        )

    except json.JSONDecodeError as exc:

        raise RuntimeError(
            "The model returned an invalid analysis format."
        ) from exc

    if not isinstance(
        result,
        dict,
    ):
        raise RuntimeError(
            "The model returned an invalid analysis object."
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
