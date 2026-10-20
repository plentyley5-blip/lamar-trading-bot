"""LM Analyzer native community feed and publishing API.

Deploy as api/community.py in the existing Vercel Python API project.
GET  /api/community  -> latest published posts (signed-in users)
POST /api/community -> publish a text post (active subscribers or owner)
DELETE /api/community -> remove a post (author or owner)
"""
import json
import os
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

MAX_BODY_BYTES = 16_384
MAX_POST_LENGTH = 5_000
FEED_LIMIT = 50


def _send(handler, status, payload):
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    if status != 204:
        handler.wfile.write(raw)


def _read_body(handler):
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid Content-Length.") from exc
    if length <= 0 or length > MAX_BODY_BYTES:
        raise ValueError("Request body is empty or too large.")
    try:
        body = json.loads(handler.rfile.read(length).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid JSON request.") from exc
    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object.")
    return body


def _supabase(method, path, payload=None, prefer=None):
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
        payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    request = urllib.request.Request(
        base + path, data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read().decode("utf-8", errors="replace")
            if not raw:
                return None
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        # Keep backend/service-role details out of public responses and logs.
        raise RuntimeError("Community database request failed (HTTP {}).".format(exc.code)) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Community database is temporarily unavailable.") from exc


def _authenticate(handler):
    token = extract_bearer_token(handler)
    user = verify_access_token(token)
    if not isinstance(user, dict) or not user.get("id"):
        raise ValueError("A valid signed-in session is required.")
    return user


def _has_active_subscription(user_id):
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    params = urllib.parse.urlencode({
        "user_id": "eq." + str(user_id),
        "status": "eq.active",
        "starts_at": "lte." + now,
        "expires_at": "gt." + now,
        "select": "user_id",
        "order": "expires_at.desc",
        "limit": "1",
    })
    rows = _supabase("GET", "/rest/v1/subscriptions?" + params)
    return isinstance(rows, list) and bool(rows)


def _query_post_id(handler):
    values = urllib.parse.parse_qs(
        urllib.parse.urlparse(handler.path).query, keep_blank_values=False
    )
    post_id = (values.get("post_id") or [""])[0].strip()
    if not post_id or len(post_id) > 64:
        raise ValueError("A valid post_id query parameter is required.")
    return post_id


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        _send(self, 204, {})

    def do_GET(self):
        try:
            _authenticate(self)
            params = urllib.parse.urlencode({
                "status": "eq.published",
                "select": "id,user_id,body,image_url,created_at,updated_at",
                "order": "created_at.desc",
                "limit": str(FEED_LIMIT),
            })
            rows = _supabase("GET", "/rest/v1/community_posts?" + params)
            _send(self, 200, {"posts": rows if isinstance(rows, list) else []})
        except ValueError as exc:
            _send(self, 401, {"error": str(exc)})
        except RuntimeError:
            _send(self, 503, {"error": "Community is temporarily unavailable."})
        except Exception:
            _send(self, 500, {"error": "Could not load community posts."})

    def do_POST(self):
        try:
            user = _authenticate(self)
            user_id = str(user["id"])
            owner = is_owner_user(user)
            if not owner and not _has_active_subscription(user_id):
                _send(self, 403, {
                    "error": "An active LM Analyzer subscription is required to publish community posts.",
                    "code": "subscription_required",
                })
                return

            body = _read_body(self)
            text = str(body.get("body", "")).strip()
            if not text:
                raise ValueError("Write a message before publishing.")
            if len(text) > MAX_POST_LENGTH:
                raise ValueError("Posts must be 5,000 characters or fewer.")

            # Images are deliberately not accepted in this first backend step;
            # add authenticated Storage uploads separately before enabling them.
            result = _supabase(
                "POST",
                "/rest/v1/community_posts",
                {
                    "user_id": user_id,
                    "body": text,
                    "status": "published",
                },
                prefer="return=representation",
            )
            post = result[0] if isinstance(result, list) and result else None
            if not post:
                _send(self, 500, {"error": "The post could not be saved."})
                return
            _send(self, 201, {"post": post})
        except ValueError as exc:
            message = str(exc)
            status = 401 if ("session" in message.lower() or "bearer" in message.lower() or "token" in message.lower()) else 400
            _send(self, status, {"error": message})
        except RuntimeError:
            _send(self, 503, {"error": "Community publishing is temporarily unavailable."})
        except Exception:
            _send(self, 500, {"error": "The post could not be published."})

    def do_DELETE(self):
        try:
            user = _authenticate(self)
            post_id = _query_post_id(self)
            owner = is_owner_user(user)
            params = {
                "id": "eq." + post_id,
                "select": "id,user_id",
            }
            if not owner:
                params["user_id"] = "eq." + str(user["id"])
            query = urllib.parse.urlencode(params)
            result = _supabase(
                "DELETE",
                "/rest/v1/community_posts?" + query,
                prefer="return=representation",
            )
            if not isinstance(result, list) or not result:
                _send(self, 404, {"error": "Post not found or you do not have permission to delete it."})
                return
            _send(self, 200, {"deleted": True, "post_id": post_id})
        except ValueError as exc:
            _send(self, 401, {"error": str(exc)})
        except RuntimeError:
            _send(self, 503, {"error": "Community moderation is temporarily unavailable."})
        except Exception:
            _send(self, 500, {"error": "Could not delete the post."})

    def log_message(self, fmt, *args):
        # Avoid logging bearer tokens, request bodies, or private post content.
        return
