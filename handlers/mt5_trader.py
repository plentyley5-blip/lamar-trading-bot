"""Authenticated LM Analyzer <-> MetaTrader 5 bridge.

The MT5 client can trade only after the account owner links the terminal and
explicitly enables auto trading with bounded risk settings. Server-side live
trading is feature-gated and defaults OFF. This endpoint never places trades
or moves money; execution occurs only inside the user's MT5 terminal.
"""

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler

from analysis_common import json_response
from api.user_security import (
    extract_bearer_token,
    get_supabase_service_key,
    get_supabase_url,
    verify_access_token,
)

MAX_BODY_BYTES = 32 * 1024
DEVICE_TOKEN_BYTES = 48
LINK_CODE_BYTES = 32
LINK_CODE_TTL_SECONDS = 15 * 60
MAX_SIGNAL_AGE_MINUTES = 120
MT5_MAGIC_NUMBER = 26112701
PRICE_RE = re.compile(r"^(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)$")


class RequestError(ValueError):
    pass


def _json_body(handler):
    try:
        size = int(handler.headers.get("Content-Length", "0"))
    except (TypeError, ValueError) as exc:
        raise RequestError("Invalid request length.") from exc
    if size <= 0 or size > MAX_BODY_BYTES:
        raise RequestError("Request body is empty or too large.")
    try:
        body = json.loads(handler.rfile.read(size).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RequestError("Request body must be valid JSON.") from exc
    if not isinstance(body, dict):
        raise RequestError("Request body must be a JSON object.")
    return body


def _text_response(handler, status, text):
    body = str(text).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    if handler.command != "HEAD" and body:
        handler.wfile.write(body)


def _db_request(method, path, payload=None, prefer=None, timeout=12):
    base = get_supabase_url().rstrip("/")
    key = get_supabase_service_key()
    headers = {
        "apikey": key,
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    data = None if payload is None else json.dumps(
        payload, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code == 409:
            raise RequestError("This signal or deal has already been recorded.") from exc
        raise RuntimeError("Trading database request failed ({}).".format(exc.code)) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Trading database is temporarily unavailable.") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("Trading database returned an invalid response.") from exc


def _secret_key():
    secret = os.environ.get("LM_MT5_TOKEN_PEPPER", "").strip()
    if not secret:
        secret = os.environ.get("JOB_TOKEN_SECRET", "").strip()
    if not secret:
        secret = os.environ.get("GEMINI_API_KEY", "").strip()
    if not secret:
        raise RuntimeError("LM_MT5_TOKEN_PEPPER or JOB_TOKEN_SECRET is required.")
    return secret.encode("utf-8")


def _token_hash(token):
    return hmac.new(_secret_key(), str(token).encode("utf-8"), hashlib.sha256).hexdigest()


def _query(table, **params):
    encoded = urllib.parse.urlencode(params, safe="(),.*:")
    return "/rest/v1/{}?{}".format(table, encoded)


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_device(handler):
    token = extract_bearer_token(handler)
    if len(token) < 40:
        raise RequestError("MT5 terminal is not linked. Generate a new link code in LM Analyzer.")
    token_hash = _token_hash(token)
    rows = _db_request(
        "GET",
        _query(
            "lm_mt5_devices",
            select="id,user_id,account_login,broker_server,account_mode,auto_trade_enabled,risk_per_trade,max_daily_loss_percent,max_open_positions,max_trades_per_day,max_lot,min_confidence,max_signal_age_minutes,max_entry_deviation_points,max_spread_points,revoked_at,last_seen_at",
            token_hash="eq." + token_hash,
            revoked_at="is.null",
            limit="1",
        ),
    )
    if not isinstance(rows, list) or not rows:
        raise RequestError("MT5 link is invalid or has been revoked. Link the account again.")
    device = rows[0]
    # Avoid writing the device row on every terminal poll; one-minute heartbeat is enough.
    last_seen = device.get("last_seen_at")
    should_touch = True
    if last_seen:
        try:
            last_seen_dt = datetime.fromisoformat(str(last_seen).replace("Z", "+00:00"))
            if last_seen_dt.tzinfo is None:
                last_seen_dt = last_seen_dt.replace(tzinfo=timezone.utc)
            should_touch = (datetime.now(timezone.utc) - last_seen_dt).total_seconds() >= 60
        except (TypeError, ValueError):
            should_touch = True
    if should_touch:
        _db_request(
            "PATCH",
            _query("lm_mt5_devices", id="eq." + str(device["id"])),
            {"last_seen_at": _now_iso()},
            prefer="return=minimal",
        )
    return device


def _check_account_identity(device, account_login=None, broker_server=None):
    if account_login is not None:
        try:
            login_number = float(account_login)
            if not login_number.is_integer():
                raise ValueError("integer required")
            login = int(login_number)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RequestError("Invalid MT5 account number.") from exc
        if login != int(device["account_login"]):
            raise RequestError("This MT5 link belongs to a different trading account.")
    if broker_server is not None:
        server = str(broker_server).strip()
        if not server or not hmac.compare_digest(server, str(device["broker_server"])):
            raise RequestError("This MT5 link belongs to a different broker server.")


def _number(value, field, minimum, maximum, integer=False):
    if isinstance(value, bool) or value is None:
        raise RequestError("Invalid " + field.replace("_", " ") + ".")
    try:
        raw_number = float(value)
        if integer and not raw_number.is_integer():
            raise ValueError("integer required")
        number = int(raw_number) if integer else raw_number
    except (TypeError, ValueError, OverflowError) as exc:
        raise RequestError("Invalid " + field.replace("_", " ") + ".") from exc
    if not math.isfinite(float(number)) or number < minimum or number > maximum:
        raise RequestError(field.replace("_", " ").capitalize() + " must be between {} and {}.".format(minimum, maximum))
    return number


def _single_price(value):
    """Accept one exact numeric price only; reject ambiguous ranges and prose."""
    text = str(value or "").strip().replace(",", "")
    if not PRICE_RE.fullmatch(text):
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _eligible_signal(result, instrument, min_confidence):
    if not isinstance(result, dict):
        return None
    direction = str(result.get("signal", "")).strip().upper()
    if direction not in ("BUY", "SELL"):
        return None
    if str(result.get("setup_status", "")).strip().upper() != "READY":
        return None
    if str(result.get("order_type", "")).strip().upper() != "MARKET":
        return None
    if str(result.get("chart_instrument_status", "")).strip().upper() != "MATCH":
        return None
    result_instrument = re.sub(r"[^A-Z0-9]", "", str(result.get("instrument", "")).upper())
    row_instrument = re.sub(r"[^A-Z0-9]", "", str(instrument or "").upper())
    if not row_instrument or result_instrument != row_instrument:
        return None
    try:
        confidence_value = float(result.get("confidence", 0))
        if not confidence_value.is_integer():
            return None
        confidence = int(confidence_value)
    except (TypeError, ValueError, OverflowError):
        return None
    if confidence < int(min_confidence) or confidence > 100:
        return None
    entry = _single_price(result.get("entry"))
    stop = _single_price(result.get("stop_loss"))
    target = _single_price(result.get("take_profit_1"))
    if entry is None or stop is None or target is None:
        return None
    if direction == "BUY" and not (stop < entry < target):
        return None
    if direction == "SELL" and not (target < entry < stop):
        return None
    return {
        "instrument": row_instrument,
        "direction": direction,
        "entry": entry,
        "stop_loss": stop,
        "take_profit_1": target,
        "confidence": confidence,
    }


def _decode_job_result(encoded):
    text = str(encoded or "").strip()
    if not text:
        return None
    try:
        padded = text + "=" * (-len(text) % 4)
        import base64
        result = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception:
        return None
    return result if isinstance(result, dict) else None


def _create_link_code(user):
    code = secrets.token_urlsafe(LINK_CODE_BYTES)
    code_hash = _token_hash(code)
    expires = datetime.now(timezone.utc) + timedelta(seconds=LINK_CODE_TTL_SECONDS)
    _db_request(
        "POST",
        "/rest/v1/lm_mt5_link_codes",
        {
            "user_id": str(user["id"]),
            "code_hash": code_hash,
            "expires_at": expires.isoformat(timespec="seconds").replace("+00:00", "Z"),
        },
        prefer="return=minimal",
    )
    return {
        "link_code": code,
        "expires_in_seconds": LINK_CODE_TTL_SECONDS,
        "expires_at": expires.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "instructions": "Enter this one-time code in LM AUTO TRADER inside MT5. It expires after 15 minutes and can be used once.",
    }


def _link_terminal(body):
    code = str(body.get("link_code", "")).strip()
    if len(code) < 40 or len(code) > 128:
        raise RequestError("Generate a valid one-time MT5 link code in LM Analyzer.")
    try:
        login_value = float(body.get("account_login"))
        if not login_value.is_integer():
            raise ValueError("integer required")
        account_login = int(login_value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RequestError("Invalid MT5 account number.") from exc
    if account_login <= 0:
        raise RequestError("Invalid MT5 account number.")
    broker_server = str(body.get("broker_server", "")).strip()[:120]
    if not broker_server:
        raise RequestError("MT5 broker server is required.")
    account_mode = str(body.get("account_mode", "")).strip().lower()
    if account_mode not in ("demo", "real", "contest"):
        raise RequestError("Invalid MT5 account type.")

    code_hash = _token_hash(code)
    code_rows = _db_request(
        "PATCH",
        _query(
            "lm_mt5_link_codes",
            code_hash="eq." + code_hash,
            consumed_at="is.null",
            expires_at="gt." + _now_iso(),
            select="id,user_id",
        ),
        {"consumed_at": _now_iso()},
        prefer="return=representation",
    )
    if not isinstance(code_rows, list) or not code_rows:
        raise RequestError("This MT5 link code is invalid, expired or already used.")
    user_id = str(code_rows[0]["user_id"])
    token = secrets.token_urlsafe(DEVICE_TOKEN_BYTES)
    token_hash = _token_hash(token)
    existing = _db_request(
        "GET",
        _query(
            "lm_mt5_devices",
            select="id,user_id",
            account_login="eq." + str(account_login),
            broker_server="eq." + broker_server,
            limit="1",
        ),
    )
    device_values = {
        "user_id": user_id,
        "account_login": account_login,
        "broker_server": broker_server,
        "account_mode": account_mode,
        "token_hash": token_hash,
        "auto_trade_enabled": False,
        "revoked_at": None,
        "last_seen_at": _now_iso(),
    }
    if isinstance(existing, list) and existing:
        current = existing[0]
        if str(current.get("user_id")) != user_id:
            raise RequestError("This MT5 account is already linked to another LM Analyzer user.")
        rows = _db_request(
            "PATCH",
            _query("lm_mt5_devices", id="eq." + str(current["id"])),
            device_values,
            prefer="return=representation",
        )
    else:
        rows = _db_request(
            "POST",
            "/rest/v1/lm_mt5_devices",
            device_values,
            prefer="return=representation",
        )
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("MT5 account link was not saved.")
    return {
        "device_token": token,
        "account_login": account_login,
        "broker_server": broker_server,
        "auto_trade_enabled": False,
        "live_trading_enabled": False,
        "message": "MT5 account linked. Auto trading remains OFF until enabled in the EA settings.",
    }


def _configure_device(device, body):
    _check_account_identity(device, body.get("account_login"), body.get("broker_server"))
    enabled = body.get("auto_trade_enabled") is True
    values = {
        "auto_trade_enabled": enabled,
        "risk_per_trade": _number(body.get("risk_per_trade", 0.25), "risk_per_trade", 0.1, 1.0),
        "max_daily_loss_percent": _number(body.get("max_daily_loss_percent", 2.0), "max_daily_loss_percent", 0.5, 5.0),
        "max_open_positions": _number(body.get("max_open_positions", 1), "max_open_positions", 1, 3, integer=True),
        "max_trades_per_day": _number(body.get("max_trades_per_day", 3), "max_trades_per_day", 1, 10, integer=True),
        "max_lot": _number(body.get("max_lot", 0.1), "max_lot", 0.01, 5.0),
        "min_confidence": _number(body.get("min_confidence", 75), "min_confidence", 50, 100, integer=True),
        "max_signal_age_minutes": _number(body.get("max_signal_age_minutes", 10), "max_signal_age_minutes", 1, MAX_SIGNAL_AGE_MINUTES, integer=True),
        "max_entry_deviation_points": _number(body.get("max_entry_deviation_points", 50), "max_entry_deviation_points", 1, 2000, integer=True),
        "max_spread_points": _number(body.get("max_spread_points", 50), "max_spread_points", 1, 2000, integer=True),
        "updated_at": _now_iso(),
    }
    _db_request(
        "PATCH",
        _query("lm_mt5_devices", id="eq." + str(device["id"]), revoked_at="is.null"),
        values,
        prefer="return=minimal",
    )
    return {"saved": True, "auto_trade_enabled": enabled, "live_trading_enabled": False}


def _load_candidates(device):
    max_age = int(device.get("max_signal_age_minutes") or 10)
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age)).isoformat(timespec="seconds").replace("+00:00", "Z")
    jobs = _db_request(
        "GET",
        _query(
            "analysis_jobs",
            select="id,user_id,instrument,trade_focus,status,created_at,openai_response_id",
            user_id="eq." + str(device["user_id"]),
            status="eq.completed",
            created_at="gte." + cutoff,
            order="created_at.desc",
            limit="40",
        ),
    )
    if not isinstance(jobs, list) or not jobs:
        return []
    executions = _db_request(
        "GET",
        _query(
            "lm_mt5_execution_records",
            select="signal_id",
            device_id="eq." + str(device["id"]),
            created_at="gte." + (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds").replace("+00:00", "Z"),
            limit="1000",
        ),
    )
    already_claimed = {
        str(row.get("signal_id")) for row in (executions or []) if isinstance(row, dict)
    }
    candidates = []
    for job in jobs:
        if not isinstance(job, dict) or str(job.get("id")) in already_claimed:
            continue
        result = _decode_job_result(job.get("openai_response_id"))
        parsed = _eligible_signal(result, job.get("instrument"), int(device.get("min_confidence") or 75))
        if not parsed:
            continue
        try:
            created = datetime.fromisoformat(str(job.get("created_at", "")).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            epoch = int(created.timestamp())
        except (TypeError, ValueError, OverflowError):
            continue
        age = time.time() - epoch
        if age < -60 or age > max_age * 60:
            continue
        candidates.append((job, parsed, epoch))
    return candidates


def _poll_signal(device):
    if not bool(device.get("auto_trade_enabled")):
        return "STATE|AUTO_DISABLED|LIVE=0\n"
    live_enabled = os.environ.get("LM_MT5_LIVE_TRADING_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
    account_mode = str(device.get("account_mode", "demo")).lower()
    if account_mode == "real" and not live_enabled:
        return "STATE|LIVE_BLOCKED|LIVE=0\n"
    if account_mode not in ("demo", "contest", "real"):
        return "STATE|ACCOUNT_MODE_BLOCKED|LIVE=0\n"
    candidates = _load_candidates(device)
    live_value = "1" if live_enabled else "0"
    output = "STATE|ACTIVE|LIVE={}\n".format(live_value)
    if not candidates:
        return output
    job, parsed, epoch = candidates[0]
    fields = [
        "SIGNAL",
        str(job["id"]),
        parsed["instrument"],
        parsed["direction"],
        "{:.10f}".format(parsed["entry"]),
        "{:.10f}".format(parsed["stop_loss"]),
        "{:.10f}".format(parsed["take_profit_1"]),
        str(parsed["confidence"]),
        str(epoch),
    ]
    return output + "|".join(fields) + "\n"


def _claim_signal(device, body):
    signal_id = str(body.get("signal_id", "")).strip()
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", signal_id):
        raise RequestError("Invalid signal reference.")
    if not bool(device.get("auto_trade_enabled")):
        raise RequestError("Auto trading is disabled for this MT5 account.")
    live_enabled = os.environ.get("LM_MT5_LIVE_TRADING_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
    if str(device.get("account_mode", "demo")).lower() == "real" and not live_enabled:
        raise RequestError("Live trading is disabled for this service. Use an MT5 demo account.")
    jobs = _db_request(
        "GET",
        _query(
            "analysis_jobs",
            select="id,user_id,instrument,created_at,openai_response_id,status",
            id="eq." + signal_id,
            user_id="eq." + str(device["user_id"]),
            status="eq.completed",
            limit="1",
        ),
    )
    if not isinstance(jobs, list) or not jobs:
        raise RequestError("This signal is no longer available to this MT5 account.")
    job = jobs[0]
    result = _decode_job_result(job.get("openai_response_id"))
    parsed = _eligible_signal(result, job.get("instrument"), int(device.get("min_confidence") or 75))
    if not parsed:
        raise RequestError("This signal does not meet the current auto-trading rules.")
    try:
        created = datetime.fromisoformat(str(job.get("created_at", "")).replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - created).total_seconds()
    except (TypeError, ValueError, OverflowError) as exc:
        raise RequestError("This signal has an invalid timestamp and cannot be traded.") from exc
    maximum_age = int(device.get("max_signal_age_minutes") or 10) * 60
    if age_seconds < -60 or age_seconds > maximum_age:
        raise RequestError("This signal has expired and cannot be traded.")
    _db_request(
        "POST",
        "/rest/v1/lm_mt5_execution_records",
        {
            "device_id": str(device["id"]),
            "user_id": str(device["user_id"]),
            "signal_id": signal_id,
            "symbol": parsed["instrument"],
            "direction": parsed["direction"],
            "status": "claimed",
        },
        prefer="return=minimal",
    )
    return {"claimed": True}


def _report_execution(device, body):
    signal_id = str(body.get("signal_id", "")).strip()
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", signal_id):
        raise RequestError("Invalid signal reference.")
    status = str(body.get("status", "")).strip().lower()
    if status not in ("executed", "rejected"):
        raise RequestError("Invalid execution result.")
    updates = {"status": status, "updated_at": _now_iso()}
    if status == "executed":
        updates.update({
            "order_ticket": int(_number(body.get("order_ticket", 0), "order_ticket", 1, 9223372036854775807, integer=True)),
            "position_id": int(_number(body.get("position_id", 0), "position_id", 0, 9223372036854775807, integer=True)),
            "requested_volume": _number(body.get("requested_volume"), "requested_volume", 0.001, 1000),
            "filled_volume": _number(body.get("filled_volume"), "filled_volume", 0.001, 1000),
            "fill_price": _number(body.get("fill_price"), "fill_price", 0.00000001, 100000000),
        })
    else:
        updates["reason"] = str(body.get("reason", "Trade was not executed."))[:300]
    rows = _db_request(
        "PATCH",
        _query(
            "lm_mt5_execution_records",
            device_id="eq." + str(device["id"]),
            signal_id="eq." + signal_id,
            status="eq.claimed",
            select="id",
        ),
        updates,
        prefer="return=representation",
    )
    if not isinstance(rows, list) or not rows:
        raise RequestError("Execution record was not found or has already been finalized.")
    return {"saved": True, "status": status}


def _record_deal(device, body):
    try:
        magic = int(body.get("magic_number", 0))
        deal_ticket = int(body.get("deal_ticket", 0))
        position_id = int(body.get("position_id", 0))
    except (TypeError, ValueError) as exc:
        raise RequestError("Invalid MT5 deal identifiers.") from exc
    if magic != MT5_MAGIC_NUMBER or deal_ticket <= 0 or position_id <= 0:
        raise RequestError("This MT5 deal is not identified as an LM AUTO TRADER deal.")
    try:
        deal_time = datetime.fromtimestamp(int(body.get("deal_time_epoch", 0)), timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        raise RequestError("Invalid MT5 deal time.") from exc
    symbol = str(body.get("symbol", "")).strip()[:32]
    if not symbol:
        raise RequestError("MT5 deal symbol is missing.")
    # A client-supplied magic number alone is not proof; require a recorded
    # executed signal position on this linked device before storing any deal.
    execution_rows = _db_request(
        "GET",
        _query(
            "lm_mt5_execution_records",
            select="id,signal_id,symbol,status",
            device_id="eq." + str(device["id"]),
            position_id="eq." + str(position_id),
            status="eq.executed",
            limit="1",
        ),
    )
    if not isinstance(execution_rows, list) or not execution_rows:
        raise RequestError("This MT5 position is not linked to a verified LM Analyzer execution.")
    stored_symbol = re.sub(r"[^A-Z0-9]", "", str(execution_rows[0].get("symbol", "")).upper())
    actual_symbol = re.sub(r"[^A-Z0-9]", "", symbol.upper())
    if not stored_symbol or not actual_symbol.startswith(stored_symbol):
        raise RequestError("MT5 deal symbol does not match the linked LM Analyzer signal.")
    payload = {
        "device_id": str(device["id"]),
        "user_id": str(device["user_id"]),
        "account_login": int(device["account_login"]),
        "broker_server": str(device["broker_server"]),
        "deal_ticket": deal_ticket,
        "position_id": position_id,
        "symbol": symbol,
        "deal_entry": str(body.get("deal_entry", ""))[:16],
        "deal_type": str(body.get("deal_type", ""))[:16],
        "volume": _number(body.get("volume", 0), "volume", 0, 1000),
        "price": _number(body.get("price", 0), "price", 0, 100000000),
        "profit": _number(body.get("profit", 0), "profit", -1000000000, 1000000000),
        "commission": _number(body.get("commission", 0), "commission", -1000000000, 1000000000),
        "swap": _number(body.get("swap", 0), "swap", -1000000000, 1000000000),
        "deal_time": deal_time.isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    try:
        _db_request("POST", "/rest/v1/lm_mt5_trade_deals", payload, prefer="return=minimal")
        return {"saved": True, "duplicate": False}
    except RequestError as exc:
        if "already been recorded" in str(exc):
            return {"saved": True, "duplicate": True}
        raise


def _revoke_user_devices(user):
    _db_request(
        "PATCH",
        _query("lm_mt5_devices", user_id="eq." + str(user["id"]), revoked_at="is.null"),
        {"revoked_at": _now_iso(), "auto_trade_enabled": False},
        prefer="return=minimal",
    )
    return {"revoked": True}


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        _text_response(self, 204, "")

    def do_GET(self):
        try:
            action = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("action", [""])[0]
            if action != "poll":
                json_response(self, 405, {"error": "Use POST for this action."})
                return
            device = _read_device(self)
            parsed = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            _check_account_identity(
                device,
                parsed.get("account_login", [None])[0],
                parsed.get("broker_server", [None])[0],
            )
            _text_response(self, 200, _poll_signal(device))
        except RequestError as exc:
            _text_response(self, 401, "STATE|UNAUTHORIZED|{}\n".format(str(exc).replace("|", "/")))
        except RuntimeError as exc:
            _text_response(self, 503, "STATE|BACKEND_UNAVAILABLE|{}\n".format(str(exc).replace("|", "/")))
        except Exception:
            _text_response(self, 500, "STATE|ERROR|Trading signal request failed.\n")

    def do_POST(self):
        try:
            body = _json_body(self)
            action = str(body.get("action", "")).strip().lower()
            if action == "create_link_code":
                user = verify_access_token(extract_bearer_token(self))
                json_response(self, 200, _create_link_code(user))
                return
            if action == "link":
                json_response(self, 200, _link_terminal(body))
                return
            if action == "revoke_all":
                user = verify_access_token(extract_bearer_token(self))
                json_response(self, 200, _revoke_user_devices(user))
                return

            device = _read_device(self)
            _check_account_identity(device, body.get("account_login"), body.get("broker_server"))
            if action == "configure":
                json_response(self, 200, _configure_device(device, body))
            elif action == "claim":
                json_response(self, 200, _claim_signal(device, body))
            elif action == "report":
                json_response(self, 200, _report_execution(device, body))
            elif action == "deal":
                json_response(self, 200, _record_deal(device, body))
            else:
                json_response(self, 400, {"error": "Unknown MT5 action."})
        except RequestError as exc:
            json_response(self, 400, {"error": str(exc)})
        except ValueError as exc:
            json_response(self, 401, {"error": str(exc) or "Your session is invalid."})
        except RuntimeError as exc:
            json_response(self, 503, {"error": str(exc) or "MT5 service is temporarily unavailable."})
        except Exception:
            json_response(self, 500, {"error": "MT5 service could not complete the request."})

    def log_message(self, *_args):
        return


def handler_entrypoint(request, context=None):
    return handler
