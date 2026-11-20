"""LM Analyzer support chat with optional human-agent takeover.

Deploy as api/support_chat.py in the existing Vercel repository.
Existing customer chat/history is retained. Human support uses the same
support_conversations and support_messages rows, with role-based API access.
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

from api.user_security import (
    extract_bearer_token,
    get_supabase_service_key,
    get_supabase_url,
    is_owner_user,
    verify_access_token,
)

def get_gemini_api_key():
    """Use the same canonical key as LM Analyzer's working analysis backend."""
    return (
        os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GEMINI_OPENAI_KEY", "").strip()
    )


# Keep one current primary and one fallback already used by the main analysis
# service. This prevents support from depending on a separate model entitlement.
GEMINI_MODELS = (
    "gemini-3.8-flash",
    "gemini-3.5-flash-lite",
)
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MAX_REQUEST_BYTES = 12_000
MAX_MESSAGE_CHARS = 2_000
MAX_HISTORY_MESSAGES = 12
GEMINI_TIMEOUT_SECONDS = 18
HANDOFF_MARKER = "[[ESCALATE_TO_HUMAN]]"
VALID_STATUSES = {"bot", "waiting", "assigned", "resolved"}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


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
    user_id = str(user.get("id", "")).strip() if isinstance(user, dict) else ""
    if not user_id:
        raise PermissionError("Your session could not be verified. Please sign in again.")
    return user


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
        # Keep service-role credentials and upstream database details out of client responses.
        raise RuntimeError("Support chat storage failed. Check the support chat database migration and Vercel configuration. (HTTP {})".format(exc.code)) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Support chat storage is temporarily unavailable.") from exc
    if not raw.strip():
        return []
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("The support chat database returned an invalid response.") from exc
    return result if isinstance(result, list) else result


def owned_conversation(conversation_id, user_id):
    rows = supabase_request(
        "GET", "support_conversations",
        {"id": "eq." + conversation_id, "user_id": "eq." + user_id,
         "select": "id,user_id,title,created_at,updated_at,support_status,assigned_agent_id,assigned_agent_name,handoff_requested_at,assigned_at,resolved_at", "limit": "1"},
    )
    if not isinstance(rows, list) or not rows:
        raise LookupError("Conversation not found.")
    return rows[0]


def conversation_for_agent(conversation_id, agent):
    query = {
        "id": "eq." + conversation_id,
        "select": "id,user_id,title,created_at,updated_at,support_status,assigned_agent_id,assigned_agent_name,handoff_requested_at,assigned_at,resolved_at",
        "limit": "1",
    }
    if agent.get("role") != "supervisor":
        query["assigned_agent_id"] = "eq." + str(agent["user_id"])
    rows = supabase_request("GET", "support_conversations", query)
    if not isinstance(rows, list) or not rows:
        raise LookupError("This conversation is not assigned to your agent account.")
    return rows[0]


def get_messages(conversation_id, customer_user_id, limit=300):
    return supabase_request(
        "GET", "support_messages",
        {"conversation_id": "eq." + conversation_id, "user_id": "eq." + customer_user_id,
         "select": "id,conversation_id,user_id,role,content,sender_id,sender_name,created_at",
         "order": "created_at.asc", "limit": str(limit)},
    )



def get_recent_gemini_messages(conversation_id, customer_user_id):
    """Fetch only the newest context Gemini needs; keep full history in storage."""
    rows = supabase_request(
        "GET", "support_messages",
        {"conversation_id": "eq." + conversation_id,
         "user_id": "eq." + customer_user_id,
         "select": "id,conversation_id,user_id,role,content,sender_id,sender_name,created_at",
         "order": "created_at.desc", "limit": str(MAX_HISTORY_MESSAGES)},
    )
    if not isinstance(rows, list):
        return []
    rows.reverse()
    return rows

def simple_greeting_reply(messages):
    """Answer a standalone greeting immediately; never send it to human support."""
    greeting = ""
    for item in reversed(messages or []):
        if str(item.get("role", "")) == "user":
            greeting = str(item.get("content", "")).strip().lower()
            break
    normalized = re.sub(r"[^a-z ]", "", greeting)
    normalized = " ".join(normalized.split())
    greetings = {
        "hi", "hello", "hey", "hiya", "hello there", "hi there",
        "good morning", "good afternoon", "good evening", "howdy",
    }
    if normalized in greetings:
        return "Hello! Welcome to LM Analyzer support. How can I help you today?"
    return None


def simple_acknowledgement_reply(messages):
    """Handle short acknowledgements locally instead of calling Gemini."""
    latest = ""
    for item in reversed(messages or []):
        if str(item.get("role", "")) == "user":
            latest = str(item.get("content", "")).strip().lower()
            break
    normalized = re.sub(r"[^a-z ]", "", latest)
    normalized = " ".join(normalized.split())
    acknowledgements = {
        "thanks", "thank you", "thanks a lot", "thank you so much",
        "okay thanks", "ok thanks", "alright thanks", "got it thanks",
        "thanks got it", "okay thank you", "ok thank you",
        "got it", "understood", "that helps", "perfect", "great thanks",
    }
    if normalized in acknowledgements:
        return "You're welcome! I'm glad I could help. If you have another LM Analyzer question, just ask."
    return None


