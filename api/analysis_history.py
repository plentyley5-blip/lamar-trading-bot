import json
import os
from http.server import BaseHTTPRequestHandler

def response(handler, status, data):
    body = json.dumps(data).encode("utf-8")

    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
    handler.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        response(self, 204, {})

    def do_GET(self):
        try:
            result = {
                "status": "success",
                "step": "basic_environment_test",
                "supabase_url_exists": bool(
                    os.environ.get("SUPABASE_URL", "").strip()
                ),
                "service_key_exists": bool(
                    os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
                ),
                "anon_key_exists": bool(
                    os.environ.get("SUPABASE_ANON_KEY", "").strip()
                ),
                "job_secret_exists": bool(
                    os.environ.get("JOB_TOKEN_SECRET", "").strip()
                ),
            }

            response(self, 200, result)

        except Exception as exc:
            response(self, 500, {
                "status": "error",
                "error_type": type(exc).__name__,
                "error": str(exc),
            })
