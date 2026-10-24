import base64
import json
import os
import socket
import urllib.error
import urllib.request


# ============================================================
# GEMINI CONFIGURATION
# ============================================================

GEMINI_MODEL = "gemini-3.5-flash-lite"

GEMINI_GENERATE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/"
    f"models/{GEMINI_MODEL}:generateContent"
)

MAX_IMAGE_DATA_URL_CHARS = 8 * 1024 * 1024
GEMINI_REQUEST_TIMEOUT_SECONDS = 45


# ============================================================
# SYSTEM PROMPT
# ============================================================

SYSTEM_PROMPT = """
You are the professional chart-analysis engine for LM Analyzer.

You receive two trading screenshots:
IMAGE 1 = 4H chart
IMAGE 2 = 15M chart

The selected instrument and trade focus are supplied separately.

Return one directional decision:
BUY, SELL, or NO TRADE.

Determine entry readiness independently:
READY, WAITING, LATE ENTRY, INVALID, or NO_SETUP.

Supported instrument categories include:
- Forex majors, crosses, and exotic pairs
- Precious metals
- Energy commodities
- Stock-market indices
- VIX and related market volatility indices
- Synthetic volatility indices
- Boom, Crash, Step, and Jump synthetic indices
- Cryptocurrencies

Do not assume every instrument is a forex pair.
Use the instrument's actual chart structure and displayed price scale.
Do not invent broker specifications, news, prices, or market data.

==================================================
CORE DECISION RULE
==================================================

Do not automatically choose NO TRADE.
Do not require every trading method to agree.
Do not require the 4H and 15M charts to show identical structures.

Use the strongest overall visible evidence.

A BUY is allowed when the overall evidence is sufficiently bullish.
A SELL is allowed when the overall evidence is sufficiently bearish.

When direction is reasonably clear but price has not reached a
suitable entry, preserve the directional signal and use WAITING.

When the original entry opportunity has already passed, preserve
the directional signal if it remains justified and use LATE ENTRY.
Do not confuse a valid directional idea with an entry that is still
available now.

Use NO TRADE only when evidence is genuinely too unclear, too weak,
materially conflicting, or chart quality prevents responsible analysis.

Never invent a direction merely to avoid NO TRADE.

==================================================
TIMEFRAME LOGIC
==================================================

SCALP:
- 15M is the primary execution timeframe.
- 4H provides broader context.
- A valid scalp setup can exist from clear 15M evidence even if
  the 4H chart is neutral.
- The 4H chart should not clearly invalidate the setup.

DAY TRADE:
- 4H provides broader market context.
- 15M provides confirmation and execution.

SWING:
- 4H is the primary structural timeframe.
- 15M may assist with entry timing.

==================================================
ANALYSIS METHODS
==================================================

Use methods supported by visible evidence:
- Support and resistance
- Pure price action and candlesticks
- Market structure and swing highs/lows
- Break of structure and change of character
- Liquidity, equal highs/lows, and liquidity sweeps
- Displacement and inducement
- Mitigation and invalidation
- Fibonacci retracement and extension
- Premium and discount
- Smart money concepts
- Order blocks and fair value gaps
- Higher-timeframe bias and lower-timeframe confirmation

Do not require every method.
Identify contributing, weak, and conflicting evidence.

==================================================
INSTRUMENT-SPECIFIC RULES
==================================================

FOREX:
Use visible price structure and account for the possibility of
spread, news, and volatility. Do not invent pip values or broker
contract specifications.

METALS AND ENERGY:
Use the displayed chart prices and visible structure. Do not assume
all symbols use the same decimal precision or contract size.

STOCK INDICES AND VIX:
Analyze the actual index chart. Do not treat an index as a currency
pair or invent an underlying tradable instrument.

CRYPTO:
Use the displayed price scale and chart structure. Do not assume
that every exchange or broker supports the selected pair.

SYNTHETIC VOLATILITY INDICES:
Use the supplied chart evidence and visible price structure.
Do not invent economic news or claim synthetic indices react to
economic announcements in the same way as forex.

BOOM, CRASH, STEP, AND JUMP:
Analyze visible price movement and structure without assuming
that a spike must occur at a particular time.
Never promise that a spike is imminent or guaranteed.

A descriptive instrument name may differ from the broker's actual
symbol. Analyze the screenshots supplied; do not claim that the
instrument is available on a particular broker without verification.

==================================================
DIRECTION AND ENTRY READINESS
==================================================

Determine independently:
1. Directional bias: BUY, SELL, or NO TRADE.
2. Entry readiness: READY, WAITING, LATE ENTRY, INVALID, or NO_SETUP.
3. Order type: MARKET, BUY_LIMIT, SELL_LIMIT, WAIT, or NONE.

A clear direction does not automatically justify immediate entry.

READY:
The setup is valid and the entry opportunity is available now.
The visible chart evidence supports the proposed entry at the current
stage of the setup. Required confirmation must be present when the
selected strategy requires confirmation.

WAITING:
The setup is developing, but the planned entry trigger or required
confirmation has not happened yet. Specify what must happen next.

LATE ENTRY:
The original planned entry zone was reached and price subsequently
moved away enough that the original entry opportunity has passed.
The directional idea may remain technically valid, but entering now
would mean chasing the original move.

When this condition is established:
- Keep signal as BUY or SELL if that direction remains justified.
- Set setup_status = LATE ENTRY.
- Set order_type = WAIT.
- Explain that the original entry opportunity has passed.
- Warn the user not to chase the move.
- Do not automatically create a new entry price.
- Do not imply that the trade will continue in the original direction.
- Keep any original planned levels clearly identified as planned
  levels, not as a guaranteed available entry.

IMPORTANT DISTINCTION:
A touch of the entry zone alone does not prove that the opportunity
has passed. A valid retest or required confirmation may still be
pending. In that case, use WAITING if the setup remains valid.

Use LATE ENTRY only when the chart evidence supports both:
1. Price reached the relevant original entry zone.
2. Price subsequently moved away enough that the original entry
   opportunity is no longer available under the proposed entry plan.

Use the screenshots' visible candles and price structure to evaluate
this. Do not invent unseen historical movement or claim that a zone
was touched when the screenshots cannot establish it.

If the available screenshots do not establish whether the zone was
previously reached and price moved away, do not confidently label it
LATE ENTRY. Explain the uncertainty and use WAITING when the setup
remains valid but readiness cannot be established.

INVALID:
The considered setup's defining conditions have failed or its
invalidation level has been breached. Do not present it as an
actionable setup.

NO_SETUP:
There is no sufficiently justified directional setup.

A WAITING or LATE ENTRY setup is not an executed trade.

==================================================
BUY LIMIT
==================================================

Consider BUY_LIMIT when:
- Directional evidence is bullish.
- A defensible entry is below the observed market price.
- Visible support, demand, a bullish order block, or a bullish
  fair value gap supports the entry.
- The stop loss can be placed beyond logical invalidation.
- Reasonable profit objectives are identifiable.
- The original entry opportunity has not already passed.

Never propose a BUY_LIMIT above the observed market price.

==================================================
SELL LIMIT
==================================================

Consider SELL_LIMIT when:
- Directional evidence is bearish.
- A defensible entry is above the observed market price.
- Visible resistance, supply, a bearish order block, or a bearish
  fair value gap supports the entry.
- The stop loss can be placed beyond logical invalidation.
- Reasonable profit objectives are identifiable.
- The original entry opportunity has not already passed.

Never propose a SELL_LIMIT below the observed market price.

==================================================
MARKET ENTRY
==================================================

Use MARKET only when an immediate entry is justified by the chart
and appropriate for the selected trade focus.

Do not use MARKET for a setup classified as WAITING, LATE ENTRY,
INVALID, or NO_SETUP.

Do not invent an exact market price when the chart does not show it
clearly.

Do not claim that an order has been placed or executed.

==================================================
PRICE PRECISION
==================================================

Provide:
- entry: proposed order price or market-entry level;
- entry_zone: justified price area to watch;
- entry_trigger: condition required before entry.

Use readable price digits from the screenshots wherever possible.
Do not fabricate precision.

If exact price digits are unreadable, explain the limitation.
Use a price zone only when it can reasonably be inferred from
visible chart structure.

For WAITING setups, entry may describe a planned area.
For LATE ENTRY setups, any entry level retained must be identified
as the original planned entry, not a fresh instruction to enter now.

If no reliable numerical price can be established, say so clearly.

==================================================
STOP LOSS AND TAKE PROFITS
==================================================

Place stop loss beyond logical invalidation.

TP1 should be the nearer logical objective.
TP2 should be a further objective when supported by visible
structure, liquidity, or another clear price objective.

Do not invent targets that contradict visible structure.

Keep risk/reward consistent with the proposed entry, stop loss,
and targets. If reliable calculation is impossible, state that.

For LATE ENTRY, do not present the original entry, stop loss,
take profits, or risk/reward as a recommendation to enter late.
Explain that the original plan may no longer be suitable at the
current price.

==================================================
SETUP STATUS
==================================================

READY:
An immediate entry is justified, or a defensible limit order can
be proposed and the original entry opportunity remains available.

WAITING:
Direction is reasonably clear, but price or confirmation conditions
are not yet suitable for entry.

LATE ENTRY:
The original entry zone was reached and price moved away enough that
the original opportunity has passed. The direction may remain valid,
but the user should not chase the original move.

INVALID:
The considered setup has been invalidated by visible evidence.

NO_SETUP:
There is no sufficiently justified directional setup.

A WAITING or LATE ENTRY setup is not an executed trade.

==================================================
NO TRADE
==================================================

Use:
signal = NO TRADE
order_type = NONE
setup_status = NO_SETUP

when direction is genuinely unclear, evidence is materially balanced,
the setup is too weak, chart quality prevents reliable analysis,
important information is missing, or risk makes a responsible setup
impossible.

For NO TRADE, entry, stop loss, take profits, risk/reward, entry zone,
and entry trigger must be empty strings.

Do not use NO TRADE merely because one method is weak.

==================================================
NEWS AND CONFIDENCE
==================================================

Do not invent current news. Only discuss news or fundamentals that
are visible or reliably supplied. If current news cannot be verified,
say so.

Confidence measures confidence in the chart analysis, not a
guaranteed win rate or probability of profit.

For LATE ENTRY, confidence describes the chart's directional evidence,
not whether a late entry is advisable.

==================================================
OUTPUT
==================================================

Return ONLY valid JSON matching the supplied schema.
No Markdown, code fences, or commentary outside JSON.
""".strip()


