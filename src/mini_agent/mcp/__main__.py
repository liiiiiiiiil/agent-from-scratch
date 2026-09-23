"""Interactive demonstration CLI for the standalone MCP client."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, TextIO

from .. import config
from . import McpClient, McpProtocolError, McpRemoteError, McpTimeoutError, McpTransportError


MAX_DISPLAY_CHARS = 64 * 1024


def main(
    argv: list[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    input_stream = stdin if stdin is not None else sys.stdin
    output_stream = stdout if stdout is not None else sys.stdout
    try:
        server = _server_by_alias(args.alias)
        if args.action == "call":
            try:
                arguments = json.loads(args.arguments)
            except (json.JSONDecodeError, TypeError, ValueError):
                _print(output_stream, "参数必须是合法 JSON object")
                return 2
            if not isinstance(arguments, dict):
                _print(output_stream, "参数必须是 JSON object")
                return 2

        client: McpClient | None = None
        status = 1
        try:
            client = McpClient(server)
            client.connect()
            tools = client.list_tools()
            if args.action == "list":
                _print(output_stream, _display_tools(tools))
                status = 0
            elif not _is_interactive(input_stream):
                _print(output_stream, "call 需要交互式终端；未发送 tools/call")
                status = 2
            else:
                try:
                    preview = json.dumps(
                        arguments, ensure_ascii=True, separators=(",", ":"), allow_nan=False,
                    )
                except (TypeError, ValueError):
                    _print(output_stream, "参数必须是可编码的 JSON object；未发送 tools/call")
                    status = 2
                else:
                    announcement = (
                        f"即将调用 alias={_display(args.alias)} tool={_display(args.tool)} "
                        f"arguments={preview}"
                    )
                    if len(announcement) > MAX_DISPLAY_CHARS:
                        _print(output_stream, "参数过长，无法完整展示；未发送 tools/call")
                        status = 2
                    else:
                        _print(output_stream, announcement)
                        _write(output_stream, "确认调用？输入 yes 或 y：")
                        output_stream.flush()
                        answer = input_stream.readline()
                        if not answer or answer.strip().casefold() not in {"y", "yes"}:
                            _print(output_stream, "未确认，未发送 tools/call")
                            status = 2
                        else:
                            result = client.call_tool(args.tool, arguments)
                            _print(output_stream, _bounded_json(result))
                            status = 0
        except (McpTimeoutError, McpProtocolError, McpTransportError, McpRemoteError) as error:
            _print(output_stream, _safe_error(error))
            status = 1
        finally:
            if client is not None:
                client.close()
                report = client.transport.close_report
                if not report.get("closed", False):
                    reason = report.get("reason", "unknown cleanup failure")
                    _print(
                        output_stream,
                        f"MCP Server 清理未完成：alias={client.alias} reason={reason}",
                    )
                    status = 1
        return status
    except (ValueError, KeyError):
        _print(output_stream, "MCP 配置无效或 alias 不存在")
        return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m mini_agent.mcp")
    parser.add_argument("alias")
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("list", help="列出冻结的 MCP tools 目录")
    call = subparsers.add_parser("call", help="在确认后调用一个 MCP tool")
    call.add_argument("tool")
    call.add_argument("arguments", help="JSON object")
    return parser


def _server_by_alias(alias: str) -> dict[str, Any]:
    for server in config.resolved_mcp_servers():
        if server["alias"] == alias:
            return server
    raise KeyError(alias)


def _is_interactive(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError):
        return False


def _display_tools(tools: list[dict[str, Any]]) -> str:
    visible = [
        {"name": item.get("name"), "description": item.get("description", "")}
        for item in tools
    ]
    return _bounded_json({"tools": visible, "total": len(tools)})


def _safe_json(value: Any) -> str:
    try:
        return _bounded_text(json.dumps(value, ensure_ascii=True, separators=(",", ":")))
    except (TypeError, ValueError):
        return "<无法显示参数>"


def _bounded_json(value: Any) -> str:
    return _safe_json(value)


def _bounded_text(value: str) -> str:
    if len(value) <= MAX_DISPLAY_CHARS:
        return value
    return value[: MAX_DISPLAY_CHARS - 24] + "...<output truncated>"


def _display(value: Any) -> str:
    text = str(value)
    safe = "".join(
        character
        if not (ord(character) < 32 or 127 <= ord(character) <= 159)
        else f"\\u{ord(character):04x}"
        for character in text
    )
    return _bounded_text(safe)


def _safe_error(error: Exception) -> str:
    if isinstance(error, McpRemoteError):
        return f"MCP server error: method={error.method} code={error.code}"
    if isinstance(error, McpTimeoutError):
        return "MCP request timed out"
    if isinstance(error, McpProtocolError):
        return "MCP protocol error"
    if isinstance(error, McpTransportError):
        return "MCP transport error"
    return "MCP client error"


def _write(stream: TextIO, value: str) -> None:
    stream.write(_display(value))


def _print(stream: TextIO, value: str) -> None:
    _write(stream, value)
    stream.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