def fallback_support_reply(messages):
    """Always return a useful, honest support response if the AI provider is unavailable."""
    latest = ""
    for item in reversed(messages or []):
        if str(item.get("role", "")) == "user":
            latest = " ".join(str(item.get("content", "")).split()).strip()
            break
    if not latest:
        return "I'm here to help with LM Analyzer. What are you trying to do, and what happens when you try?"
    short_message = latest[:240]
    return (
        "I received your message: \"" + short_message + "\". "
        "I can help with LM Analyzer setup, chart uploads, analysis results, login, subscriptions, "
        "and app troubleshooting. I couldn't get the AI response service just now, so I don't want "
        "to guess and give you incorrect instructions. "
        "Please tell me which screen or step you're on and what you expected to happen. "
        "I will use those details to guide you through the next troubleshooting step."
    )


def save_message(conversation_id, customer_user_id, role, content, sender_id=None, sender_name=None):
    payload = {
        "conversation_id": conversation_id,
        "user_id": customer_user_id,
        "role": role,
        "content": content,
    }
    if sender_id:
        payload["sender_id"] = sender_id
    if sender_name:
        payload["sender_name"] = sender_name
    rows = supabase_request(
        "POST", "support_messages", payload=payload, prefer="return=representation",
    )
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Unable to save support message.")
    return rows[0]


def touch_conversation(conversation_id, payload=None):
    changes = dict(payload or {})
    changes["updated_at"] = utc_now()
    supabase_request("PATCH", "support_conversations", {"id": "eq." + conversation_id}, changes)


def get_agent(user_id):
    rows = supabase_request(
        "GET", "support_agents",
        {"user_id": "eq." + user_id, "is_active": "eq.true",
         "select": "user_id,display_name,role,is_active,max_active_chats", "limit": "1"},
    )
    if not isinstance(rows, list) or not rows:
        return None
    row = rows[0]
    row["user_id"] = str(row.get("user_id", ""))
    row["display_name"] = str(row.get("display_name") or "Support Agent").strip()
    row["role"] = str(row.get("role") or "agent")
    row["max_active_chats"] = max(1, int(row.get("max_active_chats") or 1))
    return row


def require_agent(user):
    agent = get_agent(str(user.get("id", "")))
    if not agent:
        raise PermissionError("This account is not authorised as an LM support agent.")
    return agent


def active_assignments_for_agents(agent_ids):
    if not agent_ids:
        return []
    values = ",".join(agent_ids)
    rows = supabase_request(
        "GET", "support_conversations",
        {"assigned_agent_id": "in.({})".format(values), "support_status": "eq.assigned",
         "select": "id,assigned_agent_id", "limit": "1000"},
    )
    return rows if isinstance(rows, list) else []


def available_agents():
    rows = supabase_request(
        "GET", "support_agents",
        {"is_active": "eq.true", "select": "user_id,display_name,role,max_active_chats", "order": "created_at.asc", "limit": "1000"},
    )
    agents = rows if isinstance(rows, list) else []
    agents = [a for a in agents if a.get("user_id")]
    if not agents:
        return []
    ids = [str(a["user_id"]) for a in agents]
    assigned = active_assignments_for_agents(ids)
    counts = {}
    for row in assigned:
        key = str(row.get("assigned_agent_id", ""))
        counts[key] = counts.get(key, 0) + 1
    result = []
    for agent in agents:
        agent_id = str(agent["user_id"])
        max_chats = max(1, int(agent.get("max_active_chats") or 1))
        if counts.get(agent_id, 0) < max_chats:
            result.append(agent)
    return result


