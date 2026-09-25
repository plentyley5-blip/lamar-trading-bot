import base64
import json
import os
import uuid
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

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash").strip()
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
GEMINI_API_REVISION = "2026-05-20"
MAX_REQUEST_BYTES = 15 * 1024 * 1024


def json_response(handler, status_code, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
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
    handler.send_header("Content-Length", str(len(body)))
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
        str
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
        1
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
        "data": encoded,
    }


def build_prompt(
    instrument,
    trade_focus
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
11. Do not guarantee profit.
12. Return only valid JSON.
13. Do not use markdown code fences.

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


def create_gemini_interaction(
    instrument,
    trade_focus,
    higher,
    lower
):
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is missing from Vercel."
        )

    payload = {
        "model": GEMINI_MODEL,

        "input": [
            {
                "type": "text",
                "text": build_prompt(
                    instrument,
                    trade_focus
                )
            },
            {
                "type": "image",
                "data": higher["data"],
                "mime_type": higher["mime_type"]
            },
            {
                "type": "image",
                "data": lower["data"],
                "mime_type": lower["mime_type"]
            }
        ],

        "background": True,

        "response_format": {
            "type": "text",
            "mime_type": "application/json",
            "schema": OUTPUT_SCHEMA
        }
    }

    request = urllib.request.Request(
        GEMINI_URL,
        data=json.dumps(
            payload,
            separators=(",", ":")
        ).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-goog-api-key": GEMINI_API_KEY,
            "Api-Revision": GEMINI_API_REVISION
        }
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=25
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
            obj = json.loads(detail)

            message = (
                obj
                .get("error", {})
                .get("message")
                or detail[:2000]
            )

        except Exception:
            message = detail[:2000]

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
        obj = json.loads(raw)

    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Gemini returned invalid API JSON."
        ) from exc

    interaction_id = str(
        obj.get(
            "id",
            ""
        )
    ).strip()

    if not interaction_id:
        raise RuntimeError(
            "Gemini did not return an interaction ID."
        )

    status = str(
        obj.get(
            "status",
            "in_progress"
        )
    ).strip().lower()

    if status in {
        "failed",
        "cancelled",
        "expired"
    }:
        raise RuntimeError(
            "Gemini did not start the analysis."
        )

    return interaction_id


def create_job_row(
    job_id,
    user_id,
    instrument,
    trade_focus,
    interaction_id
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
        "status": "in_progress",
        "openai_response_id": interaction_id,
    }

    request = urllib.request.Request(
        url,
        data=json.dumps(
            payload,
            separators=(",", ":")
        ).encode("utf-8"),
        method="POST",
        headers={
            "apikey": key,
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Prefer": "return=minimal",
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
            "Supabase job creation error: "
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
                "4H chart"
            )

            lower = validate_image(
                data.get(
                    "lower_timeframe_image"
                ),
                "15M chart"
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
                            "error": "Daily analysis limit reached.",
                            "daily_limit": 4,
                            "remaining": 0,
                            "is_owner": False
                        }
                    )
                    return

                reserved = True

            interaction_id = (
                create_gemini_interaction(
                    instrument,
                    trade_focus,
                    higher,
                    lower
                )
            )

            job_id = str(
                uuid.uuid4()
            )

            try:
                create_job_row(
                    job_id,
                    user_id,
                    instrument,
                    trade_focus,
                    interaction_id
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

            remaining = None

            if not owner:
                remaining = max(
                    0,
                    4 - int(slot)
                )

            json_response(
                self,
                202,
                {
                    "status": "in_progress",
                    "job_id": job_id,
                    "poll_after_seconds": 2,
                    "is_owner": owner,
                    "daily_limit": (
                        None
                        if owner
                        else 4
                    ),
                    "remaining": remaining
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
