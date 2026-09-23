"""A bounded MCP 2025-11-25 client for stdio and JSON-only HTTP."""
from __future__ import annotations

import copy
from collections import deque
import ipaddress
import json
from threading import Lock
import time
import unicodedata
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from .. import __version__
from .http import HttpTransport
from .protocol import (
    MCP_PROTOCOL_VERSION,
    McpProtocolError,
    McpRemoteError,
    McpTimeoutError,
    McpTransportError,
    classify_message,
    make_notification,
    make_request,
    validate_notification,
    validate_response,
)
from .stdio import DEFAULT_CLOSE_TIMEOUT, DEFAULT_STARTUP_TIMEOUT, StdioTransport


MAX_LIST_PAGES = 16
MAX_TOOLS = 256
MAX_RESOURCES = 256
MAX_PROMPTS = 256
MAX_CURSOR_CHARS = 4096
MAX_TOOL_NAME_CHARS = 256
MAX_RESOURCE_URI_CHARS = 2048
MAX_RESOURCE_NAME_CHARS = 256
MAX_PROMPT_NAME_CHARS = 128
MAX_PROMPT_ARGUMENTS = 32
MAX_CONTENT_ITEMS = 32
MAX_TEXT_BYTES = 64 * 1024
MAX_METADATA_CHARS = 4096
DEFAULT_LIST_TIMEOUT = 10.0
DEFAULT_CALL_TIMEOUT = 30.0
MAX_NOTIFICATIONS = 64


