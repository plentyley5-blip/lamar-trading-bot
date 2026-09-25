import base64
import json
import os
import random
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

from api.user_security import (
    extract_bearer_token,
    get_supabase_service_key,
    get_supabase_url,
    is_owner_user,
    release_analysis_slot,
    reserve_analysis_slot,
    verify_access_token,
)


# ============================================================
# GEMINI CONFIGURATION
# ============================================================

GEMINI_API_KEY = (
    os.environ.get("GEMINI_OPENAI_KEY", "").strip()
    or os.environ.get("GEMINI_API_KEY", "").strip()
)

GEMINI_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]

GEMINI_BASE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
)

MAX_REQUEST_BYTES = 15 * 1024 * 1024

GEMINI_TIMEOUT_SECONDS = 15

MAX_TRANSIENT_RETRIES = 1


# ============================================================
# HTTP RESPONSE
# ============================================================

def json_response(handler, status_code, payload):

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
        "Access-Control-Allow-Headers",
        "Content-Type, Authorization"
    )

    handler.send_header(
        "Access-Control-Allow-Methods",
        "GET, POST, OPTIONS"
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

    try:
        handler.wfile.write(body)
    except Exception:
        pass


# ============================================================
# REQUEST PARSING
# ============================================================

def read_json(handler):

    try:

        length = int(
            handler.headers.get(
                "Content-Length",
                "0"
            )
        )

    except ValueError as exc:

        raise ValueError(
            "Invalid request length."
        ) from exc

    if length <= 0 or length > MAX_REQUEST_BYTES:

        raise ValueError(
            "Invalid or oversized chart request."
        )

    raw = handler.rfile.read(length)

    try:

        data = json.loads(
            raw.decode("utf-8")
        )

    except json.JSONDecodeError as exc:

        raise ValueError(
            "Invalid request JSON."
        ) from exc

    if not isinstance(data, dict):

        raise ValueError(
            "Invalid request."
        )

    return data


# ============================================================
# VALIDATION
# ============================================================

def validate_instrument(value):

    instrument = str(
        value or ""
    ).strip().upper()

    allowed = {
        "EURUSD",
        "GBPUSD",
        "USDJPY",
        "GBPJPY",
        "AUDUSD",
        "USDCAD",
        "USDCHF",
        "EURGBP",
        "XAUUSD",
        "XAGUSD",
        "BTCUSD",
        "ETHUSD",
    }

    if instrument not in allowed:

        raise ValueError(
            "Invalid instrument."
        )

    return instrument


def validate_focus(value):

    focus = str(
        value or ""
    ).strip().upper()

    if focus not in {
        "SCALP",
        "DAY TRADE",
        "SWING"
    }:

        raise ValueError(
            "Trade focus must be SCALP, DAY TRADE, or SWING."
        )

    return focus


def validate_image(value, label):

    if (
        not isinstance(value, str)
        or not value.strip()
    ):

        raise ValueError(
            label + " is required."
        )

    value = value.strip()

    if (
        not value.startswith("data:image/")
        or ";base64," not in value
    ):

        raise ValueError(
            label + " is not a valid base64 image."
        )

    header, encoded = value.split(
        ";base64,",
        1
    )

    mime_type = header[5:].strip().lower()

    if mime_type not in {
        "image/jpeg",
        "image/png",
        "image/webp"
    }:

        raise ValueError(
            label + " must be JPEG, PNG, or WEBP."
        )

    if not encoded.strip():

        raise ValueError(
            label + " contains no image data."
        )

    try:

        decoded = base64.b64decode(
            encoded,
            validate=False
        )

    except Exception as exc:

        raise ValueError(
            label + " contains invalid base64 data."
        ) from exc

    if not decoded:

        raise ValueError(
            label + " is empty."
        )

    return {
        "mime_type": mime_type,
        "data": encoded
    }


# ============================================================
# ANALYSIS PROMPT
# ============================================================

def build_prompt(
    instrument,
    trade_focus
):

    return f"""
You are LM ANALYZER, the professional chart-analysis engine
for LAMAR TRADING BOT.

You are given TWO actual trading chart screenshots.

IMAGE 1 = 4H chart.
IMAGE 2 = 15M chart.

Instrument: {instrument}
Trade focus: {trade_focus}

============================================================
PRIMARY INSTRUCTION
============================================================

ACTUALLY INSPECT BOTH IMAGES.

Do not answer from the text prompt alone.

Read the visible candles, market structure, price action,
support/resistance and price labels where they are readable.

The purpose of this analysis is to determine whether the
visible chart currently presents a defensible BUY setup,
SELL setup, or genuinely NO TRADE situation.

Do NOT automatically choose NO TRADE simply because the setup
is not perfect.

Trading setups are allowed to have normal pullbacks,
retracements, liquidity events, imperfect confirmations,
and areas of uncertainty.

Use the strongest visible evidence.

============================================================
STEP 1 — ANALYZE THE 4H CHART
============================================================

Determine:

- overall market direction
- higher highs / higher lows
- lower highs / lower lows
- range conditions
- recent break of structure
- CHoCH where visible
- major support
- major resistance
- liquidity pools
- important swing high
- important swing low
- displacement
- major order block if visible
- fair value gap if visible
- premium/discount location if meaningful
- current price position relative to important levels

Give a clear 4H structural conclusion:

BULLISH
BEARISH
RANGE
or UNCLEAR

Do not call the 4H UNCLEAR merely because there are
minor conflicting candles.

============================================================
STEP 2 — ANALYZE THE 15M CHART
============================================================

Determine:

- current short-term direction
- latest market structure
- BOS
- CHoCH
- liquidity sweep
- rejection
- displacement
- pullback/retest
- support/resistance
- order block
- fair value gap
- candlestick confirmation
- current price location
- whether the 15M chart confirms or invalidates the 4H idea

Give a clear 15M structural conclusion:

BULLISH
BEARISH
RANGE
or UNCLEAR

============================================================
STEP 3 — DETERMINE ALIGNMENT
============================================================

Compare the 4H and 15M.

Use:

ALIGNED
PARTIALLY ALIGNED
CONFLICTING
or INSUFFICIENT DATA

Important:

A 4H bullish structure with a temporary 15M bearish
retracement is NOT automatically conflicting.

A 4H bearish structure with a temporary 15M bullish
retracement is NOT automatically conflicting.

A lower-timeframe pullback can provide an opportunity
to enter in the direction of the higher-timeframe structure.

Look for whether the 15M is:

- continuing the 4H trend
- retracing into a meaningful area
- sweeping liquidity before continuation
- breaking back in the 4H direction
- rejecting an important level

============================================================
STEP 4 — DETERMINE DIRECTIONAL BIAS
============================================================

Based on the combined evidence determine:

BUY
SELL
or NEUTRAL

The directional bias should represent the strongest
defensible direction visible on the charts.

Do NOT make the bias neutral simply because every technical
method does not agree.

Technical methods can disagree while the overall market
structure still provides a directional bias.

============================================================
STEP 5 — DETERMINE TRADE DECISION
============================================================

The final signal must be:

BUY
SELL
or NO TRADE

Use BUY when:

- the overall evidence supports bullish direction
- there is sufficient visible confirmation
- the entry can be based on a visible price level or
  defensible price action
- there is no major unresolved contradiction

Use SELL when:

- the overall evidence supports bearish direction
- there is sufficient visible confirmation
- the entry can be based on a visible price level or
  defensible price action
- there is no major unresolved contradiction

Use NO TRADE ONLY when:

- the charts genuinely cannot be read sufficiently
OR
- the market is genuinely directionless/ranging with no
  defensible setup
OR
- the 4H and 15M evidence creates a major unresolved conflict
OR
- exact execution levels cannot reasonably be identified
  from the visible chart

Do NOT use NO TRADE merely because:

- one candle disagrees with the trend
- a minor method is conflicting
- the setup is not perfect
- the 15M is retracing
- there is normal market noise
- one technical concept is absent

============================================================
TRADE FOCUS
============================================================

SCALP:

Prioritize the 15M execution structure while respecting
the 4H directional context.

DAY TRADE:

Balance the 4H structure with 15M confirmation.

SWING:

Prioritize the 4H structure and use 15M for confirmation.

============================================================
ENTRY / SL / TP RULES
============================================================

Never invent exact prices.

Only provide numerical entry, stop loss and target levels
when they can be reasonably read or derived from visible
chart levels.

For a BUY:

Entry should be near a defensible visible support,
retest, breakout/retest, liquidity event, order block,
FVG or other clearly visible execution area.

Stop loss should be beyond a defensible invalidation point.

Targets should be based on visible liquidity, support,
resistance, swing levels or other defensible chart levels.

For a SELL:

Entry should be near a defensible visible resistance,
retest, breakdown/retest, liquidity event, order block,
FVG or other clearly visible execution area.

Stop loss should be beyond a defensible invalidation point.

Targets should be based on visible liquidity, support,
resistance, swing levels or other defensible chart levels.

If exact numerical prices genuinely cannot be read:

Use N/A for the exact numerical fields.

Do NOT fabricate prices.

============================================================
CONFIDENCE
============================================================

Confidence is an ANALYSIS CONFIDENCE score from 0 to 100.

It is NOT a probability of profit.

Use the following general guidance:

80-100:
Strong visible structure, strong alignment and clear
execution evidence.

65-79:
Good directional evidence with some manageable uncertainty.

50-64:
Moderate evidence but important uncertainty remains.

25-49:
Weak or conflicting evidence.

0-24:
Charts are genuinely unreadable or there is almost no
defensible analytical evidence.

IMPORTANT:

Do NOT automatically assign 0% to NO TRADE.

A NO TRADE caused by conflicting or incomplete confirmation
can still have a meaningful analysis confidence score.

Example:

4H clearly bearish, 15M bullish retracement,
no confirmed bearish continuation yet.

This can be:

TREND = BEARISH
BIAS = SELL
SIGNAL = NO TRADE
CONFIDENCE = 65

because the market analysis is clear even though execution
confirmation is currently insufficient.

============================================================
NEWS
============================================================

You do NOT have live news access through this request.

Do not claim that you checked ForexFactory, Investing.com,
Reuters, Bloomberg or any other live news source.

The news_fundamental_risk field must clearly state that
live news was not checked and, where appropriate, mention
that scheduled fundamental events could affect the setup.

============================================================
METHODS
============================================================

Analyze where meaningful:

- support/resistance
- pure price action
- market structure
- liquidity
- liquidity sweeps
- BOS
- CHoCH
- candlesticks
- SMC
- order blocks
- Fair Value Gaps
- Fibonacci
- premium/discount
- displacement
- inducement
- mitigation
- invalidation

Do not require every method to agree.

The overall market structure and price action should carry
the greatest weight.

Strategy names should be discussed inside the explanation.

============================================================
NO TRADE REQUIREMENT
============================================================

Even when the final signal is NO TRADE, you MUST still
populate ALL of these:

higher_timeframe_context
lower_timeframe_confirmation
data_analysis
explanation
news_fundamental_risk

These fields must describe the actual chart evidence.

Do NOT return N/A for the entire analysis.

Only execution-specific fields may be N/A when there is
no valid trade.

============================================================
FINAL INTERNAL CHECK
============================================================

Before returning the JSON, check:

1. Did I actually inspect the 4H image?
2. Did I actually inspect the 15M image?
3. Did I identify the 4H structure?
4. Did I identify the 15M structure?
5. Did I determine alignment?
6. Did I determine a directional bias?
7. Did I decide BUY, SELL or NO TRADE based on evidence?
8. If NO TRADE, did I explain exactly why?
9. Did I avoid invented prices?
10. Did I populate the analysis fields?
11. Did I avoid claiming live news access?

Return ONLY valid JSON.

Return exactly this structure:

{{
  "signal": "BUY | SELL | NO TRADE",

  "confidence": 0,

  "instrument": "{instrument}",

  "trend": "BULLISH | BEARISH | RANGE | UNCLEAR",

  "trade_idea": "BUY {instrument} | SELL {instrument} | NO TRADE",

  "entry": "N/A",

  "stop_loss": "N/A",

  "take_profit_1": "N/A",

  "take_profit_2": "N/A",

  "risk_reward": "N/A",

  "duration": "N/A",

  "higher_timeframe_context": "",

  "lower_timeframe_confirmation": "",

  "data_analysis": "",

  "explanation": "",

  "contributing_methods": [],

  "weak_methods": [],

  "conflicting_methods": [],

  "news_fundamental_risk": "",

  "warnings": []
}}
""".strip()


# ============================================================
# GEMINI RESPONSE SCHEMA
# ============================================================

OUTPUT_SCHEMA = {

    "type": "object",

    "properties": {

        "signal": {
            "type": "string",
            "enum": [
                "BUY",
                "SELL",
                "NO TRADE"
            ]
        },

        "confidence": {
            "type": "number"
        },

        "instrument": {
            "type": "string"
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
        },
    },

    "required": [
        "signal",
        "confidence",
        "instrument",
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
        "warnings",
    ],
}


# ============================================================
# ERROR CLASSIFICATION
# ============================================================

def is_transient_error(status_code):

    return status_code in {
        408,
        429,
        500,
        502,
        503,
        504,
    }


# ============================================================
# SINGLE GEMINI REQUEST
# ============================================================

def send_gemini_request(
    model,
    instrument,
    trade_focus,
    higher,
    lower
):

    if not GEMINI_API_KEY:

        raise RuntimeError(
            "Gemini key is missing from Vercel. "
            "Set GEMINI_OPENAI_KEY."
        )

    url = (
        GEMINI_BASE_URL
        + model
        + ":generateContent"
    )

    payload = {

        "system_instruction": {
            "parts": [
                {
                    "text": (
                        "You are LM ANALYZER. "
                        "You are a professional "
                        "multimodal chart-analysis engine. "
                        "You MUST inspect both supplied "
                        "chart images. "
                        "Determine 4H structure first, "
                        "then 15M structure, then alignment, "
                        "then directional bias, then final "
                        "trade decision. "
                        "Do not default to NO TRADE merely "
                        "because the setup is imperfect. "
                        "Do not invent prices. "
                        "A NO TRADE decision must still "
                        "contain a complete analysis."
                    )
                }
            ]
        },

        "contents": [

            {
                "role": "user",

                "parts": [

                    {
                        "text": build_prompt(
                            instrument,
                            trade_focus
                        )
                    },

                    {
                        "inline_data": {
                            "mime_type":
                                higher["mime_type"],
                            "data":
                                higher["data"],
                        }
                    },

                    {
                        "inline_data": {
                            "mime_type":
                                lower["mime_type"],
                            "data":
                                lower["data"],
                        }
                    },
                ]
            }
        ],

        "generationConfig": {

            "responseMimeType":
                "application/json",

            "responseSchema":
                OUTPUT_SCHEMA,

            "temperature":
                0.25,

            "maxOutputTokens":
                5000,
        },
    }

    body = json.dumps(
        payload,
        separators=(",", ":")
    ).encode("utf-8")

    request = urllib.request.Request(

        url,

        data=body,

        method="POST",

        headers={
            "Content-Type":
                "application/json",

            "Accept":
                "application/json",

            "x-goog-api-key":
                GEMINI_API_KEY,
        },
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=GEMINI_TIMEOUT_SECONDS
        ) as response:

            raw = (
                response
                .read()
                .decode(
                    "utf-8",
                    errors="replace"
                )
            )

        return json.loads(raw)

    except urllib.error.HTTPError as exc:

        detail = (
            exc.read()
            .decode(
                "utf-8",
                errors="replace"
            )
        )

        try:

            obj = json.loads(detail)

            message = str(
                obj.get(
                    "error",
                    {}
                ).get(
                    "message"
                )
                or detail[:2000]
            )

        except Exception:

            message = detail[:2000]

        error = RuntimeError(
            "Gemini "
            + str(exc.code)
            + ": "
            + message
        )

        error.http_status = exc.code
        error.gemini_message = message

        raise error from exc

    except (
        urllib.error.URLError,
        TimeoutError
    ) as exc:

        error = RuntimeError(
            "Gemini connection error: "
            + str(exc)
        )

        error.http_status = 503

        raise error from exc


# ============================================================
# PARSE JSON
# ============================================================

def parse_analysis_json(text):

    if not isinstance(text, str):

        raise RuntimeError(
            "Gemini analysis content was not text."
        )

    cleaned = text.strip()

    if not cleaned:

        raise RuntimeError(
            "Gemini returned empty analysis content."
        )

    try:

        value = json.loads(cleaned)

        if isinstance(value, dict):

            return value

    except json.JSONDecodeError:
        pass

    if cleaned.startswith("```"):

        lines = cleaned.splitlines()

        if (
            lines
            and lines[0].strip().startswith("```")
        ):

            lines = lines[1:]

        if (
            lines
            and lines[-1].strip() == "```"
        ):

            lines = lines[:-1]

        cleaned = "\n".join(
            lines
        ).strip()

    if cleaned.lower().startswith("json"):

        cleaned = cleaned[4:].strip()

    try:

        value = json.loads(cleaned)

        if isinstance(value, dict):

            return value

    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")

    if start >= 0 and end > start:

        try:

            value = json.loads(
                cleaned[start:end + 1]
            )

            if isinstance(value, dict):

                return value

        except json.JSONDecodeError:
            pass

    raise RuntimeError(
        "Gemini returned invalid analysis JSON."
    )


# ============================================================
# EXTRACT GEMINI ANALYSIS
# ============================================================

def extract_gemini_analysis(response):

    if not isinstance(response, dict):

        raise RuntimeError(
            "Gemini returned an invalid response."
        )

    candidates = response.get(
        "candidates",
        []
    )

    if (
        not isinstance(candidates, list)
        or not candidates
    ):

        prompt_feedback = response.get(
            "promptFeedback"
        )

        if prompt_feedback:

            raise RuntimeError(
                "Gemini returned no analysis candidates. "
                + json.dumps(
                    prompt_feedback,
                    ensure_ascii=False
                )
            )

        raise RuntimeError(
            "Gemini returned no analysis candidates."
        )

    candidate = candidates[0]

    if not isinstance(candidate, dict):

        raise RuntimeError(
            "Gemini returned an invalid candidate."
        )

    content = candidate.get(
        "content",
        {}
    )

    if not isinstance(content, dict):

        raise RuntimeError(
            "Gemini returned no analysis content."
        )

    parts = content.get(
        "parts",
        []
    )

    if (
        not isinstance(parts, list)
        or not parts
    ):

        finish_reason = candidate.get(
            "finishReason",
            "UNKNOWN"
        )

        raise RuntimeError(
            "Gemini returned no analysis text. "
            "Finish reason: "
            + str(finish_reason)
        )

    text_parts = []

    for part in parts:

        if not isinstance(part, dict):
            continue

        part_text = part.get(
            "text"
        )

        if (
            isinstance(part_text, str)
            and part_text.strip()
        ):

            text_parts.append(
                part_text.strip()
            )

    if not text_parts:

        finish_reason = candidate.get(
            "finishReason",
            "UNKNOWN"
        )

        raise RuntimeError(
            "Gemini returned no readable analysis. "
            "Finish reason: "
            + str(finish_reason)
        )

    analysis_text = "\n".join(
        text_parts
    ).strip()

    return parse_analysis_json(
        analysis_text
    )


# ============================================================
# VALIDATE ANALYSIS CONTENT
# ============================================================

def validate_analysis_content(
    result
):

    if not isinstance(result, dict):

        raise RuntimeError(
            "Gemini analysis is not a valid object."
        )

    required_fields = [

        "signal",

        "confidence",

        "instrument",

        "trend",

        "trade_idea",

        "higher_timeframe_context",

        "lower_timeframe_confirmation",

        "data_analysis",

        "explanation",

        "news_fundamental_risk",
    ]

    missing = []

    for field in required_fields:

        if field not in result:

            missing.append(field)

    if missing:

        raise RuntimeError(
            "Gemini returned incomplete analysis. "
            "Missing: "
            + ", ".join(missing)
        )

    # Do not allow the model to return a blank
    # analysis disguised as NO TRADE.

    descriptive_fields = [

        "higher_timeframe_context",

        "lower_timeframe_confirmation",

        "data_analysis",

        "explanation",
    ]

    blank_fields = []

    for field in descriptive_fields:

        value = result.get(field)

        if (
            value is None
            or not str(value).strip()
            or str(value).strip().upper() == "N/A"
        ):

            blank_fields.append(field)

    if blank_fields:

        raise RuntimeError(
            "Gemini returned a NO TRADE or incomplete "
            "analysis without chart reasoning. "
            "Blank fields: "
            + ", ".join(blank_fields)
        )

    return result


# ============================================================
# RESILIENT GEMINI ENGINE
# ============================================================

def call_gemini(
    instrument,
    trade_focus,
    higher,
    lower
):

    last_error = None

    total_models = len(
        GEMINI_MODELS
    )

    for model_index, model in enumerate(
        GEMINI_MODELS
    ):

        for retry_number in range(
            MAX_TRANSIENT_RETRIES + 1
        ):

            try:

                raw_response = send_gemini_request(
                    model=model,
                    instrument=instrument,
                    trade_focus=trade_focus,
                    higher=higher,
                    lower=lower,
                )

                actual_analysis = (
                    extract_gemini_analysis(
                        raw_response
                    )
                )

                validate_analysis_content(
                    actual_analysis
                )

                actual_analysis[
                    "_engine_model"
                ] = model

                return actual_analysis

            except Exception as exc:

                last_error = exc

                status = getattr(
                    exc,
                    "http_status",
                    None
                )

                # If Gemini actually answered but the
                # answer was malformed/incomplete, do not
                # silently turn it into NO TRADE.

                if status is None:

                    raise

                if not is_transient_error(
                    status
                ):

                    raise

                if retry_number < MAX_TRANSIENT_RETRIES:

                    base_delay = 1.0 * (
                        2 ** retry_number
                    )

                    jitter = random.uniform(
                        0.2,
                        0.8
                    )

                    time.sleep(
                        base_delay + jitter
                    )

                    continue

                break

        if model_index < total_models - 1:

            time.sleep(
                random.uniform(
                    0.15,
                    0.45
                )
            )

    if last_error is not None:

        status = getattr(
            last_error,
            "http_status",
            None
        )

        if status in {
            408,
            429,
            500,
            502,
            503,
            504
        }:

            raise RuntimeError(
                "The chart-analysis service is "
                "temporarily at capacity. "
                "All available Gemini analysis "
                "models were attempted. "
                "Please retry shortly."
            )

        raise last_error

    raise RuntimeError(
        "Gemini analysis failed."
    )


# ============================================================
# NORMALIZE RESULT
# ============================================================

def normalize_result(
    result,
    instrument
):

    if not isinstance(result, dict):

        raise RuntimeError(
            "Gemini analysis is not a valid object."
        )

    signal = str(
        result.get(
            "signal",
            "NO TRADE"
        )
    ).strip().upper()

    if signal not in {
        "BUY",
        "SELL",
        "NO TRADE"
    }:

        signal = "NO TRADE"

    try:

        confidence = float(
            result.get(
                "confidence",
                0
            )
        )

    except Exception:

        confidence = 0.0

    confidence = max(
        0.0,
        min(
            100.0,
            confidence
        )
    )

    trend = str(
        result.get(
            "trend",
            "UNCLEAR"
        )
    ).strip().upper()

    if trend not in {
        "BULLISH",
        "BEARISH",
        "RANGE",
        "UNCLEAR"
    }:

        trend = "UNCLEAR"

    def text(name):

        value = result.get(name)

        if value is None:

            return "N/A"

        if isinstance(value, str):

            cleaned = value.strip()

            if not cleaned:

                return "N/A"

            return cleaned

        return str(value)

    def list_value(name):

        value = result.get(name)

        if not isinstance(
            value,
            list
        ):

            return []

        return [
            str(x).strip()
            for x in value
            if str(x).strip()
        ]

    output = {

        "signal":
            signal,

        "confidence":
            confidence,

        "instrument":
            instrument,

        "trend":
            trend,

        "trade_idea":
            (
                "BUY " + instrument
                if signal == "BUY"
                else
                "SELL " + instrument
                if signal == "SELL"
                else
                "NO TRADE"
            ),

        "entry":
            text("entry"),

        "stop_loss":
            text("stop_loss"),

        "take_profit_1":
            text("take_profit_1"),

        "take_profit_2":
            text("take_profit_2"),

        "risk_reward":
            text("risk_reward"),

        "duration":
            text("duration"),

        "higher_timeframe_context":
            text(
                "higher_timeframe_context"
            ),

        "lower_timeframe_confirmation":
            text(
                "lower_timeframe_confirmation"
            ),

        "data_analysis":
            text(
                "data_analysis"
            ),

        "explanation":
            text(
                "explanation"
            ),

        "contributing_methods":
            list_value(
                "contributing_methods"
            ),

        "weak_methods":
            list_value(
                "weak_methods"
            ),

        "conflicting_methods":
            list_value(
                "conflicting_methods"
            ),

        "news_fundamental_risk":
            text(
                "news_fundamental_risk"
            ),

        "warnings":
            list_value(
                "warnings"
            ),
    }

    # Only execution fields become N/A for NO TRADE.
    #
    # We deliberately preserve:
    # - trend
    # - confidence
    # - 4H analysis
    # - 15M analysis
    # - data analysis
    # - explanation
    # - news/fundamental risk

    if signal == "NO TRADE":

        output["entry"] = "N/A"

        output["stop_loss"] = "N/A"

        output["take_profit_1"] = "N/A"

        output["take_profit_2"] = "N/A"

        output["risk_reward"] = "N/A"

    return output


# ============================================================
# HISTORY ENCODING
# ============================================================

def encode_result(result):

    raw = json.dumps(
        result,
        ensure_ascii=False,
        separators=(",", ":")
    ).encode("utf-8")

    return (
        base64
        .urlsafe_b64encode(raw)
        .decode("ascii")
        .rstrip("=")
    )


# ============================================================
# SAVE COMPLETED JOB
# ============================================================

def create_completed_job(
    job_id,
    user_id,
    instrument,
    trade_focus,
    encoded_result
):

    url = (
        get_supabase_url()
        .rstrip("/")
        + "/rest/v1/analysis_jobs"
    )

    key = (
        get_supabase_service_key()
        .strip()
    )

    payload = {

        "id":
            job_id,

        "user_id":
            user_id,

        "instrument":
            instrument,

        "trade_focus":
            trade_focus,

        "created_at":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "status":
            "completed",

        "openai_response_id":
            encoded_result,
    }

    request = urllib.request.Request(

        url,

        data=json.dumps(
            payload,
            separators=(",", ":")
        ).encode("utf-8"),

        method="POST",

        headers={

            "apikey":
                key,

            "Authorization":
                "Bearer " + key,

            "Content-Type":
                "application/json",

            "Accept":
                "application/json",

            "Prefer":
                "return=minimal",
        },
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=20
        ) as response:

            response.read()

    except urllib.error.HTTPError as exc:

        detail = (
            exc.read()
            .decode(
                "utf-8",
                errors="replace"
            )
        )

        raise RuntimeError(
            "Supabase job creation error: "
            + detail[:3000]
        ) from exc

    except (
        urllib.error.URLError,
        TimeoutError
    ) as exc:

        raise RuntimeError(
            "Supabase connection error: "
            + str(exc)
        ) from exc


# ============================================================
# HTTP HANDLER
# ============================================================

class handler(
    BaseHTTPRequestHandler
):

    def do_OPTIONS(self):

        self.send_response(204)

        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
        )

        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization"
        )

        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, POST, OPTIONS"
        )

        self.end_headers()


    def do_POST(self):

        reserved = False

        user_id = ""

        try:

            # ------------------------------------------------
            # AUTHENTICATION
            # ------------------------------------------------

            access_token = (
                extract_bearer_token(
                    self
                )
            )

            user = verify_access_token(
                access_token
            )

            user_id = str(
                user.get(
                    "id",
                    ""
                )
            ).strip()

            if not user_id:

                raise ValueError(
                    "Authenticated user ID is missing."
                )


            # ------------------------------------------------
            # REQUEST
            # ------------------------------------------------

            data = read_json(
                self
            )


            # ------------------------------------------------
            # VALIDATION
            # ------------------------------------------------

            instrument = validate_instrument(
                data.get(
                    "instrument"
                )
            )

            trade_focus = validate_focus(
                data.get(
                    "trade_focus"
                )
            )

            higher = validate_image(
                data.get(
                    "higher_timeframe_image"
                ),
                "4H chart"
            )

            lower = validate_image(
                data.get(
                    "lower_timeframe_image"
                ),
                "15M chart"
            )


            # ------------------------------------------------
            # DAILY LIMIT
            # ------------------------------------------------

            owner = is_owner_user(
                user
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
                        }
                    )

                    return

                reserved = True

                remaining = max(
                    0,
                    4 - int(slot)
                )

            else:

                remaining = None


            # ------------------------------------------------
            # GEMINI ANALYSIS
            # ------------------------------------------------

            try:

                raw_result = call_gemini(
                    instrument,
                    trade_focus,
                    higher,
                    lower
                )

                result = normalize_result(
                    raw_result,
                    instrument
                )

                job_id = str(
                    uuid.uuid4()
                )

                create_completed_job(
                    job_id,
                    user_id,
                    instrument,
                    trade_focus,
                    encode_result(
                        result
                    )
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


            reserved = False


            # ------------------------------------------------
            # SUCCESS
            # ------------------------------------------------

            json_response(
                self,
                202,
                {

                    "status":
                        "completed",

                    "job_id":
                        job_id,

                    "poll_after_seconds":
                        0,

                    "is_owner":
                        owner,

                    "daily_limit":
                        None
                        if owner
                        else 4,

                    "remaining":
                        remaining,
                }
            )


        # ----------------------------------------------------
        # CLIENT VALIDATION ERROR
        # ----------------------------------------------------

        except ValueError as exc:

            json_response(
                self,
                400,
                {
                    "error":
                        str(exc)
                }
            )


        # ----------------------------------------------------
        # EXPECTED BACKEND/GEMINI ERROR
        # ----------------------------------------------------

        except RuntimeError as exc:

            json_response(
                self,
                502,
                {
                    "error":
                        str(exc)
                }
            )


        # ----------------------------------------------------
        # UNEXPECTED ERROR
        # ----------------------------------------------------

        except Exception as exc:

            if reserved and user_id:

                try:

                    release_analysis_slot(
                        user_id
                    )

                except Exception:
                    pass

            json_response(
                self,
                500,
                {
                    "error":
                        "Analysis backend error: "
                        + str(exc)
                }
            )