def send_fcm_to_agents(agent_ids, title, body, conversation_id):
    """Send data-only FCM alerts through the Firebase Admin SDK already used by LM Analyzer."""
    if not agent_ids:
        return {"sent": 0, "failed": 0, "configured": False}
    try:
        import firebase_admin
        from firebase_admin import credentials, messaging

        try:
            firebase_app = firebase_admin.get_app()
        except ValueError:
            raw_service_account = (
                os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
                or os.environ.get("FIREBASE_SERVICE_ACCOUNT", "").strip()
            )
            if not raw_service_account:
                return {"sent": 0, "failed": 0, "configured": False}
            firebase_app = firebase_admin.initialize_app(
                credentials.Certificate(json.loads(raw_service_account))
            )

        in_clause = "(" + ",".join(agent_ids) + ")"
        devices = supabase_request(
            "GET", "support_agent_devices",
            {"agent_id": "in." + in_clause, "select": "agent_id,device_token", "limit": "1000"},
        )
        tokens = list(dict.fromkeys(
            str(row.get("device_token", "")).strip()
            for row in (devices if isinstance(devices, list) else [])
            if row.get("device_token")
        ))
        sent = failed = 0
        invalid_tokens = []
        data = {
            "type": "support_agent_request",
            "conversation_id": str(conversation_id),
            "title": str(title)[:100],
            "body": str(body)[:240],
        }
        android_config = messaging.AndroidConfig(priority="high")
        # FCM multicast supports up to 500 device tokens per batch. This avoids
        # sequential network sends as the support team grows beyond 10 agents.
        if hasattr(messaging, "send_each_for_multicast"):
            for offset in range(0, len(tokens), 500):
                batch = tokens[offset:offset + 500]
                multicast = messaging.MulticastMessage(
                    tokens=batch, data=data, android=android_config
                )
                response = messaging.send_each_for_multicast(multicast, app=firebase_app)
                sent += int(response.success_count)
                failed += int(response.failure_count)
                for token, item in zip(batch, response.responses):
                    if not item.success and item.exception is not None:
                        error_name = type(item.exception).__name__.lower()
                        error_text = str(item.exception).lower()
                        if "unregistered" in error_name or "registration-token-not-registered" in error_text:
                            invalid_tokens.append(token)
        else:
            # Compatibility fallback for older Firebase Admin SDK versions.
            for token in tokens:
                try:
                    message = messaging.Message(
                        token=token, data=data, android=android_config
                    )
                    messaging.send(message, app=firebase_app)
                    sent += 1
                except Exception as exc:
                    failed += 1
                    error_name = type(exc).__name__.lower()
                    error_text = str(exc).lower()
                    if "unregistered" in error_name or "registration-token-not-registered" in error_text:
                        invalid_tokens.append(token)
        if invalid_tokens:
            try:
                supabase_request(
                    "DELETE", "support_agent_devices",
                    {"device_token": "in.({})".format(",".join(invalid_tokens))},
                )
            except Exception:
                pass
        return {"sent": sent, "failed": failed, "configured": True}
    except Exception:
        # Push failure never deletes the waiting request or breaks customer chat.
        # Do not leak Firebase credentials, device tokens or provider payloads.
        return {"sent": 0, "failed": 0, "configured": True}


def alert_available_agents(conversation):
    agents = available_agents()
    if not agents:
        return {"available_agents": 0, "push_sent": 0}
    agent_ids = [str(agent["user_id"]) for agent in agents]
    title = "New LM Analyzer support request"
    issue = str(conversation.get("title") or "Customer needs assistance")[:90]
    delivery = send_fcm_to_agents(
        agent_ids, title, "A customer is waiting: " + issue,
        str(conversation.get("id", "")),
    )
    return {"available_agents": len(agents), "push_sent": int(delivery.get("sent", 0))}


def set_waiting_for_human(conversation, reason=""):
    conversation_id = str(conversation["id"])
    current = str(conversation.get("support_status") or "bot")
    if current in {"waiting", "assigned"}:
        return conversation, {"available_agents": 0, "push_sent": 0}, False
    rows = supabase_request(
        "PATCH", "support_conversations",
        {"id": "eq." + conversation_id, "support_status": "in.(bot,resolved)"},
        {"support_status": "waiting", "assigned_agent_id": None,
         "assigned_agent_name": None, "handoff_requested_at": utc_now(),
         "assigned_at": None, "resolved_at": None, "updated_at": utc_now()},
        prefer="return=representation",
    )
    if not isinstance(rows, list) or not rows:
        latest = supabase_request(
            "GET", "support_conversations",
            {"id": "eq." + conversation_id, "select": "*", "limit": "1"},
        )
        updated = latest[0] if isinstance(latest, list) and latest else conversation
        return updated, {"available_agents": 0, "push_sent": 0}, False
    updated = rows[0]
    note = ("A human support request was created. " + str(reason).strip()).strip()
    if note == "A human support request was created.":
        note = "The customer has requested a human support agent."
    save_message(conversation_id, str(conversation["user_id"]), "system", note)
    delivery = alert_available_agents(updated)
    return updated, delivery, True



def local_support_reply(messages):
    """Answer common LM Analyzer workflow questions without depending on Gemini."""
    latest = ""
    for item in reversed(messages or []):
        if str(item.get("role", "")) == "user":
            latest = str(item.get("content", "")).strip().lower()
            break
    normalized = re.sub(r"[^a-z0-9 ]", " ", latest)
    normalized = " ".join(normalized.split())

    if any(word in normalized for word in ("upload", "attach", "add")) and any(
        word in normalized for word in ("chart", "4h", "15m", "15 minute", "fifteen minute")
    ):
        return (
            "To upload your charts in LM Analyzer:\n"
            "1. Open the app and tap **Start Analysis**.\n"
            "2. Select the trading instrument/pair first (for example EURUSD or XAUUSD).\n"
            "3. In the chart-upload area, choose the **4H** chart screenshot for the higher-timeframe view.\n"
            "4. Choose the **15M** chart screenshot for the lower-timeframe/entry view.\n"
            "5. Check that both screenshots are clear, show the selected instrument, and display the correct timeframe.\n"
            "6. Select your trading focus (SCALP, DAY TRADE, or SWING), then tap **Analyze**.\n\n"
            "If the upload controls have different labels on your screen, tell me what you see or send a screenshot with any private information hidden, and I’ll guide you from there."
        )

    if any(term in normalized for term in ("select pair", "choose pair", "instrument", "trading pair")):
        return (
            "Before uploading charts, open **Start Analysis** and select the instrument you want analyzed. "
            "Then upload its 4H and 15M chart screenshots. Make sure both charts are for the same instrument."
        )

    if any(term in normalized for term in ("analysis result", "what does", "confidence", "take profit", "stop loss", "tp1", "tp2")):
        return (
            "LM Analyzer results can show the signal, confidence, trend, entry, stop loss (SL), "
            "take-profit levels (TP1 and TP2), risk/reward, expected duration, market context, news risk, "
            "and warnings. A **NO TRADE** result means the analysis did not find a setup that met its conditions; "
            "do not treat it as a signal to enter."
        )

    return None


