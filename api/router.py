"""Shared router for LM Analyzer endpoints on Vercel Hobby."""
import json
import sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qsl, urlencode, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from handlers import (
    analysis_history, analysis_submit, analysis_status, analysis_worker,
    analyze, auth, community, delete_account, health, market_quotes,
    news, payment, pesapal, register_device, send_notification,
    subscription_access, subscription_status, support_chat,
    trader_stats, trader_tools,
)

HANDLERS = {
    name: module for name, module in {
        "analysis_history": analysis_history,
        "analysis_submit": analysis_submit,
        "analysis_status": analysis_status,
        "analysis_worker": analysis_worker,
        "analyze": analyze,
        "auth": auth,
        "community": community,
        "delete_account": delete_account,
        "health": health,
        "market_quotes": market_quotes,
        "news": news,
        "payment": payment,
        "pesapal": pesapal,
        "register_device": register_device,
        "send_notification": send_notification,
        "subscription_access": subscription_access,
        "subscription_status": subscription_status,
        "support_chat": support_chat,
        "trader_stats": trader_stats,
        "trader_tools": trader_tools,
    }.items()
}


def resolve_route_and_path(request_path):
    parsed = urlsplit(request_path)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    route = ""
    preserved = []
    for key, value in pairs:
        if key == "route" and not route:
            route = value
        else:
            preserved.append((key, value))
    if not route and parsed.path.startswith("/api/router/"):
        route = parsed.path.rsplit("/", 1)[-1]
    original_path = "/api/" + route if route else "/api/router"
    if preserved:
        original_path += "?" + urlencode(preserved, doseq=True)
    return route, original_path


class handler(BaseHTTPRequestHandler):
    def _json_response(self, status, payload):
        body = b"" if status == 204 else json.dumps(
            payload, separators=(",", ":")
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Allow-Headers", "Content-Type, Authorization"
        )
        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, POST, PUT, PATCH, DELETE, OPTIONS, HEAD",
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _dispatch(self):
        route, original_path = resolve_route_and_path(self.path)
        module = HANDLERS.get(route)
        if module is None:
            self._json_response(404, {"error": "API route not found."})
            return
        endpoint = getattr(module, "handler", None)
        method = getattr(endpoint, "do_" + self.command, None)
        if not callable(method):
            if self.command == "OPTIONS":
                self._json_response(204, {})
            else:
                self._json_response(405, {"error": "Method not allowed."})
            return
        self.path = original_path
        method(self)

    def do_GET(self): self._dispatch()
    def do_POST(self): self._dispatch()
    def do_PUT(self): self._dispatch()
    def do_PATCH(self): self._dispatch()
    def do_DELETE(self): self._dispatch()
    def do_OPTIONS(self): self._dispatch()
    def do_HEAD(self): self._dispatch()

    def log_message(self, *_args):
        return
