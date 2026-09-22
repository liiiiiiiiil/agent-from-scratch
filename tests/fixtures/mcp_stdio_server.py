#!/usr/bin/env python3
"""Tiny stdlib MCP fixture used by the v0.43 offline tests."""
from __future__ import annotations

import argparse
import json
import sys
import time


TOOLS = [
    {
        "name": "echo",
        "description": "Return one text value",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
    },
    {
        "name": "sum",
        "description": "Add two numbers",
        "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}},
    },
    {
        "name": "third",
        "description": "A second-page tool",
        "inputSchema": {"type": "object"},
    },
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="normal")
    parser.add_argument("--record")
    args = parser.parse_args()

    for raw in sys.stdin.buffer:
        try:
            request = json.loads(raw.decode("utf-8"))
        except Exception:
            continue
        method = request.get("method") if isinstance(request, dict) else None
        _record(args.record, method)

        if args.mode == "early-exit":
            return 7
        if args.mode == "silent":
            time.sleep(60)
            continue
        if args.mode == "stderr-flood" and method == "initialize":
            sys.stderr.buffer.write(b"diagnostic-" * 25000)
            sys.stderr.buffer.flush()
        if args.mode == "bad-json" and method == "initialize":
            sys.stdout.buffer.write(b"{bad-json\n")
            sys.stdout.buffer.flush()
            continue
        if args.mode == "invalid-utf8" and method == "initialize":
            sys.stdout.buffer.write(b"\xff\n")
            sys.stdout.buffer.flush()
            continue
        if args.mode == "huge" and method == "initialize":
            sys.stdout.buffer.write(b"{" + b"x" * (1024 * 1024 + 100) + b"}\n")
            sys.stdout.buffer.flush()
            continue
        if args.mode == "stdout-flood" and method == "initialize":
            flood = b"".join(
                json.dumps(
                    {"jsonrpc": "2.0", "method": f"notifications/flood-{index}"},
                    separators=(",", ":"),
                ).encode("utf-8") + b"\n"
                for index in range(2000)
            )
            sys.stdout.buffer.write(flood)
            sys.stdout.buffer.flush()
            continue
        if args.mode == "server-request" and method == "initialize":
            _send({"jsonrpc": "2.0", "id": 77, "method": "sampling/createMessage", "params": {}})
        if method is None:
            continue
        if method == "initialize":
            if args.mode == "version-mismatch":
                version = "2026-01-01"
            else:
                version = "2025-11-25"
            capabilities = {} if args.mode == "missing-capability" else {"tools": {}}
            response = {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {
                    "protocolVersion": version,
                    "capabilities": capabilities,
                    "serverInfo": {"name": "fixture", "version": "1"},
                },
            }
            _send(response, args.mode == "wrong-id")
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            if args.mode == "duplicate-cursor":
                _send({"jsonrpc": "2.0", "id": request.get("id"), "result": {"tools": [], "nextCursor": "same"}})
                continue
            cursor = request.get("params", {}).get("cursor") if isinstance(request.get("params"), dict) else None
            if cursor is None:
                result = {"tools": TOOLS[:2], "nextCursor": "page-2"}
            else:
                result = {"tools": TOOLS[2:]}
            if args.mode == "both-result-error":
                _send({"jsonrpc": "2.0", "id": request.get("id"), "result": result, "error": {"code": -32000, "message": "both"}})
            else:
                _send({"jsonrpc": "2.0", "id": request.get("id"), "result": result})
        elif method == "tools/call":
            if args.mode == "call-silent":
                time.sleep(60)
                continue
            if args.mode == "call-disconnect":
                return 9
            if args.mode == "remote-error":
                _send({"jsonrpc": "2.0", "id": request.get("id"), "error": {"code": -32001, "message": "fixture failure"}})
                continue
            params = request.get("params", {})
            name = params.get("name") if isinstance(params, dict) else "unknown"
            arguments = params.get("arguments", {}) if isinstance(params, dict) else {}
            if args.mode == "is-error":
                result = {"isError": True, "content": [{"type": "text", "text": "tool rejected input"}]}
            elif args.mode == "image-result":
                result = {"isError": False, "content": [{"type": "image", "data": "AA==", "mimeType": "image/png"}]}
            elif args.mode == "structured-result":
                result = {"isError": False, "structuredContent": {"value": 1}, "content": []}
            elif name == "sum":
                result = {"isError": False, "content": [{"type": "text", "text": str(arguments.get("a", 0) + arguments.get("b", 0))}]}
            else:
                result = {"isError": False, "content": [{"type": "text", "text": str(arguments.get("text", ""))}]}
            _send({"jsonrpc": "2.0", "id": request.get("id"), "result": result})
        else:
            _send({"jsonrpc": "2.0", "id": request.get("id"), "error": {"code": -32601, "message": "unknown method"}})
    return 0


def _record(path: str | None, method: object) -> None:
    if not path:
        return
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(str(method) + "\n")


def _send(message: dict[str, object], wrong_id: bool = False) -> None:
    if wrong_id and "id" in message:
        message = dict(message)
        message["id"] = int(message["id"]) + 100 if isinstance(message["id"], int) else 999
    if message.get("method") == "initialize":
        return
    encoded = json.dumps(message, separators=(",", ":")).encode("utf-8")
    if _MODE in {"notify-before-response", "notify"}:
        sys.stdout.buffer.write(b'{"jsonrpc":"2.0","method":"notifications/tools/list_changed"}\n')
    sys.stdout.buffer.write(encoded + b"\n")
    sys.stdout.buffer.flush()


_MODE = "normal"


if __name__ == "__main__":
    # Keep the helper intentionally simple; the mode is also used by the
    # notification test through this module-level hook.
    parsed_mode = sys.argv[sys.argv.index("--mode") + 1] if "--mode" in sys.argv and len(sys.argv) > sys.argv.index("--mode") + 1 else "normal"
    _MODE = parsed_mode
    raise SystemExit(main())
