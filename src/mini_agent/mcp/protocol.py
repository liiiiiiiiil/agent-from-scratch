"""Small, bounded JSON-RPC helpers for the v0.43 MCP client.

The module deliberately knows nothing about subprocesses or the Agent runtime.
It only validates the parts of JSON-RPC that are needed by the stdio client.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping


JSONRPC_VERSION = "2.0"
MCP_PROTOCOL_VERSION = "2025-11-25"
MAX_MESSAGE_BYTES = 1024 * 1024


class McpError(RuntimeError):
    """Internal common base for safe MCP errors."""


class McpProtocolError(McpError):
    """The peer sent a message that is not valid for this client."""


class McpTransportError(McpError):
    """The stdio connection could not send or receive a message."""


class McpTimeoutError(McpTransportError):
    """A bounded send or receive deadline expired."""


class McpRemoteError(McpError):
    """The peer returned a JSON-RPC error object."""

    def __init__(self, method: str, code: int) -> None:
        self.method = _safe_method(method)
        self.code = code
        super().__init__(f"MCP server returned an error for {self.method} (code={self.code})")


@dataclass(frozen=True)
class Response:
    """A validated JSON-RPC response without retaining server error text."""

    request_id: int
    result: Any = None
    error: Mapping[str, Any] | None = None


def _safe_method(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        return "unknown"
    return value


def _validate_method(method: Any) -> str:
    if not isinstance(method, str) or not method or len(method) > 128:
        raise McpProtocolError("invalid JSON-RPC method")
    if "\x00" in method:
        raise McpProtocolError("invalid JSON-RPC method")
    return method


def _validate_request_id(request_id: Any) -> int:
    if isinstance(request_id, bool) or not isinstance(request_id, int) or request_id < 1:
        raise McpProtocolError("invalid JSON-RPC request id")
    return request_id


def make_request(request_id: int, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build a JSON-RPC request using an integer request ID."""
    request_id = _validate_request_id(request_id)
    method = _validate_method(method)
    message: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "id": request_id, "method": method}
    if params is not None:
        if not isinstance(params, Mapping):
            raise McpProtocolError("JSON-RPC params must be an object")
        message["params"] = dict(params)
    return message


def make_notification(method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build a JSON-RPC notification without an ID."""
    method = _validate_method(method)
    message: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": method}
    if params is not None:
        if not isinstance(params, Mapping):
            raise McpProtocolError("JSON-RPC params must be an object")
        message["params"] = dict(params)
    return message


def encode_message(message: Mapping[str, Any]) -> bytes:
    """Serialize one JSON-RPC message as one UTF-8 line within the bound."""
    if not isinstance(message, Mapping):
        raise McpProtocolError("JSON-RPC message must be an object")
    try:
        encoded = json.dumps(
            message, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise McpProtocolError("JSON-RPC message cannot be encoded") from error
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise McpProtocolError("JSON-RPC message exceeds the size limit")
    return encoded + b"\n"


def decode_message(line: bytes) -> dict[str, Any]:
    """Decode one already bounded UTF-8 line without exposing its contents."""
    if not isinstance(line, bytes) or len(line) > MAX_MESSAGE_BYTES + 1:
        raise McpProtocolError("JSON-RPC message exceeds the size limit")
    if not line.endswith(b"\n"):
        raise McpProtocolError("JSON-RPC message is not newline terminated")
    payload = line[:-1]
    if payload.endswith(b"\r"):
        payload = payload[:-1]
    if len(payload) > MAX_MESSAGE_BYTES:
        raise McpProtocolError("JSON-RPC message exceeds the size limit")
    try:
        value = json.loads(payload.decode("utf-8"), parse_constant=_reject_non_json_number)
    except UnicodeDecodeError as error:
        raise McpTransportError("stdio output is not valid UTF-8") from error
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise McpProtocolError("stdio output is not valid JSON") from error
    if not isinstance(value, dict):
        raise McpProtocolError("JSON-RPC message must be an object")
    return value


def _reject_non_json_number(value: str) -> None:
    raise ValueError(f"invalid JSON number {value}")


def classify_message(message: Mapping[str, Any]) -> str:
    """Return ``response``, ``notification`` or ``request`` after validation."""
    if not isinstance(message, Mapping) or message.get("jsonrpc") != JSONRPC_VERSION:
        raise McpProtocolError("invalid JSON-RPC version")
    has_id = "id" in message
    has_method = "method" in message
    has_result = "result" in message
    has_error = "error" in message
    if has_method:
        _validate_method(message.get("method"))
        if has_result or has_error:
            raise McpProtocolError("JSON-RPC method message cannot contain result or error")
        if has_id:
            peer_id = message.get("id")
            if (
                peer_id is None
                or isinstance(peer_id, bool)
                or not isinstance(peer_id, (int, str))
                or (isinstance(peer_id, int) and peer_id < 1)
                or (isinstance(peer_id, str) and (not peer_id or len(peer_id) > 64))
            ):
                raise McpProtocolError("invalid JSON-RPC server request id")
            return "request"
        return "notification"
    if not has_id:
        raise McpProtocolError("JSON-RPC response is missing id")
    _validate_request_id(message.get("id"))
    if has_result == has_error:
        raise McpProtocolError("JSON-RPC response must contain exactly one of result or error")
    if has_error:
        error = message.get("error")
        if not isinstance(error, Mapping):
            raise McpProtocolError("JSON-RPC error must be an object")
        if isinstance(error.get("code"), bool) or not isinstance(error.get("code"), int):
            raise McpProtocolError("JSON-RPC error is missing a valid code")
        if not isinstance(error.get("message"), str):
            raise McpProtocolError("JSON-RPC error is missing a message")
    return "response"


def validate_response(message: Mapping[str, Any], expected_id: int, method: str) -> Response:
    """Validate and pair a response, dropping untrusted error text."""
    kind = classify_message(message)
    if kind != "response":
        raise McpProtocolError(f"unexpected JSON-RPC {kind} while waiting for {_safe_method(method)}")
    if message.get("id") != expected_id:
        raise McpProtocolError(f"JSON-RPC response id mismatch for {_safe_method(method)}")
    if "error" in message:
        error = message["error"]
        raise McpRemoteError(method, error["code"])
    return Response(request_id=expected_id, result=message.get("result"))


def validate_notification(message: Mapping[str, Any]) -> str:
    """Validate a peer notification and return its method."""
    if classify_message(message) != "notification":
        raise McpProtocolError("expected a JSON-RPC notification")
    return str(message["method"])
