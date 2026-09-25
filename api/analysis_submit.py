import os
import json
import base64
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from user_security import (
    extract_bearer_token,
    verify_access_token,
    get_supabase_url,
    get_supabase_service_key,
    reserve_analysis_slot,
    release_analysis_slot,
)


GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash").strip()

GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

MAX_BODY_BYTES = 15_000_000
REQUEST_TIMEOUT_SECONDS = 85


def send_json(handler, status_code, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
    handler.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def read_json_body(handler):
    length_header = handler.headers.get("Content-Length", "0")

    try:
        length = int(length_header)
    except ValueError:
        raise ValueError("Invalid request length.")

    if length <= 0:
        raise ValueError("Request body is empty.")

    if length > MAX_BODY_BYTES:
        raise ValueError("Request is too large.")

    raw = handler.rfile.read(length)

    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        raise ValueError("Invalid JSON request.")


def clean_data_url(value):
    if not isinstance(value, str):
        raise ValueError("Chart image is missing.")

    value = value.strip()

    if not value.startswith("data:image/"):
        raise ValueError("Invalid chart image format.")

    marker = ";base64,"
    if marker not in value:
        raise ValueError("Chart image must be base64 encoded.")

    header, encoded = value.split(marker, 1)

    mime_type = header[len("data:"):].strip()

    allowed_types = {
        "image/jpeg",
        "image/png",
        "image/webp",
    }

    if mime_type not in allowed_types:
        raise ValueError("Only JPEG, PNG or WEBP chart images are supported.")

    if not encoded:
        raise ValueError("Chart image data is empty.")

    return mime_type, encoded


def decode_image_for_validation(encoded):
    try:
        return base64.b64decode(encoded, validate=False)
    except Exception:
        raise ValueError("Chart image contains invalid base64 data.")


def build_prompt(instrument, trade_focus):
    return f"""
You are the trading-analysis engine for LAMAR Trading Bot.

Analyze the TWO chart screenshots supplied with this request.

INSTRUMENT:
{instrument}

TRADE FOCUS:
{trade_focus}

TIMEFRAMES:
- Image 1 = Higher timeframe: 4H
- Image 2 = Lower timeframe: 15M

IMPORTANT:
The 4H chart is the higher-timeframe context.
The 15M chart is the lower-timeframe confirmation.

Use price action and chart evidence only.

Analyze:
- Market structure
- Support and resistance
- Liquidity and liquidity sweeps
- Candlestick behavior
- Fibonacci where meaningful
- Smart Money Concepts
- Order blocks where visible
- Fair Value Gaps where visible
- Premium/discount where meaningful
- Trend
- Breaks of structure
- Change of character
- Retests and confirmations
- Confluence between the 4H and 15M charts
- Visible price levels
- Risk/reward
- Potential fundamental/news risk only when relevant

CRITICAL PRICE RULE:
Never invent entry, stop loss or take profit prices.
Use only prices that can reasonably be identified from the supplied charts.
If the screenshots do not provide enough evidence to produce reliable levels, return NO TRADE.

CRITICAL SIGNAL RULE:
The signal must be exactly one of:
BUY
SELL
NO TRADE

When there is insufficient evidence, conflicting evidence, unreadable prices, or poor confirmation,
use NO TRADE instead of guessing.

CONFIDENCE:
Return a numeric percentage from 0 to 100.

TRADE IDEA:
For BUY use:
"BUY {instrument}"

For SELL use:
"SELL {instrument}"

For NO TRADE use:
"NO TRADE"

RR RATIO:
Return a human-readable value such as "1:2.5".

EXPLANATION:
Explain the actual reasons supporting the decision.
Explain the useful methods/concepts inside the explanation.
Do not simply list strategy names without explaining what the chart showed.
Also explain conflicting or weak evidence.

DATA ANALYSIS:
Give a concise but meaningful description of what the charts actually showed.

NEWS/FUNDAMENTAL RISK:
Do not pretend to have live news if no live news data is available.
Instead say "Not evaluated from live news data" when appropriate.

OUTPUT:
Return ONLY valid JSON.
Do not use markdown.
Do not wrap the JSON in ```.

Use exactly these fields:

{
  "signal": "BUY | SELL | NO TRADE",
  "confidence": 0,
  "instrument": "{instrument}",
  "trend": "BULLISH | BEARISH | RANGE | UNCLEAR",
  "trade_idea": "BUY {instrument} | SELL {instrument} | NO TRADE",
  "entry": "price or N/A",
  "stop_loss": "price or N/A",
  "take_profit_1": "price or N/A",
  "take_profit_2": "price or N/A",
  "risk_reward": "value or N/A",
  "duration": "estimated duration or N/A",
  "higher_timeframe_context": "4H analysis",
  "lower_timeframe_confirmation": "15M analysis",
  "data_analysis": "chart evidence",
  "explanation": "full reasoning",
  "contributing_methods": [],
  "weak_methods": [],
  "conflicting_methods": [],
  "news_fundamental_risk": "risk statement",
  "warnings": []
}

Do not fabricate missing information.
"""


def extract_gemini_text(response_json):
    candidates = response_json.get("candidates")

    if not isinstance(candidates, list) or not candidates:
        raise ValueError("Gemini returned no analysis candidate.")

    first = candidates[0]

    content = first.get("content", {})
    parts = content.get("parts", [])

    pieces = []

    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    pieces.append(text)

    result = "".join(pieces).strip()

    if not result:
        raise ValueError("Gemini returned an empty analysis.")

    return result


def parse_json_result(text):
    text = text.strip()

    if text.startswith("```"):
        lines = text.splitlines()

        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]

        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        text = "\n".join(lines).strip()

        if text.lower().startswith("json"):
            text = text[4:].strip()

    try:
        result = json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")

        if start == -1 or end == -1 or end <= start:
            raise ValueError("Gemini did not return valid JSON.")

        try:
            result = json.loads(text[start:end + 1])
        except Exception:
            raise ValueError("Gemini returned invalid JSON.")

    if not isinstance(result, dict):
        raise ValueError("Gemini JSON result is not an object.")

    return result