# ============================================================
# OUTPUT SCHEMA
# ============================================================

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
        "instrument": {"type": "string"},
        "trend": {"type": "string"},
        "trade_idea": {"type": "string"},
        "order_type": {
            "type": "string",
            "enum": [
                "MARKET",
                "BUY_LIMIT",
                "SELL_LIMIT",
                "WAIT",
                "NONE",
            ],
        },
        "setup_status": {
            "type": "string",
            "enum": [
                "READY",
                "WAITING",
                "LATE ENTRY",
                "INVALID",
                "NO_SETUP",
            ],
        },
        "entry_zone": {"type": "string"},
        "entry_trigger": {"type": "string"},
        "higher_timeframe_context": {"type": "string"},
        "lower_timeframe_confirmation": {"type": "string"},
        "entry": {"type": "string"},
        "stop_loss": {"type": "string"},
        "take_profit_1": {"type": "string"},
        "take_profit_2": {"type": "string"},
        "risk_reward": {"type": "string"},
        "duration": {"type": "string"},
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
        "order_type",
        "setup_status",
        "entry_zone",
        "entry_trigger",
        "higher_timeframe_context",
        "lower_timeframe_confirmation",
        "entry",
        "stop_loss",
        "take_profit_1",
        "take_profit_2",
        "risk_reward",
        "duration",
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
# ALLOWED INSTRUMENTS
# ============================================================

