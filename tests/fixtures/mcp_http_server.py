#!/usr/bin/env python3
"""Tiny stdlib HTTP MCP fixture for v0.46 tests."""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import time


TOOLS = [
    {
        "name": "echo",
        "description": "Return text",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
    },
]
RESOURCES = [
    {"uri": "memo://one", "name": "one", "description": "One text resource", "mimeType": "text/plain"},
    {"uri": "memo://two", "name": "two", "description": "Second text resource", "mimeType": "text/plain"},
]
PROMPTS = [{
    "name": "greet",
    "description": "A small prompt",
    "arguments": [{"name": "who", "description": "Name", "required": True}],
}]
SESSION = "fixture-session-1"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_DELETE(self):  # noqa: N802
        self._record("DELETE")
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):  # noqa: N802
        body = self._read_body()
        if body is None:
            return
        if MODE == "redirect":
            self.send_response(307)
            self.send_header("Location", "/other")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if MODE == "unauthorized":
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if MODE == "sse":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", "7")
            self.end_headers()
            self.wfile.write(b"data: x")
            return
        if MODE == "disconnect":
            self.connection.close()
            return
        if MODE == "timeout":
            time.sleep(2)
            return
        if MODE == "not-found":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            request = json.loads(body.decode("utf-8"))
        except Exception:
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        method = request.get("method") if isinstance(request, dict) else None
        self._record(method)
        if method == "initialize":
            if MODE == "bad-utf8":
                self._raw(b"\xff")
                return
            if MODE == "huge-response":
                self._raw(b"{" + b"x" * (1024 * 1024 + 1) + b"}")
                return
            capabilities = {"tools": {}, "resources": {}, "prompts": {}}
            if MODE == "resource-only":
                capabilities = {"resources": {}}
            if MODE == "prompt-only":
                capabilities = {"prompts": {}}
            self._json({
                "jsonrpc": "2.0", "id": request.get("id"),
                "result": {
                    "protocolVersion": "2025-11-25", "capabilities": capabilities,
                    "serverInfo": {"name": "http-fixture", "version": "1"},
                },
            }, session=True)
            return
        if method == "notifications/initialized":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if method == "tools/list":
            self._json({"jsonrpc": "2.0", "id": request.get("id"), "result": {"tools": TOOLS}})
            return
        if method == "tools/call":
            params = request.get("params", {})
            text = params.get("arguments", {}).get("text", "") if isinstance(params, dict) else ""
            if MODE == "bad-id":
                self._json({"jsonrpc": "2.0", "id": 999, "result": {"isError": False, "content": []}})
            elif MODE == "huge":
                self._json({"jsonrpc": "2.0", "id": request.get("id"), "result": {"x": "x" * (1024 * 1024 + 1)}})
            else:
                self._json({"jsonrpc": "2.0", "id": request.get("id"), "result": {"isError": False, "content": [{"type": "text", "text": text}]}})
            return
        if method == "resources/list":
            cursor = request.get("params", {}).get("cursor") if isinstance(request.get("params"), dict) else None
            if MODE == "repeat-cursor":
                result = {"resources": RESOURCES[:1], "nextCursor": "next"}
            else:
                result = {"resources": RESOURCES[1:]} if cursor else {"resources": RESOURCES[:1], "nextCursor": "next"}
            self._json({"jsonrpc": "2.0", "id": request.get("id"), "result": result})
            return
        if method == "resources/read":
            uri = request.get("params", {}).get("uri")
            if MODE == "bad-resource-uri":
                uri = "memo://other"
            if MODE == "blob":
                content = {"uri": uri, "blob": "AA==", "mimeType": "image/png"}
            else:
                content = {"uri": uri, "text": "resource text", "mimeType": "text/plain"}
            self._json({"jsonrpc": "2.0", "id": request.get("id"), "result": {"contents": [content]}})
            return
        if method == "prompts/list":
            self._json({"jsonrpc": "2.0", "id": request.get("id"), "result": {"prompts": PROMPTS}})
            return
        if method == "prompts/get":
            who = request.get("params", {}).get("arguments", {}).get("who", "world")
            if MODE == "bad-role":
                messages = [{"role": "system", "content": {"type": "text", "text": "bad"}}]
            elif MODE == "image-prompt":
                messages = [{"role": "user", "content": {"type": "image", "data": "AA=="}}]
            elif MODE == "c1-prompt":
                messages = [{"role": "user", "content": {"type": "text", "text": "bad\u009b31m"}}]
            else:
                messages = [
                    {"role": "user", "content": {"type": "text", "text": f"Say hello to {who}"}},
                    {"role": "assistant", "content": {"type": "text", "text": "A quoted example"}},
                ]
            self._json({"jsonrpc": "2.0", "id": request.get("id"), "result": {"description": "expanded", "messages": messages}})
            return
        self._json({"jsonrpc": "2.0", "id": request.get("id"), "error": {"code": -32601, "message": "unknown"}})

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            return self.rfile.read(length)
        except Exception:
            return None

    def _json(self, value, *, session=False):
        data = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self._raw(data, session=session, content_type="application/json")

    def _raw(self, data, *, session=False, content_type=None):
        self.send_response(200)
        if content_type is not None:
            self.send_header("Content-Type", content_type)
        if session:
            self.send_header("MCP-Session-Id", SESSION)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _record(self, method):
        if RECORD:
            with open(RECORD, "a", encoding="utf-8") as stream:
                stream.write(json.dumps({
                    "method": method,
                    "protocol": self.headers.get("MCP-Protocol-Version"),
                    "session": self.headers.get("MCP-Session-Id"),
                    "authorization": self.headers.get("Authorization"),
                    "accept": self.headers.get("Accept"),
                }, separators=(",", ":")) + "\n")

    def log_message(self, _format, *_args):
        return


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--mode", default="normal")
    parser.add_argument("--record")
    parser.add_argument("--ready-file")
    args = parser.parse_args()
    global MODE, RECORD
    MODE = args.mode
    RECORD = args.record
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    if args.ready_file:
        Path(args.ready_file).write_text(str(server.server_address[1]), encoding="ascii")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


MODE = "normal"
RECORD: str | None = None


if __name__ == "__main__":
    raise SystemExit(main())
