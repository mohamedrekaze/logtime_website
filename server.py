#!/usr/bin/env python3
"""
Logtime website server.

Serves the static dashboard (index.html) and proxies POST /api/get_log to the
42 logtime API, so the browser does not hit CORS restrictions.

Usage:
    python3 server.py                  # http://localhost:8000
    python3 server.py --port 9000
    python3 server.py --no-verify-ssl  # skip TLS verification upstream
"""

import argparse
import json
import logging
import os
import ssl
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_API_URL = "https://logtime-med.1337.ma/api/get_log"
UPSTREAM_HEADERS = {"Content-Type": "application/json", "User-Agent": "LogTimeTracker-Web/1.0"}


class Handler(SimpleHTTPRequestHandler):
    api_url = DEFAULT_API_URL
    timeout = 15
    verify_ssl = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_POST(self):
        if self.path != "/api/get_log":
            self.send_error(404, "Not found")
            return

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)

        request = urllib.request.Request(
            self.api_url, data=body, method="POST", headers=UPSTREAM_HEADERS
        )
        context = None if self.verify_ssl else ssl._create_unverified_context()

        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=context) as resp:
                self._relay(resp.status, resp.read(), resp.headers.get("Content-Type", "application/json"))
        except urllib.error.HTTPError as exc:
            self._relay(exc.code, exc.read(), exc.headers.get("Content-Type", "application/json"))
        except Exception as exc:
            payload = json.dumps({"error": f"upstream unreachable: {exc}"}).encode()
            self._relay(502, payload, "application/json")

    def _relay(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        logging.info("%s %s", self.address_string(), fmt % args)


def main():
    parser = argparse.ArgumentParser(description="Logtime dashboard server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--api-url", default=os.getenv("LOGTIME_API_URL", DEFAULT_API_URL))
    parser.add_argument("--timeout", type=int, default=15, help="Upstream request timeout in seconds")
    parser.add_argument("--no-verify-ssl", action="store_true", help="Disable TLS verification for upstream requests")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    Handler.api_url = args.api_url
    Handler.timeout = args.timeout
    Handler.verify_ssl = not args.no_verify_ssl

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    logging.info("Serving %s on http://%s:%d", ROOT, args.host, args.port)
    logging.info("Proxying /api/get_log -> %s", args.api_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
