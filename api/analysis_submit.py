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

# IMPORTANT:
# Do not use GEMINI_MODEL from Vercel.
#
# These are real multimodal Gemini models.
# The order is intentional:
#
# 1. High quality Flash
# 2. Alternate Flash capacity
# 3. Previous-generation Flash
# 4. Stable high-throughput Flash
# 5. Cheap/high-volume Flash-Lite
# 6. Additional Flash-Lite fallback
#
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

# Keep individual requests short so a failed capacity attempt
# does not consume the entire Vercel execution window.
GEMINI_TIMEOUT_SECONDS = 10

# Number of retries for transient capacity/service errors
# on the SAME model.
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
            label
            + " must be JPEG, PNG, or WEBP."
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
            label
            + " contains invalid base64 data."
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
# PROMPT
# ============================================================

def build_prompt(
    instrument,
    trade_focus
):
    return f"""
You are LM ANALYZER, the professional chart-analysis engine for
LAMAR TRADING BOT.

Analyze TWO supplied trading chart screenshots.

IMAGE 1 = 4H higher timeframe.
IMAGE 2 = 15M lower timeframe.

Instrument: {instrument}
Trade focus: {trade_focus}

Use ONLY visible chart evidence.

Analyze:

- market structure
- support and resistance
- pure price action
- liquidity
- liquidity sweeps
- BOS
- CHoCH
- candlestick behavior
- Smart Money Concepts
- order blocks
- Fair Value Gaps
- Fibonacci where meaningful
- premium/discount
- displacement
- inducement
- mitigation
- invalidation
- 4H structure
- 15M confirmation

TIMEFRAME RULES:

SCALP:
Prioritize 15M execution confirmation while respecting 4H context.

DAY TRADE:
Balance 4H structure with 15M confirmation.

SWING:
Prioritize 4H structure and use 15M for confirmation.

IMPORTANT:

1. Signal must be BUY, SELL, or NO TRADE.

2. Never invent exact entry,
   stop-loss, or target prices.

3. Use only visible and defensible levels.

4. If price labels are unreadable,
   use NO TRADE.

5. If chart evidence is insufficient,
   use NO TRADE.

6. If 4H and 15M conflict without
   a defensible resolution,
   use NO TRADE.

7. Do not claim to have live news data.

8. Confidence is an analysis-confidence
   score from 0 to 100.
   It is NOT a probability of profit.

9. For NO TRADE:
   entry = "N/A"
   stop_loss = "N/A"
   take_profit_1 = "N/A"
   take_profit_2 = "N/A"
   risk_reward = "N/A"

10. Explain the actual visible evidence.
    Strategy names should be discussed
    inside the explanation rather than
    presented as separate result categories.

11. Never guarantee profit.

12. Return ONLY valid JSON.

Return this structure:

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
                        "You are a strict JSON "
                        "financial chart-analysis "
                        "service. "
                        "Follow the supplied schema. "
                        "Never invent prices."
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
                0.2,
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

            obj = json.loads(
                detail
            )

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

        # Two attempts maximum per model.
        for retry_number in range(
            MAX_TRANSIENT_RETRIES + 1
        ):

            try:

                result = send_gemini_request(
                    model=model,
                    instrument=instrument,
                    trade_focus=trade_focus,
                    higher=higher,
                    lower=lower,
                )

                # Record which model actually
                # answered, but do not expose
                # implementation details to
                # the Android app.
                if isinstance(result, dict):
                    result["_engine_model"] = model

                return result

            except Exception as exc:

                last_error = exc

                status = getattr(
                    exc,
                    "http_status",
                    None
                )

                # Non-transient errors should
                # NOT be hammered or sent
                # through pointless retries.
                if (
                    status is not None
                    and not is_transient_error(
                        status
                    )
                ):
                    raise

                # Connection errors are treated
                # as transient.
                if retry_number < MAX_TRANSIENT_RETRIES:

                    # Exponential backoff:
                    #
                    # first retry ≈ 1s
                    # plus random jitter.
                    #
                    # This follows Google's
                    # recommended retry strategy.
                    base_delay = 1.0 * (
                        2 ** retry_number
                    )

                    jitter = random.uniform(
                        0.2,
                        0.8
                    )

                    delay = (
                        base_delay
                        + jitter
                    )

                    time.sleep(
                        delay
                    )

                    continue

                # This model is unavailable.
                # Move immediately to the next
                # model instead of waiting forever.
                break

        # Optional tiny stagger before the next
        # model to avoid hammering multiple
        # capacity pools simultaneously.
        if model_index < total_models - 1:

            time.sleep(
                random.uniform(
                    0.15,
                    0.45
                )
            )

    # Every available model failed.
    #
    # Return a clean customer-facing message
    # instead of exposing internal model names.
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
# JSON PARSER
# ============================================================

def parse_analysis_json(text):

    cleaned = text.strip()

    try:

        value = json.loads(
            cleaned
        )

        if isinstance(value, dict):
            return value

    except json.JSONDecodeError:
        pass

    if cleaned.startswith(
        "```"
    ):

        lines = cleaned.splitlines()

        if (
            lines
            and lines[0]
            .strip()
            .startswith("```")
        ):
            lines = lines[1:]

        if (
            lines
            and lines[-1]
            .strip()
            == "```"
        ):
            lines = lines[:-1]

        cleaned = "\n".join(
            lines
        ).strip()

    if cleaned.lower().startswith(
        "json"
    ):
        cleaned = cleaned[4:].strip()

    try:

        value = json.loads(
            cleaned
        )

        if isinstance(value, dict):
            return value

    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")

    if start >= 0 and end > start:

        try:

            value = json.loads(
                cleaned[
                    start:end + 1
                ]
            )

            if isinstance(value, dict):
                return value

        except json.JSONDecodeError:
            pass

    raise RuntimeError(
        "Gemini returned invalid analysis JSON."
    )


# ============================================================
# NORMALIZE RESULT
# ============================================================

def normalize_result(
    result,
    instrument
):

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

        if isinstance(
            value,
            str
        ):

            return (
                value.strip()
                or "N/A"
            )

        return str(value)

    def list_value(name):

        value = result.get(
            name
        )

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
            text("data_analysis"),

        "explanation":
            text("explanation"),

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

        # History system already reads
        # completed results from here.
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