def normalize_result(result, instrument):
    required_fields = [
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
    ]

    normalized = {}

    for field in required_fields:
        normalized[field] = result.get(field)

    signal = str(normalized.get("signal") or "").upper().strip()

    if signal not in {"BUY", "SELL", "NO TRADE"}:
        signal = "NO TRADE"

    normalized["signal"] = signal
    normalized["instrument"] = instrument

    try:
        confidence = float(normalized.get("confidence", 0))
    except Exception:
        confidence = 0

    confidence = max(0, min(100, confidence))
    normalized["confidence"] = confidence

    trend = str(normalized.get("trend") or "").upper().strip()

    if trend not in {"BULLISH", "BEARISH", "RANGE", "UNCLEAR"}:
        trend = "UNCLEAR"

    normalized["trend"] = trend

    if signal == "BUY":
        normalized["trade_idea"] = f"BUY {instrument}"

    elif signal == "SELL":
        normalized["trade_idea"] = f"SELL {instrument}"

    else:
        normalized["trade_idea"] = "NO TRADE"

    string_fields = [
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
        "news_fundamental_risk",
    ]

    for field in string_fields:
        value = normalized.get(field)

        if value is None:
            normalized[field] = "N/A"
        elif not isinstance(value, str):
            normalized[field] = str(value)

    list_fields = [
        "contributing_methods",
        "weak_methods",
        "conflicting_methods",
        "warnings",
    ]

    for field in list_fields:
        value = normalized.get(field)

        if not isinstance(value, list):
            normalized[field] = []

    if signal == "NO TRADE":
        normalized["entry"] = "N/A"
        normalized["stop_loss"] = "N/A"
        normalized["take_profit_1"] = "N/A"
        normalized["take_profit_2"] = "N/A"
        normalized["risk_reward"] = "N/A"

    return normalized


def encode_result_for_history(result):
    raw = json.dumps(
        result,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def save_completed_job(
    job_id,
    user_id,
    instrument,
    trade_focus,
    result,
):
    supabase_url = get_supabase_url().rstrip("/")
    service_key = get_supabase_service_key().strip()

    if not supabase_url:
        raise RuntimeError("Supabase URL is not configured.")

    if not service_key:
        raise RuntimeError("Supabase service key is not configured.")

    payload = {
        "id": job_id,
        "user_id": user_id,
        "instrument": instrument,
        "trade_focus": trade_focus,
        "status": "completed",
        "openai_response_id": encode_result_for_history(result),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    url = f"{supabase_url}/rest/v1/analysis_jobs"

    req = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
    )

    try:
        with urlopen(req, timeout=20) as response:
            response.read()

    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Supabase save failed ({exc.code}): {body[:1000]}"
        )

    except URLError as exc:
        raise RuntimeError(
            f"Supabase connection failed: {exc.reason}"
        )


