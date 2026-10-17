
import base64
import json
import math
import os
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

from api.subscription_access import has_active_subscription
from api.user_security import (
    extract_bearer_token,
    get_supabase_service_key,
    get_supabase_url,
    is_owner_user,
    release_analysis_slot,
    reserve_analysis_slot,
    verify_access_token,
)

GEMINI_API_KEY = (
    os.environ.get("GEMINI_OPENAI_KEY", "").strip()
    or os.environ.get("GEMINI_API_KEY", "").strip()
)

GEMINI_MODELS = (
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
)

GEMINI_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent"
)

MAX_REQUEST_BYTES = 15 * 1024 * 1024
GEMINI_TIMEOUT_SECONDS = 45

ALLOWED_INSTRUMENTS = {
    # Major Forex
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF",
    "USDCAD", "AUDUSD", "NZDUSD",

    # Forex Crosses
    "EURGBP", "EURJPY", "EURCHF", "EURAUD",
    "EURCAD", "EURNZD",
    "GBPJPY", "GBPCHF", "GBPAUD", "GBPCAD",
    "GBPNZD",
    "AUDJPY", "AUDCAD", "AUDCHF", "AUDNZD",
    "CADJPY", "CADCHF", "CHFJPY",
    "NZDJPY", "NZDCAD", "NZDCHF",

    # Metals
    "XAUUSD", "XAGUSD",

    # Crypto
    "BTCUSD", "ETHUSD",
}


def json_response(handler, status_code, payload):
    body = json.dumps(
        payload, ensure_ascii=False
    ).encode("utf-8")

    handler.send_response(status_code)
    handler.send_header(
        "Content-Type", "application/json; charset=utf-8"
    )
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header(
        "Access-Control-Allow-Headers",
        "Content-Type, Authorization",
    )
    handler.send_header(
        "Access-Control-Allow-Methods",
        "GET, POST, OPTIONS",
    )
    handler.send_header(
        "Cache-Control", "no-store, no-cache, must-revalidate"
    )
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()

    try:
        handler.wfile.write(body)
    except Exception:
        pass


def read_json(handler):
    try:
        length = int(
            handler.headers.get("Content-Length", "0")
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
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            "Invalid request JSON."
        ) from exc

    if not isinstance(data, dict):
        raise ValueError("Invalid request.")

    return data


def validate_instrument(value):
    instrument = str(value or "").strip().upper()

    if instrument not in ALLOWED_INSTRUMENTS:
        raise ValueError(
            "Invalid instrument. Please select an instrument "
            "available in LM Analyzer."
        )

    return instrument


def validate_focus(value):
    focus = str(value or "").strip().upper()

    if focus not in {"SCALP", "DAY TRADE", "SWING"}:
        raise ValueError(
            "Trade focus must be SCALP, DAY TRADE, or SWING."
        )

    return focus


def validate_image(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + " is required.")

    value = value.strip()

    if (
        not value.startswith("data:image/")
        or ";base64," not in value
    ):
        raise ValueError(
            label + " is not a valid base64 image."
        )

    header, encoded = value.split(";base64,", 1)
    mime_type = header[5:].strip().lower()

    if mime_type not in {
        "image/jpeg", "image/png", "image/webp"
    }:
        raise ValueError(
            label + " must be JPEG, PNG, or WEBP."
        )

    if not encoded.strip():
        raise ValueError(label + " contains no image data.")

    try:
        decoded = base64.b64decode(
            encoded, validate=True
        )
    except Exception as exc:
        raise ValueError(
            label + " contains invalid base64 data."
        ) from exc

    if not decoded:
        raise ValueError(label + " is empty.")

    return {
        "mime_type": mime_type,
        "data": encoded,
    }


