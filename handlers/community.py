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

try:
    from api.lm_push import notify_user_ids
except Exception:
    def notify_user_ids(*args, **kwargs):
        return {"sent": 0, "failed": 0, "unavailable": True}

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


def _follow_rows_for_ids(ids):
    """Fetch follow edges with simple PostgREST filters, avoiding fragile OR expressions."""
    clean_ids = list(dict.fromkeys(str(value).strip() for value in ids if str(value).strip()))
    if not clean_ids:
        return []
    in_values = ",".join(clean_ids)
    found = {}
    for column in ("follower_id", "following_id"):
        params = urllib.parse.urlencode({column: "in.({})".format(in_values), "select": "follower_id,following_id", "limit": "5000"})
        rows = _supabase("GET", "/rest/v1/community_follows?" + params)
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    found[(str(row.get("follower_id", "")), str(row.get("following_id", "")))] = row
    return list(found.values())


def _follow_rows_for_user(user_id):
    """Fetch incoming and outgoing follows separately; avoid PostgREST OR filters."""
    found = {}
    for column in ("follower_id", "following_id"):
        params = urllib.parse.urlencode({column: "eq." + str(user_id), "select": "follower_id,following_id", "limit": "5000"})
        rows = _supabase("GET", "/rest/v1/community_follows?" + params)
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    found[(str(row.get("follower_id", "")), str(row.get("following_id", "")))] = row
    return list(found.values())


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



def _verified_rows(user_ids):
    if not user_ids:
        return {}
    query = urllib.parse.urlencode({"user_id": "in.({})".format(",".join(user_ids)), "select": "user_id,reason,verified_at"})
    rows = _supabase("GET", "/rest/v1/community_verified_users?" + query)
    return {str(r.get("user_id")): r for r in rows if isinstance(r, dict)} if isinstance(rows, list) else {}


def _award_verified(user_id, reason):
    _supabase("POST", "/rest/v1/community_verified_users", {"user_id": user_id, "reason": reason}, prefer="resolution=ignore-duplicates,return=minimal")

def _attach_member_profiles(member_ids, viewer_id=""):
    """Return real public profile data and server-derived follow/badge state."""
    ids = list(dict.fromkeys(str(value).strip() for value in member_ids if value and str(value).strip()))
    if not ids:
        return []
    in_values = ",".join(ids)
    profile_query = urllib.parse.urlencode({
        "id": "in.({})".format(in_values),
        "select": "id,full_name",
    })
    profile_rows = _supabase("GET", "/rest/v1/profiles?" + profile_query)
    profile_rows = profile_rows if isinstance(profile_rows, list) else []
    profiles = {
        str(row.get("id")): str(row.get("full_name") or "LM Analyzer member").strip()
        for row in profile_rows if isinstance(row, dict)
    }

    relation_rows = _follow_rows_for_ids(ids)
    follower_counts, following_counts = {}, {}
    for relation in relation_rows:
        follower = str(relation.get("follower_id", ""))
        following = str(relation.get("following_id", ""))
        if following:
            follower_counts[following] = follower_counts.get(following, 0) + 1
        if follower:
            following_counts[follower] = following_counts.get(follower, 0) + 1

    verified = _verified_rows(ids)
    for member_id in ids:
        if member_id not in verified and follower_counts.get(member_id, 0) >= 500 and _has_active_subscription(member_id):
            _award_verified(member_id, "500_followers")
    verified = _verified_rows(ids)
    viewer_follows = {
        str(relation.get("following_id", "")) for relation in relation_rows
        if str(relation.get("follower_id", "")) == str(viewer_id)
    }
    result = []
    for member_id in ids:
        if member_id not in profiles:
            continue
        result.append({
            "user_id": member_id,
            "full_name": profiles.get(member_id) or "LM Analyzer member",
            "is_verified": member_id in verified,
            "verification_reason": str(verified.get(member_id, {}).get("reason", "")),
            "follower_count": follower_counts.get(member_id, 0),
            "following_count": following_counts.get(member_id, 0),
            "is_following": member_id in viewer_follows and member_id != str(viewer_id),
            "is_self": member_id == str(viewer_id),
        })
    return result


