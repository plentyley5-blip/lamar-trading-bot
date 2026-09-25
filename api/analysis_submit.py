import base64
import json
import os
import uuid
import urllib.error
import urllib.parse
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

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    + urllib.parse.quote(GEMINI_MODEL, safe=".-")
    + ":generateContent"
)

MAX_REQUEST_BYTES = 25 * 1024 * 1024
GEMINI_TIMEOUT = 80


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


def read_json(handler):
    try:
        length = int(
            handler.headers.get(
                "Content-Length",
                "0"
            )
        )
    except ValueError as exc:
        raise ValueError("Invalid request.") from exc

    if length <= 0:
        raise ValueError("Invalid chart request.")

    if length > MAX_REQUEST_BYTES:
        raise ValueError("Chart request is too large.")

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
        raise ValueError("Invalid request.")

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
        "SWING"
    }:
        raise ValueError(
            "Trade focus must be SCALP, DAY TRADE, or SWING."
        )

    return focus


def validate_image(value, name):
    if not isinstance(value, str):
        raise ValueError(
            name + " is required."
        )

    value = value.strip()

    if not value:
        raise ValueError(
            name + " is required."
        )

    if not value.startswith("data:image/"):
        raise ValueError(
            name + " is not a valid image."
        )

    if ";base64," not in value:
        raise ValueError(
            name + " must be base64 encoded."
        )

    header, encoded = value.split(
        ";base64,",
        1
    )

    mime_type = header[
        len("data:"):
    ].strip().lower()

    if mime_type not in {
        "image/jpeg",
        "image/png",
        "image/webp"
    }:
        raise ValueError(
            name + " must be JPEG, PNG or WEBP."
        )

    if not encoded.strip():
        raise ValueError(
            name + " contains no image data."
        )

    try:
        decoded = base64.b64decode(
            encoded,
            validate=False
        )
    except Exception as exc:
        raise ValueError(
            name + " contains invalid base64."
        ) from exc

    if not decoded:
        raise ValueError(
            name + " is empty."
        )

    return {
        "data_url": value,
        "mime_type": mime_type,
        "base64": encoded,
    }


def build_prompt(instrument, trade_focus):
    return f"""
You are LM ANALYZER, the professional chart-analysis engine for LAMAR Trading Bot.

Analyze TWO supplied chart screenshots.

Instrument: {instrument}
Trade focus: {trade_focus}

IMAGE 1 = 4H higher timeframe.
IMAGE 2 = 15M lower timeframe.

Use the visible chart evidence to analyze:

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
- multi-timeframe confluence

Trade-focus rules:

SCALP:
Prioritize 15M confirmation and execution.

DAY TRADE:
Balance 4H structure with 15M confirmation.

SWING:
Prioritize 4H structure and use 15M for confirmation.

IMPORTANT RULES:

1. Return only BUY, SELL, or NO TRADE.
2. Never invent entry, SL or TP prices.
3. Only use visible and defensible price levels.
4. If the chart prices are unreadable, use NO TRADE.
5. If evidence is insufficient, use NO TRADE.
6. If the timeframes conflict without a defensible resolution, use NO TRADE.
7. Do not pretend to have live news data.
8. Confidence is an analysis-confidence score from 0 to 100.
9. For NO TRADE, entry, SL, TP1, TP2 and RR must be "N/A".
10. Explain the actual chart evidence in the explanation.
11. Strategy/concept names should be explained inside the explanation rather than being presented as the main result.
12. Do not guarantee profit.

For BUY:
trade_idea must be "BUY {instrument}"

For SELL:
trade_idea must be "SELL {instrument}"

For NO TRADE:
trade_idea must be "NO TRADE"

Return ONLY valid JSON.

Use this exact JSON structure:

{{
  "signal": "BUY",
  "confidence": 0,
  "instrument": "{instrument}",
  "trend": "BULLISH",
  "trade_idea": "BUY {instrument}",
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
"""