def build_prompt(instrument, trade_focus):
    common = """
Analyze only evidence visible in the supplied 4H and 15M screenshots.
Never invent prices, candles, indicators, or confirmations.

Evaluate:
- Market structure, trend, support, and resistance
- Price action and candlestick behavior
- Liquidity pools and liquidity sweeps
- Break of Structure (BOS) and Change of Character (CHoCH)
- Smart Money Concepts, order blocks, and Fair Value Gaps
- Fibonacci only where meaningful and defensible
- Premium/discount when the relevant range is identifiable
- Displacement, inducement, and mitigation where visible
- Entry trigger, invalidation, target space, and risk-to-reward
- Agreement or conflict between 4H and 15M

A directional bias is not automatically an entry.
A missing optional pattern is not sufficient reason to reject a
setup if other visible evidence supports it.

Do not require every strategy to agree. Weigh the evidence actually
visible on the charts. Do not claim to have live news data.
Do not guarantee profit.
"""

    focus_rules = {
        "SCALP": """
SCALP MODE — CAREFULLY SEARCH FOR VALID 15M ENTRIES.

Use the 4H chart for broader context and important levels.
Prioritize the 15M chart for deciding whether an entry is available.

Actively check for:
1. Liquidity sweep of a recent high or low followed by rejection.
2. Break of structure followed by a retest.
3. Pullback to a meaningful level followed by a confirming reaction.
4. Strong rejection candle at support, resistance, or a relevant zone.
5. Continuation after a retracement in the short-term trend.
6. Change of character followed by supporting price action.
7. Reaction around a visible order block or Fair Value Gap.

One clear, defensible pattern with supporting context may be enough.
Do not demand multiple confirmations that are not necessary for the
specific setup. Do not assume a pattern exists if it is not visible.

For a potential BUY or SELL, check:
- Is a trigger visible on the 15M chart now?
- Can an entry be placed near a defensible level?
- Where would the idea be invalidated?
- Is there room to a target before a major opposing level?
- Is the move already too extended to enter without chasing?

Do not reject a scalp solely because the 4H direction differs.
Assess whether the 15M opportunity is sufficiently clear, and explain
the additional risk from trading against the higher timeframe.

Classify the situation internally as:
A. ENTRY AVAILABLE: visible trigger and defensible risk levels.
B. SETUP DEVELOPING: a specific future confirmation is still needed.
C. ENTRY MISSED: the move is too extended to enter safely.
D. NO CLEAR SETUP: evidence is insufficient or contradictory.

Return BUY or SELL only for an entry supported by the screenshots.
If a trigger has not happened, do not pretend that it has.
If the entry is missed, do not invent a better price.
If no defensible stop-loss or target exists, return NO TRADE.

In your explanation, identify the possible direction, actual 15M
evidence, entry readiness, and confirmation or invalidation level
when the chart makes that level readable.
""",
        "DAY TRADE": """
DAY TRADE MODE.

Use 4H to establish broader structure and important levels.
Use 15M to identify a practical entry trigger.

Evaluate sweeps, breaks and retests, pullbacks, rejection candles,
order blocks, Fair Value Gaps, and continuation structures when visible.
Do not require every pattern. Avoid chasing extended moves.

Return BUY or SELL only when the entry, invalidation, and target are
defensible. Otherwise explain what is missing.
""",
        "SWING": """
SWING MODE.

Prioritize 4H structure, trend, and major levels. Use 15M to refine
entry timing or confirm a reaction.

Evaluate meaningful reversals, continuation, pullbacks, sweeps,
breaks and retests, and nearby opposing levels when visible.
Do not force a trade or invent prices. Explain what is missing when
the setup is not ready.
""",
    }

    return f"""
You are LM ANALYZER, the chart-analysis engine for LAMAR TRADING BOT.

Instrument: {instrument}
Selected trade focus: {trade_focus}

IMAGE 1 is the 4H chart.
IMAGE 2 is the 15M chart.

{common}

{focus_rules[trade_focus]}

SIGNAL RULES:
- signal must be BUY, SELL, or NO TRADE.
- trade_idea must be BUY {instrument}, SELL {instrument}, or NO TRADE.
- Use NO TRADE when there is no defensible entry, price labels are
  unreadable, evidence is insufficient, or unresolved conflict makes
  the setup too uncertain.
- A strong directional move can still have a poor entry. Explain
  the distinction between direction and entry timing.
- For NO TRADE, entry, stop_loss, take_profit_1, take_profit_2,
  and risk_reward must all be "N/A".
- For BUY or SELL, give defensible levels only. Do not manufacture
  exact prices from unreadable labels.

CONFIDENCE:
Confidence measures strength of the visible analysis, not guaranteed
profit or the probability that a trade will win.
Use the range 0-100 based on structure, confirmation, clarity, and
conflicting evidence. Do not automatically use zero for NO TRADE.
Use zero only when virtually no meaningful conclusion is possible.
Do not inflate confidence to justify a trade.

OUTPUT:
- Put methods in contributing_methods, weak_methods, and
  conflicting_methods where appropriate.
- warnings must describe actual risks and missing confirmations.
- Explain the evidence, entry readiness, and invalidation.
- Return only valid JSON without Markdown.

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
            "enum": ["BUY", "SELL", "NO TRADE"],
        },
        "confidence": {"type": "number"},
        "instrument": {"type": "string"},
        "trend": {
            "type": "string",
            "enum": [
                "BULLISH", "BEARISH", "RANGE", "UNCLEAR"
            ],
        },
        "trade_idea": {"type": "string"},
        "entry": {"type": "string"},
        "stop_loss": {"type": "string"},
        "take_profit_1": {"type": "string"},
        "take_profit_2": {"type": "string"},
        "risk_reward": {"type": "string"},
        "duration": {"type": "string"},
        "higher_timeframe_context": {"type": "string"},
        "lower_timeframe_confirmation": {"type": "string"},
        "data_analysis": {"type": "string"},
        "explanation": {"type": "string"},
        "contributing_methods": {
            "type": "array",
            "items": {"type": "string"},
        },
        "weak_methods": {
            "type": "array",
            "items": {"type": "string"},
        },
        "conflicting_methods": {
            "type": "array",
            "items": {"type": "string"},
        },
        "news_fundamental_risk": {"type": "string"},
        "warnings": {
            "type": "array",
            "items": {"type": "string"},
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


def parse_analysis_json(text):
    cleaned = text.strip()

    try:
        value = json.loads(cleaned)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass

    if cleaned.startswith("```"):
        lines = cleaned.splitlines()

        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]

        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        cleaned = "\n".join(lines).strip()

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
            value = json.loads(cleaned[start:end + 1])
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass

    raise RuntimeError(
        "Gemini returned invalid analysis JSON."
    )


def call_gemini(instrument, trade_focus, higher, lower):
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "Gemini key is missing from Vercel. "
            "Set GEMINI_OPENAI_KEY."
        )

    payload = {
        "system_instruction": {
            "parts": [{
                "text": (
                    "You are a disciplined chart-analysis service. "
                    "Pay close attention to the selected trade focus. "
                    "In SCALP mode, actively evaluate 15M entry triggers. "
                    "Never invent chart evidence or prices."
                )
            }]
        },
        "contents": [{
            "role": "user",
            "parts": [
                {
                    "text": build_prompt(
                        instrument, trade_focus
                    )
                },
                {
                    "inline_data": {
                        "mime_type": higher["mime_type"],
                        "data": higher["data"],
                    }
                },
                {
                    "inline_data": {
                        "mime_type": lower["mime_type"],
                        "data": lower["data"],
                    }
                },
            ],
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": OUTPUT_SCHEMA,
            "temperature": 0.2,
        },
    }

    body = json.dumps(
        payload, separators=(",", ":")
    ).encode("utf-8")

    retryable_statuses = {
        408, 429, 500, 502, 503, 504
    }
    failures = []

    for model_index, model in enumerate(GEMINI_MODELS):
        url = GEMINI_URL_TEMPLATE.format(model=model)

        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "x-goog-api-key": GEMINI_API_KEY,
            },
        )

        try:
            with urllib.request.urlopen(
                request, timeout=GEMINI_TIMEOUT_SECONDS
            ) as response:
                raw = response.read().decode(
                    "utf-8", errors="replace"
                )

        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(
                "utf-8", errors="replace"
            )

            try:
                error_obj = json.loads(detail)
                message = str(
                    error_obj.get("error", {}).get("message")
                    or detail[:1500]
                )
            except Exception:
                message = detail[:1500] or str(exc)

            failures.append(
                "{} returned HTTP {}: {}".format(
                    model, exc.code, message
                )
            )

            if (
                exc.code in retryable_statuses
                or exc.code == 404
            ) and model_index < len(GEMINI_MODELS) - 1:
                time.sleep(1)
                continue

            raise RuntimeError(
                "Gemini error on {}: {}".format(
                    model, message
                )
            ) from exc

        except (
            urllib.error.URLError,
            TimeoutError,
        ) as exc:
            failures.append(
                "{} connection error: {}".format(
                    model, str(exc)
                )
            )

            if model_index < len(GEMINI_MODELS) - 1:
                time.sleep(1)
                continue

            break

        try:
            api_obj = json.loads(raw)
        except json.JSONDecodeError:
            failures.append(
                "{} returned invalid API JSON.".format(model)
            )

            if model_index < len(GEMINI_MODELS) - 1:
                continue

            break

        pieces = []
        candidates = api_obj.get("candidates")

        if isinstance(candidates, list):
            for candidate in candidates:
                content = (
                    candidate.get("content")
                    if isinstance(candidate, dict)
                    else None
                )
                parts = (
                    content.get("parts")
                    if isinstance(content, dict)
                    else None
                )

                if not isinstance(parts, list):
                    continue

                for part in parts:
                    if (
                        isinstance(part, dict)
                        and isinstance(part.get("text"), str)
                    ):
                        pieces.append(part["text"])

        generated_text = "\n".join(pieces).strip()

        if not generated_text:
            failures.append(
                "{} returned no analysis text. Details: {}".format(
                    model,
                    json.dumps(
                        api_obj.get("promptFeedback", {}),
                        ensure_ascii=False,
                    )[:1000],
                )
            )

            if model_index < len(GEMINI_MODELS) - 1:
                continue

            break

        try:
            return parse_analysis_json(generated_text)
        except RuntimeError as exc:
            failures.append(
                "{} returned unusable analysis JSON: {}".format(
                    model, str(exc)
                )
            )

            if model_index < len(GEMINI_MODELS) - 1:
                continue

            break

    raise RuntimeError(
        "All configured Gemini models failed. "
        + " | ".join(failures[-4:])
    )


def normalize_result(result, instrument):
    signal = str(
        result.get("signal", "NO TRADE")
    ).strip().upper()

    if signal not in {"BUY", "SELL", "NO TRADE"}:
        signal = "NO TRADE"

    try:
        confidence = float(result.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0

    if not math.isfinite(confidence):
        confidence = 0.0

    confidence = max(0.0, min(100.0, confidence))

    trend = str(
        result.get("trend", "UNCLEAR")
    ).strip().upper()

    if trend not in {
        "BULLISH", "BEARISH", "RANGE", "UNCLEAR"
    }:
        trend = "UNCLEAR"

    def text(name):
        value = result.get(name)

        if value is None:
            return "N/A"

        if isinstance(value, str):
            return value.strip() or "N/A"

        return str(value)

    def list_value(name):
        value = result.get(name)

        if not isinstance(value, list):
            return []

        return [
            str(item).strip()
            for item in value
            if str(item).strip()
        ]

    output = {
        "signal": signal,
        "confidence": confidence,
        "instrument": instrument,
        "trend": trend,
        "trade_idea": (
            "BUY " + instrument
            if signal == "BUY"
            else "SELL " + instrument
            if signal == "SELL"
            else "NO TRADE"
        ),
        "entry": text("entry"),
        "stop_loss": text("stop_loss"),
        "take_profit_1": text("take_profit_1"),
        "take_profit_2": text("take_profit_2"),
        "risk_reward": text("risk_reward"),
        "duration": text("duration"),
        "higher_timeframe_context": text(
            "higher_timeframe_context"
        ),
        "lower_timeframe_confirmation": text(
            "lower_timeframe_confirmation"
        ),
        "data_analysis": text("data_analysis"),
        "explanation": text("explanation"),
        "contributing_methods": list_value(
            "contributing_methods"
        ),
        "weak_methods": list_value("weak_methods"),
        "conflicting_methods": list_value(
            "conflicting_methods"
        ),
        "news_fundamental_risk": text(
            "news_fundamental_risk"
        ),
        "warnings": list_value("warnings"),
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

    return base64.urlsafe_b64encode(raw).decode(
        "ascii"
    ).rstrip("=")


def create_completed_job(
    job_id, user_id, instrument, trade_focus, encoded_result
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
        "openai_response_id": encoded_result,
    }

    request = urllib.request.Request(
        url,
        data=json.dumps(
            payload, separators=(",", ":")
        ).encode("utf-8"),
        method="POST",
        headers={
            "apikey": key,
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Prefer": "return=minimal",
        },
    )

    try:
        with urllib.request.urlopen(
            request, timeout=20
        ) as response:
            response.read()

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(
            "utf-8", errors="replace"
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
            "Supabase connection error: " + str(exc)
        ) from exc


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
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
            access_token = extract_bearer_token(self)
            user = verify_access_token(access_token)
            user_id = str(
                user.get("id", "")
            ).strip()

            if not user_id:
                raise ValueError(
                    "Authenticated user ID is missing."
                )

            data = read_json(self)

            instrument = validate_instrument(
                data.get("instrument")
            )
            trade_focus = validate_focus(
                data.get("trade_focus")
            )
            higher = validate_image(
                data.get("higher_timeframe_image"),
                "4H chart",
            )
            lower = validate_image(
                data.get("lower_timeframe_image"),
                "15M chart",
            )

            owner = is_owner_user(user)

            if (
                not owner
                and not has_active_subscription(user_id)
            ):
                json_response(self, 402, {
                    "error": (
                        "An active subscription is required "
                        "to analyse charts."
                    ),
                    "subscription_required": True,
                })
                return

            if not owner:
                slot = reserve_analysis_slot(user_id)

                if slot == -1:
                    json_response(self, 429, {
                        "error": "Daily analysis limit reached.",
                        "daily_limit": 4,
                        "remaining": 0,
                        "is_owner": False,
                    })
                    return

                reserved = True
                remaining = max(0, 4 - int(slot))
            else:
                remaining = None

            try:
                raw_result = call_gemini(
                    instrument,
                    trade_focus,
                    higher,
                    lower,
                )

                result = normalize_result(
                    raw_result, instrument
                )

                job_id = str(uuid.uuid4())

                create_completed_job(
                    job_id,
                    user_id,
                    instrument,
                    trade_focus,
                    encode_result(result),
                )

            except Exception:
                if reserved:
                    try:
                        release_analysis_slot(user_id)
                    except Exception:
                        pass
                raise

            reserved = False

            json_response(self, 202, {
                "status": "completed",
                "job_id": job_id,
                "poll_after_seconds": 0,
                "is_owner": owner,
                "daily_limit": None if owner else 4,
                "remaining": remaining,
            })

        except ValueError as exc:
            json_response(self, 400, {
                "error": str(exc)
            })

        except RuntimeError as exc:
            json_response(self, 502, {
                "error": str(exc)
            })

        except Exception as exc:
            if reserved and user_id:
                try:
                    release_analysis_slot(user_id)
                except Exception:
                    pass

            json_response(self, 500, {
                "error": "Analysis backend error: " + str(exc)
            })
