import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler
from datetime import datetime, timezone

from api.user_security import (
    extract_bearer_token,
    get_supabase_service_key,
    get_supabase_url,
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
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MAX_REQUEST_BYTES = 12_000
MAX_MESSAGE_CHARS = 2_000
MAX_HISTORY_MESSAGES = 12
GEMINI_TIMEOUT_SECONDS = 35


def json_response(handler, status_code, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except Exception:
        pass


def read_json(handler):
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError as exc:
        raise ValueError("Invalid request length.") from exc
    if length <= 0 or length > MAX_REQUEST_BYTES:
        raise ValueError("Message is empty or the request is too large.")
    try:
        value = json.loads(handler.rfile.read(length).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid request JSON.") from exc
    if not isinstance(value, dict):
        raise ValueError("Invalid request.")
    return value


def authenticated_user(handler):
    token = extract_bearer_token(handler)
    user = verify_access_token(token)
    user_id = str(user.get("id", "")).strip()
    if not user_id:
        raise PermissionError("Your session could not be verified. Please sign in again.")
    return user_id


def supabase_request(method, table, query=None, payload=None, prefer=None):
    base = get_supabase_url().strip().rstrip("/")
    key = get_supabase_service_key().strip()
    if not base or not key:
        raise RuntimeError("Support chat database configuration is missing.")
    url = base + "/rest/v1/" + table
    if query:
        url += "?" + urllib.parse.urlencode(query, safe="(),.*:")
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "apikey": key,
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=18) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