def call_gemini(
    instrument,
    trade_focus,
    higher_image,
    lower_image
):
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is missing from Vercel."
        )

    payload = {
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
                        "inlineData": {
                            "mimeType": higher_image["mime_type"],
                            "data": higher_image["base64"]
                        }
                    },
                    {
                        "inlineData": {
                            "mimeType": lower_image["mime_type"],
                            "data": lower_image["base64"]
                        }
                    }
                ]
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "maxOutputTokens": 5000
        }
    }

    request = urllib.request.Request(
        GEMINI_URL,
        data=json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":")
        ).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-goog-api-key": GEMINI_API_KEY
        }
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=GEMINI_TIMEOUT
        ) as response:

            raw = response.read().decode(
                "utf-8",
                errors="replace"
            )

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(
            "utf-8",
            errors="replace"
        )

        try:
            error_json = json.loads(detail)
            message = (
                error_json
                .get("error", {})
                .get("message")
                or detail[:1500]
            )
        except Exception:
            message = detail[:1500]

        raise RuntimeError(
            "Gemini error: " + message
        ) from exc

    except (
        urllib.error.URLError,
        TimeoutError
    ) as exc:
        raise RuntimeError(
            "Gemini connection error: " + str(exc)
        ) from exc

    try:
        api_response = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Gemini returned invalid API JSON."
        ) from exc

    candidates = api_response.get(
        "candidates"
    )

    if not isinstance(
        candidates,
        list
    ) or not candidates:

        feedback = api_response.get(
            "promptFeedback"
        )

        if feedback:
            raise RuntimeError(
                "Gemini returned no analysis: "
                + json.dumps(
                    feedback,
                    ensure_ascii=False
                )
            )

        raise RuntimeError(
            "Gemini returned no analysis candidate."
        )

    candidate = candidates[0]

    content = candidate.get(
        "content",
        {}
    )

    parts = (
        content.get("parts", [])
        if isinstance(content, dict)
        else []
    )

    text_parts = []

    for part in parts:
        if not isinstance(
            part,
            dict
        ):
            continue

        text = part.get(
            "text"
        )

        if isinstance(
            text,
            str
        ):
            text_parts.append(text)

    text = "".join(
        text_parts
    ).strip()

    if not text:
        raise RuntimeError(
            "Gemini returned an empty analysis."
        )

    result = parse_gemini_json(
        text
    )

    return normalize_result(
        result,
        instrument
    )


