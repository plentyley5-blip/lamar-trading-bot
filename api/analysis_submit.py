import base64
import json
import os
import time
import urllib.error
import urllib.request
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

# Use the user's existing Gemini credential.
# GEMINI_OPENAI_KEY is preferred, with GEMINI_API_KEY as fallback.
GEMINI_API_KEY = (
    os.environ.get("GEMINI_OPENAI_KEY", "").strip()
    or os.environ.get("GEMINI_API_KEY", "").strip()
)

# Fixed deliberately. No Vercel GEMINI_MODEL variable is used.
GEMINI_MODEL = "gemini-3.8-flash"

# Standard Gemini multimodal endpoint.
# This avoids the Gemini Interactions "agent" model path.
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    + GEMINI_MODEL
    + ":generateContent"
)

MAX_REQUEST_BYTES = 15 * 1024 * 1024
GEMINI_TIMEOUT_SECONDS = 45


def json_response(handler, status_code, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    handler.send_response(status_code)

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

    handler.end_headers()

    try:
        handler.wfile.write(body)
    except Exception:
        pass


def read_json(handler):
    try:
        length = int(
            handler.headers.get(
                "Content-Length",
                "0",
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
        "SWING",
    }:
        raise ValueError(
            "Trade focus must be SCALP, DAY TRADE, or SWING."
        )

    return focus


def validate_image(value, label):
    if not isinstance(
        value,
        str,
    ) or not value.strip():
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
        1,
    )

    mime_type = header[
        5:
    ].strip().lower()

    if mime_type not in {
        "image/jpeg",
        "image/png",
        "image/webp",
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
            validate=False,
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
        "data": encoded,
    }


def build_prompt(
    instrument,
    trade_focus,
):
    return f"""
You are LM ANALYZER, the chart-analysis engine for LAMAR TRADING BOT.

Analyze TWO supplied trading chart screenshots.

IMAGE 1 = 4H higher timeframe.
IMAGE 2 = 15M lower timeframe.

Instrument: {instrument}
Trade focus: {trade_focus}

Use only visible chart evidence.

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
- 4H and 15M confluence

SCALP:
Prioritize 15M confirmation.

DAY TRADE:
Balance 4H structure with 15M confirmation.

SWING:
Prioritize 4H structure and use 15M for confirmation.

IMPORTANT RULES:

1. Signal must be BUY, SELL, or NO TRADE.
2. Never invent exact entry, stop-loss, or target prices.
3. Use only visible and defensible levels.
4. If price labels are unreadable, use NO TRADE.
5. If evidence is insufficient, use NO TRADE.
6. If 4H and 15M conflict without a defensible resolution, use NO TRADE.
7. Do not claim to have live news data.
8. Confidence is an analysis-confidence score from 0 to 100.
9. For NO TRADE, entry, stop_loss, take_profit_1, take_profit_2,
   and risk_reward must be N/A.
10. Explain the evidence and methods inside the explanation.
11. Do not turn strategy names into separate result categories.
12. Do not guarantee profit.
13. Return only valid JSON.
14. Do not use markdown code fences.

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
            "type": "number",
        },
        "instrument": {
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
        "trade_idea": {
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


def call_gemini(
    instrument,
    trade_focus,
    higher,
    lower,
):
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "Gemini key is missing from Vercel. "
            "Set GEMINI_OPENAI_KEY."
        )

    payload = {
        "system_instruction": {
            "parts": [
                {
                    "text": (
                        "You are a strict JSON chart-analysis service. "
                        "Follow the user's schema and never invent prices."
                    ),
                },
            ],
        },

        "contents": [
            {
                "role": "user",

                "parts": [
                    {
                        "text": build_prompt(
                            instrument,
                            trade_focus,
                        ),
                    },

                    {
                        "inline_data": {
                            "mime_type": higher[
                                "mime_type"
                            ],
                            "data": higher[
                                "data"
                            ],
                        },
                    },

                    {
                        "inline_data": {
                            "mime_type": lower[
                                "mime_type"
                            ],
                            "data": lower[
                                "data"
                            ],
                        },
                    },
                ],
            },
        ],

        "generationConfig": {
            "responseMimeType": "application/json",

            "responseSchema": OUTPUT_SCHEMA,

            "temperature": 0.2,
        },
    }

    body = json.dumps(
        payload,
        separators=(",", ":"),
    ).encode("utf-8")

    request = urllib.request.Request(
        GEMINI_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-goog-api-key": GEMINI_API_KEY,
        },
    )

    last_message = (
        "Gemini request failed."
    )

    raw = None

    for attempt in range(2):

        try:
            with urllib.request.urlopen(
                request,
                timeout=GEMINI_TIMEOUT_SECONDS,
            ) as response:

                raw = response.read().decode(
                    "utf-8",
                    errors="replace",
                )

            break

        except urllib.error.HTTPError as exc:

            detail = exc.read().decode(
                "utf-8",
                errors="replace",
            )

            try:
                obj = json.loads(
                    detail
                )

                last_message = str(
                    obj.get(
                        "error",
                        {},
                    ).get(
                        "message"
                    )
                    or detail[:3000]
                )

            except Exception:
                last_message = detail[:3000]

            if (
                exc.code
                in {
                    429,
                    500,
                    502,
                    503,
                    504,
                }
                and attempt == 0
            ):
                time.sleep(1)
                continue

            raise RuntimeError(
                "Gemini error: "
                + last_message
            ) from exc

        except (
            urllib.error.URLError,
            TimeoutError,
        ) as exc:

            if attempt == 0:
                time.sleep(1)
                continue

            raise RuntimeError(
                "Gemini connection error: "
                + str(exc)
            ) from exc

    if raw is None:
        raise RuntimeError(
            "Gemini request failed: "
            + last_message
        )

    try:
        api_obj = json.loads(
            raw
        )

    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Gemini returned invalid API JSON."
        ) from exc

    candidates = api_obj.get(
        "candidates"
    )

    pieces = []

    if isinstance(
        candidates,
        list,
    ):

        for candidate in candidates:

            if not isinstance(
                candidate,
                dict,
            ):
                continue

            content = candidate.get(
                "content"
            )

            if not isinstance(
                content,
                dict,
            ):
                continue

            parts = content.get(
                "parts"
            )

            if not isinstance(
                parts,
                list,
            ):
                continue

            for part in parts:

                if (
                    isinstance(
                        part,
                        dict,
                    )
                    and isinstance(
                        part.get("text"),
                        str,
                    )
                ):

                    pieces.append(
                        part["text"]
                    )

    text = "\n".join(
        pieces
    ).strip()

    if not text:
        raise RuntimeError(
            "Gemini returned no analysis text."
        )

    return parse_analysis_json(
        text
    )


def parse_analysis_json(text):
    cleaned = text.strip()

    try:
        value = json.loads(
            cleaned
        )

        if isinstance(
            value,
            dict,
        ):
            return value

    except json.JSONDecodeError:
        pass

    if cleaned.startswith(
        "```"
    ):

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

    if cleaned.lower().startswith(
        "json"
    ):
        cleaned = cleaned[4:].strip()

    try:
        value = json.loads(
            cleaned
        )

        if isinstance(
            value,
            dict,
        ):
            return value

    except json.JSONDecodeError:
        pass

    start = cleaned.find(
        "{"
    )

    end = cleaned.rfind(
        "}"
    )

    if (
        start >= 0
        and end > start
    ):

        try:
            value = json.loads(
                cleaned[
                    start:end + 1
                ]
            )

            if isinstance(
                value,
                dict,
            ):
                return value

        except json.JSONDecodeError:
            pass

    raise RuntimeError(
        "Gemini returned invalid analysis JSON."
    )


def normalize_result(
    result,
    instrument,
):
    signal = str(
        result.get(
            "signal",
            "NO TRADE",
        )
    ).strip().upper()

    if signal not in {
        "BUY",
        "SELL",
        "NO TRADE",
    }:
        signal = "NO TRADE"

    try:
        confidence = float(
            result.get(
                "confidence",
                0,
            )
        )

    except Exception:
        confidence = 0.0

    confidence = max(
        0.0,
        min(
            100.0,
            confidence,
        ),
    )

    trend = str(
        result.get(
            "trend",
            "UNCLEAR",
        )
    ).strip().upper()

    if trend not in {
        "BULLISH",
        "BEARISH",
        "RANGE",
        "UNCLEAR",
    }:
        trend = "UNCLEAR"

    def text(name):
        value = result.get(
            name
        )

        if value is None:
            return "N/A"

        if isinstance(
            value,
            str,
        ):
            return (
                value.strip()
                or "N/A"
            )

        return str(
            value
        )

    def list_value(name):
        value = result.get(
            name
        )

        if not isinstance(
            value,
            list,
        ):
            return []

        return [
            str(x).strip()
            for x in value
            if str(x).strip()
        ]

    output = {
        "signal": signal,

        "confidence": confidence,

        "instrument": instrument,

        "trend": trend,

        "trade_idea": (
            "BUY " + instrument
            if signal == "BUY"
            else
            "SELL " + instrument
            if signal == "SELL"
            else
            "NO TRADE"
        ),

        "entry": text(
            "entry"
        ),

        "stop_loss": text(
            "stop_loss"
        ),

        "take_profit_1": text(
            "take_profit_1"
        ),

        "take_profit_2": text(
            "take_profit_2"
        ),

        "risk_reward": text(
            "risk_reward"
        ),

        "duration": text(
            "duration"
        ),

        "higher_timeframe_context": text(
            "higher_timeframe_context"
        ),

        "lower_timeframe_confirmation": text(
            "lower_timeframe_confirmation"
        ),

        "data_analysis": text(
            "data_analysis"
        ),

        "explanation": text(
            "explanation"
        ),

        "contributing_methods": list_value(
            "contributing_methods"
        ),

        "weak_methods": list_value(
            "weak_methods"
        ),

        "conflicting_methods": list_value(
            "conflicting_methods"
        ),

        "news_fundamental_risk": text(
            "news_fundamental_risk"
        ),

        "warnings": list_value(
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


def encode_result(result):
    raw = json.dumps(
        result,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    return (
        base64.urlsafe_b64encode(
            raw
        )
        .decode("ascii")
        .rstrip("=")
    )


def create_completed_job(
    job_id,
    user_id,
    instrument,
    trade_focus,
    encoded_result,
):
    url = (
        get_supabase_url().rstrip("/")
        + "/rest/v1/analysis_jobs"
    )

    key = get_supabase_service_key().strip()

    payload = {
        "id": job_id,

        "user_id": user_id,

        "instrument": instrument,

        "trade_focus": trade_focus,

        "created_at": datetime.now(
            timezone.utc
        ).isoformat(),

        "status": "completed",

        # History already reads completed results
        # from this field.
        "openai_response_id": encoded_result,
    }

    request = urllib.request.Request(
        url,
        data=json.dumps(
            payload,
            separators=(",", ":"),
        ).encode("utf-8"),
        method="POST",
        headers={
            "apikey": key,

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
            timeout=20,
        ) as response:

            response.read()

    except urllib.error.HTTPError as exc:

        detail = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            "Supabase job creation error: "
            + detail[:3000]
        ) from exc

    except (
        urllib.error.URLError,
        TimeoutError,
    ) as exc:

        raise RuntimeError(
            "Supabase connection error: "
            + str(exc)
        ) from exc


class handler(
    BaseHTTPRequestHandler
):

    def do_OPTIONS(self):

        self.send_response(
            204
        )

        self.send_header(
            "Access-Control-Allow-Origin",
            "*",
        )

        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization",
        )

        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, POST, OPTIONS",
        )

        self.end_headers()


    def do_POST(self):

        reserved = False

        user_id = ""

        try:

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
                    "",
                )
            ).strip()

            if not user_id:

                raise ValueError(
                    "Authenticated user ID is missing."
                )

            data = read_json(
                self
            )

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
                "4H chart",
            )

            lower = validate_image(
                data.get(
                    "lower_timeframe_image"
                ),
                "15M chart",
            )

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
                        },
                    )

                    return

                reserved = True

                remaining = max(
                    0,
                    4 - int(slot),
                )

            else:

                slot = None

                remaining = None


            try:

                raw_result = call_gemini(
                    instrument,
                    trade_focus,
                    higher,
                    lower,
                )

                result = normalize_result(
                    raw_result,
                    instrument,
                )

                job_id = str(
                    __import__(
                        "uuid"
                    ).uuid4()
                )

                create_completed_job(
                    job_id,

                    user_id,

                    instrument,

                    trade_focus,

                    encode_result(
                        result
                    ),
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
                },
            )


        except ValueError as exc:

            json_response(
                self,
                400,
                {
                    "error":
                        str(exc)
                },
            )


        except RuntimeError as exc:

            json_response(
                self,
                502,
                {
                    "error":
                        str(exc)
                },
            )


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
                },
            )
