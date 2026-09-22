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
You are the chart-analysis engine for LAMAR TRADING BOT.

You receive exactly two trading-chart screenshots:

1. 4H chart = higher-timeframe context.
2. 15M chart = lower-timeframe confirmation and execution context.

You must analyze BOTH charts together.

The selected instrument and trade focus are supplied by the user request.

IMPORTANT DECISION RULES

- Return BUY, SELL, or NO TRADE.
- Never force a trade.
- NO TRADE is valid and preferred when the evidence is insufficient or conflicting.
- Never invent chart information.
- Never invent exact prices that cannot reasonably be read or inferred from the screenshots.
- If price numbers are unclear, leave price fields empty.
- Confidence is confidence in the quality of the analysis, NOT a guarantee of profit.
- A high confidence score does not mean a guaranteed winning trade.
- Use visible chart evidence to support the final decision.
- Use both timeframes.
- The 4H timeframe should establish broader market context.
- The 15M timeframe should provide confirmation and execution context.
- Do not require every analysis method to agree.
- Conflicting evidence should reduce confidence and may produce NO TRADE.

INTERNAL ANALYSIS AREAS

Use whichever are useful from the supplied chart evidence:

- support and resistance
- pure price action
- market structure
- swing highs and lows
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
- smart-money concepts
- order blocks
- fair value gaps
- higher-timeframe bias
- lower-timeframe confirmation

The analysis should identify which evidence contributes to the decision, which evidence is weak, and which evidence conflicts.

Do not make the user-interface decision depend on one single method.

TRADE SETUP

For BUY:

- identify the bullish evidence
- identify the entry area when reasonably visible
- place the stop loss beyond logical invalidation
- identify TP1 as the nearer logical objective
- identify TP2 as a further objective only when supported by visible structure or liquidity
- provide a reasonable risk/reward description

For SELL:

- identify the bearish evidence
- identify the entry area when reasonably visible
- place the stop loss beyond logical invalidation
- identify TP1 as the nearer logical objective
- identify TP2 as a further objective only when supported by visible structure or liquidity
- provide a reasonable risk/reward description

For NO TRADE:

- signal must be "NO TRADE"
- entry must be ""
- stop_loss must be ""
- take_profit_1 must be ""
- take_profit_2 must be ""
- risk_reward must be ""
- clearly explain why there is not enough confirmation

TRADE FOCUS

Use the selected focus only to shape the expected holding duration:

SCALP:
Short-term setup and quick execution.

DAY TRADE:
Intraday setup.

SWING:
Broader move with a longer expected duration.

Do not override chart evidence merely because of the selected focus.

NEWS AND FUNDAMENTALS

Evaluate visible or known news/fundamental risk only when it can reasonably affect the setup.

Do not invent current news.

When current news cannot be verified from the supplied evidence, clearly state that limitation rather than fabricating news.

IMAGE QUALITY

Warn the user when:

- the chart is blurry
- the chart is heavily cropped
- important candles are missing
- price labels are unreadable
- timeframe information is unclear
- the two timeframes conflict
- market structure cannot be established reliably

OUTPUT

Return ONLY valid JSON matching the supplied JSON schema.

Do not return Markdown.
Do not return code fences.
Do not return commentary outside the JSON object.
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

    allowed_headers = (
        "data:image/jpeg;base64",
        "data:image/jpg;base64",
        "data:image/png;base64",
        "data:image/webp;base64",
    )

    if not header_lower.startswith(
        allowed_headers
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

    result = []

    for item in value:
        cleaned = _clean_string(item)

        if cleaned:
            result.append(cleaned)

    return result


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
                            "Analyze the supplied 4H and 15M "
                            "charts for the following request.\n\n"
                            f"Instrument: {instrument}\n"
                            f"Trade focus: {trade_focus}\n\n"
                            "The first image is the 4H chart.\n"
                            "The second image is the 15M chart.\n\n"
                            "Use both images together. "
                            "Return only the required JSON."
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
            "effort": "none",
        },

        "max_output_tokens": 3000,
    }


def _openai_error_message(raw):
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

        data = json.loads(text)

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
        return f"{message} ({code})"

    return message or code


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

            if message:
                raise RuntimeError(
                    "The AI request was rejected: "
                    + message
                ) from exc

            raise RuntimeError(
                "The AI request was rejected."
            ) from exc

        if exc.code == 401:

            raise RuntimeError(
                "The server AI API key was rejected."
            ) from exc

        if exc.code == 403:

            if message:
                raise RuntimeError(
                    "The AI service refused the request: "
                    + message
                ) from exc

            raise RuntimeError(
                "The AI service refused the request."
            ) from exc

        if exc.code == 404:

            if message:
                raise RuntimeError(
                    "The selected AI model or endpoint "
                    "was not found: "
                    + message
                ) from exc

            raise RuntimeError(
                "The selected AI model or endpoint was not found."
            ) from exc

        if exc.code == 429:

            if message:
                raise RuntimeError(
                    "The AI service rate or usage limit "
                    "was reached: "
                    + message
                ) from exc

            raise RuntimeError(
                "The AI service rate or usage limit was reached."
            ) from exc

        if 500 <= exc.code <= 599:

            raise RuntimeError(
                "The AI service is temporarily unavailable."
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
