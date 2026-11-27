"""Independent backend AI decision engine for LM AUTO TRADER.

This engine consumes only fresh MT5 market bars supplied by the linked terminal.
It deliberately does not read LM Analyzer chart uploads or analysis_jobs.
"""
import json
import math
import random
import time
import urllib.error
import urllib.request

from analysis_common import GEMINI_GENERATE_URL, api_key

DECISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "symbol": {"type": "string"},
                    "decision": {"type": "string", "enum": ["BUY", "SELL", "WAIT"]},
                    "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
                    "entry_price": {"type": "number"},
                    "stop_loss": {"type": "number"},
                    "take_profit": {"type": "number"},
                    "rationale": {"type": "string"},
                    "confirmations": {"type": "array", "items": {"type": "string"}},
                    "invalidation": {"type": "string"},
                },
                "required": [
                    "symbol", "decision", "confidence", "entry_price",
                    "stop_loss", "take_profit", "rationale", "confirmations",
                    "invalidation"
                ],
            },
        }
    },
    "required": ["decisions"],
}

SYSTEM_PROMPT = """
You are LM AUTO TRADER's independent multi-scalping market-decision engine.
You analyse fresh numerical OHLC candles supplied by the connected broker's
MT5 terminal. You do not receive, request, or depend on LM Analyzer uploaded
charts, chart-analysis jobs, or user-created signals.

MISSION
For every supplied symbol return exactly one BUY, SELL, or WAIT decision.
The trading style is multi-scalping: M1 is the execution timeframe and M5 is
the confirmation/context timeframe. Do not use a local deterministic EMA/RSI
strategy. Make the actual market decision from the supplied bars, structure,
momentum, candle behaviour, volatility, swing levels, break/retest, liquidity
and price reaction.

QUALITY RULES
- Require at least TWO independent confirming observations before BUY/SELL.
  One should come from M1 setup/trigger and another from M5 structure or
  directional confirmation. State them separately in confirmations.
- Do not force a trade. If evidence is weak, mixed, late, stale, or incomplete,
  return WAIT.
- Do not hallucinate candles, prices, news or events. Only use supplied data.
- If news_blocked is true, return WAIT for that symbol.
- Do not chase a move. Entry must be close to the latest bid/ask and realistic
  for a market order now.
- BUY: stop_loss < entry_price < take_profit.
- SELL: take_profit < entry_price < stop_loss.
- Set a structural stop beyond the nearest clear invalidation point, sized
  to recent M1 volatility. Set a realistic nearby scalp target with reward
  at least 1.2 times the stop distance where the structure permits.
- If a defensible stop and target cannot be determined, return WAIT.
- Confidence is setup quality, not probability or guarantee of profit.
- Never use more than the provided 1-minute and 5-minute candles.
- Each symbol's provided broker symbol string must be returned unchanged.
- Output JSON only and include every supplied symbol exactly once.
""".strip()


def _finite_price(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _call_gemini(market_data):
    prompt = (
        SYSTEM_PROMPT
        + "\n\nBROKER MARKET DATA (JSON):\n"
        + json.dumps(market_data, separators=(",", ":"), ensure_ascii=True)
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": DECISION_SCHEMA,
            "temperature": 0.15,
            "maxOutputTokens": 3200,
        },
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        GEMINI_GENERATE_URL,
        data=body,
        headers={
            "x-goog-api-key": api_key(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    last_error = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=28) as response:
                data = json.loads(response.read().decode("utf-8", errors="replace"))
            candidates = data.get("candidates") if isinstance(data, dict) else None
            if not isinstance(candidates, list) or not candidates:
                raise RuntimeError("AI decision engine returned no candidate.")
            parts = ((candidates[0].get("content") or {}).get("parts") or [])
            raw = "".join(str(part.get("text", "")) for part in parts if isinstance(part, dict))
            result = json.loads(raw)
            if not isinstance(result, dict) or not isinstance(result.get("decisions"), list):
                raise RuntimeError("AI decision engine returned an invalid decision set.")
            return result["decisions"]
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            last_error = RuntimeError("AI market analysis failed (HTTP {}): {}".format(exc.code, detail))
            if exc.code not in (408, 425, 429) and not 500 <= exc.code <= 599:
                break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
            last_error = exc
        if attempt == 0:
            time.sleep(1.0 + random.random() * 0.5)
    raise RuntimeError(str(last_error or "AI market analysis did not complete.")[:400])


def analyse(market_data):
    """Return validated per-symbol model decisions; unknown symbols are discarded."""
    if not isinstance(market_data, list) or not market_data or len(market_data) > 8:
        raise ValueError("Market data must contain 1 to 8 symbols.")
    expected = {}
    for item in market_data:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol", "")).strip()
        if not symbol or len(symbol) > 32:
            continue
        m1, m5 = item.get("m1"), item.get("m5")
        if not isinstance(m1, list) or len(m1) < 12 or not isinstance(m5, list) or len(m5) < 8:
            continue
        if item.get("news_blocked") is True:
            # Explicitly pass this to the model so the result contains a WAIT reason.
            pass
        expected[symbol] = item
    if not expected:
        raise ValueError("No symbol has enough valid M1 and M5 candles.")
    decisions = _call_gemini(list(expected.values()))
    if len(decisions) > len(expected) + 2:
        raise RuntimeError("AI returned too many market decisions.")
    clean = []
    seen = set()
    for item in decisions:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol", "")).strip()
        if symbol not in expected or symbol in seen:
            continue
        seen.add(symbol)
        direction = str(item.get("decision", "")).strip().upper()
        if direction not in ("BUY", "SELL", "WAIT"):
            direction = "WAIT"
        try:
            confidence = int(item.get("confidence", 0))
        except (TypeError, ValueError, OverflowError):
            confidence = 0
        confidence = max(0, min(100, confidence))
        entry = _finite_price(item.get("entry_price"))
        stop = _finite_price(item.get("stop_loss"))
        target = _finite_price(item.get("take_profit"))
        confirmations = item.get("confirmations")
        if not isinstance(confirmations, list):
            confirmations = []
        confirmations = [str(value)[:180] for value in confirmations[:5] if str(value).strip()]
        rationale = str(item.get("rationale", "")).strip()[:800]
        invalidation = str(item.get("invalidation", "")).strip()[:300]
        source = expected[symbol]
        if source.get("news_blocked") is True:
            direction = "WAIT"
            rationale = "Blocked by the MT5 high-impact news window."
        if direction in ("BUY", "SELL"):
            if entry is None or stop is None or target is None or len(confirmations) < 2:
                direction = "WAIT"
            elif direction == "BUY" and not (stop < entry < target):
                direction = "WAIT"
            elif direction == "SELL" and not (target < entry < stop):
                direction = "WAIT"
        clean.append({
            "symbol": symbol,
            "decision": direction,
            "confidence": confidence,
            "entry_price": entry,
            "stop_loss": stop,
            "take_profit": target,
            "rationale": rationale,
            "confirmations": confirmations,
            "invalidation": invalidation,
        })
    # Missing model symbols are represented as WAIT, never silently treated as a trade.
    for symbol in expected:
        if symbol not in seen:
            clean.append({
                "symbol": symbol, "decision": "WAIT", "confidence": 0,
                "entry_price": None, "stop_loss": None, "take_profit": None,
                "rationale": "AI did not return a decision for this symbol.",
                "confirmations": [], "invalidation": "",
            })
    return clean
