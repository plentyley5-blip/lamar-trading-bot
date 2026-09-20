import base64
import hashlib
import hmac
import json
import os
import time

OPENAI_URL = "https://api.openai.com/v1/responses"
MODEL = "gpt-5.6-luna"
JOB_TTL_SECONDS = 24 * 60 * 60

SYSTEM_PROMPT = """
You are the vision analysis engine for LAMAR TRADING BOT.

Analyze the supplied 4H and 15M trading charts for the selected instrument.

Return ONLY JSON matching the supplied schema.

Rules:
1. Use both screenshots together.
2. Be conservative.
3. If the chart evidence is insufficient, return NO TRADE.
4. Never invent an exact entry, stop loss, take profit, or risk/reward.
5. Confidence is an analysis-confidence score, not a guaranteed probability of profit.
6. For BUY or SELL, only provide exact price levels when they can be reasonably read from the screenshots.
7. For NO TRADE, return empty strings for entry, stop_loss, take_profit_1, take_profit_2, and risk_reward.
8. Explain the decision using visible chart evidence.
9. Evaluate higher-timeframe structure, support/resistance, price action, liquidity behavior, Fibonacci context, premium/discount context, and lower-timeframe confirmation.
10. Do not present internal strategy names as UI categories.
11. Mention warnings when chart quality or price visibility is poor.
""".strip()

OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "signal": {"type": "string", "enum": ["BUY", "SELL", "NO TRADE"]},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        "instrument": {"type": "string"},
        "higher_timeframe_context": {"type": "string"},
        "lower_timeframe_confirmation": {"type": "string"},
        "entry": {"type": "string"},
        "stop_loss": {"type": "string"},
        "take_profit_1": {"type": "string"},
        "take_profit_2": {"type": "string"},
        "risk_reward": {"type": "string"},
        "data_analysis": {"type": "string"},
        "explanation": {"type": "string"},
        "warnings": {"type": "string"},
    },
    "required": [
        "signal",
        "confidence",
        "instrument",
        "higher_timeframe_context",
        "lower_timeframe_confirmation",
        "entry",
        "stop_loss",
        "take_profit_1",
        "take_profit_2",
        "risk_reward",
        "data_analysis",
        "explanation",
        "warnings",
    ],
}


def json_response(handler, status, payload, extra_headers=None):
    raw = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Cache-Control", "no-store")
    if extra_headers:
        for k, v in extra_headers.items():
            handler.send_header(k, str(v))
    handler.end_headers()
    try:
        handler.wfile.write(raw)
    except Exception:
        pass


def api_key():
    return os.getenv("OPENAI_API_KEY", "").strip()


def make_job_token(response_id, instrument):
    key = api_key().encode("utf-8")
    created = int(time.time())
    payload_obj = {
        "r": response_id,
        "t": created,
        "i": instrument,
    }
    payload = json.dumps(payload_obj, separators=(",", ":")).encode("utf-8")
    encoded_payload = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    signature = hmac.new(
        key,
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return encoded_payload + "." + signature


def read_job_token(token):
    if not token:
        raise ValueError("Missing analysis job id.")
    parts = token.split(".", 1)
    if len(parts) != 2:
        raise ValueError("Invalid analysis job id.")
    encoded_payload, signature = parts
    expected = hmac.new(
        api_key().encode("utf-8"),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("Invalid analysis job id.")
    padded = encoded_payload + "=" * (-len(encoded_payload) % 4)
    try:
        payload_obj = json.loads(
            base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        )
    except Exception as exc:
        raise ValueError("Invalid analysis job id.") from exc
    response_id = str(payload_obj.get("r") or "")
    instrument = str(payload_obj.get("i") or "")
    try:
        created = int(payload_obj.get("t"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid analysis job id.") from exc
    if not response_id or time.time() - created > JOB_TTL_SECONDS:
        raise ValueError("This analysis job has expired.")
    return response_id, instrument


def extract_output_text(response_data):
    direct = response_data.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    for item in response_data.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if not isinstance(content, dict):
                continue
            if content.get("type") == "output_text":
                text = content.get("text")
                if isinstance(text, str) and text.strip():
                    return text.strip()
    raise RuntimeError("The model returned no analysis text.")


def clean_json_text(text):
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def parse_completed_response(response_data, instrument):
    text = clean_json_text(extract_output_text(response_data))
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("The model returned an invalid analysis format.") from exc
    if not isinstance(result, dict):
        raise RuntimeError("The model returned an invalid analysis object.")
    result["instrument"] = instrument
    signal = str(result.get("signal", "NO TRADE")).upper().strip()
    if signal not in {"BUY", "SELL", "NO TRADE"}:
        signal = "NO TRADE"
    result["signal"] = signal
    try:
        confidence = int(result.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0
    result["confidence"] = max(0, min(100, confidence))
    if signal == "NO TRADE":
        result["entry"] = ""
        result["stop_loss"] = ""
        result["take_profit_1"] = ""
        result["take_profit_2"] = ""
        result["risk_reward"] = ""
    return result


def classify_error_body(raw):
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return "", ""
    err = data.get("error")
    if not isinstance(err, dict):
        return "", ""
    return str(err.get("code") or ""), str(err.get("message") or "")
