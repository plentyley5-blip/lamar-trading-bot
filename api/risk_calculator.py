"""Authenticated AI-assisted, read-only position-size estimate for LM Analyzer.

This endpoint does not connect to a brokerage account and never places orders.
The response is an estimate based on common retail contract assumptions; broker
symbol specifications and account-currency conversions can materially change it.
"""
import json
import math
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

from analysis_common import GEMINI_GENERATE_URL, api_key, json_response, validate_instrument
from api.user_security import extract_bearer_token, verify_access_token

MAX_REQUEST_BYTES = 16 * 1024
MAX_ACCOUNT_EQUITY = 1_000_000_000_000.0
MIN_RISK_PERCENT = 0.1
MAX_RISK_PERCENT = 2.0
MAX_TIMEOUT_SECONDS = 45


class RequestValidationError(ValueError):
    pass


def _read_json(handler):
    try:
        size = int(handler.headers.get("Content-Length", "0"))
    except (TypeError, ValueError) as exc:
        raise RequestValidationError("Invalid request length.") from exc
    if size <= 0:
        raise RequestValidationError("Request body is empty.")
    if size > MAX_REQUEST_BYTES:
        raise RequestValidationError("Risk calculator request is too large.")
    try:
        body = json.loads(handler.rfile.read(size).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RequestValidationError("Request body must be valid JSON.") from exc
    if not isinstance(body, dict):
        raise RequestValidationError("Request body must be a JSON object.")
    return body


def _number(body, field, required=True):
    value = body.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise RequestValidationError("Enter a valid " + field.replace("_", " ") + ".")
        return None
    if isinstance(value, bool):
        raise RequestValidationError("Invalid numeric value for " + field.replace("_", " ") + ".")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise RequestValidationError("Invalid numeric value for " + field.replace("_", " ") + ".") from exc
    if not math.isfinite(result):
        raise RequestValidationError("Invalid numeric value for " + field.replace("_", " ") + ".")
    return result


def _validate_input(body):
    try:
        instrument = validate_instrument(body.get("instrument"))
    except ValueError as exc:
        raise RequestValidationError(str(exc)) from exc

    direction = str(body.get("direction", "")).strip().upper()
    if direction not in ("BUY", "SELL"):
        raise RequestValidationError("Risk sizing is only available for a BUY or SELL result, not NO TRADE.")

    equity = _number(body, "account_equity")
    if equity <= 0 or equity > MAX_ACCOUNT_EQUITY:
        raise RequestValidationError("Account equity must be greater than zero and within the supported range.")

    currency = str(body.get("account_currency", "USD")).strip().upper()
    if len(currency) != 3 or not currency.isalpha() or not currency.isascii():
        raise RequestValidationError("Account currency must be a three-letter code such as USD or UGX.")

    risk_percent = _number(body, "risk_percent")
    if risk_percent < MIN_RISK_PERCENT or risk_percent > MAX_RISK_PERCENT:
        raise RequestValidationError("Risk per trade must be between 0.1% and 2%.")

    entry = _number(body, "entry")
    stop = _number(body, "stop_loss")
    tp1 = _number(body, "take_profit_1")
    tp2 = _number(body, "take_profit_2", required=False)
    if any(x <= 0 for x in (entry, stop, tp1)) or (tp2 is not None and tp2 <= 0):
        raise RequestValidationError("Entry, stop-loss and take-profit prices must be greater than zero.")
    if entry == stop:
        raise RequestValidationError("Entry and stop-loss cannot be the same price.")
    if direction == "BUY":
        if stop >= entry:
            raise RequestValidationError("For a BUY, stop-loss must be below entry.")
        if tp1 <= entry or (tp2 is not None and tp2 <= entry):
            raise RequestValidationError("For a BUY, take-profit levels must be above entry.")
    else:
        if stop <= entry:
            raise RequestValidationError("For a SELL, stop-loss must be above entry.")
        if tp1 >= entry or (tp2 is not None and tp2 >= entry):
            raise RequestValidationError("For a SELL, take-profit levels must be below entry.")

    risk_cash = equity * (risk_percent / 100.0)
    return {
        "instrument": instrument,
        "direction": direction,
        "account_equity": equity,
        "account_currency": currency,
        "risk_percent": risk_percent,
        "cash_risk_limit": risk_cash,
        "entry": entry,
        "stop_loss": stop,
        "take_profit_1": tp1,
        "take_profit_2": tp2,
    }


def _risk_reward(entry, stop, target):
    if target is None:
        return None
    risk_distance = abs(entry - stop)
    if risk_distance <= 0:
        return None
    return round(abs(target - entry) / risk_distance, 3)


def _json_text_from_gemini(data):
    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError("Gemini did not return a risk estimate. Please try again.")
    content = candidates[0].get("content") or {}
    parts = content.get("parts") or []
    snippets = [part.get("text", "").strip() for part in parts if isinstance(part, dict) and isinstance(part.get("text"), str) and part.get("text", "").strip()]
    if not snippets:
        raise RuntimeError("Gemini returned an empty risk estimate. Please try again.")
    raw = "\n".join(snippets).strip()
    if raw.startswith("```"):
        rows = raw.splitlines()
        if rows:
            rows = rows[1:]
        if rows and rows[-1].strip() == "```":
            rows = rows[:-1]
        raw = "\n".join(rows).strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Gemini returned an unreadable estimate. Please try again.") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Gemini returned an invalid risk estimate. Please try again.")
    return parsed


