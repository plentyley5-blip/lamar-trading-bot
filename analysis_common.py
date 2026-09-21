import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request


OPENAI_URL = "https://api.openai.com/v1/responses"

# Keep the same model.
MODEL = "gpt-5.6-luna"

# The job token is intentionally short-lived.
JOB_TTL_SECONDS = 15 * 60


SYSTEM_PROMPT = """
You are LM ANALYZER, a professional chart-analysis engine.

Analyze the supplied 4H and 15M charts for the supplied instrument and trade
focus: SCALP, DAY TRADE, or SWING.

Return BUY, SELL, or NO TRADE.

Use genuine multi-timeframe technical analysis:
- support/resistance
- pure price action
- market structure
- BOS and CHoCH
- liquidity and liquidity sweeps
- SMC
- order blocks
- fair value gaps/imbalances
- Fibonacci
- premium/discount
- displacement
- inducement
- mitigation/invalidation
- higher-timeframe context
- lower-timeframe confirmation

Do not merely name methods. Explain how the visible evidence supports or
conflicts with the setup.

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
            "enum": ["BUY", "SELL", "NO TRADE"]
        },
        "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 100
        },
        "strength": {
            "type": "string",
            "enum": [
                "VERY STRONG",
                "STRONG",
                "MODERATE",
                "WEAK"
            ]
        },
        "instrument": {
            "type": "string"
        },
        "trade_focus": {
            "type": "string",
            "enum": [
                "SCALP",
                "DAY TRADE",
                "SWING"
            ]
        },
        "trend": {
            "type": "string",
            "enum": [
                "BULLISH",
                "BEARISH",
                "RANGE",
                "UNCLEAR"
            ]
        },
        "trade_idea": {
            "type": "string"
        },
        "entry": {
            "type": "string"
        },
        "stop_loss": {
            "type": "string"
        },
        "take_profit_1": {
            "type": "string"
        },
        "take_profit_2": {
            "type": "string"
        },
        "risk_reward": {
            "type": "string"
        },
        "duration": {
            "type": "string"
        },
        "higher_timeframe_context": {
            "type": "string"
        },
        "lower_timeframe_confirmation": {
            "type": "string"
        },
        "data_analysis": {
            "type": "string"
        },
        "explanation": {
            "type": "string"
        },
        "contributing_methods": {
            "type": "array",
            "items": {
                "type": "string"
            }
        },
        "weak_methods": {
            "type": "array",
            "items": {
                "type": "string"
            }
        },
        "conflicting_methods": {
            "type": "array",
            "items": {
                "type": "string"
            }
        },
        "news_fundamental_risk": {
            "type": "string"
        },
        "warnings": {
            "type": "array",
            "items": {
                "type": "string"
            }
        }
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
        "warnings"
    ]
}


def now():
    return int(time.time())


def get_openai_key():
    key = os.environ.get("OPENAI_API_KEY", "").strip()

    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY is missing from Vercel Production."
        )

    return key


def token_key():
    return get_openai_key().encode("utf-8")


def create_job_token(response_id, instrument, trade_focus):
    payload = {
        "response_id": response_id,
        "instrument": instrument,
        "trade_focus": trade_focus,
        "created_at": now()
    }

    raw = json.dumps(
        payload,
        separators=(",", ":"),
        sort_keys=True
    ).encode("utf-8")

    signature = hmac.new(
        token_key(),
        raw,
        hashlib.sha256
    ).digest()

    encoded_payload = (
        base64.urlsafe_b64encode(raw)
        .decode("ascii")
        .rstrip("=")
    )

    encoded_signature = (
        base64.urlsafe_b64encode(signature)
        .decode("ascii")
        .rstrip("=")
    )

    return encoded_payload + "." + encoded_signature


def read_job_token(token):
    try:
        parts = token.split(".", 1)

        if len(parts) != 2:
            raise ValueError("Invalid job token.")

        raw_part, signature_part = parts

        raw = base64.urlsafe_b64decode(
            raw_part + "=" * (-len(raw_part) % 4)
        )

        supplied_signature = base64.urlsafe_b64decode(
            signature_part + "=" * (-len(signature_part) % 4)
        )

        expected_signature = hmac.new(
            token_key(),
            raw,
            hashlib.sha256
        ).digest()

        if not hmac.compare_digest(
            supplied_signature,
            expected_signature
        ):
            raise ValueError(
                "Invalid job token signature."
            )

        data = json.loads(
            raw.decode("utf-8")
        )

        created_at = int(
            data["created_at"]
        )

        if now() - created_at > JOB_TTL_SECONDS:
            raise ValueError(
                "Analysis job has expired."
            )

        return (
            str(data["response_id"]).strip(),
            str(data["instrument"]).strip(),
            str(data["trade_focus"]).strip()
        )

    except Exception as exc:
        raise ValueError(
            "Invalid analysis job: " + str(exc)
        )


def validate_instrument(value):
    instrument = str(value or "").strip()

    if not instrument:
        raise ValueError(
            "Instrument is required."
        )

    if len(instrument) > 50:
        raise ValueError(
            "Instrument name is too long."
        )

    return instrument


def validate_focus(value):
    focus = str(value or "").strip().upper()

    if focus not in {
        "SCALP",
        "DAY TRADE",
        "SWING"
    }:
        raise ValueError(
            "Trade focus must be SCALP, DAY TRADE, or SWING."
        )

    return focus


def validate_image_data_url(value, name):
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


def make_user_prompt(instrument, trade_focus):
    return f"""
Instrument: {instrument}
Trade focus: {trade_focus}