def _attach_author_profiles(posts, viewer_id=""):
    """Attach public names and real follow statistics to community posts."""
    if not isinstance(posts, list) or not posts:
        return posts if isinstance(posts, list) else []
    ids = list(dict.fromkeys(
        str(row.get("user_id", "")).strip()
        for row in posts
        if isinstance(row, dict) and row.get("user_id")
    ))
    if not ids:
        return posts
    # Supabase/PostgREST in-filter. UUID values are validated by the database;
    # only IDs taken from stored community rows are included.
    in_values = ",".join(ids)
    query = urllib.parse.urlencode({
        "id": "in.({})".format(in_values),
        "select": "id,full_name",
    })
    profiles = _supabase("GET", "/rest/v1/profiles?" + query)
    names = {
        str(row.get("id")): str(row.get("full_name", "")).strip()
        for row in profiles if isinstance(row, dict)
    } if isinstance(profiles, list) else {}
    # Read real follow rows once for the authors shown in this feed.
    follow_rows = _follow_rows_for_ids(ids)
    follower_counts = {}
    following_counts = {}
    for relation in follow_rows:
        target = str(relation.get("following_id", ""))
        follower_counts[target] = follower_counts.get(target, 0) + 1
        source = str(relation.get("follower_id", ""))
        following_counts[source] = following_counts.get(source, 0) + 1
    # Verified badge is server-derived: owner, or active subscriber with 500+ real followers.
    verified = _verified_rows(ids)
    for author_id in ids:
        if author_id not in verified and follower_counts.get(author_id, 0) >= 500 and _has_active_subscription(author_id):
            _award_verified(author_id, "500_followers")
    verified = _verified_rows(ids)
    following_now = {
        str(row.get("following_id", "")) for row in follow_rows
        if str(row.get("follower_id", "")) == str(viewer_id)
    }
    result = []
    for post in posts:
        if not isinstance(post, dict):
            result.append(post)
            continue
        item = dict(post)
        author_id = str(item.get("user_id", ""))
        item["author_name"] = names.get(author_id) or "LM Analyzer member"
        item["author_avatar_url"] = None
        item["follower_count"] = follower_counts.get(author_id, 0)
        item["following_count"] = following_counts.get(author_id, 0)
        item["is_following"] = author_id in following_now and author_id != str(viewer_id)
        badge = verified.get(author_id, {})
        item["is_verified"] = bool(badge)
        item["verification_reason"] = str(badge.get("reason", ""))
        result.append(item)
    return result


def _attach_post_engagement(posts, viewer_id):
    """Attach persistent unique-view, like and comment counts to each post."""
    if not isinstance(posts, list) or not posts:
        return posts if isinstance(posts, list) else []
    ids = list(dict.fromkeys(str(row.get("id", "")).strip() for row in posts if isinstance(row, dict) and row.get("id")))
    if not ids:
        return posts
    in_values = ",".join(ids)
    def rows_for(table, columns):
        query = urllib.parse.urlencode({"post_id": "in.({})".format(in_values), "select": columns})
        result = _supabase("GET", "/rest/v1/{}?{}".format(table, query))
        return result if isinstance(result, list) else []
    likes = rows_for("community_post_likes", "post_id,user_id")
    comments = rows_for("community_comments", "post_id,id,user_id,body,created_at")
    views = rows_for("community_post_views", "post_id,user_id")
    like_counts, comment_counts, view_counts = {}, {}, {}
    liked_by_viewer = set()
    for row in likes:
        pid = str(row.get("post_id", ""))
        like_counts[pid] = like_counts.get(pid, 0) + 1
        if str(row.get("user_id", "")) == str(viewer_id):
            liked_by_viewer.add(pid)
    for row in comments:
        pid = str(row.get("post_id", ""))
        comment_counts[pid] = comment_counts.get(pid, 0) + 1
    for row in views:
        pid = str(row.get("post_id", ""))
        view_counts[pid] = view_counts.get(pid, 0) + 1
    out = []
    for row in posts:
        item = dict(row)
        pid = str(item.get("id", ""))
        item["like_count"] = like_counts.get(pid, 0)
        item["comment_count"] = comment_counts.get(pid, 0)
        item["view_count"] = view_counts.get(pid, 0)
        item["is_liked"] = pid in liked_by_viewer
        out.append(item)
    return out


