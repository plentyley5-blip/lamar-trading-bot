                time.sleep(0.5)
                continue
            if exc.code in {400, 401, 403}:
                break
        except (urllib.error.URLError, TimeoutError) as exc:
            failures.append("{} connection error".format(model))
            if index < len(GEMINI_MODELS) - 1:
                continue
        else:
            try:
                data = json.loads(raw)
                pieces = []
                for candidate in data.get("candidates", []):
                    content = candidate.get("content", {}) if isinstance(candidate, dict) else {}
                    for part in content.get("parts", []) if isinstance(content, dict) else []:
                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                            pieces.append(part["text"])
                answer = "\n".join(pieces).strip()
                if answer:
                    return answer[:6000]
                failures.append("{} returned an empty reply".format(model))
            except (ValueError, TypeError):
                failures.append("{} returned invalid JSON".format(model))
    raise RuntimeError("The AI support assistant is temporarily unavailable. Please try again or contact LAMAR on WhatsApp or Telegram.")


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        json_response(self, 204, {})

    def do_GET(self):
        try:
            user_id = authenticated_user(self)
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            conversation_id = str((params.get("conversation_id") or [""])[0]).strip()
            if conversation_id:
                owned_conversation(conversation_id, user_id)
                messages = get_messages(conversation_id, user_id)
                json_response(self, 200, {"conversation_id": conversation_id, "messages": messages})
                return
            conversations = supabase_request(
                "GET", "support_conversations",
                {"user_id": "eq." + user_id, "select": "id,title,created_at,updated_at",
                 "order": "updated_at.desc", "limit": "50"},
            )
            json_response(self, 200, {"conversations": conversations if isinstance(conversations, list) else []})
        except PermissionError as exc:
            json_response(self, 401, {"error": str(exc)})
        except LookupError:
            json_response(self, 404, {"error": "Conversation not found."})
        except Exception as exc:
            json_response(self, 500, {"error": str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "Unable to load support chat."})

    def do_POST(self):
        try:
            user_id = authenticated_user(self)
            data = read_json(self)
            action = str(data.get("action", "send")).strip().lower()
            if action != "send":
                raise ValueError("Unsupported support chat action.")
            message = str(data.get("message", "")).strip()
            if not message:
                raise ValueError("Type a message before sending.")
            if len(message) > MAX_MESSAGE_CHARS:
                raise ValueError("Messages must be 2,000 characters or fewer.")

            conversation_id = str(data.get("conversation_id", "")).strip()
            if conversation_id:
                owned_conversation(conversation_id, user_id)
            else:
                title = " ".join(message.split())[:72]
                rows = supabase_request(
                    "POST", "support_conversations", payload={"user_id": user_id, "title": title or "Support conversation"},
                    prefer="return=representation",
                )
                if not isinstance(rows, list) or not rows:
                    raise RuntimeError("Unable to create support conversation.")
                conversation_id = str(rows[0].get("id", ""))
                if not conversation_id:
                    raise RuntimeError("Support conversation was created without an ID.")

            save_message(conversation_id, user_id, "user", message)
            # Update timestamp so the most recently used conversation appears first.
            supabase_request(
                "PATCH", "support_conversations",
                {"id": "eq." + conversation_id, "user_id": "eq." + user_id},
                {"updated_at": datetime.now(timezone.utc).isoformat()},
            )
            context = get_messages(conversation_id, user_id, limit=200)
            try:
                reply = call_gemini(context)
            except Exception:
                # Keep the conversation usable even during Gemini outages; the
                # saved transcript records the fallback so users are not forced
                # to resend a message that was already stored.
                reply = ("I'm temporarily unable to generate a full answer. Please try again shortly. "
                         "If this is urgent or relates to your account or subscription, use the WhatsApp or Telegram buttons "
                         "above to contact LAMAR directly.")
            save_message(conversation_id, user_id, "assistant", reply)
            supabase_request(
                "PATCH", "support_conversations",
                {"id": "eq." + conversation_id, "user_id": "eq." + user_id},
                {"updated_at": datetime.now(timezone.utc).isoformat()},
            )
            json_response(self, 200, {"conversation_id": conversation_id, "reply": reply})
        except PermissionError as exc:
            json_response(self, 401, {"error": str(exc)})
        except ValueError as exc:
            json_response(self, 400, {"error": str(exc)})
        except Exception as exc:
            json_response(self, 500, {"error": str(exc) if isinstance(exc, RuntimeError) else "Unable to send support message. Please try again."})