ALLOWED_INSTRUMENTS = {
    # Major forex
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF",
    "USDCAD", "AUDUSD", "NZDUSD",

    # Forex crosses
    "EURGBP", "EURJPY", "EURCHF", "EURAUD",
    "EURCAD", "EURNZD", "GBPEUR", "GBPJPY",
    "GBPCHF", "GBPAUD", "GBPCAD", "GBPNZD",
    "AUDJPY", "AUDCAD", "AUDCHF", "AUDNZD",
    "CADJPY", "CADCHF", "CHFJPY",
    "NZDJPY", "NZDCAD", "NZDCHF",

    # Exotic forex
    "USDSEK", "USDNOK", "USDDKK", "USDPLN",
    "USDCZK", "USDHUF", "USDTRY", "USDMXN",
    "USDZAR", "USDSGD", "USDHKD", "USDCNH",
    "USDTHB", "EURSEK", "EURNOK", "EURDKK",
    "EURPLN", "EURHUF", "EURTRY", "EURZAR",
    "EURSGD", "EURHKD", "GBPSEK", "GBPNOK",
    "GBPDKK", "GBPPLN", "GBPHUF", "GBPTRY",
    "GBPZAR", "GBPSGD", "AUDSGD", "NZDSGD",
    "NZDHKD", "SGDJPY", "CADSGD", "CADHKD",
    "CHFSGD", "EURCNH", "GBPCNH", "AUDCNH",

    # Metals
    "XAUUSD", "XAGUSD", "XAUEUR", "XAUGBP",
    "XAGEUR", "XPTUSD", "XPDUSD",

    # Energy
    "USOIL", "UKOIL", "XTIUSD", "XBRUSD",
    "NATGAS", "NGAS",

    # Stock indices
    "US30", "US500", "US100", "USTEC", "NAS100",
    "US2000", "UK100", "GER40", "DE40", "FRA40",
    "EU50", "ESP35", "AUS200", "JP225", "HK50",
    "CHINA50", "SG20", "NED25", "SWI20", "VIX",

    # Synthetic volatility indices
    "VOLATILITY_10_INDEX",
    "VOLATILITY_25_INDEX",
    "VOLATILITY_50_INDEX",
    "VOLATILITY_75_INDEX",
    "VOLATILITY_100_INDEX",
    "VOLATILITY_10_1S_INDEX",
    "VOLATILITY_25_1S_INDEX",
    "VOLATILITY_50_1S_INDEX",
    "VOLATILITY_75_1S_INDEX",
    "VOLATILITY_100_1S_INDEX",

    # Boom and Crash
    "BOOM_300_INDEX", "BOOM_500_INDEX",
    "BOOM_600_INDEX", "BOOM_900_INDEX",
    "BOOM_1000_INDEX",
    "CRASH_300_INDEX", "CRASH_500_INDEX",
    "CRASH_600_INDEX", "CRASH_900_INDEX",
    "CRASH_1000_INDEX",

    # Step and Jump
    "STEP_INDEX",
    "JUMP_10_INDEX", "JUMP_25_INDEX",
    "JUMP_50_INDEX", "JUMP_75_INDEX",
    "JUMP_100_INDEX",

    # Crypto
    "BTCUSD", "ETHUSD", "LTCUSD", "BCHUSD",
    "XRPUSD", "ADAUSD", "SOLUSD", "DOGEUSD",
    "DOTUSD", "LINKUSD", "AVAXUSD", "BNBUSD",
    "TRXUSD", "XLMUSD", "ETCUSD", "ATOMUSD",
    "NEARUSD", "UNIUSD", "APTUSD", "FILUSD",
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT",
}

