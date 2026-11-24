# SPDX-License-Identifier: Apache-2.0
"""Loopback-only JSON-RPC shim for a public Bitcoin RPC endpoint.

Forwards each POST body unchanged with its own User-Agent and no client Authorization.
When the response id equals the request id and the top-level "jsonrpc" is empty or
absent, sets it to "2.0"; every other response value is preserved and verified.
Each rewrite is logged. Transport failures, 429/5xx and non-JSON upstream answers are
retried a few times (all methods are read-only for this client); each retry is logged.
"""
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BYTES = 16 * 1024 * 1024
EMPTY_VERSION = re.compile(rb'"jsonrpc"\s*:\s*""')
OBJECT_START = re.compile(rb'^\s*\{')
RETRIES = 4
TRANSIENT = {429, 500, 502, 503, 504}


def log(*parts):
    print(time.strftime("%Y-%m-%dT%H:%M:%S"), *parts, file=sys.stderr, flush=True)


def parse(raw):
    return json.loads(raw, parse_float=Decimal)


def normalize(raw, data):
    if data.get("jsonrpc") == "":
        return EMPTY_VERSION.sub(b'"jsonrpc":"2.0"', raw, count=1), "EMPTY"
    if "jsonrpc" not in data:
        start = OBJECT_START.match(raw)
        if not start:
            return None, "MISSING"
        end = start.end()
        separator = b"," if data else b""
        return raw[:end] + b'"jsonrpc":"2.0"' + separator + raw[end:], "MISSING"
    return raw, None


class Handler(BaseHTTPRequestHandler):
    upstream = None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def fetch(self, body, method):
        for attempt in range(1, RETRIES + 1):
            upstream = urllib.request.Request(self.upstream, body, {
                "Content-Type": "application/json",
                "User-Agent": "satoshifi-btc-rpc-shim/2",
            })
            try:
                with self.opener.open(upstream, timeout=30) as response:
                    status, raw = response.status, response.read(MAX_BYTES + 1)
            except urllib.error.HTTPError as error:
                status, raw = error.code, error.read(MAX_BYTES + 1)
            except Exception as error:
                status, raw = None, type(error).__name__
            retry = status is None or status in TRANSIENT
            if not retry:
                try:
                    parse(raw) if len(raw) <= MAX_BYTES else None
                except ValueError:
                    retry, raw = True, b""
                    log("UPSTREAM_NOT_JSON", method, status)
            if not retry or attempt == RETRIES:
                return status, raw
            log("UPSTREAM_RETRY", method, attempt, status if status else raw)
            time.sleep(attempt)
        return None, b""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= MAX_BYTES:
            return self.reply(400, b'{"error":"bad length"}')
        body = self.rfile.read(length)
        try:
            request = parse(body)
        except ValueError:
            return self.reply(400, b'{"error":"bad json"}')
        if not isinstance(request, dict):
            return self.reply(400, b'{"error":"batch not supported"}')
        status, raw = self.fetch(body, request.get("method"))
        if status is None or not isinstance(raw, bytes):
            log("UPSTREAM_FAILED", request.get("method"), raw)
            return self.reply(502, b'{"error":"upstream failed"}')
        if len(raw) > MAX_BYTES:
            return self.reply(502, b'{"error":"upstream too large"}')
        try:
            data = parse(raw)
        except ValueError:
            log("UPSTREAM_NOT_JSON", request.get("method"), status)
            return self.reply(502, b'{"error":"upstream not json"}')
        if isinstance(data, dict) and data.get("id") == request.get("id"):
            fixed, kind = normalize(raw, data)
            if kind:
                if fixed is None or parse(fixed) != dict(data, jsonrpc="2.0"):
                    log("REWRITE_REFUSED", request.get("method"), kind)
                    return self.reply(502, b'{"error":"unsafe rewrite"}')
                log("REWRITE_JSONRPC", kind, request.get("method"))
                raw = fixed
        self.reply(status, raw)

    def reply(self, status, raw):
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            log("CLIENT_GONE")

    def log_message(self, *args):
        pass


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--upstream", required=True)
    p.add_argument("--port", type=int, required=True)
    a = p.parse_args()
    if not a.upstream.startswith("https://"):
        raise SystemExit("upstream must be https")
    Handler.upstream = a.upstream
    server = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    log("LISTENING", "127.0.0.1:%d" % a.port, "->", a.upstream)
    server.serve_forever()


if __name__ == "__main__":
    main()
