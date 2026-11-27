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

MAX_BODY_BYTES = 64 * 1024
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
    # Instrument identity is the authoritative fallback when older completed
    # analysis records predate chart_instrument_status. Never override an
    # explicit MISMATCH (or any other explicit non-MATCH value).
    chart_status = str(result.get("chart_instrument_status", "")).strip().upper()
    result_instrument = re.sub(r"[^A-Z0-9]", "", str(result.get("instrument", "")).upper())
    row_instrument = re.sub(r"[^A-Z0-9]", "", str(instrument or "").upper())
    if not row_instrument or not result_instrument or result_instrument != row_instrument:
        return None
    if chart_status and chart_status != "MATCH":
        return None
    # Missing status is accepted only after exact normalized instrument equality
    # above, preserving the mismatch guard for legacy analysis payloads.
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
        "message": "MT5 account linked. The independent AI trading session remains STOPPED until you press Start in LM Analyzer.",
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




def _user_is_owner(user_id):
    """Resolve OWNER_EMAIL server-side; no client-provided owner flags are trusted."""
    owner_email = os.environ.get("OWNER_EMAIL", "").strip().lower()
    if not owner_email:
        return False
    url = get_supabase_url().rstrip("/") + "/auth/v1/admin/users/" + urllib.parse.quote(str(user_id), safe="")
    key = get_supabase_service_key()
    request = urllib.request.Request(url, headers={
        "apikey": key, "Authorization": "Bearer " + key, "Accept": "application/json"
    }, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
        email = str((data or {}).get("email", "")).strip().lower()
        return bool(email and hmac.compare_digest(email, owner_email))
    except Exception:
        return False


def _has_auto_trader_access(user_id):
    """Only active AUTO TRADER entitlements unlock customers; OWNER_EMAIL is test bypass."""
    if _user_is_owner(user_id):
        return True, "owner_test_bypass"
    rows = _db_request("GET", _query(
        "lm_auto_trader_subscriptions",
        select="id,plan_key,expires_at",
        user_id="eq." + str(user_id),
        status="eq.active",
        expires_at="gt." + _now_iso(),
        order="expires_at.desc",
        limit="1",
    ))
    if isinstance(rows, list) and rows:
        return True, "subscription"
    return False, "subscription_required"


def _iso_age_seconds(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())
    except (TypeError, ValueError):
        return 10 ** 9


def _read_user_device(user, device_id):
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", str(device_id or "")):
        raise RequestError("Select a valid linked MT5 account.")
    rows = _db_request("GET", _query(
        "lm_mt5_devices",
        select="id,user_id,account_login,broker_server,account_mode,auto_trade_enabled,revoked_at",
        id="eq." + str(device_id),
        user_id="eq." + str(user["id"]),
        revoked_at="is.null",
        limit="1",
    ))
    if not isinstance(rows, list) or not rows:
        raise RequestError("This MT5 account is not linked to your signed-in account.")
    return rows[0]


def _latest_snapshot(device):
    rows = _db_request("GET", _query(
        "lm_mt5_account_snapshots",
        select="balance,equity,floating_profit,free_margin,margin,margin_level,currency,leverage,terminal_connected,positions,updated_at,account_mode",
        device_id="eq." + str(device["id"]),
        limit="1",
    ))
    return rows[0] if isinstance(rows, list) and rows else None


def _latest_market_data(device):
    rows = _db_request("GET", _query(
        "lm_mt5_market_data",
        select="market_data,updated_at,account_login,broker_server",
        device_id="eq." + str(device["id"]),
        limit="1",
    ))
    return rows[0] if isinstance(rows, list) and rows else None


def _load_risk_tier(equity):
    rows = _db_request("GET", _query(
        "lm_auto_trader_risk_tiers",
        select="tier_key,min_equity,max_equity,max_open_positions,max_lot,risk_per_trade_percent",
        order="min_equity.desc",
        limit="10",
    ))
    for tier in rows if isinstance(rows, list) else []:
        try:
            minimum = float(tier.get("min_equity", 0))
            maximum = tier.get("max_equity")
            if equity >= minimum and (maximum is None or equity < float(maximum)):
                return tier
        except (TypeError, ValueError):
            continue
    return {
        "tier_key": "under_50", "max_open_positions": 1,
        "max_lot": 0.01, "risk_per_trade_percent": 0.50
    }


def _ensure_daily_risk(device, equity, risk_config):
    today = datetime.now(timezone.utc).date().isoformat()
    rows = _db_request("GET", _query(
        "lm_mt5_daily_risk_state",
        select="device_id,trading_day,starting_equity,realized_pnl,peak_equity,daily_loss_locked,drawdown_locked,trades_opened",
        device_id="eq." + str(device["id"]),
        trading_day="eq." + today,
        limit="1",
    ))
    if isinstance(rows, list) and rows:
        state = rows[0]
    else:
        payload = {
            "device_id": str(device["id"]),
            "trading_day": today,
            "starting_equity": max(float(equity), 0.01),
            "realized_pnl": 0,
            "peak_equity": max(float(equity), 0.01),
            "daily_loss_locked": False,
            "drawdown_locked": False,
            "trades_opened": 0,
            "updated_at": _now_iso(),
        }
        created = _db_request(
            "POST",
            "/rest/v1/lm_mt5_daily_risk_state?on_conflict=device_id,trading_day",
            payload,
            prefer="resolution=merge-duplicates,return=representation",
        )
        state = created[0] if isinstance(created, list) and created else payload
    peak = max(float(state.get("peak_equity") or 0), float(equity))
    if peak > float(state.get("peak_equity") or 0):
        _db_request(
            "PATCH",
            _query("lm_mt5_daily_risk_state",
                   device_id="eq." + str(device["id"]), trading_day="eq." + today),
            {"peak_equity": peak, "updated_at": _now_iso()},
            prefer="return=minimal",
        )
        state["peak_equity"] = peak
    start = max(float(state.get("starting_equity") or 0), 0.01)
    daily_loss = max(0.0, (start - float(equity)) / start * 100.0)
    drawdown = max(0.0, (peak - float(equity)) / peak * 100.0)
    daily_limit = float(risk_config.get("max_daily_loss_percent", 2.0))
    drawdown_limit = float(risk_config.get("max_drawdown_percent", 5.0))
    if daily_loss >= daily_limit or bool(state.get("daily_loss_locked")):
        state["daily_loss_locked"] = True
    if drawdown >= drawdown_limit or bool(state.get("drawdown_locked")):
        state["drawdown_locked"] = True
    if state["daily_loss_locked"] or state["drawdown_locked"]:
        _db_request(
            "PATCH",
            _query("lm_mt5_daily_risk_state",
                   device_id="eq." + str(device["id"]), trading_day="eq." + today),
            {
                "daily_loss_locked": bool(state["daily_loss_locked"]),
                "drawdown_locked": bool(state["drawdown_locked"]),
                "updated_at": _now_iso(),
            },
            prefer="return=minimal",
        )
    state["daily_loss_percent"] = daily_loss
    state["drawdown_percent"] = drawdown
    return state


def _session_start(user, body):
    device = _read_user_device(user, body.get("device_id"))
    allowed, access_source = _has_auto_trader_access(str(user["id"]))
    if not allowed:
        raise RequestError("An active LM AUTO TRADER subscription is required. Billing verification is not enabled yet.")
    snapshot = _latest_snapshot(device)
    market = _latest_market_data(device)
    if not snapshot or snapshot.get("terminal_connected") is not True or _iso_age_seconds(snapshot.get("updated_at")) > 45:
        raise RequestError("MT5 must be connected and sending fresh account telemetry before Start.")
    if not market or _iso_age_seconds(market.get("updated_at")) > 60:
        raise RequestError("Waiting for fresh M1/M5 broker candles from MT5. Keep the EA connected and try Start again.")
    if str(market.get("account_login")) != str(device.get("account_login")) or str(market.get("broker_server")) != str(device.get("broker_server")):
        raise RequestError("The market-data feed does not match this linked MT5 account.")
    equity = float(snapshot.get("equity") or 0)
    if equity <= 0:
        raise RequestError("MT5 reported invalid account equity.")
    tier = _load_risk_tier(equity)
    risk_config = {
        "tier_key": tier.get("tier_key", "under_50"),
        "risk_per_trade_percent": min(0.50, float(tier.get("risk_per_trade_percent") or 0.50)),
        "max_daily_loss_percent": 2.0,
        "max_drawdown_percent": 5.0,
        "max_open_positions": int(tier.get("max_open_positions") or 1),
        "max_trades_per_day": 3,
        "max_lot": float(tier.get("max_lot") or 0.01),
        "min_confidence": 75,
        "max_signal_age_seconds": 55,
        "max_entry_deviation_points": 50,
        "max_spread_fx_points": 30,
        "max_spread_gold_points": 100,
    }
    risk_state = _ensure_daily_risk(device, equity, risk_config)
    if risk_state.get("daily_loss_locked") or risk_state.get("drawdown_locked"):
        raise RequestError("Risk lock is active. Trading cannot start until the next UTC trading day or an owner risk review.")
    existing = _db_request("GET", _query(
        "lm_mt5_trading_sessions",
        select="id",
        device_id="eq." + str(device["id"]),
        limit="1",
    ))
    payload = {
        "device_id": str(device["id"]),
        "user_id": str(user["id"]),
        "status": "active",
        "account_mode": str(device.get("account_mode", "demo")),
        "started_at": _now_iso(),
        "stopped_at": None,
        "stop_reason": None,
        "risk_config": risk_config,
        "updated_at": _now_iso(),
    }
    if isinstance(existing, list) and existing:
        _db_request(
            "PATCH",
            _query("lm_mt5_trading_sessions", device_id="eq." + str(device["id"])),
            payload,
            prefer="return=minimal",
        )
    else:
        _db_request("POST", "/rest/v1/lm_mt5_trading_sessions", payload, prefer="return=minimal")
    _db_request(
        "PATCH", _query("lm_mt5_devices", id="eq." + str(device["id"]), revoked_at="is.null"),
        {"auto_trade_enabled": True}, prefer="return=minimal"
    )
    return {
        "started": True, "status": "active", "account_mode": device.get("account_mode"),
        "access_source": access_source, "risk": risk_config,
        "message": "AI session started. New orders require fresh backend AI approval."
    }


def _session_stop(user, body):
    device = _read_user_device(user, body.get("device_id"))
    rows = _db_request("GET", _query(
        "lm_mt5_trading_sessions",
        select="id,status",
        device_id="eq." + str(device["id"]),
        user_id="eq." + str(user["id"]),
        limit="1",
    ))
    if rows:
        _db_request(
            "PATCH",
            _query("lm_mt5_trading_sessions", device_id="eq." + str(device["id"])),
            {"status": "stopped", "stopped_at": _now_iso(),
             "stop_reason": "Stopped by account owner", "updated_at": _now_iso()},
            prefer="return=minimal",
        )
    _db_request(
        "PATCH", _query("lm_mt5_devices", id="eq." + str(device["id"]), revoked_at="is.null"),
        {"auto_trade_enabled": False}, prefer="return=minimal"
    )
    _db_request(
        "PATCH",
        _query(
            "lm_mt5_ai_decisions",
            device_id="eq." + str(device["id"]),
            status="in.(pending,claimed)",
        ),
        {"status": "cancelled", "completed_at": _now_iso(),
         "execution_result": {"reason": "Trading session stopped by account owner."}},
        prefer="return=minimal",
    )
    return {"stopped": True, "status": "stopped", "message": "New entries are stopped. MT5 continues protective management of positions already open."}


def _session_status(user, body):
    device = _read_user_device(user, body.get("device_id"))
    rows = _db_request("GET", _query(
        "lm_mt5_trading_sessions",
        select="id,status,account_mode,started_at,stopped_at,last_decision_at,last_heartbeat_at,stop_reason,risk_config,updated_at",
        device_id="eq." + str(device["id"]),
        user_id="eq." + str(user["id"]),
        limit="1",
    ))
    snapshot = _latest_snapshot(device)
    market = _latest_market_data(device)
    return {
        "device_id": str(device["id"]),
        "session": rows[0] if isinstance(rows, list) and rows else None,
        "account": snapshot,
        "market_data_updated_at": market.get("updated_at") if market else None,
        "telemetry_status": "live" if snapshot and snapshot.get("terminal_connected") is True and _iso_age_seconds(snapshot.get("updated_at")) <= 45 else "stale",
    }


def _record_market_data(device, body):
    _check_account_identity(device, body.get("account_login"), body.get("broker_server"))
    symbols = body.get("symbols")
    if not isinstance(symbols, list) or not symbols or len(symbols) > 8:
        raise RequestError("MT5 must report between 1 and 8 supported scalping symbols.")
    clean_symbols = []
    seen = set()
    for item in symbols:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{3,32}", symbol) or symbol in seen:
            continue
        seen.add(symbol)
        bid = _number(item.get("bid"), "bid", 0.00000001, 100000000)
        ask = _number(item.get("ask"), "ask", bid, 100000000)
        point = _number(item.get("point"), "point", 0.00000001, 1000)
        digits = int(_number(item.get("digits"), "digits", 0, 10, integer=True))
        if ask < bid:
            raise RequestError("MT5 reported an invalid bid/ask spread.")
        clean = {
            "symbol": symbol, "bid": bid, "ask": ask, "point": point,
            "digits": digits, "news_blocked": item.get("news_blocked") is True,
        }
        for timeframe in ("m1", "m5"):
            bars = item.get(timeframe)
            maximum = 24 if timeframe == "m1" else 18
            if not isinstance(bars, list) or len(bars) < 8 or len(bars) > maximum:
                raise RequestError("Incomplete " + timeframe.upper() + " candles for " + symbol + ".")
            parsed_bars = []
            previous_time = 0
            for bar in bars:
                if not isinstance(bar, dict):
                    continue
                stamp = int(_number(bar.get("time"), "candle_time", 1, 4102444800, integer=True))
                opening = _number(bar.get("open"), "candle_open", 0.00000001, 100000000)
                high = _number(bar.get("high"), "candle_high", 0.00000001, 100000000)
                low = _number(bar.get("low"), "candle_low", 0.00000001, 100000000)
                close = _number(bar.get("close"), "candle_close", 0.00000001, 100000000)
                if high < max(opening, close, low) or low > min(opening, close, high):
                    raise RequestError("MT5 candle OHLC values are inconsistent.")
                if stamp <= previous_time:
                    raise RequestError("MT5 candles must be ordered oldest to newest.")
                previous_time = stamp
                parsed_bars.append({
                    "time": stamp, "open": opening, "high": high,
                    "low": low, "close": close,
                    "tick_volume": int(_number(bar.get("tick_volume", 0), "tick_volume", 0, 1000000000, integer=True)),
                })
            if len(parsed_bars) < 8:
                raise RequestError("MT5 candle history is incomplete for " + symbol + ".")
            clean[timeframe] = parsed_bars
        if time.time() - clean["m1"][-1]["time"] > 180 or clean["m1"][-1]["time"] > time.time() + 60:
            raise RequestError("MT5 M1 candle data is stale for " + symbol + ".")
        clean_symbols.append(clean)
    if not clean_symbols:
        raise RequestError("No valid broker market data was supplied.")
    payload = {
        "device_id": str(device["id"]), "user_id": str(device["user_id"]),
        "account_login": int(device["account_login"]),
        "broker_server": str(device["broker_server"]),
        "market_data": {"provider": "linked_mt5_broker", "symbols": clean_symbols, "received_at": _now_iso()},
        "updated_at": _now_iso(),
    }
    _db_request(
        "POST", "/rest/v1/lm_mt5_market_data?on_conflict=device_id",
        payload, prefer="resolution=merge-duplicates,return=minimal"
    )
    # Heartbeat also confirms this terminal has delivered fresh market bars.
    _db_request(
        "PATCH", _query("lm_mt5_trading_sessions", device_id="eq." + str(device["id"]), status="eq.active"),
        {"last_heartbeat_at": _now_iso(), "updated_at": _now_iso()},
        prefer="return=minimal"
    )
    return {"saved": True, "symbols": len(clean_symbols), "provider": "linked_mt5_broker"}


def _validate_and_queue_ai_decisions(device, session, market_data, decisions, snapshot):
    config = session.get("risk_config") if isinstance(session.get("risk_config"), dict) else {}
    equity = float(snapshot.get("equity") or 0)
    risk_state = _ensure_daily_risk(device, equity, config)
    if risk_state.get("daily_loss_locked") or risk_state.get("drawdown_locked"):
        _db_request("PATCH", _query("lm_mt5_trading_sessions", device_id="eq." + str(device["id"])),
                    {"status": "risk_locked", "stop_reason": "Daily loss or equity drawdown limit reached", "updated_at": _now_iso()},
                    prefer="return=minimal")
        _db_request("PATCH", _query(
            "lm_mt5_ai_decisions",
            device_id="eq." + str(device["id"]),
            status="eq.pending",
        ), {
            "status": "cancelled", "completed_at": _now_iso(),
            "execution_result": {"reason": "Daily risk lock activated."},
        }, prefer="return=minimal")
        return 0, "RISK_LOCKED"
    positions = snapshot.get("positions") if isinstance(snapshot.get("positions"), list) else []
    open_positions = [p for p in positions if isinstance(p, dict) and int(p.get("magic_number", 0) or 0) == MT5_MAGIC_NUMBER]
    open_symbols = {str(p.get("symbol", "")).upper() for p in open_positions}
    if len(open_positions) >= int(config.get("max_open_positions", 1)):
        return 0, "POSITION_LIMIT"
    trades_opened = int(risk_state.get("trades_opened") or 0)
    if trades_opened >= int(config.get("max_trades_per_day", 3)):
        return 0, "DAILY_TRADE_LIMIT"
    data_by_symbol = {str(item.get("symbol", "")).upper(): item for item in market_data if isinstance(item, dict)}
    queued = 0
    for decision in decisions:
        if not isinstance(decision, dict) or str(decision.get("decision", "")).upper() not in ("BUY", "SELL"):
            continue
        symbol = str(decision.get("symbol", "")).strip()
        market = data_by_symbol.get(symbol.upper())
        if not market or market.get("news_blocked") is True or symbol.upper() in open_symbols:
            continue
        if int(decision.get("confidence", 0)) < int(config.get("min_confidence", 75)):
            continue
        if len(decision.get("confirmations") or []) < 2:
            continue
        direction = str(decision["decision"]).upper()
        entry = _single_price(decision.get("entry_price"))
        stop = _single_price(decision.get("stop_loss"))
        target = _single_price(decision.get("take_profit"))
        if not entry or not stop or not target:
            continue
        if direction == "BUY" and not stop < entry < target:
            continue
        if direction == "SELL" and not target < entry < stop:
            continue
        point = float(market["point"])
        bid, ask = float(market["bid"]), float(market["ask"])
        spread_points = (ask - bid) / point
        gold = symbol.upper().startswith("XAU")
        allowed_spread = float(config.get("max_spread_gold_points" if gold else "max_spread_fx_points", 100 if gold else 30))
        if spread_points > allowed_spread:
            continue
        live_price = ask if direction == "BUY" else bid
        if abs(live_price - entry) / point > float(config.get("max_entry_deviation_points", 50)):
            continue
        risk_distance = abs(entry - stop)
        reward_distance = abs(target - entry)
        if risk_distance <= point * 2 or reward_distance / risk_distance < 1.2:
            continue
        if risk_distance > entry * 0.02:
            continue
        payload = {
            "device_id": str(device["id"]), "user_id": str(device["user_id"]),
            "session_id": str(session["id"]), "symbol": symbol,
            "decision": direction, "confidence": int(decision["confidence"]),
            "entry_price": entry, "stop_loss": stop, "take_profit": target,
            "rationale": (str(decision.get("rationale", "")) + " Confirmations: " + "; ".join(decision.get("confirmations") or []))[:1200],
            "model": "Gemini independent MT5 market analysis",
            "market_snapshot": {"bid": bid, "ask": ask, "point": point, "spread_points": spread_points,
                                "m1_last": market["m1"][-1], "m5_last": market["m5"][-1]},
            "status": "pending",
            "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=55)).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
        _db_request("POST", "/rest/v1/lm_mt5_ai_decisions", payload, prefer="return=minimal")
        queued += 1
        open_symbols.add(symbol.upper())
        if queued + len(open_positions) >= int(config.get("max_open_positions", 1)):
            break
    return queued, "QUEUED" if queued else "NO_APPROVED_SETUP"


def _run_ai_cycle(device, session, market_row, snapshot):
    market_data = market_row.get("market_data") if isinstance(market_row.get("market_data"), dict) else {}
    symbols = market_data.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        return 0, "NO_MARKET_DATA"
    last = session.get("last_decision_at")
    if last and _iso_age_seconds(last) < 45:
        return 0, "ANALYSIS_COOLDOWN"
    _db_request(
        "PATCH", _query("lm_mt5_trading_sessions", device_id="eq." + str(device["id"]), status="eq.active"),
        {"last_decision_at": _now_iso(), "updated_at": _now_iso()},
        prefer="return=minimal"
    )
    from handlers.lm_auto_trader_ai import analyse
    decisions = analyse(symbols)
    return _validate_and_queue_ai_decisions(device, session, symbols, decisions, snapshot)


def _claim_ai_decision(device):
    rows = _db_request("GET", _query(
        "lm_mt5_ai_decisions",
        select="id,symbol,decision,confidence,entry_price,stop_loss,take_profit,expires_at",
        device_id="eq." + str(device["id"]),
        status="eq.pending",
        expires_at="gt." + _now_iso(),
        order="created_at.asc",
        limit="1",
    ))
    if not isinstance(rows, list) or not rows:
        return None
    decision = rows[0]
    claimed = _db_request(
        "PATCH",
        _query("lm_mt5_ai_decisions", id="eq." + str(decision["id"]),
               device_id="eq." + str(device["id"]), status="eq.pending",
               expires_at="gt." + _now_iso(), select="id"),
        {"status": "claimed", "claimed_at": _now_iso()},
        prefer="return=representation",
    )
    if not isinstance(claimed, list) or not claimed:
        return None
    return decision


def _poll_signal(device):
    """MT5 polls this endpoint; backend AI, never chart-upload jobs, creates commands."""
    sessions = _db_request("GET", _query(
        "lm_mt5_trading_sessions",
        select="id,status,account_mode,last_decision_at,risk_config,user_id",
        device_id="eq." + str(device["id"]),
        user_id="eq." + str(device["user_id"]),
        limit="1",
    ))
    if not isinstance(sessions, list) or not sessions or sessions[0].get("status") != "active":
        return "STATE|STOPPED|SESSION=STOPPED|LIVE=0\n"
    session = sessions[0]
    allowed, access_source = _has_auto_trader_access(str(device["user_id"]))
    if not allowed:
        _db_request("PATCH", _query("lm_mt5_trading_sessions", device_id="eq." + str(device["id"])),
                    {"status": "subscription_locked", "stop_reason": "LM AUTO TRADER subscription expired", "updated_at": _now_iso()},
                    prefer="return=minimal")
        _db_request("PATCH", _query(
            "lm_mt5_ai_decisions",
            device_id="eq." + str(device["id"]),
            status="in.(pending,claimed)",
        ), {
            "status": "cancelled", "completed_at": _now_iso(),
            "execution_result": {"reason": "LM AUTO TRADER entitlement is no longer active."},
        }, prefer="return=minimal")
        return "STATE|SUBSCRIPTION_LOCKED|SESSION=LOCKED|LIVE=0\n"
    snapshot = _latest_snapshot(device)
    market_row = _latest_market_data(device)
    if not snapshot or snapshot.get("terminal_connected") is not True or _iso_age_seconds(snapshot.get("updated_at")) > 45:
        return "STATE|TERMINAL_STALE|SESSION=ACTIVE|LIVE=0\n"
    if not market_row or _iso_age_seconds(market_row.get("updated_at")) > 45:
        return "STATE|MARKET_DATA_STALE|SESSION=ACTIVE|LIVE=0\n"
    config = session.get("risk_config") if isinstance(session.get("risk_config"), dict) else {}
    try:
        _run_ai_cycle(device, session, market_row, snapshot)
    except Exception as exc:
        # Never create a command from partial or failed AI output.
        print("LM AUTO TRADER AI cycle failed:", type(exc).__name__, str(exc)[:180])
    command = _claim_ai_decision(device)
    state = (
        "STATE|ACTIVE|SESSION=ACTIVE|LIVE=1"
        "|RISK={:.3f}|MAXLOT={:.5f}|MAXPOS={}|DAILY={:.3f}|TRADES={}"
        "|DEVIATION={}|SPREADFX={}|SPREADGOLD={}|AGE={}\n"
    ).format(
        float(config.get("risk_per_trade_percent", 0.50)),
        float(config.get("max_lot", 0.01)),
        int(config.get("max_open_positions", 1)),
        float(config.get("max_daily_loss_percent", 2.0)),
        int(config.get("max_trades_per_day", 3)),
        int(config.get("max_entry_deviation_points", 50)),
        int(config.get("max_spread_fx_points", 30)),
        int(config.get("max_spread_gold_points", 100)),
        int(config.get("max_signal_age_seconds", 55)),
    )
    if not command:
        return state
    expiry_epoch = int(datetime.fromisoformat(str(command["expires_at"]).replace("Z", "+00:00")).timestamp())
    fields = [
        "AI_CMD", str(command["id"]), str(command["symbol"]), str(command["decision"]),
        "{:.10f}".format(float(command["entry_price"])),
        "{:.10f}".format(float(command["stop_loss"])),
        "{:.10f}".format(float(command["take_profit"])),
        str(int(command["confidence"])), str(int(time.time())),
        "{:.3f}".format(float(config.get("risk_per_trade_percent", 0.50))),
        "{:.5f}".format(float(config.get("max_lot", 0.01))),
        str(int(config.get("max_open_positions", 1))),
        "{:.3f}".format(float(config.get("max_daily_loss_percent", 2.0))),
        str(int(config.get("max_trades_per_day", 3))),
        str(int(config.get("max_entry_deviation_points", 50))),
        str(int(config.get("max_spread_gold_points" if str(command["symbol"]).upper().startswith("XAU") else "max_spread_fx_points", 100 if str(command["symbol"]).upper().startswith("XAU") else 30))),
        str(expiry_epoch),
    ]
    return state + "|".join(fields) + "\n"


def _report_ai_execution(device, body):
    decision_id = str(body.get("decision_id", "")).strip()
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", decision_id):
        raise RequestError("Invalid AI decision reference.")
    status = str(body.get("status", "")).strip().lower()
    if status not in ("executed", "rejected"):
        raise RequestError("Invalid AI execution result.")
    rows = _db_request("GET", _query(
        "lm_mt5_ai_decisions",
        select="id,session_id,symbol,decision,entry_price,stop_loss,take_profit,device_id,user_id,status",
        id="eq." + decision_id,
        device_id="eq." + str(device["id"]),
        user_id="eq." + str(device["user_id"]),
        status="eq.claimed",
        limit="1",
    ))
    if not isinstance(rows, list) or not rows:
        raise RequestError("This AI command is not claimed by this linked MT5 account.")
    decision = rows[0]
    result = {"reported_at": _now_iso()}
    if status == "rejected":
        result["reason"] = str(body.get("reason", "MT5 rejected the order."))[:300]
    else:
        order_ticket = int(_number(body.get("order_ticket"), "order_ticket", 1, 9223372036854775807, integer=True))
        position_id = int(_number(body.get("position_id"), "position_id", 1, 9223372036854775807, integer=True))
        requested_volume = _number(body.get("requested_volume"), "requested_volume", 0.001, 1000)
        filled_volume = _number(body.get("filled_volume"), "filled_volume", 0.001, 1000)
        fill_price = _number(body.get("fill_price"), "fill_price", 0.00000001, 100000000)
        result.update({
            "order_ticket": order_ticket, "position_id": position_id,
            "requested_volume": requested_volume, "filled_volume": filled_volume,
            "fill_price": fill_price,
        })
        _db_request("POST", "/rest/v1/lm_mt5_scalp_trades", {
            "device_id": str(device["id"]), "user_id": str(device["user_id"]),
            "position_id": position_id, "order_ticket": order_ticket,
            "symbol": decision["symbol"], "direction": decision["decision"],
            "requested_volume": requested_volume, "filled_volume": filled_volume,
            "entry_price": fill_price, "stop_loss": float(decision["stop_loss"]),
            "take_profit": float(decision["take_profit"]),
            "initial_risk_distance": abs(float(decision["entry_price"]) - float(decision["stop_loss"])),
            "magic_number": MT5_MAGIC_NUMBER, "status": "open", "updated_at": _now_iso(),
        }, prefer="return=minimal")
        today = datetime.now(timezone.utc).date().isoformat()
        state = _db_request("GET", _query(
            "lm_mt5_daily_risk_state",
            select="trades_opened",
            device_id="eq." + str(device["id"]),
            trading_day="eq." + today,
            limit="1",
        ))
        if isinstance(state, list) and state:
            _db_request("PATCH", _query(
                "lm_mt5_daily_risk_state",
                device_id="eq." + str(device["id"]), trading_day="eq." + today,
            ), {"trades_opened": int(state[0].get("trades_opened") or 0) + 1, "updated_at": _now_iso()},
                prefer="return=minimal")
    updated = _db_request("PATCH", _query(
        "lm_mt5_ai_decisions", id="eq." + decision_id,
        device_id="eq." + str(device["id"]), status="eq.claimed",
        select="id",
    ), {
        "status": status, "completed_at": _now_iso(), "execution_result": result,
    }, prefer="return=representation")
    if not isinstance(updated, list) or not updated:
        raise RequestError("AI execution report could not be saved.")
    return {"saved": True, "status": status, "decision_id": decision_id}


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



def _record_scalp_trade(device, body):
    _check_account_identity(device, body.get("account_login"), body.get("broker_server"))
    try:
        magic = int(body.get("magic_number", 0))
    except (TypeError, ValueError, OverflowError) as exc:
        raise RequestError("Invalid LM SCALPER magic number.") from exc
    if magic != MT5_MAGIC_NUMBER:
        raise RequestError("This position is not an LM AUTO TRADER scalping trade.")
    position_id = int(_number(body.get("position_id"), "position_id", 1, 9223372036854775807, integer=True))
    order_ticket = int(_number(body.get("order_ticket"), "order_ticket", 1, 9223372036854775807, integer=True))
    symbol = str(body.get("symbol", "")).strip()[:32]
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", symbol):
        raise RequestError("Invalid scalping symbol.")
    direction = str(body.get("direction", "")).strip().upper()
    if direction not in ("BUY", "SELL"):
        raise RequestError("Invalid scalping direction.")
    payload = {
        "device_id": str(device["id"]),
        "user_id": str(device["user_id"]),
        "position_id": position_id,
        "order_ticket": order_ticket,
        "symbol": symbol,
        "direction": direction,
        "requested_volume": _number(body.get("requested_volume"), "requested_volume", 0.001, 1000),
        "filled_volume": _number(body.get("filled_volume"), "filled_volume", 0.001, 1000),
        "entry_price": _number(body.get("entry_price"), "entry_price", 0.00000001, 100000000),
        "stop_loss": _number(body.get("stop_loss"), "stop_loss", 0.00000001, 100000000),
        "take_profit": _number(body.get("take_profit"), "take_profit", 0.00000001, 100000000),
        "initial_risk_distance": _number(body.get("initial_risk_distance"), "initial_risk_distance", 0.00000001, 100000000),
        "magic_number": magic,
        "status": "open",
        "updated_at": _now_iso(),
    }
    existing = _db_request(
        "GET",
        _query(
            "lm_mt5_scalp_trades",
            select="id,status",
            device_id="eq." + str(device["id"]),
            position_id="eq." + str(position_id),
            limit="1",
        ),
    )
    if isinstance(existing, list) and existing:
        _db_request(
            "PATCH",
            _query(
                "lm_mt5_scalp_trades",
                device_id="eq." + str(device["id"]),
                position_id="eq." + str(position_id),
            ),
            {"status": "open", "updated_at": _now_iso()},
            prefer="return=minimal",
        )
        return {"saved": True, "position_id": position_id, "existing": True}
    _db_request(
        "POST",
        "/rest/v1/lm_mt5_scalp_trades",
        payload,
        prefer="return=minimal",
    )
    return {"saved": True, "position_id": position_id, "existing": False}



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
    # Require a recorded LM Analyzer execution or an LM SCALPER trade on this
    # linked device before accepting broker deal history.
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
    scalp_rows = []
    if not isinstance(execution_rows, list) or not execution_rows:
        scalp_rows = _db_request(
            "GET",
            _query(
                "lm_mt5_scalp_trades",
                select="id,symbol,status",
                device_id="eq." + str(device["id"]),
                position_id="eq." + str(position_id),
                limit="1",
            ),
        )
    if (not isinstance(execution_rows, list) or not execution_rows) and (not isinstance(scalp_rows, list) or not scalp_rows):
        raise RequestError("This MT5 position is not linked to a verified LM AUTO TRADER execution.")
    matched_row = execution_rows[0] if isinstance(execution_rows, list) and execution_rows else scalp_rows[0]
    stored_symbol = re.sub(r"[^A-Z0-9]", "", str(matched_row.get("symbol", "")).upper())
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
        if isinstance(scalp_rows, list) and scalp_rows and payload["deal_entry"] in ("DEAL_ENTRY_OUT", "DEAL_ENTRY_OUT_BY", "DEAL_ENTRY_INOUT"):
            _db_request(
                "PATCH",
                _query(
                    "lm_mt5_scalp_trades",
                    device_id="eq." + str(device["id"]),
                    position_id="eq." + str(position_id),
                ),
                {"status": "closed", "updated_at": _now_iso()},
                prefer="return=minimal",
            )
        return {"saved": True, "duplicate": False}
    except RequestError as exc:
        if "already been recorded" in str(exc):
            return {"saved": True, "duplicate": True}
        raise



def _snapshot_positions(raw_positions):
    if not isinstance(raw_positions, list) or len(raw_positions) > 50:
        raise RequestError("Invalid MT5 open-position snapshot.")
    positions = []
    for item in raw_positions:
        if not isinstance(item, dict):
            raise RequestError("Invalid MT5 position entry.")
        try:
            magic = int(item.get("magic_number", 0))
        except (TypeError, ValueError, OverflowError) as exc:
            raise RequestError("Invalid MT5 position magic number.") from exc
        # The customer dashboard must not expose or manage manual/other-EA trades.
        if magic != MT5_MAGIC_NUMBER:
            continue
        ticket = int(_number(item.get("ticket"), "position_ticket", 1, 9223372036854775807, integer=True))
        position_id = int(_number(item.get("position_id"), "position_id", 1, 9223372036854775807, integer=True))
        symbol = str(item.get("symbol", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", symbol):
            raise RequestError("Invalid MT5 position symbol.")
        direction = str(item.get("direction", "")).strip().upper()
        if direction not in ("BUY", "SELL"):
            raise RequestError("Invalid MT5 position direction.")
        positions.append({
            "ticket": ticket,
            "position_id": position_id,
            "symbol": symbol,
            "direction": direction,
            "volume": _number(item.get("volume"), "position_volume", 0.001, 1000),
            "price_open": _number(item.get("price_open"), "position_price_open", 0.00000001, 100000000),
            "stop_loss": _number(item.get("stop_loss", 0), "position_stop_loss", 0, 100000000),
            "take_profit": _number(item.get("take_profit", 0), "position_take_profit", 0, 100000000),
            "profit": _number(item.get("profit", 0), "position_profit", -1000000000, 1000000000),
            "swap": _number(item.get("swap", 0), "position_swap", -1000000000, 1000000000),
            "magic_number": magic,
            "opened_at_epoch": int(_number(item.get("opened_at_epoch", 0), "position_open_time", 0, 4102444800, integer=True)),
        })
    return positions


def _record_account_snapshot(device, body):
    _check_account_identity(device, body.get("account_login"), body.get("broker_server"))
    currency = str(body.get("currency", "")).strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{3,8}", currency):
        raise RequestError("Invalid MT5 account currency.")
    connected = body.get("terminal_connected") is True
    positions = _snapshot_positions(body.get("positions", []))
    payload = {
        "device_id": str(device["id"]),
        "user_id": str(device["user_id"]),
        "account_login": int(device["account_login"]),
        "broker_server": str(device["broker_server"]),
        "account_mode": str(device.get("account_mode", "demo")),
        "balance": _number(body.get("balance"), "balance", -1000000000, 1000000000),
        "equity": _number(body.get("equity"), "equity", -1000000000, 1000000000),
        "floating_profit": _number(body.get("floating_profit", 0), "floating_profit", -1000000000, 1000000000),
        "credit": _number(body.get("credit", 0), "credit", -1000000000, 1000000000),
        "margin": _number(body.get("margin", 0), "margin", 0, 1000000000),
        "free_margin": _number(body.get("free_margin", 0), "free_margin", -1000000000, 1000000000),
        "margin_level": _number(body.get("margin_level", 0), "margin_level", 0, 1000000000),
        "currency": currency,
        "leverage": int(_number(body.get("leverage", 1), "leverage", 1, 1000000, integer=True)),
        "terminal_connected": connected,
        "terminal_build": int(_number(body.get("terminal_build", 0), "terminal_build", 0, 1000000, integer=True)),
        "positions": positions,
        "updated_at": _now_iso(),
    }
    _db_request(
        "POST",
        "/rest/v1/lm_mt5_account_snapshots?on_conflict=device_id",
        payload,
        prefer="resolution=merge-duplicates,return=minimal",
    )
    return {"saved": True, "terminal_connected": connected, "open_positions": len(positions)}


def _dashboard_for_user(user):
    devices = _db_request(
        "GET",
        _query(
            "lm_mt5_devices",
            select="id,account_login,broker_server,account_mode,auto_trade_enabled,risk_per_trade,max_daily_loss_percent,max_open_positions,max_trades_per_day,max_lot,min_confidence,max_signal_age_minutes,last_seen_at,created_at",
            user_id="eq." + str(user["id"]),
            revoked_at="is.null",
            order="created_at.desc",
            limit="5",
        ),
    )
    if not isinstance(devices, list) or not devices:
        return {"connected": False, "telemetry_status": "not_connected", "device": None, "account": None, "open_positions": [], "executions": [], "recent_deals": []}
    device = devices[0]
    snapshots = _db_request(
        "GET",
        _query(
            "lm_mt5_account_snapshots",
            select="balance,equity,floating_profit,credit,margin,free_margin,margin_level,currency,leverage,terminal_connected,terminal_build,positions,updated_at",
            device_id="eq." + str(device["id"]),
            limit="1",
        ),
    )
    snapshot = snapshots[0] if isinstance(snapshots, list) and snapshots else None
    telemetry_status = "waiting_for_terminal"
    if snapshot:
        try:
            updated = datetime.fromisoformat(str(snapshot.get("updated_at", "")).replace("Z", "+00:00"))
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - updated).total_seconds()
            telemetry_status = "live" if age <= 45 and snapshot.get("terminal_connected") is True else "stale"
        except (TypeError, ValueError):
            telemetry_status = "stale"
    executions = _db_request(
        "GET",
        _query(
            "lm_mt5_execution_records",
            select="signal_id,symbol,direction,status,order_ticket,position_id,requested_volume,filled_volume,fill_price,reason,created_at,updated_at",
            device_id="eq." + str(device["id"]),
            order="created_at.desc",
            limit="20",
        ),
    )
    deals = _db_request(
        "GET",
        _query(
            "lm_mt5_trade_deals",
            select="deal_ticket,position_id,symbol,deal_entry,deal_type,volume,price,profit,commission,swap,deal_time",
            device_id="eq." + str(device["id"]),
            order="deal_time.desc",
            limit="30",
        ),
    )
    scalp_trades = _db_request(
        "GET",
        _query(
            "lm_mt5_scalp_trades",
            select="position_id,order_ticket,symbol,direction,requested_volume,filled_volume,entry_price,stop_loss,take_profit,initial_risk_distance,status,created_at,updated_at",
            device_id="eq." + str(device["id"]),
            order="created_at.desc",
            limit="20",
        ),
    )
    sessions = _db_request(
        "GET",
        _query(
            "lm_mt5_trading_sessions",
            select="id,status,account_mode,started_at,stopped_at,last_decision_at,last_heartbeat_at,stop_reason,risk_config,updated_at",
            device_id="eq." + str(device["id"]),
            user_id="eq." + str(user["id"]),
            limit="1",
        ),
    )
    ai_decisions = _db_request(
        "GET",
        _query(
            "lm_mt5_ai_decisions",
            select="id,symbol,decision,confidence,entry_price,stop_loss,take_profit,rationale,status,created_at,completed_at,execution_result",
            device_id="eq." + str(device["id"]),
            order="created_at.desc",
            limit="20",
        ),
    )
    market = _latest_market_data(device)
    return {
        "connected": True,
        "telemetry_status": telemetry_status,
        "device": device,
        "account": snapshot,
        "open_positions": (snapshot or {}).get("positions") or [],
        "executions": executions if isinstance(executions, list) else [],
        "recent_deals": deals if isinstance(deals, list) else [],
        "scalp_trades": scalp_trades if isinstance(scalp_trades, list) else [],
        "session": sessions[0] if isinstance(sessions, list) and sessions else None,
        "ai_decisions": ai_decisions if isinstance(ai_decisions, list) else [],
        "market_data_updated_at": market.get("updated_at") if market else None,
    }

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
                parsed.get("account_login", [None])[0] or self.headers.get("X-LM-Account-Login"),
                parsed.get("broker_server", [None])[0] or self.headers.get("X-LM-Broker-Server"),
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
            if action == "dashboard":
                user = verify_access_token(extract_bearer_token(self))
                json_response(self, 200, _dashboard_for_user(user))
                return
            if action == "link":
                json_response(self, 200, _link_terminal(body))
                return
            if action == "revoke_all":
                user = verify_access_token(extract_bearer_token(self))
                json_response(self, 200, _revoke_user_devices(user))
                return
            if action in ("session_start", "session_stop", "session_status"):
                user = verify_access_token(extract_bearer_token(self))
                if action == "session_start":
                    json_response(self, 200, _session_start(user, body))
                elif action == "session_stop":
                    json_response(self, 200, _session_stop(user, body))
                else:
                    json_response(self, 200, _session_status(user, body))
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
            elif action == "snapshot":
                json_response(self, 200, _record_account_snapshot(device, body))
            elif action == "scalp_trade":
                json_response(self, 200, _record_scalp_trade(device, body))
            elif action == "market_data":
                json_response(self, 200, _record_market_data(device, body))
            elif action == "ai_report":
                json_response(self, 200, _report_ai_execution(device, body))
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