def _ask_gemini(values):
    key = api_key()
    data = values
    prompt = f"""You are LM Analyzer's conservative, educational position-sizing assistant. Do not execute orders or make guarantees.

Calculate an ESTIMATED lot size for this trade setup. Use a conservative risk budget, already capped by the user at {data['risk_percent']:.4f}% of equity = {data['cash_risk_limit']:.8f} {data['account_currency']}. Do not exceed that cash-risk budget. The preferred size must be rounded DOWN (never up) to a commonly used volume step. If the size would be below minimum trade volume, or the contract size/conversion cannot be reasonably estimated, return can_estimate=false and recommended_lot_size=0. Never invent a claim that broker-specific details are known.

Inputs:
- Instrument: {data['instrument']}
- Direction: {data['direction']}
- Account equity: {data['account_equity']:.8f} {data['account_currency']}
- Maximum planned risk: {data['risk_percent']:.4f}% = {data['cash_risk_limit']:.8f} {data['account_currency']}
- Entry: {data['entry']:.10f}
- Stop-loss: {data['stop_loss']:.10f}
- Take-profit 1: {data['take_profit_1']:.10f}
- Take-profit 2: {('not provided' if data['take_profit_2'] is None else format(data['take_profit_2'], '.10f'))}

Method:
- Use standard retail Forex contract size of 100,000 base currency units per standard lot only where appropriate.
- Typical XAUUSD often uses 100 troy ounces per lot and XAGUSD often uses 5,000 troy ounces, but brokers vary; disclose this assumption.
- BTCUSD/ETHUSD contract sizes vary significantly across brokers; only estimate if you can explicitly state the assumption and whether conversion is reliable. Otherwise return can_estimate=false.
- For FX pairs whose quote currency differs from the account currency, conversion impacts loss. Do not pretend to know a live conversion quote. Use a reasonable conventional estimate only when the currency conversion is reliable; disclose it. If you cannot, return can_estimate=false.
- Calculate stop-loss risk based on the distance from entry to stop, instrument contract economics, and account-currency conversion. Account risk must never exceed the cash-risk limit. When in doubt, choose no estimate.
- Reward/risk ratios are computed from the supplied prices and should match: abs(TP-entry)/abs(entry-SL).
- The app is not connected to the user's broker. Do not claim the result is exact or that it accounts for the broker's leverage, margin, spread, commissions, swaps, slippage, gaps or volume limits.

Return JSON only with these fields:
{{
  "can_estimate": boolean,
  "recommended_lot_size": number,
  "estimated_cash_loss_at_stop": number or null,
  "explanation": "short plain-language explanation",
  "assumptions": "explicit contract size and any conversion assumptions used; if cannot estimate, explain missing specification",
  "warning": "short warning to verify the broker symbol specification"
}}

If you cannot safely estimate, use can_estimate=false and recommended_lot_size=0. Do not return multiple options or a lot-size range. Do not increase the size to satisfy a broker minimum."""

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.1,
            "maxOutputTokens": 900,
        },
    }
    request = urllib.request.Request(
        GEMINI_GENERATE_URL,
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        headers={"x-goog-api-key": key, "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=MAX_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        if exc.code == 429:
            raise RuntimeError("The AI risk calculator has reached its request limit. Please try again later.") from exc
        if exc.code in (401, 403):
            raise RuntimeError("The AI service rejected its configuration. Please contact support.") from exc
        raise RuntimeError("The AI risk calculator request failed. Please try again.") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("The AI risk calculator could not be reached. Please try again.") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("The AI service returned an invalid response. Please try again.") from exc

    if not isinstance(data, dict):
        raise RuntimeError("The AI service returned an invalid response. Please try again.")
    return _json_text_from_gemini(data)


def _build_response(values, ai):
    can_estimate = ai.get("can_estimate") is True
    try:
        lot = float(ai.get("recommended_lot_size", 0) or 0)
    except (TypeError, ValueError):
        lot = 0.0
    try:
        estimated_loss = ai.get("estimated_cash_loss_at_stop")
        estimated_loss = None if estimated_loss is None else float(estimated_loss)
    except (TypeError, ValueError):
        estimated_loss = None

    # Refuse malformed, non-positive or unexpectedly large AI outputs. A lot
    # estimate without its estimated stop-loss cash exposure is not sufficient
    # to verify the stated risk budget, so suppress it instead of guessing.
    if not math.isfinite(lot) or lot <= 0 or lot > 1000:
        can_estimate = False
    if estimated_loss is None or not math.isfinite(estimated_loss) or estimated_loss < 0 or estimated_loss > values["cash_risk_limit"] * 1.05:
        can_estimate = False

    # Standard retail volume steps are commonly 0.01 lots, but the broker's
    # symbol specification must still be checked. Always round down, never up.
    if can_estimate:
        lot = math.floor(lot * 100.0 + 1e-10) / 100.0
        if lot < 0.01:
            can_estimate = False
    if not can_estimate:
        lot_value = None
    else:
        lot_value = f"{lot:.2f}".rstrip("0").rstrip(".")
        if not lot_value:
            lot_value = "0"

    explanation = str(ai.get("explanation", "")).strip()[:900]
    assumptions = str(ai.get("assumptions", "")).strip()[:1200]
    warning = str(ai.get("warning", "")).strip()[:800] or (
        "This is an AI estimate using typical assumptions. Confirm contract size, tick value, minimum volume, margin and account-currency conversion in your broker's symbol specification before using it."
    )
    if not explanation:
        explanation = "The estimate depends on the contract assumptions shown below; verify them with your broker before using the lot size."
    if not assumptions:
        assumptions = "The AI did not provide complete contract assumptions. Do not rely on an unverified lot-size figure."
    return {
        "can_estimate": can_estimate,
        "recommended_lot_size": lot_value,
        "estimated_cash_loss_at_stop": round(estimated_loss, 4) if can_estimate and estimated_loss is not None else None,
        "account_equity": round(values["account_equity"], 4),
        "account_currency": values["account_currency"],
        "risk_percent": values["risk_percent"],
        "cash_risk_limit": round(values["cash_risk_limit"], 4),
        "risk_reward_tp1": _risk_reward(values["entry"], values["stop_loss"], values["take_profit_1"]),
        "risk_reward_tp2": _risk_reward(values["entry"], values["stop_loss"], values["take_profit_2"]),
        "explanation": explanation,
        "assumptions": assumptions,
        "warning": warning,
        "mode": "estimate_only",
    }


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        json_response(self, 204, {})

    def do_GET(self):
        json_response(self, 200, {"status": "LM Analyzer risk calculator online", "mode": "estimate_only"})

    def do_POST(self):
        try:
            access_token = extract_bearer_token(self)
            user = verify_access_token(access_token)
            if not isinstance(user, dict) or not str(user.get("id", "")).strip():
                raise RequestValidationError("Please sign in again before using Risk Calculator.")
            body = _read_json(self)
            values = _validate_input(body)
            ai = _ask_gemini(values)
            response = _build_response(values, ai)
            json_response(self, 200, response)
        except RequestValidationError as exc:
            json_response(self, 400, {"error": str(exc)})
        except ValueError as exc:
            json_response(self, 401, {"error": str(exc) or "Your session is invalid. Please sign in again."})
        except RuntimeError as exc:
            json_response(self, 502, {"error": str(exc) or "AI risk calculator is temporarily unavailable."})
        except Exception:
            json_response(self, 500, {"error": "The AI risk calculator is temporarily unavailable. Please try again."})


def handler_entrypoint(request, context=None):
    return handler