ALLOWED_FOCUSES = {
    "SCALP",
    "DAY TRADE",
    "SWING",
}


# ============================================================
# JSON RESPONSE
# ============================================================

def json_response(handler, status, payload, extra_headers=None):
    body = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    handler.send_response(status)
    handler.send_header(
        "Content-Type",
        "application/json; charset=utf-8",
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
        "Cache-Control",
        "no-store, no-cache, must-revalidate",
    )
    handler.send_header("Content-Length", str(len(body)))

    if extra_headers:
        for key, value in extra_headers.items():
            handler.send_header(key, str(value))

    handler.end_headers()

    try:
        handler.wfile.write(body)
    except Exception:
        pass


# ============================================================
# GEMINI API KEY
# ============================================================

def api_key():
    key = (
        os.getenv("GEMINI_API_KEY", "").strip()
        or os.getenv("GEMINI_OPENAI_KEY", "").strip()
    )

    if not key:
        raise RuntimeError(
            "Gemini API key is missing. Configure GEMINI_API_KEY "
            "or GEMINI_OPENAI_KEY in Vercel."
        )

    return key


# ============================================================
# VALIDATION
# ============================================================

def validate_instrument(value):
    instrument = str(value or "").strip().upper()

    if instrument not in ALLOWED_INSTRUMENTS:
        raise ValueError(
            "Invalid instrument. Select a supported instrument."
        )

    return instrument


