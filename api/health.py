from http.server import BaseHTTPRequestHandler

from analysis_common import json_response


class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        json_response(
            self,
            {
                "status": "ok",
                "service": "LM ANALYZER",
                "version": "2.0",
                "analysis_engine": "background",
            },
            200,
        )

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header(
            "Access-Control-Allow-Origin",
            "*",
        )
        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, OPTIONS",
        )
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type",
        )
        self.end_headers()
