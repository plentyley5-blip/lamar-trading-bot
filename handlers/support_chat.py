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

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/"
    "v1beta/models/{model}:generateContent"
)

MAX_REQUEST_BYTES = 12_000
MAX_MESSAGE_CHARS = 2_000
MAX_HISTORY_MESSAGES = 12
GEMINI_TIMEOUT_SECONDS = 35


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
            "Message is empty or the request is too large."
        )

    try:
        value = json.loads(
            handler.rfile.read(length).decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            "Invalid request JSON."
        ) from exc

    if not isinstance(value, dict):
        raise ValueError("Invalid request.")

    return value


def authenticated_user(handler):
    token = extract_bearer_token(handler)
    user = verify_access_token(token)

    user_id = str(user.get("id", "")).strip()

    if not user_id:
        raise PermissionError(
            "Your session could not be verified. "
            "Please sign in again."
        )

    return user_id


def supabase_request(
    method, table, query=None, payload=None, prefer=None
):
    base = get_supabase_url().strip().rstrip("/")
    key = get_supabase_service_key().strip()

    if not base or not key:
        raise RuntimeError(
            "Support chat database configuration is missing."
        )

    url = base + "/rest/v1/" + table

    if query:
        url += "?" + urllib.parse.urlencode(
            query, safe="(),.*:"
        )

    data = (
        None
        if payload is None
        else json.dumps(
            payload, ensure_ascii=False
        ).encode("utf-8")
    )

    headers = {
        "apikey": key,
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    if prefer:
        headers["Prefer"] = prefer

    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers=headers,
    )

    try:
        with urllib.request.urlopen(
            request, timeout=18
        ) as response:
            raw = response.read().decode(
                "utf-8", errors="replace"
            )

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(
            "utf-8", errors="replace"
        )

        raise RuntimeError(
            "Support chat storage failed. "
            "Check the support chat database tables "
            "and Vercel configuration. (HTTP {})".format(
                exc.code
            )
        ) from exc

    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(
            "Support chat storage is temporarily unavailable."
        ) from exc

    if not raw.strip():
        return []

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "The support chat database returned an invalid response."
        ) from exc

    return result if isinstance(result, list) else result


def owned_conversation(conversation_id, user_id):
    rows = supabase_request(
        "GET",
        "support_conversations",
        {
            "id": "eq." + conversation_id,
            "user_id": "eq." + user_id,
            "select": "id,user_id,title,created_at,updated_at",
            "limit": "1",
        },
    )

    if not isinstance(rows, list) or not rows:
        raise LookupError("Conversation not found.")

    return rows[0]


def get_messages(conversation_id, user_id, limit=200):
    return supabase_request(
        "GET",
        "support_messages",
        {
            "conversation_id": "eq." + conversation_id,
            "user_id": "eq." + user_id,
            "select": (
                "id,conversation_id,user_id,role,content,created_at"
            ),
            "order": "created_at.asc",
            "limit": str(limit),
        },
    )


def save_message(conversation_id, user_id, role, content):
    rows = supabase_request(
        "POST",
        "support_messages",
        payload={
            "conversation_id": conversation_id,
            "user_id": user_id,
            "role": role,
            "content": content,
        },
        prefer="return=representation",
    )

    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Unable to save support message.")

    return rows[0]