def validate_focus(value):
    focus = str(value or "DAY TRADE").strip().upper()

    if focus not in ALLOWED_FOCUSES:
        raise ValueError("Invalid trade focus.")

    return focus


def validate_image_data_url(value, label):
    if not isinstance(value, str):
        raise ValueError(f"A valid {label} is required.")

    image = value.strip()

    if not image.startswith("data:image/"):
        raise ValueError(f"A valid {label} is required.")

    if len(image) > MAX_IMAGE_DATA_URL_CHARS:
        raise ValueError(f"{label} is too large.")

    header, separator, encoded = image.partition(",")

    if not separator or not encoded:
        raise ValueError(f"A valid {label} is required.")

    allowed_headers = (
        "data:image/jpeg;base64",
        "data:image/jpg;base64",
        "data:image/png;base64",
        "data:image/webp;base64",
    )

    if not header.lower().startswith(allowed_headers):
        raise ValueError(
            f"Unsupported {label} format. Use JPG, PNG, or WEBP."
        )

    encoded = encoded.strip()

    if len(encoded) < 100:
        raise ValueError(f"The {label} appears to be empty.")

    try:
        base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise ValueError(
            f"The {label} contains invalid image data."
        ) from exc

    return image


# ============================================================
# GEMINI ERROR PARSING
# ============================================================

def _gemini_error_message(raw):
    if not raw:
        return ""

    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")

        data = json.loads(raw)
    except Exception:
        return ""

    if not isinstance(data, dict):
        return ""

    error = data.get("error")

    if not isinstance(error, dict):
        return ""

    message = str(error.get("message", "")).strip()
    status = str(error.get("status", "")).strip()

    if message and status:
        return f"{message} ({status})"

    return message or status


# ============================================================
# GEMINI GENERATECONTENT
# ============================================================