def _attach_comment_authors(comments):
    ids = list(dict.fromkeys(str(row.get("user_id", "")) for row in comments if isinstance(row, dict) and row.get("user_id")))
    if not ids:
        return comments
    query = urllib.parse.urlencode({"id": "in.({})".format(",".join(ids)), "select": "id,full_name"})
    profiles = _supabase("GET", "/rest/v1/profiles?" + query)
    names = {str(r.get("id")): str(r.get("full_name") or "").strip() for r in profiles if isinstance(r, dict)} if isinstance(profiles, list) else {}
    result = []
    for row in comments:
        item = dict(row)
        item["author_name"] = names.get(str(item.get("user_id", ""))) or "LM Analyzer member"
        result.append(item)
    return result


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
            user = _authenticate(self)
            query_args = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            action = (query_args.get("action") or [""])[0].strip().lower()
            if action == "profile":
                target_id = ((query_args.get("user_id") or [""])[0].strip() or str(user.get("id", "")).strip())
                if not target_id or len(target_id) > 64:
                    raise ValueError("A valid user_id is required.")

                # The follow table is the source of truth for these two counts.
                # Query it first; optional profile/post/badge lookups must not block counts.
                relations = _follow_rows_for_user(target_id)
                followers = sum(1 for r in relations if str(r.get("following_id")) == target_id)
                following = sum(1 for r in relations if str(r.get("follower_id")) == target_id)
                is_following = any(str(r.get("follower_id")) == str(user["id"]) and str(r.get("following_id")) == target_id for r in relations)

                profile = {}
                authored_posts = []
                badge = {}
                try:
                    profile_query = urllib.parse.urlencode({"id": "eq." + target_id, "select": "id,full_name", "limit": "1"})
                    profile_rows = _supabase("GET", "/rest/v1/profiles?" + profile_query)
                    profile = profile_rows[0] if isinstance(profile_rows, list) and profile_rows else {}
                except Exception as exc:
                    print("community optional profile lookup failed; type={}; error={!r}".format(type(exc).__name__, str(exc)))
                try:
                    posts_query = urllib.parse.urlencode({"user_id": "eq." + target_id, "status": "eq.published", "select": "id,user_id,body,created_at", "order": "created_at.desc", "limit": str(FEED_LIMIT)})
                    authored_posts = _supabase("GET", "/rest/v1/community_posts?" + posts_query)
                    authored_posts = authored_posts if isinstance(authored_posts, list) else []
                except Exception as exc:
                    print("community optional posts lookup failed; type={}; error={!r}".format(type(exc).__name__, str(exc)))
                try:
                    if target_id == str(user["id"]) and is_owner_user(user):
                        _award_verified(target_id, "owner")
                    verified = _verified_rows([target_id])
                    if target_id not in verified and followers >= 500 and _has_active_subscription(target_id):
                        _award_verified(target_id, "500_followers")
                        verified = _verified_rows([target_id])
                    badge = verified.get(target_id, {})
                except Exception as exc:
                    print("community optional badge lookup failed; type={}; error={!r}".format(type(exc).__name__, str(exc)))

                _send(self, 200, {"profile": {"id": target_id, "full_name": str(profile.get("full_name") or "LM Analyzer member"), "followers": followers, "following": following, "posts_count": len(authored_posts), "is_following": is_following, "is_self": target_id == str(user["id"]), "is_verified": bool(badge), "verification_reason": str(badge.get("reason", ""))}, "posts": authored_posts})
                return
            if action == "members":
                direction = (query_args.get("direction") or [""])[0].strip().lower()
                if direction not in ("followers", "following"):
                    raise ValueError("A valid direction (followers or following) is required.")
                target_id = ((query_args.get("user_id") or [""])[0].strip() or str(user["id"]))
                if not target_id or len(target_id) > 64:
                    raise ValueError("A valid user_id is required.")
                column = "following_id" if direction == "followers" else "follower_id"
                params = urllib.parse.urlencode({
                    column: "eq." + target_id,
                    "select": "follower_id,following_id",
                    "limit": "1000",
                })
                relations = _supabase("GET", "/rest/v1/community_follows?" + params)
                relations = relations if isinstance(relations, list) else []
                member_ids = []
                for relation in relations:
                    candidate = str(relation.get("follower_id" if direction == "followers" else "following_id", ""))
                    if candidate and candidate != target_id and candidate not in member_ids:
                        member_ids.append(candidate)
                members = _attach_member_profiles(member_ids, str(user["id"]))
                _send(self, 200, {"members": members, "direction": direction, "user_id": target_id, "count": len(members), "limit": 1000})
                return
            if action == "comments":
                post_id = (query_args.get("post_id") or [""])[0].strip()
                if not post_id or len(post_id) > 64:
                    raise ValueError("A valid post_id is required.")
                comment_query = urllib.parse.urlencode({"post_id": "eq." + post_id, "select": "id,post_id,user_id,body,created_at", "order": "created_at.asc", "limit": "200"})
                comment_rows = _supabase("GET", "/rest/v1/community_comments?" + comment_query)
                comment_rows = comment_rows if isinstance(comment_rows, list) else []
                _send(self, 200, {"comments": _attach_comment_authors(comment_rows)})
                return
            params = urllib.parse.urlencode({
                "status": "eq.published",
                "select": "id,user_id,body,image_url,created_at,updated_at",
                "order": "created_at.desc",
                "limit": str(FEED_LIMIT),
            })
            rows = _supabase("GET", "/rest/v1/community_posts?" + params)
            rows = rows if isinstance(rows, list) else []
            # A viewer is counted once per post; the database unique key prevents refreshes inflating views.
            if rows:
                view_rows = [{"post_id": str(row["id"]), "user_id": str(user["id"])} for row in rows if row.get("id")]
                if view_rows:
                    _supabase("POST", "/rest/v1/community_post_views?on_conflict=post_id%2Cuser_id", view_rows, prefer="resolution=ignore-duplicates,return=minimal")
            owner = is_owner_user(user)
            if owner:
                _award_verified(str(user["id"]), "owner")
            rows = _attach_author_profiles(rows, str(user["id"]))
            rows = _attach_post_engagement(rows, str(user["id"]))
            can_post = owner or _has_active_subscription(str(user["id"]))
            _send(self, 200, {"posts": rows, "user_id": str(user["id"]), "can_post": can_post})
        except ValueError as exc:
            _send(self, 401, {"error": str(exc)})
        except RuntimeError as exc:
            # Log only the sanitized database/API status; never log request headers or tokens.
            print("community GET failed; action={!r}; error={!r}".format(action if "action" in locals() else "", str(exc)))
            if "action" in locals() and action in ("profile", "members"):
                safe_detail = str(exc)
                if "HTTP " in safe_detail and len(safe_detail) < 120 and "\n" not in safe_detail:
                    message = "Community follow data is temporarily unavailable (" + safe_detail + ")."
                else:
                    message = "Community follow data is temporarily unavailable. Please retry shortly."
            else:
                message = "Community is temporarily unavailable."
            _send(self, 503, {"error": message})
        except Exception as exc:
            # Avoid logging tokens, request bodies, or private post content.
            print("community GET unexpected failure; action={!r}; type={}; error={!r}".format(action if "action" in locals() else "", type(exc).__name__, str(exc)))
            message = "Community profile could not be loaded. Check the Vercel function log for the sanitized error." if "action" in locals() and action in ("profile", "members") else "Could not load community posts."
            _send(self, 500, {"error": message})

    def do_POST(self):
        try:
            user = _authenticate(self)
            user_id = str(user["id"])
            body = _read_body(self)
            action = str(body.get("action", "publish")).strip().lower()
            if action in ("like", "unlike", "comment"):
                post_id = str(body.get("post_id", "")).strip()
                if not post_id or len(post_id) > 64:
                    raise ValueError("A valid post_id is required.")
                # Ensure interactions can only target a published post.
                post_query = urllib.parse.urlencode({"id": "eq." + post_id, "status": "eq.published", "select": "id", "limit": "1"})
                post_rows = _supabase("GET", "/rest/v1/community_posts?" + post_query)
                if not isinstance(post_rows, list) or not post_rows:
                    _send(self, 404, {"error": "Post not found."})
                    return
                if action == "like":
                    _supabase("POST", "/rest/v1/community_post_likes?on_conflict=post_id%2Cuser_id", {"post_id": post_id, "user_id": user_id}, prefer="resolution=ignore-duplicates,return=minimal")
                    _send(self, 200, {"success": True, "liked": True, "post_id": post_id})
                elif action == "unlike":
                    like_query = urllib.parse.urlencode({"post_id": "eq." + post_id, "user_id": "eq." + user_id})
                    _supabase("DELETE", "/rest/v1/community_post_likes?" + like_query, prefer="return=minimal")
                    _send(self, 200, {"success": True, "liked": False, "post_id": post_id})
                else:
                    comment_text = str(body.get("body", "")).strip()
                    if not comment_text:
                        raise ValueError("Write a comment before sending.")
                    if len(comment_text) > 2000:
                        raise ValueError("Comments must be 2,000 characters or fewer.")
                    result = _supabase("POST", "/rest/v1/community_comments", {"post_id": post_id, "user_id": user_id, "body": comment_text}, prefer="return=representation")
                    comment = result[0] if isinstance(result, list) and result else None
                    if not comment:
                        _send(self, 500, {"error": "Comment could not be saved."})
                        return
                    _send(self, 201, {"success": True, "comment": _attach_comment_authors([comment])[0]})
                return
            if action in ("follow", "unfollow"):
                target_id = str(body.get("target_user_id", "")).strip()
                if not target_id or len(target_id) > 64:
                    raise ValueError("A valid target_user_id is required.")
                if target_id == user_id:
                    raise ValueError("You cannot follow your own profile.")
                if action == "follow":
                    _supabase("POST", "/rest/v1/community_follows", {"follower_id": user_id, "following_id": target_id}, prefer="resolution=ignore-duplicates,return=minimal")
                    _send(self, 200, {"success": True, "following": True, "target_user_id": target_id})
                else:
                    params = urllib.parse.urlencode({"follower_id": "eq." + user_id, "following_id": "eq." + target_id})
                    _supabase("DELETE", "/rest/v1/community_follows?" + params, prefer="return=minimal")
                    _send(self, 200, {"success": True, "following": False, "target_user_id": target_id})
                return
            owner = is_owner_user(user)
            if not owner and not _has_active_subscription(user_id):
                _send(self, 403, {
                    "error": "An active LM Analyzer subscription is required to publish community posts.",
                    "code": "subscription_required",
                })
                return

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
            # The post is already saved. Push failures must never undo publishing.
            push_summary = {"sent": 0, "failed": 0}
            try:
                follower_query = urllib.parse.urlencode({
                    "following_id": "eq." + user_id,
                    "select": "follower_id",
                    "limit": "1000",
                })
                follower_rows = _supabase("GET", "/rest/v1/community_follows?" + follower_query)
                follower_ids = list(dict.fromkeys(
                    str(row.get("follower_id", "")).strip()
                    for row in (follower_rows if isinstance(follower_rows, list) else [])
                    if isinstance(row, dict) and row.get("follower_id")
                ))
                if follower_ids:
                    author_rows = _supabase("GET", "/rest/v1/profiles?" + urllib.parse.urlencode({
                        "id": "eq." + user_id, "select": "full_name", "limit": "1"
                    }))
                    author_name = (str(author_rows[0].get("full_name") or "LM Analyzer member")
                                   if isinstance(author_rows, list) and author_rows else "LM Analyzer member")
                    push_summary = notify_user_ids(
                        follower_ids,
                        title="New community post",
                        body=author_name + ": " + " ".join(text.split())[:110],
                        data={"type": "community_post", "post_id": str(post.get("id", ""))},
                    ) or push_summary
            except Exception as push_error:
                # Do not log credentials, access tokens, or post content.
                print("community push delivery failed; error_type={}".format(type(push_error).__name__))
            enriched = _attach_author_profiles([post], user_id)
            _send(self, 201, {
                "post": enriched[0] if enriched else post,
                "notifications": {
                    "sent": int(push_summary.get("sent", 0)),
                    "failed": int(push_summary.get("failed", 0)),
                },
            })
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
