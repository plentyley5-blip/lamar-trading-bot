import base64
import json
import os
import urllib.error
import urllib.request


# ============================================================
# GEMINI CONFIGURATION
# ============================================================

GEMINI_MODEL = "gemini-3.5-flash-lite"

GEMINI_GENERATE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/"
    "models/gemini-3.5-flash-lite:generateContent"
)

MAX_IMAGE_DATA_URL_CHARS = 8 * 1024 * 1024


# ============================================================
# SYSTEM PROMPT
# ============================================================

SYSTEM_PROMPT = """
You are the professional chart-analysis engine for LAMAR TRADING BOT.

You always receive two required trading screenshots:

IMAGE 1 = required 4H chart
IMAGE 2 = required 15M chart

You may also receive optional screenshots labelled 1D, 1H, 5M, or 1M. Use every optional chart provided as supplemental evidence. Never infer or claim that an optional chart was provided if it was not. The required 4H and 15M charts remain the core of every analysis.

The selected instrument and trade focus are supplied separately.

==================================================
MANDATORY INSTRUMENT IDENTITY CHECK — RUN FIRST
==================================================

Before analysing price action or creating a trade idea, independently read
the visible ticker/symbol from every supplied chart screenshot.

The user-selected instrument is NOT proof of what is shown in the image.
Never assume the chart matches the selected instrument.

Check these images separately:
- Required 4H chart
- Required 15M chart
- Each optional 1D, 1H, 5M, or 1M chart that is actually supplied

Read the chart's visible symbol/ticker label. Do not guess from price,
candlestick patterns, currency values, or the selected-instrument text.

Return the exact detected ticker in the corresponding detected_*_instrument
field. Return "NONE" for an optional timeframe image that was not supplied.
If a supplied chart's symbol cannot be read confidently, return "UNREADABLE".

Set chart_instrument_status to:
- "MATCH" only when every supplied chart clearly shows the selected instrument.
- "MISMATCH" when any supplied chart clearly shows a different instrument.
- "UNREADABLE" when the symbol on any supplied chart cannot be identified
  confidently, including either required chart.

If status is "MISMATCH" or "UNREADABLE", do not perform trading analysis.
Return signal "NO TRADE", confidence 0, setup_status "INVALID",
order_type "NONE", empty entry/stop-loss/take-profit/risk-reward fields,
and explain which chart does not match or cannot be verified in warnings
and explanation.

Only proceed to trading analysis after all supplied chart symbols have
been independently verified as an exact match.

Your task is to analyze both charts and produce exactly one decision:

BUY
SELL
NO TRADE

==================================================
CORE DECISION RULE
==================================================

Do NOT automatically choose NO TRADE.

Do NOT require every trading method to agree.

Do NOT require the 4H and 15M charts to show identical structures.

Use the strongest overall visible evidence.

A BUY is allowed when the overall evidence is sufficiently bullish.

A SELL is allowed when the overall evidence is sufficiently bearish.

Use NO TRADE only when the evidence is genuinely too unclear,
too weak, materially conflicting, or the chart quality prevents
a responsible directional setup.

==================================================
TIMEFRAME LOGIC
==================================================

SCALP:

- 15M is the primary execution timeframe.
- 4H provides broader context.
- A valid scalp BUY or SELL can exist from a sufficiently clear
  15M setup even if the 4H chart is neutral.
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

Use whichever are supported by visible chart evidence:

- support and resistance
- pure price action
- market structure
- swing highs
- swing lows
- break of structure
- change of character
- liquidity
- equal highs
- equal lows
- liquidity sweeps
- stop hunts
- displacement
- inducement
- mitigation
- invalidation
- Fibonacci retracement
- Fibonacci extension
- premium and discount
- smart money concepts
- order blocks
- fair value gaps
- higher-timeframe bias
- lower-timeframe confirmation

Do NOT require every method.

Identify:

- contributing evidence
- weak evidence
- conflicting evidence

==================================================
BUY
==================================================

A BUY may be selected when a meaningful combination of bullish evidence
is visible.

Examples include:

- bullish market structure
- bullish break of structure
- bullish change of character
- support reaction
- bullish liquidity sweep
- bullish displacement
- bullish order block
- bullish fair value gap
- discount positioning
- bullish price action
- lower-timeframe bullish confirmation

Not all are required.

==================================================
SELL
==================================================

A SELL may be selected when a meaningful combination of bearish evidence
is visible.

Examples include:

- bearish market structure
- bearish break of structure
- bearish change of character
- resistance reaction
- bearish liquidity sweep
- bearish displacement
- bearish order block
- bearish fair value gap
- premium positioning
- bearish price action
- lower-timeframe bearish confirmation

Not all are required.

==================================================
ENTRY
==================================================

For BUY or SELL:

Provide a logical entry level or entry area when the chart supports it.

Do not invent false precision.

If exact price digits are clearly visible, use them.

If exact digits are not clearly readable, use a reasonable level or zone
when that can be inferred responsibly.

==================================================
STOP LOSS
==================================================

Place the stop beyond the logical invalidation area, but keep the stop
range proportionate to the selected trade focus and the recent volatility
visible on the chart. The stop must protect the setup without being placed
needlessly far from entry.

For SCALP:

- Use the 15M chart for the stop-loss placement. Find the nearest meaningful
  15M micro-swing, wick-sweep extreme, or structure level that invalidates
  the entry.
- Place the stop just beyond that level with a small, defensible buffer.
  Estimate the buffer from the nearby candle range and instrument volatility;
  do not add a large arbitrary buffer.
- As a guide, aim for an entry-to-stop distance around 0.5 to 1.0 times the
  typical recent 15M candle range when the visible structure permits it.
  This is a guide, not a fixed pip limit or a reason to put the stop inside
  the invalidation level.
- Do not use a distant 4H swing as the scalp stop merely because it is visible.
  Use a 4H level for the stop only when it directly defines the actual setup
  invalidation.

For DAY TRADE:

- Base the stop on the nearest meaningful execution/invalidation structure
  on 15M, respecting a 4H level only when that level directly invalidates
  the idea.

For SWING:

- Base the stop on the nearest meaningful 4H structural invalidation level,
  with only a modest buffer beyond it.

For every instrument:

- Scale the distance to the instrument's displayed price precision and
  volatility. Do not apply one fixed pip or point range to forex, metals,
  indices, crypto, and other instruments.
- Never widen a stop to a distant unrelated swing, and never make it
  artificially tight inside the nearest valid invalidation level just to
  produce a preferred risk/reward ratio.
- If the valid structural stop is wider than the guide, keep it just beyond
  the actual invalidation level rather than inventing an invalid stop.

==================================================
TAKE PROFITS
==================================================

TP1 should be the nearer logical objective.

TP2 should be a further logical objective when supported by visible
structure, liquidity, or another clear price objective.

==================================================
RISK / REWARD
==================================================

Keep the risk/reward description consistent with the proposed entry,
stop loss, and targets.

==================================================
NO TRADE
==================================================

Use NO TRADE when:

- directional evidence is genuinely unclear
- bullish and bearish evidence are materially balanced
- the setup is too weak
- the chart quality prevents reliable analysis
- important information is missing
- the proposed idea is materially invalidated

Do NOT use NO TRADE merely because one method is weak.

==================================================
NEWS
==================================================

Do not invent current news.

Only discuss news or fundamentals that are visible or reliably supplied.

If current news cannot be verified, say so.

==================================================
CONFIDENCE
==================================================

Confidence is confidence in the quality of the chart analysis.

It is NOT:

- guaranteed win rate
- guaranteed probability of profit
- guarantee of success

==================================================
OUTPUT
==================================================

SETUP STATUS AND EXECUTION SAFETY

setup_status MUST be one of: READY, WAITING, LATE ENTRY, INVALID, NO_SETUP.
order_type MUST be one of: MARKET, LIMIT, STOP, WAIT, NONE.

READY: the original entry is still valid now and required confirmation is visible.
WAITING: the original entry area has not been reached yet, or its retest/confirmation is still pending.
LATE ENTRY: price already reached the original entry area and then moved away far enough that entering now would chase price. The directional idea may remain valid, but the original entry opportunity has passed. Never invent a new entry to justify chasing.
INVALID: the setup has been structurally invalidated.
NO_SETUP: no coherent setup can be justified from the supplied charts.

For WAITING and LATE ENTRY, order_type MUST be WAIT. For INVALID and NO_SETUP, order_type MUST be NONE and signal MUST be NO TRADE. If the setup is LATE ENTRY, include a warning that the original entry opportunity has passed and the trader must not chase price. A mere touch of the zone is not automatically LATE ENTRY when a valid retest or confirmation is still pending; that is WAITING.

Return ONLY valid JSON matching the supplied schema.

No Markdown.
No code fences.
No commentary outside JSON.
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
            "enum": [
                "BUY",
                "SELL",
                "NO TRADE",
            ],
        },

        "confidence": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
        },

        "instrument": {
            "type": "string",
        },

        "chart_instrument_status": {
            "type": "string",
            "enum": ["MATCH", "MISMATCH", "UNREADABLE"],
        },
        "detected_4h_instrument": {
            "type": "string",
        },
        "detected_15m_instrument": {
            "type": "string",
        },
        "detected_1d_instrument": {
            "type": "string",
        },
        "detected_1h_instrument": {
            "type": "string",
        },
        "detected_5m_instrument": {
            "type": "string",
        },
        "detected_1m_instrument": {
            "type": "string",
        },

        "trend": {
            "type": "string",
        },

        "trade_idea": {
            "type": "string",
        },

        "higher_timeframe_context": {
            "type": "string",
        },

        "lower_timeframe_confirmation": {
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

        "setup_status": {
            "type": "string",
            "enum": ["READY", "WAITING", "LATE ENTRY", "INVALID", "NO_SETUP"],
        },
        "order_type": {
            "type": "string",
            "enum": ["MARKET", "LIMIT", "STOP", "WAIT", "NONE"],
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
        "chart_instrument_status",
        "detected_4h_instrument",
        "detected_15m_instrument",
        "detected_1d_instrument",
        "detected_1h_instrument",
        "detected_5m_instrument",
        "detected_1m_instrument",
        "trend",
        "trade_idea",
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
        "setup_status",
        "order_type",
        "warnings",
    ],
}


# ============================================================
# ALLOWED VALUES
# ============================================================

ALLOWED_INSTRUMENTS = {
    'EURUSD', 'GBPUSD', 'USDJPY', 'USDCHF', 'USDCAD',
    'AUDUSD', 'NZDUSD', 'EURGBP', 'EURJPY', 'EURCHF',
    'EURAUD', 'EURCAD', 'EURNZD', 'GBPJPY', 'GBPCHF',
    'GBPAUD', 'GBPCAD', 'GBPNZD', 'AUDJPY', 'AUDCAD',
    'AUDCHF', 'AUDNZD', 'CADJPY', 'CADCHF', 'CHFJPY',
    'NZDJPY', 'NZDCAD', 'NZDCHF', 'USDSEK', 'USDNOK',
    'USDDKK', 'USDPLN', 'USDCZK', 'USDHUF', 'USDTRY',
    'USDMXN', 'USDZAR', 'USDSGD', 'USDHKD', 'USDCNH',
    'USDTHB', 'USDINR', 'USDKRW', 'USDTWD', 'USDILS',
    'USDSAR', 'EURSEK', 'EURNOK', 'EURDKK', 'EURPLN',
    'EURCZK', 'EURHUF', 'EURTRY', 'EURZAR', 'EURSGD',
    'EURHKD', 'EURCNH', 'EURMXN', 'GBPSEK', 'GBPNOK',
    'GBPDKK', 'GBPPLN', 'GBPCZK', 'GBPHUF', 'GBPTRY',
    'GBPZAR', 'GBPSGD', 'GBPHKD', 'AUDSGD', 'AUDHKD',
    'AUDCNH', 'NZDSGD', 'NZDHKD', 'CADSGD', 'CADHKD',
    'CHFSGD', 'CHFNOK', 'CHFDKK', 'SGDJPY', 'SGDCHF',
    'SGDHKD', 'HKDJPY', 'NOKJPY', 'SEKJPY', 'ZARJPY',
    'MXNJPY', 'TRYJPY', 'PLNJPY', 'CNHJPY', 'XAUUSD',
    'XAGUSD', 'XPTUSD', 'XPDUSD', 'XAUEUR', 'XAGEUR',
    'XAUGBP', 'XAUJPY', 'USOIL', 'UKOIL', 'XTIUSD',
    'XBRUSD', 'NATGAS', 'NGAS', 'CORN', 'WHEAT',
    'SOYBEAN', 'COFFEE', 'SUGAR', 'COCOA', 'COTTON',
    'US30', 'US500', 'US100', 'USTEC', 'NAS100',
    'US2000', 'UK100', 'GER40', 'DE40', 'FRA40',
    'EU50', 'ESP35', 'AUS200', 'JP225', 'HK50',
    'CHINA50', 'SG20', 'NED25', 'SWI20', 'VIX',
    'BTCUSD', 'ETHUSD', 'LTCUSD', 'XRPUSD', 'BCHUSD',
    'ADAUSD', 'SOLUSD', 'DOGEUSD', 'DOTUSD', 'AVAXUSD',
    'LINKUSD', 'BNBUSD', 'TRXUSD', 'XLMUSD', 'ETCUSD',
    'ATOMUSD', 'UNIUSD', 'NEARUSD', 'APTUSD', 'ICPUSD',
    'FILUSD', 'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT',
    'ADAUSDT', 'DOGEUSDT', 'VOLATILITY_10_INDEX', 'VOLATILITY_25_INDEX', 'VOLATILITY_50_INDEX',
    'VOLATILITY_75_INDEX', 'VOLATILITY_100_INDEX', 'VOLATILITY_10_1S_INDEX', 'VOLATILITY_25_1S_INDEX', 'VOLATILITY_50_1S_INDEX',
    'VOLATILITY_75_1S_INDEX', 'VOLATILITY_100_1S_INDEX', 'BOOM_300_INDEX', 'BOOM_500_INDEX', 'BOOM_600_INDEX',
    'BOOM_900_INDEX', 'BOOM_1000_INDEX', 'CRASH_300_INDEX', 'CRASH_500_INDEX', 'CRASH_600_INDEX',
    'CRASH_900_INDEX', 'CRASH_1000_INDEX', 'STEP_INDEX', 'JUMP_10_INDEX', 'JUMP_25_INDEX',
    'JUMP_50_INDEX', 'JUMP_75_INDEX', 'JUMP_100_INDEX',
}

ALLOWED_FOCUSES = {
    "SCALP",
    "DAY TRADE",
    "SWING",
}


# ============================================================
# JSON RESPONSE
# ============================================================

def json_response(
    handler,
    status,
    payload,
    extra_headers=None,
):
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

    if extra_headers:

        for key, value in extra_headers.items():

            handler.send_header(
                key,
                str(value),
            )

    handler.end_headers()

    try:
        handler.wfile.write(body)
    except Exception:
        pass


# ============================================================
# GEMINI API KEY
# ============================================================

def api_key():
    key = os.getenv(
        "GEMINI_API_KEY",
        "",
    ).strip()

    if not key:

        raise RuntimeError(
            "GEMINI_API_KEY is missing from Vercel."
        )

    return key


# ============================================================
# VALIDATION
# ============================================================

def validate_instrument(
    value,
):
    instrument = str(
        value or ""
    ).strip().upper()

    if instrument not in ALLOWED_INSTRUMENTS:

        raise ValueError(
            "Invalid instrument. Select a supported instrument."
        )

    return instrument


def validate_focus(
    value,
):
    focus = str(
        value or "DAY TRADE"
    ).strip().upper()

    if focus not in ALLOWED_FOCUSES:

        raise ValueError(
            "Invalid trade focus."
        )

    return focus


def validate_image_data_url(
    value,
    label,
):
    if not isinstance(
        value,
        str,
    ):

        raise ValueError(
            f"A valid {label} is required."
        )

    image = value.strip()

    if not image.startswith(
        "data:image/"
    ):

        raise ValueError(
            f"A valid {label} is required."
        )

    if (
        len(image)
        > MAX_IMAGE_DATA_URL_CHARS
    ):

        raise ValueError(
            f"{label} is too large."
        )

    header, separator, encoded = (
        image.partition(",")
    )

    if (
        not separator
        or not encoded
    ):

        raise ValueError(
            f"A valid {label} is required."
        )

    allowed_headers = (
        "data:image/jpeg;base64",
        "data:image/jpg;base64",
        "data:image/png;base64",
        "data:image/webp;base64",
    )

    if not header.lower().startswith(
        allowed_headers
    ):

        raise ValueError(
            f"Unsupported {label} format. "
            "Use JPG, PNG, or WEBP."
        )

    encoded = encoded.strip()

    if len(encoded) < 100:

        raise ValueError(
            f"The {label} appears to be empty."
        )

    try:

        base64.b64decode(
            encoded,
            validate=True,
        )

    except Exception as exc:

        raise ValueError(
            f"The {label} contains invalid image data."
        ) from exc

    return image


# ============================================================
# GEMINI ERROR PARSING
# ============================================================

def _gemini_error_message(
    raw,
):
    if not raw:
        return ""

    try:

        if isinstance(
            raw,
            bytes,
        ):

            raw = raw.decode(
                "utf-8",
                errors="replace",
            )

        data = json.loads(
            raw
        )

    except Exception:
        return ""

    if not isinstance(
        data,
        dict,
    ):
        return ""

    error = data.get(
        "error"
    )

    if not isinstance(
        error,
        dict,
    ):
        return ""

    message = str(
        error.get(
            "message",
            "",
        )
    ).strip()

    status = str(
        error.get(
            "status",
            "",
        )
    ).strip()

    if message and status:

        return (
            f"{message} ({status})"
        )

    return message or status


# ============================================================
# STANDARD GEMINI GENERATECONTENT
# ============================================================

def create_background_response(
    instrument,
    trade_focus,
    higher_image,
    lower_image,
    additional_images=None,
):
    """
    Compatibility function name.

    Despite the old name, this no longer uses a background
    Interactions/Agent request.

    It uses the standard Gemini generateContent API directly.
    """

    key = api_key()

    instrument = validate_instrument(
        instrument
    )

    trade_focus = validate_focus(
        trade_focus
    )

    higher_image = validate_image_data_url(
        higher_image,
        "4H chart",
    )

    lower_image = validate_image_data_url(
        lower_image,
        "15M chart",
    )

    if additional_images is None:
        additional_images = {}
    if not isinstance(additional_images, dict):
        raise ValueError("Additional timeframe charts must be an object.")
    allowed_additional_timeframes = ("1D", "1H", "5M", "1M")
    if len(additional_images) > len(allowed_additional_timeframes):
        raise ValueError("Too many additional timeframe charts were supplied.")
    validated_additional_images = {}
    for raw_timeframe, image in additional_images.items():
        timeframe = str(raw_timeframe).strip().upper()
        if timeframe not in allowed_additional_timeframes:
            raise ValueError("Unsupported additional timeframe: " + timeframe)
        if timeframe in validated_additional_images:
            raise ValueError("Duplicate additional timeframe: " + timeframe)
        validated_additional_images[timeframe] = validate_image_data_url(
            image, timeframe + " chart"
        )

    def image_part(
        image_data_url,
    ):
        header, separator, encoded = (
            image_data_url.partition(",")
        )

        if not separator:
            raise ValueError(
                "Invalid chart image."
            )

        header_lower = header.lower()

        if "image/png" in header_lower:

            mime_type = "image/png"

        elif "image/webp" in header_lower:

            mime_type = "image/webp"

        else:

            mime_type = "image/jpeg"

        return {
            "inline_data": {
                "mime_type":
                    mime_type,

                "data":
                    encoded,
            }
        }

    user_prompt = (
        "FIRST perform the mandatory chart-instrument identity check.\n"
        "Independently read the visible ticker from each supplied screenshot; "
        "never assume it matches the selected instrument.\n"
        "Compare the 4H, 15M, and every supplied optional chart against the "
        "selected instrument.\n"
        "If any chart differs, set chart_instrument_status=MISMATCH and do not "
        "analyse the trade. If any supplied chart ticker is unclear, set "
        "chart_instrument_status=UNREADABLE and do not analyse the trade.\n"
        "Return only the ticker code in each detected_*_instrument field; "
        "use NONE only when that optional timeframe image was not supplied.\n\n"
        "Analyze the supplied trading charts only after all symbols match.\n\n"
        f"Selected instrument: {instrument}\n"
        f"Trade focus: {trade_focus}\n\n"
        "IMAGE 1 = required 4H chart.\n"
        "IMAGE 2 = required 15M chart.\n"
        "Optional chart screenshots, when supplied, are separately labelled by timeframe.\n\n"
        "For SCALP, make the 15M chart the primary execution "
        "timeframe and use the 4H chart as context.\n"
        "For DAY TRADE, combine 4H context with 15M confirmation.\n"
        "For SWING, make the 4H chart the primary structural timeframe.\n\n"
        "Do not automatically return NO TRADE.\n"
        "Return the strongest justified decision: BUY, SELL, or NO TRADE."
    )

    content_parts = [
        {"text": SYSTEM_PROMPT + "\n\n" + user_prompt},
        {"text": "IMAGE 1 = required 4H chart."},
        image_part(higher_image),
        {"text": "IMAGE 2 = required 15M chart."},
        image_part(lower_image),
    ]
    for timeframe in ("1D", "1H", "5M", "1M"):
        if timeframe in validated_additional_images:
            content_parts.append({
                "text": "ADDITIONAL CHART = " + timeframe + " timeframe. Use as supplemental evidence."
            })
            content_parts.append(image_part(validated_additional_images[timeframe]))

    payload = {

        "contents": [
            {
                "role": "user",
                "parts": content_parts,
            }
        ],

        "generationConfig": {

            "responseMimeType":
                "application/json",

            "responseJsonSchema":
                OUTPUT_SCHEMA,

            "thinkingConfig": {
                "thinkingLevel":
                    "low",
            },

            "maxOutputTokens":
                2400,
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
            "x-goog-api-key":
                key,

            "Content-Type":
                "application/json",

            "Accept":
                "application/json",
        },

        method="POST",
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=120,
        ) as response:

            raw = (
                response
                .read()
                .decode("utf-8")
            )

            data = json.loads(
                raw
            )

            if not isinstance(
                data,
                dict,
            ):

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

        message = _gemini_error_message(
            raw
        )

        if exc.code == 400:

            raise RuntimeError(
                "Gemini rejected the request: "
                + (
                    message
                    or "invalid request"
                )
            ) from exc

        if exc.code in {
            401,
            403,
        }:

            raise RuntimeError(
                "The Gemini API key was rejected: "
                + (
                    message
                    or "access denied"
                )
            ) from exc

        if exc.code == 404:

            raise RuntimeError(
                "The Gemini model was not found: "
                + (
                    message
                    or GEMINI_MODEL
                )
            ) from exc

        if exc.code == 429:

            raise RuntimeError(
                "Gemini quota or rate limit reached: "
                + (
                    message
                    or "quota exceeded"
                )
            ) from exc

        if 500 <= exc.code <= 599:

            raise RuntimeError(
                "Gemini is temporarily unavailable."
            ) from exc

        raise RuntimeError(
            "Gemini request failed: "
            + (
                message
                or "unknown error"
            )
        ) from exc

    except (
        urllib.error.URLError,
        TimeoutError,
    ) as exc:

        raise RuntimeError(
            "Gemini could not be reached."
        ) from exc

    except json.JSONDecodeError as exc:

        raise RuntimeError(
            "Gemini returned invalid JSON."
        ) from exc


# ============================================================
# RESPONSE PARSING
# ============================================================

def extract_output_text(
    response_data,
):
    if not isinstance(
        response_data,
        dict,
    ):

        raise RuntimeError(
            "Gemini returned an invalid response."
        )

    candidates = response_data.get(
        "candidates",
        [],
    )

    if not isinstance(
        candidates,
        list,
    ) or not candidates:

        raise RuntimeError(
            "Gemini returned no analysis candidate."
        )

    candidate = candidates[0]

    if not isinstance(
        candidate,
        dict,
    ):

        raise RuntimeError(
            "Gemini returned an invalid analysis candidate."
        )

    finish_reason = str(
        candidate.get(
            "finishReason",
            "",
        )
    ).strip()

    content = candidate.get(
        "content",
        {},
    )

    if not isinstance(
        content,
        dict,
    ):

        raise RuntimeError(
            "Gemini returned no analysis content."
        )

    parts = content.get(
        "parts",
        [],
    )

    if not isinstance(
        parts,
        list,
    ):

        raise RuntimeError(
            "Gemini returned no analysis parts."
        )

    pieces = []

    for part in parts:

        if not isinstance(
            part,
            dict,
        ):
            continue

        text = part.get(
            "text",
            "",
        )

        if (
            isinstance(
                text,
                str,
            )
            and text.strip()
        ):

            pieces.append(
                text.strip()
            )

    if not pieces:

        if finish_reason:
            raise RuntimeError(
                "Gemini returned no analysis text. "
                f"Finish reason: {finish_reason}"
            )

        raise RuntimeError(
            "Gemini returned no analysis text."
        )

    return "\n".join(
        pieces
    ).strip()


def clean_json_text(
    text,
):
    cleaned = str(
        text or ""
    ).strip()

    if not cleaned:
        return ""

    if cleaned.startswith(
        "```"
    ):

        lines = cleaned.splitlines()

        if lines:
            lines = lines[1:]

        if (
            lines
            and lines[-1].strip()
            == "```"
        ):

            lines = lines[:-1]

        cleaned = "\n".join(
            lines
        ).strip()

    return cleaned


def _safe_int(
    value,
):
    try:
        return int(value)
    except (
        TypeError,
        ValueError,
    ):
        return 0


def _clean_string(
    value,
):
    if value is None:
        return ""

    return str(
        value
    ).strip()


def _clean_string_list(
    value,
):
    if not isinstance(
        value,
        list,
    ):
        return []

    result = []

    for item in value:

        text = _clean_string(
            item
        )

        if text:
            result.append(
                text
            )

    return result


def parse_completed_response(
    response_data,
    instrument,
    trade_focus="DAY TRADE",
    additional_timeframes=None,
):
    text = clean_json_text(
        extract_output_text(
            response_data
        )
    )

    try:

        result = json.loads(
            text
        )

    except json.JSONDecodeError as exc:

        raise RuntimeError(
            "Gemini returned invalid analysis JSON."
        ) from exc

    if not isinstance(
        result,
        dict,
    ):

        raise RuntimeError(
            "Gemini returned an invalid analysis object."
        )

    # Do not trust the selected instrument echoed by the model. Independently
    # validate the chart-identity fields before accepting any trade signal.
    selected_instrument = _clean_string(instrument).upper()
    check_status = _clean_string(result.get("chart_instrument_status")).upper()
    if check_status not in {"MATCH", "MISMATCH", "UNREADABLE"}:
        check_status = "UNREADABLE"

    supplied_optional = {
        str(value).strip().upper()
        for value in (additional_timeframes or [])
        if str(value).strip()
    }
    detected_fields = [
        ("4H", "detected_4h_instrument", True),
        ("15M", "detected_15m_instrument", True),
        ("1D", "detected_1d_instrument", "1D" in supplied_optional),
        ("1H", "detected_1h_instrument", "1H" in supplied_optional),
        ("5M", "detected_5m_instrument", "5M" in supplied_optional),
        ("1M", "detected_1m_instrument", "1M" in supplied_optional),
    ]
    mismatches = []
    unreadable = []

    for timeframe, field_name, required_chart in detected_fields:
        detected = _clean_string(result.get(field_name)).upper()
        # Ignore optional timeframes that were not uploaded. If one was
        # uploaded, NONE/blank is a verification failure, not a pass.
        if not required_chart:
            continue
        if detected in {"", "NONE", "NOT PROVIDED", "N/A", "UNREADABLE", "UNKNOWN", "UNCLEAR"}:
            unreadable.append(timeframe)
            continue

        compact = "".join(character for character in detected if character.isalnum())
        # Broker suffixes such as EURUSDm or EURUSDpro are acceptable only
        # when the complete selected ticker is the leading symbol.
        if compact.startswith(selected_instrument) and len(compact) <= len(selected_instrument) + 5:
            compact = selected_instrument
        if compact != selected_instrument:
            mismatches.append((timeframe, detected))

    if check_status == "UNREADABLE" or unreadable:
        labels = ", ".join(unreadable) if unreadable else "one or more charts"
        raise RuntimeError(
            "Cannot verify chart instrument: the ticker on the "
            + labels
            + " chart could not be read clearly. Please upload a clearer screenshot "
              "with the symbol visible. No analysis was generated."
        )

    if check_status == "MISMATCH" or mismatches:
        if mismatches:
            details = "; ".join(
                timeframe + " chart shows " + detected
                for timeframe, detected in mismatches
            )
            raise RuntimeError(
                "Instrument mismatch: you selected "
                + selected_instrument
                + ", but the uploaded "
                + details
                + ". Select the correct instrument or upload matching charts. "
                  "No analysis was generated."
            )
        raise RuntimeError(
            "Instrument mismatch: Gemini detected that at least one uploaded "
            "chart does not match the selected "
            + selected_instrument
            + ". Please check the 4H, 15M, and optional chart uploads. "
              "No analysis was generated."
        )

    signal = (
        _clean_string(
            result.get(
                "signal"
            )
        )
        .upper()
    )

    if signal not in {
        "BUY",
        "SELL",
        "NO TRADE",
    }:

        signal = "NO TRADE"

    setup_status = _clean_string(result.get("setup_status")).upper()
    allowed_statuses = {"READY", "WAITING", "LATE ENTRY", "INVALID", "NO_SETUP"}
    if setup_status not in allowed_statuses:
        setup_status = "NO_SETUP"

    order_type = _clean_string(result.get("order_type")).upper()
    allowed_order_types = {"MARKET", "LIMIT", "STOP", "WAIT", "NONE"}
    if order_type not in allowed_order_types:
        order_type = "NONE"

    if setup_status in {"WAITING", "LATE ENTRY"}:
        order_type = "WAIT"
    elif setup_status in {"INVALID", "NO_SETUP"}:
        order_type = "NONE"
        signal = "NO TRADE"
    elif order_type not in {"MARKET", "LIMIT", "STOP"}:
        setup_status = "WAITING"
        order_type = "WAIT"

    warnings = _clean_string_list(result.get("warnings"))
    if setup_status == "LATE ENTRY":
        late_warning = "LATE ENTRY: the original entry opportunity has already passed. Do not chase price; wait for a fresh setup."
        if late_warning not in warnings:
            warnings.append(late_warning)
        late_explanation = "The original entry opportunity has passed. Do not chase the current price; wait for a fresh pullback and new confirmation."
        old_explanation = _clean_string(result.get("explanation"))
        if late_explanation not in old_explanation:
            result["explanation"] = (old_explanation + " " + late_explanation).strip()

    confidence = _safe_int(
        result.get(
            "confidence",
            0,
        )
    )

    result = {

        "signal":
            signal,

        "setup_status":
            setup_status,

        "order_type":
            order_type,

        "confidence":
            max(
                0,
                min(
                    100,
                    confidence,
                ),
            ),

        "instrument":
            _clean_string(
                instrument
            ).upper(),

        "trend":
            _clean_string(
                result.get(
                    "trend"
                )
            ),

        "trade_idea":
            _clean_string(
                result.get(
                    "trade_idea"
                )
            ),

        "higher_timeframe_context":
            _clean_string(
                result.get(
                    "higher_timeframe_context"
                )
            ),

        "lower_timeframe_confirmation":
            _clean_string(
                result.get(
                    "lower_timeframe_confirmation"
                )
            ),

        "entry":
            _clean_string(
                result.get(
                    "entry"
                )
            ),

        "stop_loss":
            _clean_string(
                result.get(
                    "stop_loss"
                )
            ),

        "take_profit_1":
            _clean_string(
                result.get(
                    "take_profit_1"
                )
            ),

        "take_profit_2":
            _clean_string(
                result.get(
                    "take_profit_2"
                )
            ),

        "risk_reward":
            _clean_string(
                result.get(
                    "risk_reward"
                )
            ),

        "duration":
            _clean_string(
                result.get(
                    "duration"
                )
            ),

        "data_analysis":
            _clean_string(
                result.get(
                    "data_analysis"
                )
            ),

        "explanation":
            _clean_string(
                result.get(
                    "explanation"
                )
            ),

        "contributing_methods":
            _clean_string_list(
                result.get(
                    "contributing_methods"
                )
            ),

        "weak_methods":
            _clean_string_list(
                result.get(
                    "weak_methods"
                )
            ),

        "conflicting_methods":
            _clean_string_list(
                result.get(
                    "conflicting_methods"
                )
            ),

        "news_fundamental_risk":
            _clean_string(
                result.get(
                    "news_fundamental_risk"
                )
            ),

        "warnings":
            warnings,
    }

    # For NO TRADE, price fields remain empty.
    if signal == "NO TRADE":

        result["entry"] = ""
        result["stop_loss"] = ""
        result["take_profit_1"] = ""
        result["take_profit_2"] = ""
        result["risk_reward"] = ""

    return result