def parse_gemini_json(text):
    try:
        result = json.loads(text)

        if isinstance(
            result,
            dict
        ):
            return result

    except json.JSONDecodeError:
        pass

    cleaned = text.strip()

    if cleaned.startswith("```"):
        lines = cleaned.splitlines()

        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]

        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        cleaned = "\n".join(
            lines
        ).strip()

    if cleaned.lower().startswith("json"):
        cleaned = cleaned[4:].strip()

    try:
        result = json.loads(cleaned)

        if isinstance(
            result,
            dict
        ):
            return result

    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")

    if start >= 0 and end > start:
        try:
            result = json.loads(
                cleaned[start:end + 1]
            )

            if isinstance(
                result,
                dict
            ):
                return result

        except json.JSONDecodeError:
            pass

    raise RuntimeError(
        "Gemini returned invalid analysis JSON."
    )


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
        confidence = 0

    confidence = max(
        0,
        min(
            100,
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

    if signal == "BUY":
        trade_idea = (
            f"BUY {instrument}"
        )
    elif signal == "SELL":
        trade_idea = (
            f"SELL {instrument}"
        )
    else:
        trade_idea = "NO TRADE"

    def text_value(name):
        value = result.get(name)

        if value is None:
            return "N/A"

        if isinstance(
            value,
            str
        ):
            return value.strip() or "N/A"

        return str(value)

    def list_value(name):
        value = result.get(name)

        if not isinstance(
            value,
            list
        ):
            return []

        output = []

        for item in value:
            item_text = str(
                item
            ).strip()

            if item_text:
                output.append(
                    item_text
                )

        return output

    output = {
        "signal": signal,
        "confidence": confidence,
        "instrument": instrument,
        "trend": trend,
        "trade_idea": trade_idea,
        "entry": text_value("entry"),
        "stop_loss": text_value("stop_loss"),
        "take_profit_1": text_value("take_profit_1"),
        "take_profit_2": text_value("take_profit_2"),
        "risk_reward": text_value("risk_reward"),
        "duration": text_value("duration"),
        "higher_timeframe_context": text_value(
            "higher_timeframe_context"
        ),
        "lower_timeframe_confirmation": text_value(
            "lower_timeframe_confirmation"
        ),
        "data_analysis": text_value(
            "data_analysis"
        ),
        "explanation": text_value(
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
        "news_fundamental_risk": text_value(
            "news_fundamental_risk"
        ),
        "warnings": list_value(
            "warnings"
        )
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
        separators=(",", ":")
    ).encode("utf-8")

    return (
        base64.urlsafe_b64encode(raw)
        .decode("ascii")
        .rstrip("=")
    )


def save_completed_job(
    job_id,
    user_id,
    instrument,
    trade_focus,
    higher_image,
    lower_image,
    result
):
    url = (
        get_supabase_url().rstrip("/")
        + "/rest/v1/analysis_jobs"
    )

    service_key = (
        get_supabase_service_key()
        .strip()
    )

    payload = {
        "id": job_id,
        "user_id": user_id,
        "instrument": instrument,
        "trade_focus": trade_focus,
        "created_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "status": "completed",

        # Historical column name.
        # Existing analysis_history.py reads this field.
        "openai_response_id": encode_result(
            result
        ),

        "higher_timeframe_image": (
            higher_image["data_url"]
        ),

        "lower_timeframe_image": (
            lower_image["data_url"]
        )
    }

    request = urllib.request.Request(
        url,
        data=json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":")
        ).encode("utf-8"),
        method="POST",
        headers={
            "apikey": service_key,
            "Authorization": (
                "Bearer " + service_key
            ),
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Prefer": "return=minimal"
        }
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=20
        ) as response:
            response.read()

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(
            "utf-8",
            errors="replace"
        )

        raise RuntimeError(
            "Supabase save error: "
            + detail[:2000]
        ) from exc

    except (
        urllib.error.URLError,
        TimeoutError
    ) as exc:
        raise RuntimeError(
            "Supabase connection error: "
            + str(exc)
        ) from exc


class handler(BaseHTTPRequestHandler):

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
            access_token = (
                extract_bearer_token(self)
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

            owner = is_owner_user(
                user
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

            higher_image = validate_image(
                data.get(
                    "higher_timeframe_image"
                ),
                "4H chart"
            )

            lower_image = validate_image(
                data.get(
                    "lower_timeframe_image"
                ),
                "15M chart"
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
                            "error": "Daily analysis limit reached.",
                            "daily_limit": 4,
                            "remaining": 0,
                            "is_owner": False
                        }
                    )
                    return

                reserved = True

            result = call_gemini(
                instrument,
                trade_focus,
                higher_image,
                lower_image
            )

            job_id = str(
                uuid.uuid4()
            )

            save_completed_job(
                job_id,
                user_id,
                instrument,
                trade_focus,
                higher_image,
                lower_image,
                result
            )

            reserved = False

            json_response(
                self,
                200,
                {
                    "status": "completed",
                    "job_id": job_id,
                    "result": result
                }
            )

        except ValueError as exc:
            json_response(
                self,
                400,
                {
                    "error": str(exc)
                }
            )

        except RuntimeError as exc:
            if reserved and user_id:
                try:
                    release_analysis_slot(
                        user_id
                    )
                except Exception:
                    pass

            # Return the actual backend reason.
            # This prevents the old generic 500 loop.
            json_response(
                self,
                502,
                {
                    "error": str(exc)
                }
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
                    "error": (
                        "Analysis backend error: "
                        + str(exc)
                    )
                }
            )