def call_gemini(messages):
    api_key = get_gemini_api_key()
    if not api_key:
        raise RuntimeError("Gemini support is not configured. Set GEMINI_API_KEY in Vercel.")

    system_prompt = (
        "You are LM Analyzer's friendly, concise customer-support assistant for a forex chart-analysis Android app. "
        "Use this product knowledge as the source of truth for LM Analyzer support: users open Start Analysis, select a supported instrument before uploading, "
        "upload TWO screenshots for that same instrument (4H higher-timeframe chart and 15M lower-timeframe chart), choose SCALP, DAY TRADE, or SWING, then tap Analyze. "
        "Supported instruments include major and cross forex pairs, XAUUSD and XAGUSD metals, and BTCUSD and ETHUSD crypto. "
        "Results may include signal, confidence, trend, entry, SL, TP1, TP2, risk/reward, expected duration, context, news risk, and warnings; NO TRADE means no qualifying setup was found. "
        "Subscription plans currently include Weekly (USD 9.23, 5 analyses/day), Monthly (USD 25.21, 10/day), 3-month (USD 63.69, 30/day), and 6-month (USD 127.38, 50/day). "
        "Never promise that a payment has succeeded or claim to inspect an individual customer's plan; direct account-specific payment/entitlement checks to a human agent. "
        "When the app has no internet connection, advise the customer to restore connectivity and retry rather than changing app settings. "
        "Help with app navigation, login/session problems, chart screenshot upload, 4H and 15M chart requirements, "
        "analysis-result fields, subscription access/expiry/payment troubleshooting, saved analyses, and general forex terminology. "
        "Do not claim access to the user's account, payments, private data, or backend logs. Do not request passwords, one-time codes, "
        "API keys, or payment-card details. Never guarantee profits, fabricate subscription status, or present yourself as a human. "
        "You are customer support, not a signal service: do not promise a trade will win or invent live prices. Explain that AI can make mistakes. "
        "Your first responsibility is to resolve the customer's question yourself. You are a real general-purpose AI assistant as well as an LM Analyzer product-support specialist: "
        "answer ordinary knowledge questions, explanations, definitions, and general forex/trading-education questions when safe, while clearly separating general education from personalized financial advice. "
        "For LM Analyzer questions, give direct practical instructions using the product facts in this prompt. "
        "The Community lets signed-in members read posts; only active paid subscribers and the owner can publish, and only the owner can change the official community avatar. "
        "The support chat keeps conversation history and can be continued later. "
        "Answer greetings warmly. For an incomplete question, ask one focused follow-up and continue helping; do not send the customer away. "
        "Never tell a customer to contact an admin or human agent just because a question is unfamiliar, general, short, or outside the exact examples in this prompt. "
        "Use your general knowledge to make a best effort, state uncertainty plainly, and ask for missing details when needed. "
        "Only append the exact token [[ESCALATE_TO_HUMAN]] when the customer explicitly asks for a person, an action requires private staff-only access you do not have "
        "(for example verifying a specific payment, changing account entitlements, or investigating private server records), or the customer has followed reasonable troubleshooting "
        "steps and the issue is still unresolved. Before escalating a technical issue, give practical steps and ask for the result at least once. "
        "Do not claim you have changed account settings, checked private records, or completed a payment. Do not request passwords, one-time codes, API keys, or card details. "
        "For trading topics, explain concepts and risk; never promise profit, issue fabricated live prices, or guarantee a trade outcome. "
        "Do not display or explain the handoff token. Keep answers practical, clear, and step-by-step. Do not use markdown tables."
    )
    contents = []
    for item in messages[-MAX_HISTORY_MESSAGES:]:
        stored_role = str(item.get("role", ""))
        if stored_role in {"assistant", "agent"}:
            role = "model"
        elif stored_role == "user":
            role = "user"
        else:
            continue
        content = str(item.get("content", "")).strip()
        if content:
            contents.append({"role": role, "parts": [{"text": content[:MAX_MESSAGE_CHARS]}]})
    if not contents:
        raise ValueError("Please enter a message.")

    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.35, "maxOutputTokens": 500},
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    started = time.monotonic()

    for index, model in enumerate(GEMINI_MODELS):
        request = urllib.request.Request(
            GEMINI_URL.format(model=model), data=body, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "x-goog-api-key": api_key},
        )
        try:
            with urllib.request.urlopen(request, timeout=GEMINI_TIMEOUT_SECONDS) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            # Log only the provider status and model. Never log the API key,
            # prompt, message content, or raw upstream response.
            provider_error = ""
            try:
                detail = json.loads(exc.read().decode("utf-8", errors="replace") or "{}")
                provider_error = str(
                    (detail.get("error") or {}).get("status")
                    or (detail.get("error") or {}).get("code")
                    or ""
                )[:48]
            except Exception:
                pass
            print("LM_SUPPORT_GEMINI model={} http={} provider_status={}".format(
                model, exc.code, provider_error or "unavailable"
            ))
            # Try the compatibility model for model-specific, capacity, and
            # transient upstream errors. A malformed local request is not
            # retried with the same payload.
            if index == 0 and len(GEMINI_MODELS) > 1 and exc.code in {404, 408, 429, 500, 502, 503, 504}:
                print("LM_SUPPORT_GEMINI trying_fallback=true")
                continue
            if exc.code == 429:
                raise RuntimeError("The AI support assistant is busy. Please try again shortly.") from exc
            raise RuntimeError("The AI support assistant is temporarily unavailable. Please try again shortly.") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if index == 0 and len(GEMINI_MODELS) > 1:
                print("LM_SUPPORT_GEMINI primary_model_timeout trying_fallback=true")
                continue
            raise RuntimeError("The AI support assistant timed out. Please try again shortly.") from exc

        try:
            data = json.loads(raw)
            pieces = []
            for candidate in data.get("candidates", []):
                content = candidate.get("content", {}) if isinstance(candidate, dict) else {}
                for part in content.get("parts", []) if isinstance(content, dict) else []:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        pieces.append(part["text"])
            answer = "\n".join(pieces).strip()
        except (ValueError, TypeError) as exc:
            raise RuntimeError("The AI support assistant returned an invalid response.") from exc

        if answer:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            print("LM_SUPPORT_GEMINI model={} duration_ms={}".format(model, elapsed_ms))
            return answer[:6000]
        raise RuntimeError("The AI support assistant returned an empty response.")

    raise RuntimeError("The AI support assistant is temporarily unavailable. Please try again shortly.")

