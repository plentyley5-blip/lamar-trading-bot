import base64
import hashlib
import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler


OPENAI_URL = "https://api.openai.com/v1/responses"
MODEL = "gpt-5.6-luna"

# Background responses are intended for asynchronous processing.
# The job token itself is stateless and contains the OpenAI response ID.
JOB_TTL_SECONDS = 15 * 60


SYSTEM_PROMPT = """
You are LM ANALYZER, a professional multi-timeframe trading-chart analysis engine.

You analyze ONLY the chart images supplied by the user and the explicitly supplied
instrument and trade focus.

The user supplies:
- one 4H chart
- one 15M chart
- an instrument
- a trade focus: SCALP, DAY TRADE, or SWING

Your job is to determine whether the available evidence supports:
BUY, SELL, or NO TRADE.

IMPORTANT:
- Never force a trade.
- NO TRADE is valid and should be used when evidence is insufficient,
  conflicting, unclear, or the chart cannot be read reliably.
- Never invent exact prices that cannot be reasonably read from the chart.
- If exact prices are not sufficiently readable, return NO TRADE.
- Do not claim certainty.
- Confidence is an analysis-confidence score from 0 to 100, NOT a guaranteed
  probability of winning.
- Do not present a trade as guaranteed profit.
- Do not fabricate news or fundamental events.

TIMEFRAME LOGIC:
- 4H establishes the higher-timeframe context.
- 15M is used for confirmation and execution structure.
- The requested trade focus changes the weighting of evidence:
  SCALP emphasizes immediate 15M confirmation.
  DAY TRADE balances 4H context and 15M execution.
  SWING emphasizes 4H structure and uses 15M for refinement.

ANALYZE THESE METHODS INTERNALLY:

1. Support and Resistance
   - Key highs/lows
   - Reaction zones
   - Breaks and retests
   - Rejection

2. Pure Price Action
   - Candlestick behavior
   - Rejection
   - Momentum
   - Consolidation
   - Breakout behavior
   - Retests

3. Market Structure
   - Higher highs / higher lows
   - Lower highs / lower lows
   - BOS
   - CHoCH
   - Structural continuation or failure

4. Liquidity
   - Equal highs
   - Equal lows
   - Stop runs
   - Liquidity sweeps
   - High/low raids
   - Whether liquidity was taken and rejected

5. Smart Money Concepts
   Apply SMC as actual analysis, not merely as a label.
   Identify when visually supported:
   - BOS
   - CHoCH
   - displacement
   - inducement
   - liquidity sweep
   - order blocks
   - fair value gaps / imbalances
   - mitigation
   - premium / discount
   - invalidation

6. Fibonacci
   - Relevant swing
   - Retracement area
   - Confluence with structure
   - Premium/discount relationship

7. Order Blocks
   - Identify only when visually justified.
   - Do not invent an order block simply because the method is requested.

8. Fair Value Gaps / Imbalances
   - Identify only when visible.
   - Consider whether they have been filled or remain relevant.

9. Premium / Discount
   - Determine the relevant dealing range when possible.
   - Consider whether price is in a logical area for the proposed direction.

10. News / Fundamentals
   - Only identify news/fundamental risk when supplied by the user or
     when reliable information is explicitly available.
   - Do not invent economic releases.

METHOD SYNTHESIS:
The methods do NOT need unanimous agreement.

Determine:
- contributing_methods: methods that materially support the conclusion
- weak_methods: methods that provide limited or weak evidence
- conflicting_methods: methods that materially disagree

A trade can still be valid when some methods are weak or conflicting,
provided the overall structure and evidence are sufficiently clear.

ENTRY:
Only provide an exact entry when the chart allows a reasonable reading.
Prefer a logical entry based on:
- retest
- confirmation
- structure
- liquidity reaction
- order block
- FVG
- support/resistance
- price action

STOP LOSS:
Place the conceptual stop beyond the relevant invalidation structure.

TARGETS:
- take_profit_1 = initial logical target
- take_profit_2 = final logical target
- Do not invent targets unsupported by visible structure.

RISK/REWARD:
Give a reasonable RR representation when a valid trade exists.
For example:
"1:2.5"

If there is no valid trade, use:
""

DURATION:
Describe the expected holding period according to the selected focus,
without making guarantees.

TRADE FOCUS:
SCALP:
- prioritize immediate 15M structure and confirmation
- avoid holding assumptions beyond the visible setup

DAY TRADE:
- combine 4H direction/context with 15M execution

SWING:
- prioritize 4H structure and major levels
- use 15M for refined confirmation where possible

NO TRADE CONDITIONS:
Return NO TRADE when:
- charts are unreadable
- required timeframe evidence is missing
- price digits cannot be read sufficiently
- structure is too unclear
- the higher and lower timeframes materially conflict without a clear resolution
- entry/SL cannot be reasonably established
- the setup has poor structure
- the chart does not provide enough evidence

When signal is NO TRADE:
- entry = ""
- stop_loss = ""
- take_profit_1 = ""
- take_profit_2 = ""
- risk_reward = ""
- trade_idea should explain why there is no valid setup.

OUTPUT:
Return ONLY the requested structured JSON.
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
            "enum": ["VERY STRONG", "STRONG", "MODERATE", "WEAK"]
        },
        "instrument": {
            "type": "string"
        },
        "trade_focus": {
            "type": "string",
            "enum": ["SCALP", "DAY TRADE", "SWING"]
        },
        "trend": {
            "type": "string",
            "enum": ["BULLISH", "BEARISH", "RANGE", "UNCLEAR"]
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
            "OPENAI_API_KEY is not configured in the Vercel Production environment."
        )

    return key


def token_key():
    """
    We intentionally do not require a second Vercel secret.

    The existing OPENAI_API_KEY is used only as an HMAC signing secret.
    The key itself is NEVER sent to the browser.
    """
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

    return (
        base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        + "."
        + base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    )


def read_job_token(token):
    try:
        parts = token.split(".", 1)

        if len(parts) != 2:
            raise ValueError("Invalid job token.")

        raw_part, sig_part = parts

        raw = base64.urlsafe_b64decode(
            raw_part + "=" * (-len(raw_part) % 4)
        )

        supplied_signature = base64.urlsafe_b64decode(
            sig_part + "=" * (-len(sig_part) % 4)
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
            raise ValueError("Invalid job token signature.")

        payload = json.loads(raw.decode("utf-8"))

        response_id = str(payload["response_id"]).strip()
        instrument = str(payload["instrument"]).strip()
        trade_focus = str(payload["trade_focus"]).strip()
        created_at = int(payload["created_at"])

        if not response_id:
            raise ValueError("Job token contains no response ID.")

        if now() - created_at > JOB_TTL_SECONDS:
            raise ValueError("This analysis job has expired.")

        return response_id, instrument, trade_focus

    except Exception as exc:
        raise ValueError(
            "Invalid or expired analysis job token: " + str(exc)
        )


def validate_focus(value):
    focus = str(value or "").strip().upper()

    allowed = {
        "SCALP",
        "DAY TRADE",
        "SWING"
    }

    if focus not in allowed:
        raise ValueError(
            "trade_focus must be SCALP, DAY TRADE, or SWING."
        )

    return focus


def validate_instrument(value):
    instrument = str(value or "").strip()

    if not instrument:
        raise ValueError("instrument is required.")

    if len(instrument) > 50:
        raise ValueError("instrument is too long.")

    return instrument


def validate_image_data_url(value, field_name):
    if not isinstance(value, str):
        raise ValueError(field_name + " must be an image data URL.")

    if not value.startswith("data:image/"):
        raise ValueError(
            field_name + " must start with data:image/."
        )

    if ";base64," not in value:
        raise ValueError(
            field_name + " must contain a base64 image."
        )

    # Prevent excessively large requests.
    if len(value) > 12 * 1024 * 1024:
        raise ValueError(
            field_name + " is too large. Please upload a smaller image."
        )

    return value


def make_user_prompt(instrument, trade_focus):
    return f"""