def create_background_response(
    instrument,
    trade_focus,
    higher_image,
    lower_image,
):
    """Compatibility function using Gemini generateContent."""

    key = api_key()

    instrument = validate_instrument(instrument)
    trade_focus = validate_focus(trade_focus)

    higher_image = validate_image_data_url(
        higher_image,
        "4H chart",
    )
    lower_image = validate_image_data_url(
        lower_image,
        "15M chart",
    )

    def image_part(image_data_url):
        _, separator, encoded = image_data_url.partition(",")

        if not separator:
            raise ValueError("Invalid chart image.")

        header = image_data_url.partition(",")[0].lower()

        if "image/png" in header:
            mime_type = "image/png"
        elif "image/webp" in header:
            mime_type = "image/webp"
        else:
            mime_type = "image/jpeg"

        encoded = "".join(encoded.split())

        return {
            "inline_data": {
                "mime_type": mime_type,
                "data": encoded,
            }
        }

    user_prompt = (
        "Analyze the supplied trading charts.\n\n"
        f"Instrument: {instrument}\n"
        f"Trade focus: {trade_focus}\n\n"
        "IMAGE 1 = 4H chart.\n"
        "IMAGE 2 = 15M chart.\n\n"
        "Use the chart's actual instrument category and price scale.\n"
        "For SCALP, use 15M for execution and 4H as context.\n"
        "For DAY TRADE, combine 4H context with 15M confirmation.\n"
        "For SWING, use 4H as the primary structural timeframe.\n"
        "Return the strongest justified directional decision.\n"
        "When direction is clear and a defensible entry is visible, "
        "consider a limit order only on the correct side of market.\n"
        "If direction is clear but entry is not ready, use "
        "order_type WAIT and setup_status WAITING.\n"
        "If the original entry zone was reached and price moved away "
        "so the opportunity has passed, use setup_status LATE ENTRY "
        "and order_type WAIT. Preserve BUY or SELL only if the "
        "directional evidence still supports it. Warn against chasing "
        "and do not invent a replacement entry price.\n"
        "Do not call a zone touch LATE ENTRY if a valid retest or "
        "required confirmation is still pending. Use WAITING instead.\n"
        "Do not infer unseen historical movement. If the screenshots "
        "cannot establish whether the entry has passed, explain "
        "the uncertainty and avoid claiming certainty.\n"
        "Use NO TRADE when a responsible direction cannot be established.\n"
        "Do not invent prices or claim an order was executed."
    )

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": SYSTEM_PROMPT + "\n\n" + user_prompt,
                    },
                    image_part(higher_image),
                    image_part(lower_image),
                ],
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": OUTPUT_SCHEMA,
            "thinkingConfig": {
                "thinkingLevel": "minimal",
            },
            "maxOutputTokens": 2400,
        },
    }

    body = json.dumps(
        payload,
        separators=(",", ":"),
    ).encode("utf-8")

    request = urllib.request.Request(
        GEMINI_GENERATE_URL,
        data=body,
        headers={
            "x-goog-api-key": key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=GEMINI_REQUEST_TIMEOUT_SECONDS,
        ) as response:
            raw = response.read().decode("utf-8")
            data = json.loads(raw)

            if not isinstance(data, dict):
                raise RuntimeError(
                    "Gemini returned an invalid response."
                )

            return data

    except urllib.error.HTTPError as exc:
        raw = b""

        try:
            raw = exc.read()
        except Exception:
            pass

        message = _gemini_error_message(raw)

        if exc.code == 400:
            raise RuntimeError(
                "Gemini rejected the request: "
                + (message or "invalid request")
            ) from exc

        if exc.code in {401, 403}:
            raise RuntimeError(
                "The Gemini API key was rejected: "
                + (message or "access denied")
            ) from exc

        if exc.code == 404:
            raise RuntimeError(
                "The Gemini model was not found: "
                + (message or GEMINI_MODEL)
            ) from exc

        if exc.code == 429:
            raise RuntimeError(
                "Gemini quota or rate limit reached: "
                + (message or "quota exceeded")
            ) from exc

        if 500 <= exc.code <= 599:
            raise RuntimeError(
                "Gemini is temporarily unavailable: "
                + (message or f"HTTP {exc.code}")
            ) from exc

        raise RuntimeError(
            "Gemini request failed: "
            + (message or f"HTTP {exc.code}")
        ) from exc

    except (
        urllib.error.URLError,
        TimeoutError,
        socket.timeout,
    ) as exc:
        reason = getattr(exc, "reason", None)

        if isinstance(
            exc,
            (TimeoutError, socket.timeout),
        ) or isinstance(
            reason,
            (TimeoutError, socket.timeout),
        ):
            raise RuntimeError(
                "Gemini timed out after "
                f"{GEMINI_REQUEST_TIMEOUT_SECONDS} seconds. "
                "Please try again."
            ) from exc

        raise RuntimeError(
            "Gemini could not be reached. Check the connection "
            "and try again."
        ) from exc

    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Gemini returned invalid JSON."
        ) from exc


# ============================================================
# RESPONSE PARSING
# ============================================================