def update_conversation_status(conversation_id, changes):
    rows = supabase_request(
        "PATCH", "support_conversations", {"id": "eq." + conversation_id},
        dict(changes, updated_at=utc_now()), prefer="return=representation",
    )
    return rows[0] if isinstance(rows, list) and rows else None


def agent_queue(agent):
    agent_id = str(agent["user_id"])
    waiting = supabase_request(
        "GET", "support_conversations",
        {"support_status": "eq.waiting",
         "select": "id,user_id,title,created_at,updated_at,support_status,assigned_agent_id,assigned_agent_name,handoff_requested_at",
         "order": "handoff_requested_at.asc", "limit": "200"},
    )
    mine = supabase_request(
        "GET", "support_conversations",
        {"support_status": "eq.assigned", "assigned_agent_id": "eq." + agent_id,
         "select": "id,user_id,title,created_at,updated_at,support_status,assigned_agent_id,assigned_agent_name,handoff_requested_at,assigned_at",
         "order": "updated_at.desc", "limit": "100"},
    )
    active = len(mine) if isinstance(mine, list) else 0
    waiting = waiting if isinstance(waiting, list) else []
    mine = mine if isinstance(mine, list) else []
    return {
        "waiting": waiting,
        "assigned": mine,
        "waiting_count": len(waiting),
        "active_count": active,
        "max_active_chats": int(agent.get("max_active_chats", 1)),
    }