Analyze the supplied 4H and 15M charts.

Instrument:
{instrument}

Trade focus:
{trade_focus}

Use the 4H chart for higher-timeframe context and the 15M chart for
confirmation/execution.

Return a structured analysis.

If the evidence does not support a sufficiently clear trade,
return NO TRADE.

Do not invent exact price values.
"""


def create_background_response(
    instrument,
    trade_focus,
    higher_image,
    lower_image
):
    import urllib.request
    import urllib.error

    api_key = get_openai_key()

    payload = {
        "model": MODEL,

        # Critical:
        # background processing allows the Vercel request to finish while
        # OpenAI continues the analysis.
        "background": True,

        # Explicitly request stored response data.
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
                        "detail": "high"
                    },
                    {
                        "type": "input_image",
                        "image_url": lower_image,
                        "detail": "high"
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

        "max_output_tokens": 3000
    }

    body = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        OPENAI_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=55
        ) as response:

            response_body = response.read().decode("utf-8")

            if response.status < 200 or response.status >= 300:
                raise RuntimeError(
                    "OpenAI create response returned HTTP "
                    + str(response.status)
                    + ": "
                    + response_body
                )

            data = json.loads(response_body)

    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")

        raise RuntimeError(
            "OpenAI create response failed. HTTP "
            + str(exc.code)
            + ": "
            + error_body
        )

    except urllib.error.URLError as exc:
        raise RuntimeError(
            "Could not connect to OpenAI: " + str(exc.reason)
        )

    response_id = str(data.get("id", "")).strip()

    if not response_id:
        raise RuntimeError(
            "OpenAI did not return a response ID. "
            + json.dumps(data)
        )

    return data


def extract_output_text(response_data):
    """
    Handles the normal Responses API output structure.
    """

    if not isinstance(response_data, dict):
        raise ValueError("OpenAI response is not an object.")

    output = response_data.get("output")

    if not isinstance(output, list):
        raise ValueError(
            "OpenAI response contains no output array."
        )

    pieces = []

    for item in output:
        if not isinstance(item, dict):
            continue

        content = item.get("content")

        if not isinstance(content, list):
            continue

        for part in content:
            if not isinstance(part, dict):
                continue

            text_value = part.get("text")

            if isinstance(text_value, str):
                pieces.append(text_value)

    if pieces:
        return "\n".join(pieces).strip()

    # Some Responses API responses expose output_text.
    output_text = response_data.get("output_text")

    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    raise ValueError(
        "OpenAI completed the response but no text output was found."
    )


def parse_completed_response(
    response_data,
    instrument,
    trade_focus
):
    text_value = extract_output_text(response_data)

    try:
        result = json.loads(text_value)
    except json.JSONDecodeError:
        # Occasionally a model can return JSON wrapped in whitespace or
        # markdown despite the structured-output request.
        cleaned = text_value.strip()

        if cleaned.startswith("```"):
            cleaned = cleaned.replace("```json", "", 1)
            cleaned = cleaned.replace("```", "", 1)
            cleaned = cleaned.strip()

        try:
            result = json.loads(cleaned)
        except Exception as exc:
            raise ValueError(
                "Completed analysis was not valid JSON: "
                + str(exc)
            )

    if not isinstance(result, dict):
        raise ValueError(
            "Completed analysis is not a JSON object."
        )

    result["instrument"] = instrument
    result["trade_focus"] = trade_focus

    signal = str(result.get("signal", "")).upper().strip()

    if signal not in ["BUY", "SELL", "NO TRADE"]:
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

    handler.send_response(status_code)

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

    handler.wfile.write(body)