def extract_output_text(response_data):
    if not isinstance(response_data, dict):
        raise RuntimeError("Gemini returned an invalid response.")

    candidates = response_data.get("candidates", [])

    if not isinstance(candidates, list) or not candidates:
        prompt_feedback = response_data.get("promptFeedback", {})
        block_reason = ""

        if isinstance(prompt_feedback, dict):
            block_reason = str(
                prompt_feedback.get("blockReason", "")
            ).strip()

        if block_reason:
            raise RuntimeError(
                "Gemini could not analyze these charts. "
                f"Reason: {block_reason}."
            )

        raise RuntimeError("Gemini returned no analysis candidate.")

    candidate = candidates[0]

    if not isinstance(candidate, dict):
        raise RuntimeError(
            "Gemini returned an invalid analysis candidate."
        )

    finish_reason = str(
        candidate.get("finishReason", "")
    ).strip()

    content = candidate.get("content", {})

    if not isinstance(content, dict):
        raise RuntimeError("Gemini returned no analysis content.")

    parts = content.get("parts", [])

    if not isinstance(parts, list):
        raise RuntimeError("Gemini returned no analysis parts.")

    pieces = []

    for part in parts:
        if not isinstance(part, dict):
            continue

        text = part.get("text", "")

        if isinstance(text, str) and text.strip():
            pieces.append(text.strip())

    if not pieces:
        if finish_reason:
            raise RuntimeError(
                "Gemini returned no analysis text. "
                f"Finish reason: {finish_reason}"
            )

        raise RuntimeError("Gemini returned no analysis text.")

    return "\n".join(pieces).strip()


def clean_json_text(text):
    cleaned = str(text or "").strip()

    if not cleaned:
        return ""

    if cleaned.startswith("```"):
        lines = cleaned.splitlines()

        if lines:
            lines = lines[1:]

        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        cleaned = "\n".join(lines).strip()

    return cleaned


def _safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _clean_string(value):
    if value is None:
        return ""

    return str(value).strip()


def _clean_string_list(value):
    if not isinstance(value, list):
        return []

    result = []

    for item in value:
        text = _clean_string(item)

        if text:
            result.append(text)

    return result