class McpClient:
    """One serialized MCP session and its frozen capability directories."""

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
        if self.server["transport"] == "http":
            self.transport = HttpTransport(
                self.alias, self.server["url"], self.server.get("headers", {}),
                close_timeout=self.close_timeout,
            )
        else:
            self.transport = StdioTransport.from_config(
                self.server, close_timeout=self.close_timeout,
            )
        self._request_id = 1
        self._request_lock = Lock()
        self._operation_lock = Lock()
        self._connected = False
        self._closed = False
        self._capabilities: dict[str, Mapping[str, Any]] = {}
        self._tools: tuple[dict[str, Any], ...] | None = None
        self._resources: tuple[dict[str, Any], ...] | None = None
        self._prompts: tuple[dict[str, Any], ...] | None = None
        self._notifications: deque[str] = deque(maxlen=MAX_NOTIFICATIONS)

    def connect(
        self_or_server: "McpClient | Mapping[str, Any]", **kwargs: Any,
    ) -> "McpClient":
        """Start a configured server, supporting class and instance styles."""
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
        return tuple(copy.deepcopy(item) for item in (self._tools or ()))

    @property
    def resources(self) -> tuple[dict[str, Any], ...]:
        return tuple(copy.deepcopy(item) for item in (self._resources or ()))

    @property
    def prompts(self) -> tuple[dict[str, Any], ...]:
        return tuple(copy.deepcopy(item) for item in (self._prompts or ()))

    @property
    def capabilities(self) -> tuple[str, ...]:
        return tuple(sorted(self._capabilities))

    def supports(self, capability: str) -> bool:
        return capability in self._capabilities

    @property
    def notifications(self) -> tuple[str, ...]:
        return tuple(self._notifications)

    @property
    def close_report(self) -> dict[str, Any]:
        return self.transport.close_report

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
        if not isinstance(capabilities, Mapping):
            raise McpProtocolError("MCP initialize capabilities are invalid")
        accepted: dict[str, Mapping[str, Any]] = {}
        for name in ("tools", "resources", "prompts"):
            if name in capabilities:
                if not isinstance(capabilities[name], Mapping):
                    raise McpProtocolError(f"MCP {name} capability is invalid")
                accepted[name] = dict(capabilities[name])
        if not accepted:
            raise McpProtocolError("MCP server advertises no supported capability")
        self._capabilities = accepted
        self._send_notification("notifications/initialized", None)
        self._connected = True

    def list_tools(self) -> list[dict[str, Any]]:
        with self._operation_lock:
            self._require_connected()
            self._require_capability("tools")
            if self._tools is None:
                self._tools = tuple(self._list_directory(
                    "tools/list", "tools", _validate_tool, MAX_TOOLS, "tool name",
                ))
            return copy.deepcopy(list(self._tools))

    def list_resources(self) -> list[dict[str, Any]]:
        with self._operation_lock:
            self._require_connected()
            self._require_capability("resources")
            if self._resources is None:
                self._resources = tuple(self._list_directory(
                    "resources/list", "resources", _validate_resource,
                    MAX_RESOURCES, "resource URI",
                ))
            return copy.deepcopy(list(self._resources))

    def read_resource(self, uri: str) -> dict[str, Any]:
        with self._operation_lock:
            self._require_connected()
            self._require_capability("resources")
            if self._resources is None:
                raise McpProtocolError("resources/list must complete before resources/read")
            if not isinstance(uri, str) or uri not in {item["uri"] for item in self._resources}:
                raise McpProtocolError("resource URI is not in the frozen directory")
            result = self._request("resources/read", {"uri": uri}, timeout=self.call_timeout)
            try:
                return _validate_resource_read(result, uri)
            except McpProtocolError:
                self._invalidate()
                raise

    def list_prompts(self) -> list[dict[str, Any]]:
        with self._operation_lock:
            self._require_connected()
            self._require_capability("prompts")
            if self._prompts is None:
                self._prompts = tuple(self._list_directory(
                    "prompts/list", "prompts", _validate_prompt,
                    MAX_PROMPTS, "prompt name",
                ))
            return copy.deepcopy(list(self._prompts))

    def get_prompt(
        self, name: str, arguments: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        with self._operation_lock:
            self._require_connected()
            self._require_capability("prompts")
            if self._prompts is None:
                raise McpProtocolError("prompts/list must complete before prompts/get")
            definition = next((item for item in self._prompts if item["name"] == name), None)
            if definition is None:
                raise McpProtocolError("prompt name is not in the frozen directory")
            normalized = _validate_prompt_arguments(definition, arguments or {})
            result = self._request(
                "prompts/get", {"name": name, "arguments": normalized},
                timeout=self.call_timeout,
            )
            try:
                return _validate_prompt_result(result)
            except McpProtocolError:
                self._invalidate()
                raise

    def _list_directory(
        self, method: str, key: str, validator: Callable[[Any], dict[str, Any]],
        maximum: int, duplicate_label: str,
    ) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        identities: set[str] = set()
        seen_cursors: set[str] = set()
        cursor: str | None = None
        for _page_number in range(1, MAX_LIST_PAGES + 1):
            params: dict[str, Any] = {} if cursor is None else {"cursor": cursor}
            result = self._request(method, params, timeout=self.list_timeout)
            if not isinstance(result, Mapping) or not isinstance(result.get(key), list):
                self._invalidate()
                raise McpProtocolError(f"{method} result is missing a {key} array")
            for raw_item in result[key]:
                item = validator(raw_item)
                identity = item["name"] if key in {"tools", "prompts"} else item["uri"]
                if identity in identities:
                    self._invalidate()
                    raise McpProtocolError(f"{method} returned a duplicate {duplicate_label}")
                if len(collected) >= maximum:
                    self._invalidate()
                    raise McpProtocolError(f"{method} exceeded the directory limit")
                identities.add(identity)
                collected.append(item)
            if _json_size(collected) > 512 * 1024:
                self._invalidate()
                raise McpProtocolError(f"{method} directory exceeds the size limit")
            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                return collected
            if not isinstance(next_cursor, str) or not next_cursor or len(next_cursor) > MAX_CURSOR_CHARS:
                self._invalidate()
                raise McpProtocolError(f"{method} returned an invalid cursor")
            if next_cursor in seen_cursors:
                self._invalidate()
                raise McpProtocolError(f"{method} returned a repeated cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        self._invalidate()
        raise McpProtocolError(f"{method} exceeded the page limit")

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        with self._operation_lock:
            self._require_connected()
            self._require_capability("tools")
            if self._tools is None:
                raise McpProtocolError("tools/list must complete before tools/call")
            if not isinstance(name, str) or name not in {item["name"] for item in self._tools}:
                raise McpProtocolError("tool name is not in the frozen directory")
            if not _is_json_object(arguments):
                raise McpProtocolError("tool arguments must be a JSON object")
            result = self._request(
                "tools/call", {"name": name, "arguments": copy.deepcopy(dict(arguments))},
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
        except (McpTransportError, McpTimeoutError, McpProtocolError):
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

    def _require_capability(self, name: str) -> None:
        if name not in self._capabilities:
            raise McpProtocolError(f"MCP server does not advertise {name} capability")

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
    transport = server.get("transport", "stdio")
    if transport not in {"stdio", "http"}:
        raise ValueError("MCP transport is invalid")
    if transport == "http":
        url = server.get("url")
        if not isinstance(url, str) or not url or "\x00" in url:
            raise ValueError("MCP HTTP URL is invalid")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("MCP HTTP URL is invalid")
        if parsed.scheme == "http" and (
            not server.get("allow_loopback_http", False)
            or not _is_loopback_host(parsed.hostname)
        ):
            raise ValueError("MCP HTTP URL is not an allowed loopback URL")
        headers = server.get("headers", {})
        if not isinstance(headers, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in headers.items()
        ):
            raise ValueError("MCP HTTP headers are invalid")
        return {"alias": alias, "transport": "http", "url": url, "headers": dict(headers)}
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
        "alias": alias, "transport": "stdio", "command": tuple(frozen_command),
        "cwd": cwd, "environment": frozen_environment,
    }


def _is_loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_tool(item: Any) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise McpProtocolError("tools/list returned an invalid tool entry")
    name = item.get("name")
    if not isinstance(name, str) or not name or len(name) > MAX_TOOL_NAME_CHARS or "\x00" in name:
        raise McpProtocolError("tools/list returned an invalid tool name")
    if "inputSchema" not in item or not isinstance(item.get("inputSchema"), Mapping):
        raise McpProtocolError("tools/list returned an invalid input schema")
    for field in ("title", "description"):
        if field in item and not isinstance(item[field], str):
            raise McpProtocolError("tools/list returned an invalid tool description")
    return copy.deepcopy(dict(item))


def _validate_resource(item: Any) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise McpProtocolError("resources/list returned an invalid resource entry")
    uri = item.get("uri")
    name = item.get("name")
    if not _bounded_text(uri, MAX_RESOURCE_URI_CHARS, allow_empty=False):
        raise McpProtocolError("resources/list returned an invalid URI")
    if not _bounded_text(name, MAX_RESOURCE_NAME_CHARS, allow_empty=False):
        raise McpProtocolError("resources/list returned an invalid resource name")
    for field in ("description", "mimeType"):
        if field in item and not _bounded_text(item[field], MAX_METADATA_CHARS):
            raise McpProtocolError("resources/list returned invalid metadata")
    if "mimeType" in item and not _is_text_mime(item["mimeType"]):
        raise McpProtocolError("resources/list returned a non-text resource")
    if "size" in item and (
        isinstance(item["size"], bool) or not isinstance(item["size"], int) or item["size"] < 0
    ):
        raise McpProtocolError("resources/list returned an invalid resource size")
    return copy.deepcopy(dict(item))


def _validate_prompt(item: Any) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise McpProtocolError("prompts/list returned an invalid prompt entry")
    name = item.get("name")
    if not _bounded_text(name, MAX_PROMPT_NAME_CHARS, allow_empty=False):
        raise McpProtocolError("prompts/list returned an invalid prompt name")
    if "description" in item and not _bounded_text(item["description"], MAX_METADATA_CHARS):
        raise McpProtocolError("prompts/list returned invalid prompt description")
    arguments = item.get("arguments", [])
    if not isinstance(arguments, list) or len(arguments) > MAX_PROMPT_ARGUMENTS:
        raise McpProtocolError("prompts/list returned invalid prompt arguments")
    seen: set[str] = set()
    for argument in arguments:
        if not isinstance(argument, Mapping) or not _bounded_text(
            argument.get("name"), MAX_PROMPT_NAME_CHARS, allow_empty=False,
        ):
            raise McpProtocolError("prompts/list returned an invalid prompt argument")
        argument_name = argument["name"]
        if argument_name in seen:
            raise McpProtocolError("prompts/list returned duplicate prompt arguments")
        seen.add(argument_name)
        if "description" in argument and not _bounded_text(argument["description"], MAX_METADATA_CHARS):
            raise McpProtocolError("prompts/list returned invalid argument description")
        if "required" in argument and not isinstance(argument["required"], bool):
            raise McpProtocolError("prompts/list returned invalid argument required flag")
    return copy.deepcopy(dict(item))


def _validate_resource_read(result: Any, uri: str) -> dict[str, Any]:
    if not isinstance(result, Mapping) or not isinstance(result.get("contents"), list):
        raise McpProtocolError("resources/read result is missing contents")
    contents = result["contents"]
    if not contents or len(contents) > MAX_CONTENT_ITEMS:
        raise McpProtocolError("resources/read returned an invalid content count")
    total = 0
    normalized: list[dict[str, Any]] = []
    for item in contents:
        if not isinstance(item, Mapping) or item.get("uri") != uri:
            raise McpProtocolError("resources/read returned a mismatched URI")
        if any(key in item for key in ("blob", "resource", "image", "audio", "embedded")):
            raise McpProtocolError("resources/read returned unsupported content")
        text = item.get("text")
        if not _bounded_text(
            text, MAX_TEXT_BYTES, allow_empty=True, bytes_limit=True, allow_newlines=True,
        ):
            raise McpProtocolError("resources/read returned non-text or oversized content")
        mime = item.get("mimeType")
        if mime is not None and not _is_text_mime(mime):
            raise McpProtocolError("resources/read returned non-text content")
        total += len(text.encode("utf-8"))
        if total > MAX_TEXT_BYTES:
            raise McpProtocolError("resources/read returned oversized content")
        normalized.append(copy.deepcopy(dict(item)))
    return {"contents": normalized}


def _validate_prompt_arguments(definition: Mapping[str, Any], arguments: Any) -> dict[str, str]:
    if not isinstance(arguments, Mapping) or any(not isinstance(key, str) for key in arguments):
        raise McpProtocolError("prompts/get arguments must be a JSON object")
    declared = {item["name"]: item for item in definition.get("arguments", [])}
    unknown = set(arguments) - set(declared)
    if unknown:
        raise McpProtocolError("prompts/get received an undeclared argument")
    missing = [
        name for name, item in declared.items()
        if item.get("required", False) and name not in arguments
    ]
    if missing:
        raise McpProtocolError("prompts/get is missing a required argument")
    normalized: dict[str, str] = {}
    for key, value in arguments.items():
        if not _bounded_text(value, MAX_METADATA_CHARS, allow_empty=True):
            raise McpProtocolError("prompts/get arguments must be bounded strings")
        normalized[key] = value
    if _json_size(normalized) > 16 * 1024:
        raise McpProtocolError("prompts/get arguments exceed the size limit")
    return normalized


def _validate_prompt_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, Mapping) or not isinstance(result.get("messages"), list):
        raise McpProtocolError("prompts/get result is missing messages")
    messages = result["messages"]
    if not messages or len(messages) > MAX_CONTENT_ITEMS:
        raise McpProtocolError("prompts/get returned an invalid message count")
    total = 0
    normalized: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, Mapping) or message.get("role") not in {"user", "assistant"}:
            raise McpProtocolError("prompts/get returned an invalid message role")
        content = message.get("content")
        if not isinstance(content, Mapping) or content.get("type") != "text":
            raise McpProtocolError("prompts/get returned non-text content")
        text = content.get("text")
        if not _bounded_text(
            text, MAX_TEXT_BYTES, allow_empty=True, bytes_limit=True, allow_newlines=True,
        ):
            raise McpProtocolError("prompts/get returned oversized text")
        total += len(text.encode("utf-8"))
        if total > MAX_TEXT_BYTES:
            raise McpProtocolError("prompts/get returned oversized content")
        normalized.append({"role": message["role"], "content": {"type": "text", "text": text}})
    description = result.get("description", "")
    if not _bounded_text(description, MAX_METADATA_CHARS):
        raise McpProtocolError("prompts/get returned invalid description")
    return {"description": description, "messages": normalized}


def _is_text_mime(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    base = value.split(";", 1)[0].strip().casefold()
    return base.startswith("text/") or base in {
        "application/json", "application/xml", "application/javascript",
    }


def _bounded_text(
    value: Any, maximum: int, *, allow_empty: bool = True, bytes_limit: bool = False,
    allow_newlines: bool = False,
) -> bool:
    if not isinstance(value, str) or (not allow_empty and not value) or len(value) > maximum:
        return False
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    allowed_controls = "\n\r\t" if allow_newlines else ""
    if any(
        unicodedata.category(char).startswith("C") and char not in allowed_controls
        for char in value
    ):
        return False
    if bytes_limit:
        return len(encoded) <= maximum
    return True


def _json_size(value: Any) -> int:
    try:
        return len(json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8"))
    except (TypeError, ValueError, UnicodeError) as error:
        raise McpProtocolError("MCP directory is not finite JSON") from error


def _is_json_object(value: Any) -> bool:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        return False
    try:
        json.dumps(dict(value), allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError):
        return False
    return True


__all__ = ["MAX_NOTIFICATIONS", "MAX_PROMPTS", "MAX_RESOURCES", "McpClient"]
