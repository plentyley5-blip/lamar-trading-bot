import json
from http.server import BaseHTTPRequestHandler

try:
    from user_security import extract_bearer_token, verify_access_token
    IMPORT_OK = True
    IMPORT_ERROR = ""
except Exception as exc:
    IMPORT_OK = False
    IMPORT_ERROR = type(exc).__name__ + ": " + str(exc)


def response(handler, status, data):
    body = json.dumps(data).encode("utf-8")

    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header(
        "Access-Control-Allow-Headers",
        "Content-Type, Authorization"
    )
    handler.send_header(
        "Access-Control-Allow-Methods",
        "GET, OPTIONS"
    )
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        response(self, 204, {})

    def do_GET(self):
        try:
            if not IMPORT_OK:
                response(self, 500, {
                    "status": "error",
                    "step": "user_security_import",
                    "error": IMPORT_ERROR
                })
                return

            authorization = self.headers.get("Authorization", "")

            if not authorization:
                response(self, 200, {
                    "status": "success",
                    "step": "user_security_import",
                    "message": "user_security imported successfully",
                    "authorization_header": False
                })
                return

            token = extract_bearer_token(authorization)

            response(self, 200, {
                "status": "success",
                "step": "token_extraction",
                "message": "user_security imported and token extraction works",
                "authorization_header": True,
                "token_found": bool(token)
            })

        except Exception as exc:
            response(self, 500, {
                "status": "error",
                "step": "authentication_test",
                "error_type": type(exc).__name__,
                "error": str(exc)
            })

