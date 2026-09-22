"""A deliberately small MCP 2025-11-25 client for local stdio servers."""
from __future__ import annotations

import copy
from collections import deque
import json
from threading import Lock
import time
from typing import Any, Mapping

from .. import __version__
from .protocol import (
    MCP_PROTOCOL_VERSION,
    McpProtocolError,
    McpRemoteError,
    McpTimeoutError,
    McpTransportError,
    classify_message,
    make_notification,
    make_request,
    validate_response,
    validate_notification,
)
from .stdio import (
    DEFAULT_CLOSE_TIMEOUT,
    DEFAULT_STARTUP_TIMEOUT,
    StdioTransport,
)


MAX_LIST_PAGES = 16
MAX_TOOLS = 256
MAX_CURSOR_CHARS = 4096
MAX_TOOL_NAME_CHARS = 256
DEFAULT_LIST_TIMEOUT = 10.0
DEFAULT_CALL_TIMEOUT = 30.0
MAX_NOTIFICATIONS = 64


class McpClient:
    """One serialized MCP session and its frozen tools directory."""

    def __init__(
        self,
        server: Mapping[str, Any],
        *,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
        list_timeout: float = DEFAULT_LIST_TIMEOUT,
        call_timeout: float = DEFAULT_CALL_TIMEOUT,
        close_timeout: float = DEFAULT_CLOSE_TIMEOUT,
    ) -> None:
        if not isinstance(server, Mapping):
            raise ValueError("MCP server configuration must be an object")
        self.server = _freeze_server(server)
        self.alias = self.server["alias"]
        self.startup_timeout = _positive_timeout(startup_timeout, "startup_timeout")
        self.list_timeout = _positive_timeout(list_timeout, "list_timeout")
        self.call_timeout = _positive_timeout(call_timeout, "call_timeout")
        self.close_timeout = _positive_timeout(close_timeout, "close_timeout")
        self.transport = StdioTransport.from_config(self.server, close_timeout=self.close_timeout)
        self._request_id = 1
        self._request_lock = Lock()
        self._operation_lock = Lock()
        self._connected = False
        self._closed = False
        self._tools: tuple[dict[str, Any], ...] | None = None
        self._notifications: deque[str] = deque(maxlen=MAX_NOTIFICATIONS)

    def connect(
        self_or_server: "McpClient | Mapping[str, Any]", **kwargs: Any,
    ) -> "McpClient":
        """Start a configured server, supporting class and instance styles.

        Both ``McpClient.connect(config)`` and
        ``McpClient(config).connect()`` are intentionally equivalent so the
        small public API remains convenient for scripts and tests.
        """
        if isinstance(self_or_server, McpClient):
            client = self_or_server
            if client.connected:
                return client
        else:
            client = McpClient(self_or_server, **kwargs)
        try:
            client._connect()
        except Exception:
            client.close()
            raise
        return client

    @property
    def connected(self) -> bool:
        return self._connected and not self._closed

    @property
    def tools(self) -> tuple[dict[str, Any], ...]:
        """Return a detached copy of the last frozen tools snapshot."""
        return tuple(copy.deepcopy(item) for item in (self._tools or ()))

    @property
    def notifications(self) -> tuple[str, ...]:
        return tuple(self._notifications)

    def __enter__(self) -> "McpClient":
        if not self.connected:
            raise McpTransportError(f"MCP client for alias {self.alias} is not connected")
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()

    def _connect(self) -> None:
        self.transport.start()
        result = self._request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "mini-agent", "version": __version__},
            },
            timeout=self.startup_timeout,
        )
        if not isinstance(result, Mapping):
            raise McpProtocolError("initialize result must be an object")
        if result.get("protocolVersion") != MCP_PROTOCOL_VERSION:
            raise McpProtocolError("MCP server returned an unsupported protocol version")
        server_info = result.get("serverInfo")
        if (
            not isinstance(server_info, Mapping)
            or not isinstance(server_info.get("name"), str)
            or not isinstance(server_info.get("version"), str)
        ):
            raise McpProtocolError("initialize result is missing server information")
        capabilities = result.get("capabilities")
        if not isinstance(capabilities, Mapping) or "tools" not in capabilities:
            raise McpProtocolError("MCP server does not advertise tools capability")
        if not isinstance(capabilities.get("tools"), Mapping):
            raise McpProtocolError("MCP tools capability is invalid")
        self._send_notification("notifications/initialized", None)
        self._connected = True

    def list_tools(self) -> list[dict[str, Any]]:
        """Read the complete tools directory once, then return detached copies."""
        with self._operation_lock:
            self._require_connected()
            if self._tools is not None:
                return [copy.deepcopy(item) for item in self._tools]
            return self._list_tools()

    def _list_tools(self) -> list[dict[str, Any]]:
        self._require_connected()
        collected: list[dict[str, Any]] = []
        names: set[str] = set()
        seen_cursors: set[str] = set()
        cursor: str | None = None
        for page_number in range(1, MAX_LIST_PAGES + 1):
            params: dict[str, Any] = {} if cursor is None else {"cursor": cursor}
            result = self._request("tools/list", params, timeout=self.list_timeout)
            if not isinstance(result, Mapping):
                self._invalidate()
                raise McpProtocolError("tools/list result must be an object")
            page_tools = result.get("tools")
            if not isinstance(page_tools, list):
                self._invalidate()
                raise McpProtocolError("tools/list result is missing a tools array")
            for item in page_tools:
                tool = _validate_tool(item)
                name = tool["name"]
                if name in names:
                    self._invalidate()
                    raise McpProtocolError("tools/list returned a duplicate tool name")
                if len(collected) >= MAX_TOOLS:
                    self._invalidate()
                    raise McpProtocolError("tools/list exceeded the tool limit")
                names.add(name)
                collected.append(tool)
            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                break
            if not isinstance(next_cursor, str) or not next_cursor or len(next_cursor) > MAX_CURSOR_CHARS:
                self._invalidate()
                raise McpProtocolError("tools/list returned an invalid cursor")
            if next_cursor in seen_cursors:
                self._invalidate()
                raise McpProtocolError("tools/list returned a repeated cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            self._invalidate()
            raise McpProtocolError("tools/list exceeded the page limit")
        self._tools = tuple(copy.deepcopy(item) for item in collected)
        return copy.deepcopy(collected)

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Call one tool from the frozen directory and return its MCP result."""
        with self._operation_lock:
            return self._call_tool(name, arguments)

    def _call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        self._require_connected()
        if self._tools is None:
            raise McpProtocolError("tools/list must complete before tools/call")
        if not isinstance(name, str) or name not in {item["name"] for item in self._tools}:
            raise McpProtocolError("tool name is not in the frozen directory")
        if not _is_json_object(arguments):
            raise McpProtocolError("tool arguments must be a JSON object")
        result = self._request(
            "tools/call",
            {"name": name, "arguments": copy.deepcopy(dict(arguments))},
            timeout=self.call_timeout,
        )
        if not isinstance(result, Mapping):
            self._invalidate()
            raise McpProtocolError("tools/call result must be an object")
        if "isError" in result and not isinstance(result["isError"], bool):
            self._invalidate()
            raise McpProtocolError("tools/call isError must be a boolean")
        return copy.deepcopy(dict(result))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._connected = False
        self.transport.close()

    def _send_notification(self, method: str, params: Mapping[str, Any] | None) -> None:
        try:
            self.transport.send(make_notification(method, params), timeout=self.startup_timeout)
        except (McpTransportError, McpTimeoutError):
            self._invalidate()
            raise

    def _request(self, method: str, params: Mapping[str, Any], *, timeout: float) -> Any:
        with self._request_lock:
            if self._closed:
                raise McpTransportError(f"MCP client for alias {self.alias} is closed")
            request_id = self._request_id
            self._request_id += 1
            try:
                self.transport.send(make_request(request_id, method, params), timeout=timeout)
                deadline = time.monotonic() + timeout
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise McpTimeoutError(f"MCP request timed out for alias {self.alias}")
                    message = self.transport.receive(timeout=remaining)
                    kind = classify_message(message)
                    if kind == "notification":
                        self._notifications.append(validate_notification(message))
                        continue
                    if kind == "request":
                        raise McpProtocolError(
                            f"MCP server requests are unsupported for alias {self.alias}"
                        )
                    return validate_response(message, request_id, method).result
            except McpRemoteError:
                raise
            except (McpProtocolError, McpTransportError, McpTimeoutError):
                self._invalidate()
                raise

    def _require_connected(self) -> None:
        if not self.connected:
            raise McpTransportError(f"MCP client for alias {self.alias} is not connected")

    def _invalidate(self) -> None:
        self._connected = False
        if not self._closed:
            self._closed = True
            self.transport.close()


def _positive_timeout(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{field} must be positive")
    return float(value)


def _freeze_server(server: Mapping[str, Any]) -> dict[str, Any]:
    alias = server.get("alias")
    if not isinstance(alias, str) or not alias or len(alias) > 64 or "\x00" in alias:
        raise ValueError("MCP server alias is invalid")
    command = server.get("command")
    if not isinstance(command, (list, tuple)) or not command:
        raise ValueError("MCP command must be a non-empty argv list")
    frozen_command: list[str] = []
    for item in command:
        if not isinstance(item, str) or not item or "\x00" in item:
            raise ValueError("MCP command entries must be non-empty strings")
        frozen_command.append(item)
    cwd = server.get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or "\x00" in cwd):
        raise ValueError("MCP cwd must be a string")
    environment = server.get("environment", {})
    if not isinstance(environment, Mapping):
        raise ValueError("MCP environment must be an object")
    frozen_environment: dict[str, str] = {}
    for key, value in environment.items():
        if (
            not isinstance(key, str) or not key or "\x00" in key
            or not isinstance(value, str) or "\x00" in value
        ):
            raise ValueError("MCP environment keys and values must be strings")
        frozen_environment[key] = value
    return {
        "alias": alias,
        "command": tuple(frozen_command),
        "cwd": cwd,
        "environment": frozen_environment,
    }


def _validate_tool(item: Any) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise McpProtocolError("tools/list returned an invalid tool entry")
    name = item.get("name")
    if not isinstance(name, str) or not name or len(name) > MAX_TOOL_NAME_CHARS:
        raise McpProtocolError("tools/list returned an invalid tool name")
    if "\x00" in name:
        raise McpProtocolError("tools/list returned an invalid tool name")
    if "inputSchema" not in item or not isinstance(item.get("inputSchema"), Mapping):
        raise McpProtocolError("tools/list returned an invalid input schema")
    for field in ("title", "description"):
        if field in item and not isinstance(item[field], str):
            raise McpProtocolError("tools/list returned an invalid tool description")
    return copy.deepcopy(dict(item))


def _is_json_object(value: Any) -> bool:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        return False
    try:
        json.dumps(dict(value), allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError):
        return False
    return True
