
import json
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


MAX_BODY_BYTES = 16 * 1024
MAX_POST_CHARS = 5000
FEED_LIMIT = 50


def _send(handler, status, payload):
    body = json.dumps(
        payload, separators=(",", ":")
    ).encode("utf-8")

    handler.send_response(status)
    handler.send_header(
        "Content-Type", "application/json; charset=utf-8"
    )
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header(
        "Access-Control-Allow-Headers",
        "Authorization, Content-Type",
    )
    handler.send_header(
        "Access-Control-Allow-Methods",
        "GET, POST, DELETE, OPTIONS",
    )
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()

    if handler.command != "HEAD" and status != 204:
        handler.wfile.write(body)


def _read_body(handler):
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except (TypeError, ValueError):
        raise ValueError("Invalid Content-Length.")

    if length < 0 or length > MAX_BODY_BYTES:
        raise ValueError("Request body is too large.")

    raw = handler.rfile.read(length) if length else b"{}"

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("Request body must be valid JSON.")

    if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object.")

    return payload


def _supabase(method, path, payload=None, prefer=None):
    base_url = get_supabase_url()
    service_key = get_supabase_service_key()

    headers = {
        "apikey": service_key,
        "Authorization": "Bearer " + service_key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    if prefer:
        headers["Prefer"] = prefer

    body = None
    if payload is not None:
        body = json.dumps(
            payload, separators=(",", ":")
        ).encode("utf-8")

    request = urllib.request.Request(
        base_url + path,
        data=body,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(
            request, timeout=20
        ) as response:
            raw = response.read().decode("utf-8")

            if not raw:
                return response.status, None

            try:
                return response.status, json.loads(raw)
            except json.JSONDecodeError:
                return response.status, raw

    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(
            "utf-8", errors="replace"
        )

        try:
            detail = json.loads(raw)
        except json.JSONDecodeError:
            detail = {"error": raw}

        return exc.code, detail

    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(
            "Supabase is temporarily unavailable."
        ) from exc


def _authenticate(handler):
    token = extract_bearer_token(handler)
    user = verify_access_token(token)

    if not isinstance(user, dict) or not user.get("id"):
        raise ValueError("Invalid user session.")

    return user


def _has_active_subscription(user_id):
    # Match the existing subscription_status.py schema.
    # A subscription must have started and must not have expired.
    now = (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )

    params = {
        "user_id": "eq." + str(user_id),
        "status": "eq.active",
        "starts_at": "lte." + now,
        "expires_at": "gt." + now,
        "select": "user_id,status,starts_at,expires_at",
        "order": "expires_at.desc",
        "limit": "1",
    }

    path = (
        "/rest/v1/subscriptions?"
        + urllib.parse.urlencode(params)
    )

    status, rows = _supabase("GET", path)

    if status != 200 or not isinstance(rows, list):
        raise RuntimeError(
            "Subscription status could not be verified."
        )

    return bool(rows)


def _is_admin(user):
    try:
        return is_owner_user(user)
    except Exception:
        return False


def _post_fields(row):
    if not isinstance(row, dict):
        return row

    return {
        "id": row.get("id"),
        "user_id": row.get("user_id"),
        "body": row.get("body", ""),
        "image_url": row.get("image_url"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


class handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        # Avoid logging private post contents or access tokens.
        return

    def do_OPTIONS(self):
        _send(self, 204, {})

    def do_GET(self):
        try:
            user = _authenticate(self)

            params = {
                "status": "eq.published",
                "select": (
                    "id,user_id,body,image_url,"
                    "created_at,updated_at"
                ),
                "order": "created_at.desc",
                "limit": str(FEED_LIMIT),
            }

            path = (
                "/rest/v1/community_posts?"
                + urllib.parse.urlencode(params)
            )

            status, rows = _supabase("GET", path)

            if status != 200 or not isinstance(rows, list):
                _send(
                    self, 502,
                    {"error": "Community feed could not be loaded."},
                )
                return

            is_admin = _is_admin(user)
            can_post = (
                is_admin
                or _has_active_subscription(user["id"])
            )

            _send(self, 200, {
                "posts": [_post_fields(row) for row in rows],
                "user_id": str(user["id"]),
                "can_post": can_post,
            })

        except ValueError as exc:
            _send(self, 401, {"error": str(exc)})

        except RuntimeError as exc:
            _send(self, 503, {"error": str(exc)})

        except Exception:
            _send(
                self, 500,
                {"error": "Community request failed."},
            )

    def do_POST(self):
        try:
            user = _authenticate(self)

            # Server-side permission check.
            if (
                not _is_admin(user)
                and not _has_active_subscription(user["id"])
            ):
                _send(self, 403, {
                    "error": (
                        "An active LM Analyzer subscription "
                        "is required to publish community posts."
                    )
                })
                return

            payload = _read_body(self)
            body = payload.get("body", "")

            if not isinstance(body, str):
                _send(
                    self, 400,
                    {"error": "Post body must be text."},
                )
                return

            body = body.strip()

            if not body:
                _send(
                    self, 400,
                    {"error": "Write something before publishing."},
                )
                return

            if len(body) > MAX_POST_CHARS:
                _send(
                    self, 400,
                    {"error": "Posts cannot exceed 5000 characters."},
                )
                return

            row = {
                "user_id": str(user["id"]),
                "body": body,
                "status": "published",
            }

            status, result = _supabase(
                "POST",
                "/rest/v1/community_posts",
                row,
                prefer="return=representation",
            )

            if (
                status not in (200, 201)
                or not isinstance(result, list)
                or not result
            ):
                _send(
                    self, 502,
                    {"error": "Community post could not be published."},
                )
                return

            _send(self, 201, {
                "post": _post_fields(result[0])
            })

        except ValueError as exc:
            _send(self, 401, {"error": str(exc)})

        except RuntimeError as exc:
            _send(self, 503, {"error": str(exc)})

        except Exception:
            _send(
                self, 500,
                {"error": "Community request failed."},
            )

    def do_DELETE(self):
        try:
            user = _authenticate(self)

            query = urllib.parse.parse_qs(
                urllib.parse.urlsplit(self.path).query,
                keep_blank_values=False,
            )

            post_id = (
                query.get("post_id") or [""]
            )[0].strip()

            if not post_id:
                _send(
                    self, 400,
                    {"error": "post_id is required."},
                )
                return

            # Find the post and verify who owns it.
            filters = {
                "id": "eq." + post_id,
                "select": "id,user_id",
            }

            if not _is_admin(user):
                filters["user_id"] = "eq." + str(user["id"])

            path = (
                "/rest/v1/community_posts?"
                + urllib.parse.urlencode(filters)
            )

            status, rows = _supabase("GET", path)

            if status != 200 or not isinstance(rows, list):
                _send(
                    self, 502,
                    {"error": "Post ownership could not be verified."},
                )
                return

            if not rows:
                _send(self, 404, {
                    "error": (
                        "Post not found or you do not have "
                        "permission to delete it."
                    )
                })
                return

            # Restrict deletion to the verified author ID.
            delete_path = (
                "/rest/v1/community_posts?"
                + urllib.parse.urlencode({
                    "id": "eq." + post_id,
                    "user_id": "eq." + str(
                        rows[0].get("user_id")
                    ),
                })
            )

            delete_status, _ = _supabase(
                "DELETE", delete_path
            )

            if delete_status not in (200, 204):
                _send(
                    self, 502,
                    {"error": "Post could not be deleted."},
                )
                return

            _send(self, 200, {
                "deleted": True,
                "post_id": post_id,
            })

        except ValueError as exc:
            _send(self, 401, {"error": str(exc)})

        except RuntimeError as exc:
            _send(self, 503, {"error": str(exc)})

        except Exception:
            _send(
                self, 500,
                {"error": "Community request failed."},
            )
