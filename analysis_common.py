import base64
import hashlib
import hmac
import json
import os
import time


OPENAI_URL = "https://api.openai.com/v1/responses"

MODEL = "gpt-5.6-luna"

JOB_TTL_SECONDS = 24 * 60 * 60


SYSTEM_PROMPT = """
You are the primary chart-analysis engine for LAMAR TRADING BOT.

Your task is to analyze TWO chart screenshots belonging to the SAME selected
financial instrument:

1. Higher timeframe: 4H
2. Lower timeframe: 15M

The user's selected trade focus will be supplied as:
- SCALP
- DAY TRADE
- SWING

You must use the visible chart evidence only.

Do not invent information that cannot reasonably be read or inferred from
the screenshots.

The application may use the following analytical concepts internally:

- Support and Resistance
- Pure Price Action
- Market Structure
- Liquidity
- Liquidity Sweeps
- BOS
- CHoCH
- Smart Money Concepts
- Order Blocks
- Fair Value Gaps / Imbalances
- Premium and Discount
- Fibonacci
- Trend
- Candlestick behavior
- News and fundamental risk when information is available in the supplied
  request/context

SMC MUST be based on actual visible structure. Do not simply mention the
term "SMC".

For SMC analysis, look for evidence such as:
- Break of Structure
- Change of Character
- Liquidity sweeps
- Displacement
- Order Blocks
- Fair Value Gaps
- Premium/Discount positioning
- Inducement when reasonably identifiable
- Mitigation/invalidation when reasonably identifiable

Use the 4H chart primarily for higher-timeframe context and the 15M chart
primarily for confirmation and execution context.

The trade focus changes the weighting:

SCALP:
Prioritize recent 15M structure, nearby liquidity, recent OB/FVG,
entry confirmation, volatility and immediate risk.

DAY TRADE:
Prioritize 4H context together with 15M structure, intraday liquidity,
support/resistance, session behavior and execution confirmation.

SWING:
Prioritize 4H structure, major support/resistance, larger liquidity,
higher-timeframe OB/FVG, Fibonacci and broader market context.

Do NOT require every analytical method to agree.

Instead:
- identify the evidence contributing to the setup
- identify weak evidence
- identify conflicting evidence
- explain why the final decision was reached

A strong setup may be supported by several methods while another method
provides little or no confirmation.

If the evidence is insufficient, conflicting, unclear, or the screenshots
are poor quality, return NO TRADE.

Never force BUY or SELL.

Never invent exact price levels.

Only provide an entry, stop loss or target when the relevant price can be
reasonably read from the screenshots and the level is technically justified.

For NO TRADE:
- entry must be ""
- stop_loss must be ""
- take_profit_1 must be ""
- take_profit_2 must be ""
- risk_reward must be ""

Confidence is NOT a guaranteed probability of profit.

Confidence measures how strongly the visible evidence supports the analysis.

Strength must describe the quality of the setup:
- VERY STRONG
- STRONG
- MODERATE
- WEAK

The initial take profit should represent the nearest technically reasonable
objective.

The final take profit should represent a larger technically justified
objective when one exists.

Do not manufacture targets simply to fill fields.

Duration should be an approximate expectation based on the selected trade
focus, structure and volatility. Avoid false precision.

News/fundamental risk must be reported when known from the request/context.
If no reliable news/fundamental information is available, explicitly say so
rather than inventing news.

Return ONLY JSON matching the supplied schema.
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
            "type": "integer",
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
            "type": "string",
        },
    },
    "required": [
        "signal",
        "confidence",
        "strength",
        "instrument",
        "trade_focus",
        "trend",
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


def json_response(handler, status, payload, extra_headers=None):
    raw = json.dumps(payload).encode("utf-8")

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
        "no-store",
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
    return os.getenv("OPENAI_API_KEY", "").strip()


def make_job_token(response_id, instrument, trade_focus):
    key = api_key().encode("utf-8")

    created = int(time.time())

    payload_obj = {
        "r": response_id,
        "t": created,
        "i": instrument,
        "f": trade_focus,
    }

    payload = json.dumps(
        payload_obj,
        separators=(",", ":"),
    ).encode("utf-8")

    encoded_payload = (
        base64.urlsafe_b64encode(payload)
        .decode("ascii")
        .rstrip("=")
    )

    signature = hmac.new(
        key,
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()

    return encoded_payload + "." + signature


def read_job_token(token):
    if not token:
        raise ValueError("Missing analysis job id.")

    parts = token.split(".", 1)

    if len(parts) != 2:
        raise ValueError("Invalid analysis job id.")

    encoded_payload, signature = parts

    expected = hmac.new(
        api_key().encode("utf-8"),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(
        signature,
        expected,
    ):
        raise ValueError("Invalid analysis job id.")

    padded = encoded_payload + "=" * (
        -len(encoded_payload) % 4
    )

    try:
        payload_obj = json.loads(
            base64.urlsafe_b64decode(
                padded.encode("ascii")
            ).decode("utf-8")
        )
    except Exception as exc:
        raise ValueError(
            "Invalid analysis job id."
        ) from exc

    response_id = str(
        payload_obj.get("r") or ""
    )

    instrument = str(
        payload_obj.get("i") or ""
    )

    trade_focus = str(
        payload_obj.get("f") or ""
    )

    try:
        created = int(
            payload_obj.get("t")
        )
    except (
        TypeError,
        ValueError,
    ) as exc:
        raise ValueError(
            "Invalid analysis job id."
        ) from exc

    if not response_id:
        raise ValueError(
            "Invalid analysis job id."
        )

    if time.time() - created > JOB_TTL_SECONDS:
        raise ValueError(
            "This analysis job has expired."
        )

    return (
        response_id,
        instrument,
        trade_focus,
    )


def extract_output_text(response_data):
    direct = response_data.get(
        "output_text"
    )

    if (
        isinstance(direct, str)
        and direct.strip()
    ):
        return direct.strip()

    for item in response_data.get(
        "output",
        [],
    ):
        if not isinstance(
            item,
            dict,
        ):
            continue

        for content in item.get(
            "content",
            [],
        ):
            if not isinstance(
                content,
                dict,
            ):
                continue

            if content.get(
                "type"
            ) == "output_text":

                text = content.get(
                    "text"
                )

                if (
                    isinstance(text, str)
                    and text.strip()
                ):
                    return text.strip()

    raise RuntimeError(
        "The model returned no analysis text."
    )


def clean_json_text(text):
    text = text.strip()

    if text.startswith("```"):
        lines = text.splitlines()

        if lines:
            lines = lines[1:]

        if (
            lines
            and lines[-1].strip()
            == "```"
        ):
            lines = lines[:-1]

        text = "\n".join(
            lines
        ).strip()

    return text


def parse_completed_response(
    response_data,
    instrument,
    trade_focus,
):
    text = clean_json_text(
        extract_output_text(
            response_data
        )
    )

    try:
        result = json.loads(text)
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

    result["instrument"] = instrument
    result["trade_focus"] = trade_focus

    signal = str(
        result.get(
            "signal",
            "NO TRADE",
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

    if signal == "NO TRADE":
        result["entry"] = ""
        result["stop_loss"] = ""
        result["take_profit_1"] = ""
        result["take_profit_2"] = ""
        result["risk_reward"] = ""

    return result


def classify_error_body(raw):
    try:
        data = json.loads(
            raw.decode(
                "utf-8",
                errors="replace",
            )
        )
    except Exception:
        return "", ""

    err = data.get("error")

    if not isinstance(
        err,
        dict,
    ):
        return "", ""

    return (
        str(
            err.get("code") or ""
        ),
        str(
            err.get("message") or ""
        ),
    )