def claim_conversation(agent, conversation_id):
    result = supabase_request(
        "POST", "rpc/claim_support_conversation",
        payload={"p_conversation_id": conversation_id, "p_agent_id": agent["user_id"]},
    )
    if isinstance(result, dict):
        return result
    raise RuntimeError("The support assignment service returned an invalid response. Confirm the support-agent migration is installed.")


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        json_response(self, 204, {})

    def do_GET(self):
        try:
            user = authenticated_user(self)
            user_id = str(user["id"])
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            action = str((params.get("action") or [""])[0]).strip().lower()
            if action == "bot_profile":
                rows = supabase_request(
                    "GET", "support_bot_settings",
                    {"id": "eq.global", "select": "avatar_data_url,updated_at", "limit": "1"},
                )
                setting = rows[0] if isinstance(rows, list) and rows else {}
                json_response(self, 200, {
                    "is_owner": bool(is_owner_user(user)),
                    "avatar_data_url": str(setting.get("avatar_data_url") or ""),
                    "updated_at": str(setting.get("updated_at") or ""),
                })
                return
            if action == "agent_me":
                agent = get_agent(user_id)
                if not agent:
                    json_response(self, 200, {"is_agent": False})
                    return
                queue = agent_queue(agent)
                json_response(self, 200, {
                    "is_agent": True, "display_name": agent["display_name"],
                    "role": agent["role"], "max_active_chats": agent["max_active_chats"],
                    "active_count": queue["active_count"],
                })
                return
            if action == "agent_queue":
                agent = require_agent(user)
                json_response(self, 200, agent_queue(agent))
                return

            conversation_id = str((params.get("conversation_id") or [""])[0]).strip()
            if conversation_id:
                try:
                    conversation = owned_conversation(conversation_id, user_id)
                except LookupError:
                    agent = require_agent(user)
                    conversation = conversation_for_agent(conversation_id, agent)
                messages = get_messages(conversation_id, str(conversation["user_id"]))
                json_response(self, 200, {
                    "conversation_id": conversation_id,
                    "conversation": conversation,
                    "support_status": conversation.get("support_status") or "bot",
                    "assigned_agent_name": conversation.get("assigned_agent_name") or "",
                    "messages": messages if isinstance(messages, list) else [],
                })
                return
            conversations = supabase_request(
                "GET", "support_conversations",
                {"user_id": "eq." + user_id,
                 "select": "id,title,created_at,updated_at,support_status,assigned_agent_name",
                 "order": "updated_at.desc", "limit": "50"},
            )
            json_response(self, 200, {"conversations": conversations if isinstance(conversations, list) else []})
        except PermissionError as exc:
            json_response(self, 401, {"error": str(exc)})
        except LookupError as exc:
            json_response(self, 404, {"error": str(exc)})
        except Exception as exc:
            json_response(self, 500, {"error": str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "Unable to load support chat."})

    def do_POST(self):
        try:
            user = authenticated_user(self)
            user_id = str(user["id"])
            data = read_json(self)
            action = str(data.get("action", "send")).strip().lower()

            if action == "update_bot_avatar":
                if not is_owner_user(user):
                    raise PermissionError("Only the LM Analyzer administrator can change the support avatar.")
                avatar_data_url = str(data.get("avatar_data_url", "")).strip()
                if not avatar_data_url.startswith(("data:image/jpeg;base64,", "data:image/png;base64,", "data:image/webp;base64,")):
                    raise ValueError("Choose a valid JPG, PNG, or WEBP image.")
                encoded = avatar_data_url.split(",", 1)[1]
                if len(encoded) > 700_000:
                    raise ValueError("The support avatar must be 500 KB or smaller after compression.")
                try:
                    import base64
                    decoded = base64.b64decode(encoded, validate=True)
                except Exception as exc:
                    raise ValueError("The selected avatar image is invalid.") from exc
                if not decoded or len(decoded) > 500_000:
                    raise ValueError("The support avatar must be 500 KB or smaller after compression.")
                supabase_request(
                    "POST", "support_bot_settings?on_conflict=id",
                    payload={"id": "global", "avatar_data_url": avatar_data_url,
                             "updated_by": user_id, "updated_at": utc_now()},
                    prefer="resolution=merge-duplicates,return=representation",
                )
                json_response(self, 200, {"updated": True, "avatar_data_url": avatar_data_url})
                return

            if action == "agent_me":
                agent = get_agent(user_id)
                json_response(self, 200, {"is_agent": bool(agent), "display_name": agent.get("display_name", "") if agent else ""})
                return

            if action == "agent_register_device":
                agent = require_agent(user)
                device_token = str(data.get("device_token", "")).strip()
                platform = str(data.get("platform", "android")).strip().lower()
                if not device_token or len(device_token) > 4096:
                    raise ValueError("A valid Firebase device token is required.")
                if platform not in {"android", "ios"}:
                    platform = "android"
                existing_devices = supabase_request(
                    "GET", "support_agent_devices",
                    {"device_token": "eq." + device_token,
                     "select": "agent_id,device_token", "limit": "1"},
                )
                existing_row = existing_devices[0] if isinstance(existing_devices, list) and existing_devices else None
                is_new_agent_device = not existing_row or str(existing_row.get("agent_id", "")) != str(agent["user_id"])
                supabase_request(
                    "POST", "support_agent_devices",
                    payload={"agent_id": agent["user_id"], "device_token": device_token,
                             "platform": platform, "updated_at": utc_now()},
                    prefer="resolution=merge-duplicates,return=representation",
                )
                pending_alert_sent = False
                # A newly registered agent phone should be notified if requests
                # were already waiting while the agent was offline/unregistered.
                if is_new_agent_device:
                    waiting_rows = supabase_request(
                        "GET", "support_conversations",
                        {"support_status": "eq.waiting",
                         "select": "id,user_id,title,created_at,handoff_requested_at",
                         "order": "handoff_requested_at.asc", "limit": "1"},
                    )
                    has_capacity = any(
                        str(candidate.get("user_id", "")) == str(agent["user_id"])
                        for candidate in available_agents()
                    )
                    if has_capacity and isinstance(waiting_rows, list) and waiting_rows:
                        item = waiting_rows[0]
                        delivery = send_fcm_to_agents(
                            [str(agent["user_id"])],
                            "Waiting LM Analyzer support request",
                            "A customer is waiting: " + str(item.get("title") or "Customer needs assistance")[:90],
                            str(item.get("id", "")),
                        )
                        pending_alert_sent = int(delivery.get("sent", 0)) > 0
                json_response(self, 200, {"registered": True, "pending_alert_sent": pending_alert_sent})
                return

            if action == "agent_unregister_device":
                agent = require_agent(user)
                device_token = str(data.get("device_token", "")).strip()
                if not device_token:
                    raise ValueError("A Firebase device token is required.")
                supabase_request(
                    "DELETE", "support_agent_devices",
                    {"agent_id": "eq." + str(agent["user_id"]), "device_token": "eq." + device_token},
                )
                json_response(self, 200, {"unregistered": True})
                return

            if action == "agent_claim":
                agent = require_agent(user)
                conversation_id = str(data.get("conversation_id", "")).strip()
                if not conversation_id:
                    raise ValueError("A support conversation ID is required.")
                result = claim_conversation(agent, conversation_id)
                if result.get("claimed") is not True:
                    json_response(self, 409, {"claimed": False, "error": result.get("reason", "This request has already been attended or your active-chat limit has been reached.")})
                    return
                conversation = result.get("conversation") or {}
                # Notify available agents about the next oldest waiting request.
                # The newly assigned agent is excluded automatically if their capacity is full.
                waiting_rows = supabase_request(
                    "GET", "support_conversations",
                    {"support_status": "eq.waiting", "select": "id,user_id,title,created_at,updated_at,support_status,handoff_requested_at",
                     "order": "handoff_requested_at.asc", "limit": "1"},
                )
                if isinstance(waiting_rows, list) and waiting_rows:
                    alert_available_agents(waiting_rows[0])
                json_response(self, 200, {"claimed": True, "conversation": conversation,
                                          "agent_name": agent["display_name"]})
                return

            if action == "agent_reply":
                agent = require_agent(user)
                conversation_id = str(data.get("conversation_id", "")).strip()
                message = str(data.get("message", "")).strip()
                if not conversation_id or not message:
                    raise ValueError("A conversation and message are required.")
                if len(message) > MAX_MESSAGE_CHARS:
                    raise ValueError("Messages must be 2,000 characters or fewer.")
                conversation = conversation_for_agent(conversation_id, agent)
                if conversation.get("support_status") != "assigned":
                    raise PermissionError("This conversation is no longer assigned to your agent account.")
                saved = save_message(conversation_id, str(conversation["user_id"]), "agent", message,
                                     agent["user_id"], agent["display_name"])
                touch_conversation(conversation_id)
                json_response(self, 200, {"message": saved})
                return

            if action == "agent_resolve":
                agent = require_agent(user)
                conversation_id = str(data.get("conversation_id", "")).strip()
                if not conversation_id:
                    raise ValueError("A support conversation ID is required.")
                conversation = conversation_for_agent(conversation_id, agent)
                if conversation.get("support_status") != "assigned":
                    raise PermissionError("This conversation is no longer assigned to your agent account.")
                update_conversation_status(conversation_id, {
                    "support_status": "resolved", "assigned_agent_id": None,
                    "assigned_agent_name": None, "resolved_at": utc_now(),
                })
                save_message(conversation_id, str(conversation["user_id"]), "system",
                             "Your conversation was marked resolved by " + agent["display_name"] + ".")
                # Wake the remaining queue now that one agent's slot is free.
                waiting_rows = supabase_request(
                    "GET", "support_conversations",
                    {"support_status": "eq.waiting", "select": "id,user_id,title,created_at,updated_at,support_status,handoff_requested_at",
                     "order": "handoff_requested_at.asc", "limit": "1"},
                )
                notified = alert_available_agents(waiting_rows[0]) if isinstance(waiting_rows, list) and waiting_rows else {"available_agents": 0, "push_sent": 0}
                json_response(self, 200, {"resolved": True, "next_waiting_request": bool(waiting_rows), "notification": notified})
                return

            if action == "request_human":
                conversation_id = str(data.get("conversation_id", "")).strip()
                if not conversation_id:
                    raise ValueError("Start a support conversation before requesting a human agent.")
                conversation = owned_conversation(conversation_id, user_id)
                conversation, delivery, changed = set_waiting_for_human(conversation)
                status = str(conversation.get("support_status") or "waiting")
                if status == "assigned":
                    reply = "A support agent has already joined this conversation. You can continue messaging here."
                elif not changed:
                    reply = "Your request is already saved in the human-support waiting queue. Please stay in this conversation; an agent will join as soon as one becomes available."
                elif delivery.get("available_agents", 0) <= 0:
                    reply = "All human support agents are currently assisting other customers. Your request has been saved in the waiting queue. Please stay in this conversation; an agent will join as soon as one becomes available."
                elif delivery.get("push_sent", 0) <= 0:
                    reply = "Your request has been saved in the waiting queue. Please stay in this conversation; the support team will see it in their agent inbox."
                else:
                    reply = "Your request has been sent to our human support team. Please stay in this conversation; an agent will join here shortly."
                if changed:
                    save_message(conversation_id, user_id, "assistant", reply)
                    touch_conversation(conversation_id)
                json_response(self, 200, {"conversation_id": conversation_id, "reply": reply,
                                          "support_status": status, "assigned_agent_name": conversation.get("assigned_agent_name") or "",
                                          "available_agents": delivery.get("available_agents", 0),
                                          "push_sent": delivery.get("push_sent", 0)})
                return

            if action != "send":
                raise ValueError("Unsupported support chat action.")
            message = str(data.get("message", "")).strip()
            if not message:
                raise ValueError("Type a message before sending.")
            if len(message) > MAX_MESSAGE_CHARS:
                raise ValueError("Messages must be 2,000 characters or fewer.")

            conversation_id = str(data.get("conversation_id", "")).strip()
            if conversation_id:
                conversation = owned_conversation(conversation_id, user_id)
            else:
                title = " ".join(message.split())[:72]
                rows = supabase_request(
                    "POST", "support_conversations",
                    payload={"user_id": user_id, "title": title or "Support conversation"},
                    prefer="return=representation",
                )
                if not isinstance(rows, list) or not rows:
                    raise RuntimeError("Unable to create support conversation.")
                conversation = rows[0]
                conversation_id = str(conversation.get("id", ""))
                if not conversation_id:
                    raise RuntimeError("Support conversation was created without an ID.")

            save_message(conversation_id, user_id, "user", message)
            touch_conversation(conversation_id)
            status = str(conversation.get("support_status") or "bot")
            if status in {"waiting", "assigned"}:
                # Human handoff is active: never let Gemini compete with the assigned agent.
                latest = owned_conversation(conversation_id, user_id)
                json_response(self, 200, {"conversation_id": conversation_id, "reply": "",
                                          "support_status": latest.get("support_status") or status,
                                          "assigned_agent_name": latest.get("assigned_agent_name") or ""})
                return
            if status == "resolved":
                conversation = update_conversation_status(conversation_id, {
                    "support_status": "bot", "assigned_agent_id": None,
                    "assigned_agent_name": None, "resolved_at": None,
                }) or conversation

            context = get_recent_gemini_messages(conversation_id, user_id)
            try:
                # Handle a standalone greeting locally so provider/model issues
                # can never turn a simple hello into a human-support referral.
                raw_reply = simple_greeting_reply(context)
                if raw_reply is None:
                    raw_reply = simple_acknowledgement_reply(context)
                if raw_reply is None:
                    raw_reply = local_support_reply(context)
                if raw_reply is None:
                    raw_reply = call_gemini(context)
            except Exception as exc:
                # A provider timeout/quota spike is not a reason to create a
                # human-support ticket. Keep the conversation in bot mode so
                # the customer can retry; explicit model escalation still
                # follows the normal human-agent handoff path below.
                safe_reason = re.sub(r"[^A-Za-z0-9 _.,():/-]", "", str(exc))[:180]
                print("LM_SUPPORT_GEMINI unavailable error_type={} reason={}".format(
                    type(exc).__name__, safe_reason or "unspecified"
                ))
                # Never leave a customer with the same dead-end error. Preserve bot
                # mode and return a helpful, honest fallback tied to their message.
                raw_reply = fallback_support_reply(context)
            should_handoff = HANDOFF_MARKER in raw_reply
            reply = raw_reply.replace(HANDOFF_MARKER, "").strip()
            if not reply:
                reply = "I need a human support agent to help resolve this."
            save_message(conversation_id, user_id, "assistant", reply)
            touch_conversation(conversation_id)
            if should_handoff:
                latest = owned_conversation(conversation_id, user_id)
                updated, delivery, handoff_created = set_waiting_for_human(latest, "The AI assistant escalated this issue for human review.")
                if str(updated.get("support_status")) == "assigned":
                    queue_note = "A human support agent has already joined this conversation. Continue here; you do not need to repeat your explanation."
                elif not handoff_created:
                    queue_note = "Your request is already saved in the human-support waiting queue. Please stay in this conversation; an agent will join as soon as one becomes available."
                elif delivery.get("available_agents", 0) <= 0:
                    queue_note = "All human support agents are currently assisting other customers. Your request has been saved in the waiting queue. Please stay in this conversation; an agent will join as soon as one becomes available."
                elif delivery.get("push_sent", 0) <= 0:
                    queue_note = "Your request has been saved in the waiting queue. The support team can see it in their agent inbox; please stay in this conversation."
                else:
                    queue_note = "I've asked a human support agent to continue helping you here. You do not need to repeat your explanation."
                save_message(conversation_id, user_id, "assistant", queue_note)
                reply = reply + "\n\n" + queue_note
                status = str(updated.get("support_status") or "waiting")
            else:
                status = "bot"
            json_response(self, 200, {"conversation_id": conversation_id, "reply": reply,
                                      "support_status": status,
                                      "assigned_agent_name": ""})
        except PermissionError as exc:
            json_response(self, 401, {"error": str(exc)})
        except ValueError as exc:
            json_response(self, 400, {"error": str(exc)})
        except LookupError as exc:
            json_response(self, 404, {"error": str(exc)})
        except Exception as exc:
            json_response(self, 500, {"error": str(exc) if isinstance(exc, RuntimeError) else "Unable to process the support request. Please try again."})

    def log_message(self, format, *args):
        # Do not log message text, tokens or customer identifiers.
        return