Analyze both supplied charts:
1. 4H = higher-timeframe context
2. 15M = confirmation and execution

Identify the strongest technically supported setup.

If evidence is insufficient, return NO TRADE.

Do not invent prices.
"""


def _send_openai_request(
    instrument,
    trade_focus,
    higher_image,
    lower_image,
    image_detail="low",
    output_tokens=1200
):
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
                        "text": make_user_prompt(
                            instrument,
                            trade_focus
                        )
                    },
                    {
                        "type": "input_image",
                        "image_url": higher_image,
                        "detail": image_detail
                    },
                    {
                        "type": "input_image",
                        "image_url": lower_image,
                        "detail": image_detail
                    }
                ]
            }
        ],

        "text": {
            "format": {
                "type": "json_schema",
                "name": "lamar_trade_analysis",
                "strict": True,
                "schema": OUTPUT_SCHEMA
            }
        },

        # Reduced from 3000.
        # This includes reasoning/output allocation.
        "max_output_tokens": output_tokens
    }

    body = json.dumps(
        payload,
        separators=(",", ":")
    ).encode("utf-8")

    request = urllib.request.Request(
        OPENAI_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + get_openai_key(),
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=55
        ) as response:

            response_body = (
                response.read()
                .decode("utf-8")
            )

            return json.loads(
                response_body
            )

    except urllib.error.HTTPError as exc:

        error_body = (
            exc.read()
            .decode(
                "utf-8",
                errors="replace"
            )
        )

        # Preserve the actual HTTP status so the caller
        # can perform a controlled fallback.
        error = RuntimeError(
            "OpenAI HTTP "
            + str(exc.code)
            + ": "
            + error_body
        )

        error.openai_status = exc.code
        error.openai_body = error_body

        raise error

    except urllib.error.URLError as exc:

        raise RuntimeError(
            "Unable to connect to OpenAI: "
            + str(exc.reason)
        )


def create_background_response(
    instrument,
    trade_focus,
    higher_image,
    lower_image
):
    """
    First attempt:
        low-detail images
        1200 output tokens

    If OpenAI rejects the request for rate limiting,
    make one smaller attempt.

    We deliberately do NOT perform repeated rapid retries.
    OpenAI says unsuccessful requests also contribute to
    rate limits.
    """

    try:

        return _send_openai_request(
            instrument=instrument,
            trade_focus=trade_focus,
            higher_image=higher_image,
            lower_image=lower_image,
            image_detail="low",
            output_tokens=1200
        )

    except Exception as first_error:

        status = getattr(
            first_error,
            "openai_status",
            None
        )

        if status != 429:
            raise

        # Smaller fallback.
        return _send_openai_request(
            instrument=instrument,
            trade_focus=trade_focus,
            higher_image=higher_image,
            lower_image=lower_image,
            image_detail="low",
            output_tokens=700
        )


def extract_output_text(response_data):
    output = response_data.get(
        "output"
    )

    if isinstance(output, list):

        pieces = []

        for item in output:

            if not isinstance(item, dict):
                continue

            content = item.get(
                "content"
            )

            if not isinstance(content, list):
                continue

            for part in content:

                if not isinstance(part, dict):
                    continue

                text = part.get(
                    "text"
                )

                if isinstance(text, str):
                    pieces.append(text)

        if pieces:
            return "\n".join(
                pieces
            ).strip()

    output_text = response_data.get(
        "output_text"
    )

    if isinstance(
        output_text,
        str
    ) and output_text.strip():

        return output_text.strip()

    raise ValueError(
        "OpenAI completed the analysis but returned no text."
    )


def parse_completed_response(
    response_data,
    instrument,
    trade_focus
):
    text = extract_output_text(
        response_data
    )

    try:
        result = json.loads(
            text
        )

    except json.JSONDecodeError:

        cleaned = text.strip()

        if cleaned.startswith(
            "```"
        ):
            cleaned = (
                cleaned
                .replace(
                    "```json",
                    "",
                    1
                )
                .replace(
                    "```",
                    "",
                    1
                )
                .strip()
            )

        result = json.loads(
            cleaned
        )

    if not isinstance(
        result,
        dict
    ):
        raise ValueError(
            "Analysis result is not a JSON object."
        )

    result["instrument"] = instrument
    result["trade_focus"] = trade_focus

    signal = str(
        result.get(
            "signal",
            ""
        )
    ).upper().strip()

    if signal not in {
        "BUY",
        "SELL",
        "NO TRADE"
    }:
        raise ValueError(
            "Analysis returned an invalid signal."
        )

    result["signal"] = signal

    if signal == "NO TRADE":
        result["entry"] = ""
        result["stop_loss"] = ""
        result["take_profit_1"] = ""
        result["take_profit_2"] = ""
        result["risk_reward"] = ""

    return result


def json_response(
    handler,
    status_code,
    payload
):
    body = json.dumps(
        payload,
        ensure_ascii=False
    ).encode("utf-8")

    handler.send_response(
        status_code
    )

    handler.send_header(
        "Content-Type",
        "application/json; charset=utf-8"
    )

    handler.send_header(
        "Access-Control-Allow-Origin",
        "*"
    )

    handler.send_header(
        "Access-Control-Allow-Methods",
        "GET, POST, OPTIONS"
    )

    handler.send_header(
        "Access-Control-Allow-Headers",
        "Content-Type, Authorization"
    )

    handler.send_header(
        "Cache-Control",
        "no-store, no-cache, must-revalidate"
    )

    handler.send_header(
        "Content-Length",
        str(len(body))
    )

    handler.end_headers()

    handler.wfile.write(
        body
    )