def parse_completed_response(
    response_data,
    instrument,
    trade_focus="DAY TRADE",
):
    text = clean_json_text(
        extract_output_text(response_data)
    )

    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Gemini returned invalid analysis JSON."
        ) from exc

    if not isinstance(result, dict):
        raise RuntimeError(
            "Gemini returned an invalid analysis object."
        )

    signal = _clean_string(
        result.get("signal")
    ).upper()

    if signal not in {"BUY", "SELL", "NO TRADE"}:
        signal = "NO TRADE"

    confidence = _safe_int(result.get("confidence", 0))

    order_type = _clean_string(
        result.get("order_type")
    ).upper()

    setup_status = _clean_string(
        result.get("setup_status")
    ).upper()

    allowed_order_types = {
        "MARKET",
        "BUY_LIMIT",
        "SELL_LIMIT",
        "WAIT",
        "NONE",
    }

    allowed_setup_statuses = {
        "READY",
        "WAITING",
        "LATE ENTRY",
        "INVALID",
        "NO_SETUP",
    }

    if order_type not in allowed_order_types:
        order_type = "NONE"

    if setup_status not in allowed_setup_statuses:
        setup_status = "NO_SETUP"

    result = {
        "signal": signal,
        "confidence": max(0, min(100, confidence)),
        "instrument": _clean_string(instrument).upper(),
        "trend": _clean_string(result.get("trend")),
        "trade_idea": _clean_string(result.get("trade_idea")),
        "order_type": order_type,
        "setup_status": setup_status,
        "entry_zone": _clean_string(result.get("entry_zone")),
        "entry_trigger": _clean_string(result.get("entry_trigger")),
        "higher_timeframe_context": _clean_string(
            result.get("higher_timeframe_context")
        ),
        "lower_timeframe_confirmation": _clean_string(
            result.get("lower_timeframe_confirmation")
        ),
        "entry": _clean_string(result.get("entry")),
        "stop_loss": _clean_string(result.get("stop_loss")),
        "take_profit_1": _clean_string(result.get("take_profit_1")),
        "take_profit_2": _clean_string(result.get("take_profit_2")),
        "risk_reward": _clean_string(result.get("risk_reward")),
        "duration": _clean_string(result.get("duration")),
        "data_analysis": _clean_string(result.get("data_analysis")),
        "explanation": _clean_string(result.get("explanation")),
        "contributing_methods": _clean_string_list(
            result.get("contributing_methods")
        ),
        "weak_methods": _clean_string_list(
            result.get("weak_methods")
        ),
        "conflicting_methods": _clean_string_list(
            result.get("conflicting_methods")
        ),
        "news_fundamental_risk": _clean_string(
            result.get("news_fundamental_risk")
        ),
        "warnings": _clean_string_list(result.get("warnings")),
    }

    # --------------------------------------------------------
    # NO TRADE NORMALIZATION
    # --------------------------------------------------------

    if signal == "NO TRADE":
        result["order_type"] = "NONE"
        result["setup_status"] = "NO_SETUP"
        result["entry"] = ""
        result["stop_loss"] = ""
        result["take_profit_1"] = ""
        result["take_profit_2"] = ""
        result["risk_reward"] = ""
        result["entry_zone"] = ""
        result["entry_trigger"] = ""

    else:
        # ----------------------------------------------------
        # DIRECTION / ORDER-TYPE CONSISTENCY
        # ----------------------------------------------------

        if (
            result["order_type"] == "BUY_LIMIT"
            and signal != "BUY"
        ):
            result["order_type"] = "WAIT"
            result["setup_status"] = "WAITING"
            result["warnings"].append(
                "Order type did not match the directional signal. "
                "Verify the proposed setup before entry."
            )

        if (
            result["order_type"] == "SELL_LIMIT"
            and signal != "SELL"
        ):
            result["order_type"] = "WAIT"
            result["setup_status"] = "WAITING"
            result["warnings"].append(
                "Order type did not match the directional signal. "
                "Verify the proposed setup before entry."
            )

        # ----------------------------------------------------
        # LATE ENTRY SAFETY
        # ----------------------------------------------------

        if result["setup_status"] == "LATE ENTRY":
            # The original opportunity has passed. Do not allow
            # a MARKET or limit order to be presented as actionable.
            result["order_type"] = "WAIT"

            late_entry_warning = (
                "LATE ENTRY: The original entry opportunity appears "
                "to have passed. Do not chase the move. The directional "
                "idea may still be valid, but continuation is not "
                "guaranteed. Wait for a new, independently confirmed "
                "setup rather than entering at the old level."
            )

            # Avoid repeatedly adding the same warning.
            if not any(
                "LATE ENTRY:" in warning.upper()
                for warning in result["warnings"]
            ):
                result["warnings"].append(late_entry_warning)

            if not result["explanation"]:
                result["explanation"] = (
                    "The original entry opportunity appears to have "
                    "passed. The direction may remain valid, but the "
                    "original entry should not be chased."
                )

            if not result["entry_trigger"]:
                result["entry_trigger"] = (
                    "Do not enter the original setup late. Wait for "
                    "a new setup and fresh confirmation."
                )

        # ----------------------------------------------------
        # WAITING NORMALIZATION
        # ----------------------------------------------------

        elif result["setup_status"] == "WAITING":
            result["order_type"] = "WAIT"

        # ----------------------------------------------------
        # INVALID NORMALIZATION
        # ----------------------------------------------------

        elif result["setup_status"] == "INVALID":
            result["order_type"] = "NONE"

            if not any(
                "INVALID" in warning.upper()
                for warning in result["warnings"]
            ):
                result["warnings"].append(
                    "The setup is marked INVALID. Do not enter using "
                    "the invalidated setup."
                )

        # ----------------------------------------------------
        # NO SETUP NORMALIZATION
        # ----------------------------------------------------

        elif result["setup_status"] == "NO_SETUP":
            result["order_type"] = "NONE"

        # ----------------------------------------------------
        # READY NORMALIZATION
        # ----------------------------------------------------

        elif result["setup_status"] == "READY":
            if result["order_type"] in {"WAIT", "NONE"}:
                # Do not silently promote an uncertain entry to
                # a market order. Require a fresh analysis.
                result["setup_status"] = "WAITING"
                result["order_type"] = "WAIT"
                result["warnings"].append(
                    "The analysis marked the setup READY but did not "
                    "provide an actionable order type. Entry readiness "
                    "is uncertain; wait for clarification."
                )

    return result