def call_gemini(messages):
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "Gemini support is not configured. "
            "Set GEMINI_OPENAI_KEY in Vercel."
        )

    system_prompt = (
        "You are LM Analyzer's friendly, concise customer-support "
        "assistant for a forex chart-analysis Android app. "
        "Help with app navigation, login/session problems, chart "
        "screenshot upload, 4H and 15M chart requirements, "
        "analysis-result fields, subscription access/expiry/payment "
        "troubleshooting, saved analyses, and general forex terminology. "
        "Do not claim access to the user's account, payments, private "
        "data, or backend logs. Do not request passwords, one-time codes, "
        "API keys, or payment-card details. Never guarantee profits, "
        "fabricate subscription status, or present yourself as a human. "
        "You are customer support, not a signal service: do not promise "
        "a trade will win or invent live prices. Explain that AI can "
        "make mistakes. If a problem requires account changes, billing "
        "investigation, repeated technical troubleshooting, or you "
        "cannot confidently solve it, say so plainly and direct the "
        "user to contact LAMAR through the WhatsApp or Telegram buttons "
        "visible in the chat screen. Keep answers practical and "
        "step-by-step. Do not use markdown tables."
    )

    contents = []

    for item in messages[-MAX_HISTORY_MESSAGES:]:
        role = (
            "model"
            if item.get("role") == "assistant"
            else "user"
        )

        content = str(
            item.get("content", "")
        ).strip()

        if content:
            contents.append({
                "role": role,
                "parts": [{
                    "text": content[:MAX_MESSAGE_CHARS]
                }],
            })

    if not contents:
        raise ValueError("Please enter a message.")

    payload = {
        "system_instruction": {
            "parts": [{"text": system_prompt}]
        },
        "contents": contents,
        "generationConfig": {
            "temperature": 0.35,
            "maxOutputTokens": 700,
        },
    }

    body = json.dumps(
        payload, separators=(",", ":")
    ).encode("utf-8")

    failures = []
    retryable = {408, 429, 500, 502, 503, 504}

    for index, model in enumerate(GEMINI_MODELS):
        request = urllib.request.Request(
            GEMINI_URL.format(model=model),
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
                request, timeout=GEMINI_TIMEOUT_SECONDS
            ) as response:
                raw = response.read().decode(
                    "utf-8", errors="replace"
                )

        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(
                "utf-8", errors="replace"
            )

            try:
                detail = str(
                    json.loads(detail)
                    .get("error", {})
                    .get("message")
                    or detail[:500]
                )
            except Exception:
                detail = detail[:500]

            failures.append(
                "{} HTTP {}: {}".format(
                    model, exc.code, detail
                )
            )

            if (
                exc.code in retryable or exc.code == 404
            ) and index < len(GEMINI_MODELS) - 1:
                time.sleep(0.5)
                continue

            if exc.code in {400, 401, 403}:
                break

        except (
            urllib.error.URLError,
            TimeoutError,
        ):
            failures.append(
                "{} connection error".format(model)
            )

            if index < len(GEMINI_MODELS) - 1:
                continue

        else:
            try:
                data = json.loads(raw)
                pieces = []

                for candidate in data.get(
                    "candidates", []
                ):
                    content = (
                        candidate.get("content", {})
                        if isinstance(candidate, dict)
                        else {}
                    )

                    parts = (
                        content.get("parts", [])
                        if isinstance(content, dict)
                        else []
                    )

                    for part in parts:
                        if (
                            isinstance(part, dict)
                            and isinstance(
                                part.get("text"), str
                            )
                        ):
                            pieces.append(part["text"])

                answer = "\n".join(pieces).strip()

                if answer:
                    return answer[:6000]

                failures.append(
                    "{} returned an empty reply".format(model)
                )

            except (ValueError, TypeError):
                failures.append(
                    "{} returned invalid JSON".format(model)
                )

    raise RuntimeError(
        "The AI support assistant is temporarily unavailable. "
        "Please try again or contact LAMAR on WhatsApp or Telegram."
    )


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        json_response(self, 204, {})

    def do_GET(self):
        try:
            user_id = authenticated_user(self)

            params = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query
            )

            conversation_id = str(
                (params.get("conversation_id") or [""])[0]
            ).strip()

            if conversation_id:
                owned_conversation(
                    conversation_id, user_id
                )

                messages = get_messages(
                    conversation_id, user_id
                )

                json_response(self, 200, {
                    "conversation_id": conversation_id,
                    "messages": messages,
                })
                return

            conversations = supabase_request(
                "GET",
                "support_conversations",
                {
                    "user_id": "eq." + user_id,
                    "select": (
                        "id,title,created_at,updated_at"
                    ),
                    "order": "updated_at.desc",
                    "limit": "50",
                },
            )

            json_response(self, 200, {
                "conversations": (
                    conversations
                    if isinstance(conversations, list)
                    else []
                )
            })

        except PermissionError as exc:
            json_response(self, 401, {
                "error": str(exc)
            })

        except LookupError:
            json_response(self, 404, {
                "error": "Conversation not found."
            })

        except Exception as exc:
            json_response(self, 500, {
                "error": (
                    str(exc)
                    if isinstance(
                        exc, (ValueError, RuntimeError)
                    )
                    else "Unable to load support chat."
                )
            })

    def do_POST(self):
        try:
            user_id = authenticated_user(self)
            data = read_json(self)

            action = str(
                data.get("action", "send")
            ).strip().lower()

            if action != "send":
                raise ValueError(
                    "Unsupported support chat action."
                )

            message = str(
                data.get("message", "")
            ).strip()

            if not message:
                raise ValueError(
                    "Type a message before sending."
                )

            if len(message) > MAX_MESSAGE_CHARS:
                raise ValueError(
                    "Messages must be 2,000 characters or fewer."
                )

            conversation_id = str(
                data.get("conversation_id", "")
            ).strip()

            if conversation_id:
                owned_conversation(
                    conversation_id, user_id
                )
            else:
                title = " ".join(
                    message.split()
                )[:72]

                rows = supabase_request(
                    "POST",
                    "support_conversations",
                    payload={
                        "user_id": user_id,
                        "title": title or "Support conversation",
                    },
                    prefer="return=representation",
                )

                if not isinstance(rows, list) or not rows:
                    raise RuntimeError(
                        "Unable to create support conversation."
                    )

                conversation_id = str(
                    rows[0].get("id", "")
                )

                if not conversation_id:
                    raise RuntimeError(
                        "Support conversation was created without an ID."
                    )

            save_message(
                conversation_id,
                user_id,
                "user",
                message,
            )

            supabase_request(
                "PATCH",
                "support_conversations",
                {
                    "id": "eq." + conversation_id,
                    "user_id": "eq." + user_id,
                },
                {
                    "updated_at": datetime.now(
                        timezone.utc
                    ).isoformat()
                },
            )

            context = get_messages(
                conversation_id,
                user_id,
                limit=200,
            )

            try:
                reply = call_gemini(context)
            except Exception:
                reply = (
                    "I'm temporarily unable to generate a full answer. "
                    "Please try again shortly. If this is urgent or "
                    "relates to your account or subscription, use the "
                    "WhatsApp or Telegram buttons above to contact "
                    "LAMAR directly."
                )

            save_message(
                conversation_id,
                user_id,
                "assistant",
                reply,
            )

            supabase_request(
                "PATCH",
                "support_conversations",
                {
                    "id": "eq." + conversation_id,
                    "user_id": "eq." + user_id,
                },
                {
                    "updated_at": datetime.now(
                        timezone.utc
                    ).isoformat()
                },
            )

            json_response(self, 200, {
                "conversation_id": conversation_id,
                "reply": reply,
            })

        except PermissionError as exc:
            json_response(self, 401, {
                "error": str(exc)
            })

        except ValueError as exc:
            json_response(self, 400, {
                "error": str(exc)
            })

        except Exception as exc:
            json_response(self, 500, {
                "error": (
                    str(exc)
                    if isinstance(exc, RuntimeError)
                    else (
                        "Unable to send support message. "
                        "Please try again."
                    )
                )
            })