def call_gemini(
    instrument,
    trade_focus,
    higher_mime,
    higher_b64,
    lower_mime,
    lower_b64,
):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured in Vercel.")

    prompt = build_prompt(instrument, trade_focus)

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": prompt
                    },
                    {
                        "inlineData": {
                            "mimeType": higher_mime,
                            "data": higher_b64,
                        }
                    },
                    {
                        "inlineData": {
                            "mimeType": lower_mime,
                            "data": lower_b64,
                        }
                    },
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 5000,
            "responseMimeType": "application/json",
        },
    }

    req = Request(
        GEMINI_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": GEMINI_API_KEY,
        },
    )

    try:
        with urlopen(
            req,
            timeout=REQUEST_TIMEOUT_SECONDS,
        ) as response:

            raw = response.read().decode("utf-8", errors="replace")

    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")

        try:
            details = json.loads(body)
            message = (
                details.get("error", {}).get("message")
                or body[:1200]
            )
        except Exception:
            message = body[:1200]

        raise RuntimeError(f"Gemini error: {message}")

    except URLError as exc:
        raise RuntimeError(
            f"Gemini connection error: {exc.reason}"
        )

    except TimeoutError:
        raise RuntimeError("Gemini request timed out.")

    try:
        response_json = json.loads(raw)
    except Exception:
        raise RuntimeError("Gemini returned invalid API JSON.")

    text = extract_gemini_text(response_json)

    result = parse_json_result(text)

    return normalize_result(result, instrument)


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Authorization, Content-Type",
        )
        self.send_header(
            "Access-Control-Allow-Methods",
            "POST, OPTIONS",
        )
        self.end_headers()

    def do_POST(self):
        slot_reserved = False

        try:
            token = extract_bearer_token(self)

            if not token:
                send_json(
                    self,
                    401,
                    {
                        "error": "Authentication required."
                    },
                )
                return

            user = verify_access_token(token)

            if not user:
                send_json(
                    self,
                    401,
                    {
                        "error": "Invalid or expired session."
                    },
                )
                return

            user_id = user.get("id")

            if not user_id:
                send_json(
                    self,
                    401,
                    {
                        "error": "User ID was not found."
                    },
                )
                return

            body = read_json_body(self)

            instrument = str(
                body.get("instrument", "")
            ).strip().upper()

            trade_focus = str(
                body.get("trade_focus", "")
            ).strip().upper()

            higher_image = body.get(
                "higher_timeframe_image"
            )

            lower_image = body.get(
                "lower_timeframe_image"
            )

            allowed_instruments = {
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

            allowed_focus = {
                "SCALP",
                "SWING",
                "DAY TRADE",
            }

            if instrument not in allowed_instruments:
                send_json(
                    self,
                    400,
                    {
                        "error": "Invalid instrument."
                    },
                )
                return

            if trade_focus not in allowed_focus:
                send_json(
                    self,
                    400,
                    {
                        "error": "Invalid trade focus."
                    },
                )
                return

            if not higher_image or not lower_image:
                send_json(
                    self,
                    400,
                    {
                        "error": "Both 4H and 15M charts are required."
                    },
                )
                return

            higher_mime, higher_b64 = clean_data_url(
                higher_image
            )

            lower_mime, lower_b64 = clean_data_url(
                lower_image
            )

            decode_image_for_validation(higher_b64)
            decode_image_for_validation(lower_b64)

            try:
                reserved = reserve_analysis_slot(user_id)
            except Exception as exc:
                send_json(
                    self,
                    500,
                    {
                        "error": f"Unable to reserve analysis slot: {str(exc)}"
                    },
                )
                return

            if not reserved:
                send_json(
                    self,
                    429,
                    {
                        "error": "Daily analysis limit reached."
                    },
                )
                return

            slot_reserved = True

            result = call_gemini(
                instrument=instrument,
                trade_focus=trade_focus,
                higher_mime=higher_mime,
                higher_b64=higher_b64,
                lower_mime=lower_mime,
                lower_b64=lower_b64,
            )

            job_id = str(uuid.uuid4())

            save_completed_job(
                job_id=job_id,
                user_id=user_id,
                instrument=instrument,
                trade_focus=trade_focus,
                result=result,
            )

            slot_reserved = False

            send_json(
                self,
                200,
                {
                    "job_id": job_id
                },
            )

        except ValueError as exc:
            if slot_reserved:
                try:
                    release_analysis_slot(user_id)
                except Exception:
                    pass

            send_json(
                self,
                400,
                {
                    "error": str(exc)
                },
            )

        except RuntimeError as exc:
            if slot_reserved:
                try:
                    release_analysis_slot(user_id)
                except Exception:
                    pass

            send_json(
                self,
                502,
                {
                    "error": str(exc)
                },
            )

        except Exception as exc:
            if slot_reserved:
                try:
                    release_analysis_slot(user_id)
                except Exception:
                    pass

            send_json(
                self,
                500,
                {
                    "error": f"Analysis service error: {str(exc)}"
                },
            )
