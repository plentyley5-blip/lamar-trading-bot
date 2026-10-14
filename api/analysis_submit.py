
import base64
import json
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

    allowed = {
        "EURUSD", "GBPUSD", "USDJPY", "GBPJPY", "AUDUSD",
        "USDCAD", "USDCHF", "EURGBP", "XAUUSD", "XAGUSD",
        "BTCUSD", "ETHUSD",
    }

    if instrument not in allowed:
        raise ValueError("Invalid instrument.")

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
            encoded, validate=False
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
    focus_instructions = {
        "SCALP": """
SCALP MODE — ENTRY IDENTIFICATION IS THE PRIORITY.

Use the 4H chart mainly to establish the larger market context,
important support/resistance, and nearby opposing levels.

Examine the 15M chart carefully for an entry that could reasonably
be taken now. Look for visible evidence such as:
- A pullback into support, resistance, a supply/demand zone, or an
  identifiable order block.
- A liquidity sweep followed by rejection or a structure shift.
- A break of structure followed by a retest.
- A Fair Value Gap retracement and a visible reaction.
- A strong rejection candle at a meaningful level.
- Continuation after a pullback when the structure remains valid.

A scalp may be valid even when the 4H trend is not perfectly aligned,
provided the 15M setup is clear, the local structure supports it,
and the trade has defensible invalidation and target levels.
Describe any conflict with the 4H context.

Do not require every listed pattern. One or more clear, relevant
confirmations can be sufficient.

Distinguish between:
A. ENTRY AVAILABLE: the charts show a defensible entry area or trigger.
B. SETUP DEVELOPING: direction is plausible but confirmation or a
   retest is still needed.
C. ENTRY MISSED: price has already moved too far from the last
   defensible entry and entering now would be chasing.
D. NO CLEAR SETUP: evidence is insufficient or contradictory.

Return BUY or SELL only for an entry that is actually defensible
from the supplied charts. If the setup is developing, the entry is
missed, or the evidence is unclear, return NO TRADE and explain
what confirmation or pullback is needed.

Do not reject a valid scalp merely because the market made a strong
move. Determine whether the move is still enterable using visible
structure, distance to invalidation, and available target space.
""",
        "DAY TRADE": """
DAY TRADE MODE.

Use the 4H chart to establish the main directional context and
important levels. Use the 15M chart to identify a practical entry,
confirmation, invalidation, and target.

Look for structure breaks, retests, liquidity sweeps, pullbacks,
rejections, order blocks, and Fair Value Gaps when visible.
Do not require every method to agree. Prefer a clear setup with
defensible risk and reward.

Distinguish between an entry available now, a setup requiring
confirmation, an entry that has already been missed, and no clear
setup. Do not chase an extended move.
""",
        "SWING": """
SWING MODE.

Prioritize the 4H structure, directional context, and major levels.
Use the 15M chart only to refine entry timing or confirm a reaction.

Look for meaningful trend continuation, reversals at major levels,
structure breaks, retests, liquidity sweeps, and pullbacks when
visible. Consider nearby opposing levels and whether the target
has enough room.

Do not force a trade if the charts do not support defensible levels.
Distinguish between a valid setup, a setup needing confirmation,
a missed entry, and no clear setup.
""",
    }

    selected_instructions = focus_instructions[trade_focus]

    return f"""
You are LM ANALYZER, the chart-analysis engine for LAMAR TRADING BOT.

Analyze the two supplied chart screenshots:
IMAGE 1 = 4H higher timeframe.
IMAGE 2 = 15M lower timeframe.

Instrument: {instrument}
Selected trade focus: {trade_focus}

Use only evidence visible in the supplied charts. Do not invent
prices, indicators, candles, patterns, news, or confirmations.

{selected_instructions}

ANALYSIS CHECKLIST:
- Market structure and trend
- Support and resistance
- Price action and candlestick behavior
- Liquidity pools and liquidity sweeps
- Break of Structure (BOS)
- Change of Character (CHoCH)
- Smart Money Concepts (SMC)
- Order blocks and Fair Value Gaps
- Fibonacci only when meaningful and supportable
- Premium/discount when the relevant range is identifiable
- Displacement, inducement, and mitigation where visible
- Entry trigger and entry readiness
- Stop-loss invalidation
- Initial target and final target
- Risk-to-reward and nearby opposing levels
- Agreement or conflict between 4H and 15M

ENTRY RULES:
1. A directional bias is not automatically an entry.
2. For BUY, identify the evidence supporting buying and a defensible
   invalidation level below the relevant structure.
3. For SELL, identify the evidence supporting selling and a defensible
   invalidation level above the relevant structure.
4. Prefer an entry at a meaningful level or after a visible trigger.
5. Do not recommend chasing an extended move when there is insufficient
   target space or no defensible stop-loss.
6. Do not insist on waiting for a pullback if the chart already shows
   a clear, defensible entry trigger and adequate target space.
7. Do not invent exact prices when labels are unreadable.
8. If the setup needs a future confirmation, do not pretend that the
   confirmation has already happened.
9. Consider the selected trade focus. A scalp opportunity should not
   be rejected solely because it is not a swing-trade opportunity.
10. If no defensible entry exists, use NO TRADE and explain exactly why.

SIGNAL RULES:
- signal must be BUY, SELL, or NO TRADE.
- trade_idea must be BUY {instrument}, SELL {instrument}, or NO TRADE.
- Use NO TRADE when evidence is insufficient, timeframes conflict
  without a defensible resolution, prices cannot be read, or entry
  risk is not defensible.
- A possible directional opportunity with no entry trigger must not
  be represented as an immediately actionable trade.

CONFIDENCE RULES:
- Confidence measures the strength of the visible chart analysis.
  It is not a guaranteed probability of profit.
- Assess structure, confirmation, timeframe agreement, clarity, and
  conflicting evidence.
- Confidence must be a number from 0 to 100.
- Do not automatically set confidence to zero for NO TRADE.
- If direction is clear but the entry is too extended, reflect the
  strength of the directional evidence and explain the entry risk.
- Use 0 only when the supplied evidence supports virtually no
  meaningful conclusion. Never invent confidence to make a result
  look better.

OUTPUT RULES:
- For NO TRADE, entry, stop_loss, take_profit_1, take_profit_2,
  and risk_reward must be "N/A".
- For BUY or SELL, provide only defensible levels. If those levels
  cannot be established, use NO TRADE.
- Use warnings to explain specific risks, not as a substitute for
  the actual analysis.
- Explain what evidence supports the decision and what would
  invalidate it.
- Put strategy names in contributing_methods, not in separate
  UI result categories.
- Do not claim to have live news data.
- Do not guarantee profit.
- Return only valid JSON. Do not include Markdown or code fences.

Return exactly this JSON structure:
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
                    "You are LM Analyzer, a disciplined chart-analysis "
                    "service. Follow the supplied JSON schema. Pay "
                    "attention to the selected trade focus, especially "
                    "15M entry triggers in SCALP mode. Never invent prices."
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
                request,
                timeout=GEMINI_TIMEOUT_SECONDS,
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

        candidates = api_obj.get("candidates")
        pieces = []

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
            prompt_feedback = api_obj.get(
                "promptFeedback", {}
            )
            failures.append(
                "{} returned no analysis text. Details: {}".format(
                    model,
                    json.dumps(
                        prompt_feedback,
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

    summary = " | ".join(failures[-4:])

    raise RuntimeError(
        "All configured Gemini models failed. " + summary
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

    if confidence != confidence:
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
                "error": (
                    "Analysis backend error: " + str(exc)
                )
            })
