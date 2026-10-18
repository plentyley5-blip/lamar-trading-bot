"""Authenticated endpoint for deleting the currently signed-in Supabase user."""

import json
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

from api.user_security import (
    extract_bearer_token,
    get_supabase_service_key,
    get_supabase_url,
    verify_access_token,
)


def json_response(handler, status_code, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
    handler.send_header("Access-Control-Allow-Methods", "DELETE, OPTIONS")
    handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except Exception:
        pass


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "DELETE, OPTIONS")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self):
        json_response(self, 405, {"error": "Method not allowed."})

    def do_DELETE(self):
        try:
            access_token = extract_bearer_token(self)
            user = verify_access_token(access_token)
            user_id = str(user.get("id", "")).strip() if isinstance(user, dict) else ""
            if not user_id:
                json_response(self, 401, {"error": "Your session could not be verified."})
                return

            # Only delete the user proven by the supplied bearer token. The
            # client cannot supply a different user ID to delete.
            base_url = get_supabase_url().rstrip("/")
            service_key = get_supabase_service_key().strip()
            endpoint = base_url + "/auth/v1/admin/users/" + urllib.parse.quote(user_id, safe="")
            request = urllib.request.Request(
                endpoint,
                headers={
                    "apikey": service_key,
                    "Authorization": "Bearer " + service_key,
                    "Accept": "application/json",
                },
                method="DELETE",
            )
            with urllib.request.urlopen(request, timeout=25):
                pass

            json_response(self, 200, {"status": "deleted", "message": "Account deleted."})

        except ValueError as exc:
            json_response(self, 401, {"error": str(exc)})
        except urllib.error.HTTPError as exc:
            # Avoid returning raw upstream details or credentials to the app.
            if exc.code in {401, 403}:
                json_response(self, 503, {"error": "Account deletion is not configured correctly on the server."})
            elif exc.code == 404:
                json_response(self, 502, {"error": "The account could not be found by the authentication service."})
            else:
                json_response(self, 502, {"error": "The authentication service could not delete the account."})
        except (urllib.error.URLError, TimeoutError):
            json_response(self, 503, {"error": "The authentication service is temporarily unavailable."})
        except RuntimeError as exc:
            json_response(self, 503, {"error": str(exc)})
        except Exception:
            json_response(self, 500, {"error": "Account deletion failed."})

    def log_message(self, format, *args):
        return
