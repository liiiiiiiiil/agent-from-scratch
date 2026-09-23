"""Restricted JSON-only MCP HTTP transport.

This is the teaching subset used by v0.46.  It deliberately does not follow
redirects, consume SSE, negotiate OAuth, or retry a request after it has been
sent.  The client owns the protocol and session headers; configured headers
are treated as local secrets and are never included in errors or reports.
"""
from __future__ import annotations

import http.client
import json
import re
from threading import Lock
from typing import Any, Mapping
from urllib.parse import urlsplit

from .protocol import (
    MAX_MESSAGE_BYTES,
    McpProtocolError,
    McpTimeoutError,
    McpTransportError,
    encode_message,
)


MAX_HTTP_RESPONSE_BYTES = MAX_MESSAGE_BYTES
DEFAULT_HTTP_TIMEOUT = 30.0
_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")


class HttpTransport:
    """One serialized HTTP MCP session with one pending JSON response."""

    def __init__(
        self,
        alias: str,
        url: str,
        headers: Mapping[str, str] | None = None,
        *,
        close_timeout: float = 2.0,
    ) -> None:
        self.alias = _safe_alias(alias)
        self.url = _freeze_url(url)
        parsed = urlsplit(self.url)
        self._scheme = parsed.scheme
        self._host = parsed.hostname or ""
        self._port = parsed.port
        self._target = parsed.path or "/"
        if parsed.query:
            self._target += "?" + parsed.query
        self._headers = _freeze_headers(headers or {})
        if close_timeout <= 0:
            raise ValueError("close_timeout must be positive")
        self.close_timeout = float(close_timeout)
        self._connection: http.client.HTTPConnection | http.client.HTTPSConnection | None = None
        self._session_id: str | None = None
        self._initialized = False
        self._pending: dict[str, Any] | None = None
        self._send_lock = Lock()
        self._closed = False
        self._failed = False
        self._close_report: dict[str, Any] = {"alias": self.alias, "closed": False}

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def close_report(self) -> dict[str, Any]:
        return dict(self._close_report)

    def start(self) -> "HttpTransport":
        if self._closed:
            raise McpTransportError(f"HTTP connection for alias {self.alias} is closed")
        return self

    def send(self, message: Mapping[str, Any], *, timeout: float) -> None:
        payload = encode_message(message)[:-1]
        self._ensure_usable()
        if timeout <= 0:
            raise McpTimeoutError(f"HTTP request timed out for alias {self.alias}")
        with self._send_lock:
            self._ensure_usable()
            method = message.get("method")
            request_headers = {
                # Streamable HTTP requires both media types in Accept.  This
                # teaching client still rejects an SSE response below.
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Accept-Encoding": "identity",
                **self._headers,
            }
            if self._initialized:
                request_headers["MCP-Protocol-Version"] = "2025-11-25"
                if self._session_id is not None:
                    request_headers["MCP-Session-Id"] = self._session_id
            try:
                response = self._request_http(payload, request_headers, float(timeout))
                status = int(response.status)
                body = _read_bounded(response, status == 202)
                if method and str(method).startswith("notifications/"):
                    if status != 202 or body:
                        raise McpProtocolError("HTTP MCP notification response is invalid")
                    return
                if status != 200:
                    raise _status_error(status)
                content_type = response.getheader("Content-Type")
                if not _is_json_content_type(content_type):
                    raise McpProtocolError("HTTP MCP response is not application/json")
                session_id = response.getheader("MCP-Session-Id")
                if session_id:
                    self._session_id = _validate_session_id(session_id)
                if method == "initialize":
                    self._initialized = True
                self._pending = {"body": body}
            except (McpProtocolError, McpTransportError, McpTimeoutError):
                self._failed = True
                self._drop_connection()
                raise
            except (http.client.HTTPException, OSError, ValueError) as error:
                self._failed = True
                self._drop_connection()
                if isinstance(error, (TimeoutError,)) or "timed out" in str(error).casefold():
                    raise McpTimeoutError(f"HTTP request timed out for alias {self.alias}") from error
                raise McpTransportError(f"HTTP request failed for alias {self.alias}") from error

    def receive(self, *, timeout: float) -> dict[str, Any]:
        self._ensure_usable()
        if timeout <= 0:
            raise McpTimeoutError(f"HTTP response timed out for alias {self.alias}")
        pending = self._pending
        self._pending = None
        if pending is None:
            raise McpTransportError(f"HTTP response is unavailable for alias {self.alias}")
        body = pending["body"]
        try:
            value = json.loads(body.decode("utf-8"), parse_constant=_reject_non_json_number)
        except UnicodeDecodeError as error:
            self._failed = True
            self._drop_connection()
            raise McpTransportError("HTTP MCP response is not valid UTF-8") from error
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            self._failed = True
            self._drop_connection()
            raise McpProtocolError("HTTP MCP response is not valid JSON") from error
        if not isinstance(value, dict):
            self._failed = True
            self._drop_connection()
            raise McpProtocolError("HTTP MCP response must be an object")
        return value

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        reasons: list[str] = []
        connection = self._connection
        if connection is not None and self._session_id is not None and self._initialized and not self._failed:
            try:
                _set_connection_timeout(connection, self.close_timeout)
                connection.request(
                    "DELETE", self._target,
                    body=None,
                    headers={
                        **self._headers,
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                        "MCP-Protocol-Version": "2025-11-25",
                        "MCP-Session-Id": self._session_id,
                    },
                )
                response = connection.getresponse()
                response.read(min(MAX_HTTP_RESPONSE_BYTES, 64 * 1024) + 1)
                if response.status not in (200, 202, 204):
                    reasons.append("session delete rejected")
            except Exception:
                # Closing is best effort.  The error is a bounded category,
                # never a URL, session id, or configured header value.
                reasons.append("session delete failed")
        self._drop_connection()
        self._close_report = {"alias": self.alias, "closed": not reasons}
        if reasons:
            self._close_report["reason"] = "; ".join(reasons)

    def _request_http(
        self, payload: bytes, headers: Mapping[str, str], timeout: float,
    ) -> http.client.HTTPResponse:
        connection = self._connection
        if connection is None:
            if self._scheme == "https":
                connection = http.client.HTTPSConnection(self._host, self._port, timeout=timeout)
            else:
                connection = http.client.HTTPConnection(self._host, self._port, timeout=timeout)
            self._connection = connection
        else:
            _set_connection_timeout(connection, timeout)
        connection.request("POST", self._target, body=payload, headers=dict(headers))
        return connection.getresponse()

    def _ensure_usable(self) -> None:
        if self._closed:
            raise McpTransportError(f"HTTP connection for alias {self.alias} is closed")
        if self._failed:
            raise McpTransportError(f"HTTP connection for alias {self.alias} is unavailable")

    def _drop_connection(self) -> None:
        connection = self._connection
        self._connection = None
        self._pending = None
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass


def _set_connection_timeout(
    connection: http.client.HTTPConnection | http.client.HTTPSConnection,
    timeout: float,
) -> None:
    """Apply a new deadline to both future connects and an existing socket."""
    connection.timeout = timeout
    sock = connection.sock
    if sock is not None:
        sock.settimeout(timeout)


def _read_bounded(response: http.client.HTTPResponse, notification: bool) -> bytes:
    length = response.getheader("Content-Length")
    if length is not None:
        try:
            if int(length) > MAX_HTTP_RESPONSE_BYTES:
                raise McpProtocolError("HTTP MCP response exceeds the size limit")
        except ValueError as error:
            raise McpProtocolError("HTTP MCP response length is invalid") from error
    body = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
    if len(body) > MAX_HTTP_RESPONSE_BYTES:
        raise McpProtocolError("HTTP MCP response exceeds the size limit")
    if notification and body:
        raise McpProtocolError("HTTP MCP notification must have an empty body")
    return body


def _status_error(status: int) -> McpTransportError:
    if 300 <= status < 400:
        return McpTransportError("HTTP MCP redirects are unsupported")
    if status in (401, 403):
        return McpTransportError("HTTP MCP authentication failed")
    if status == 404:
        return McpTransportError("HTTP MCP session or endpoint was not found")
    return McpTransportError("HTTP MCP server returned an unsupported status")


def _is_json_content_type(value: str | None) -> bool:
    if not isinstance(value, str):
        return False
    return value.split(";", 1)[0].strip().casefold() == "application/json"


def _validate_session_id(value: str) -> str:
    if (
        not value or len(value) > 256
        or any(ord(char) < 33 or ord(char) > 126 for char in value)
    ):
        raise McpProtocolError("HTTP MCP session id is invalid")
    return value


def _freeze_url(value: Any) -> str:
    if (
        not isinstance(value, str) or not value or len(value) > 2048
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("MCP HTTP URL is invalid")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("MCP HTTP URL is invalid")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError("MCP HTTP URL is invalid")
    try:
        parsed.port
    except ValueError as error:
        raise ValueError("MCP HTTP URL is invalid") from error
    return value


def _freeze_headers(value: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("MCP HTTP headers are invalid")
    result: dict[str, str] = {}
    seen: set[str] = set()
    for key, item in value.items():
        if (
            not isinstance(key, str) or not key or len(key) > 128
            or _HEADER_NAME.fullmatch(key) is None
            or key.casefold() in {
                "accept", "accept-encoding", "content-length", "content-type", "connection",
                "host", "keep-alive", "mcp-protocol-version", "mcp-session-id",
                "proxy-connection", "transfer-encoding", "upgrade",
            }
            or key.casefold() in seen
            or not isinstance(item, str) or len(item) > 4096
            or any(ord(char) < 32 or ord(char) == 127 for char in item)
            or any(ord(char) > 255 for char in item)
        ):
            raise ValueError("MCP HTTP headers are invalid")
        seen.add(key.casefold())
        result[key] = item
    return result


def _safe_alias(value: Any) -> str:
    if isinstance(value, str) and value and len(value) <= 64 and "\x00" not in value:
        return value
    return "unknown"


def _reject_non_json_number(value: str) -> None:
    raise ValueError(f"invalid JSON number {value}")


__all__ = ["DEFAULT_HTTP_TIMEOUT", "HttpTransport", "MAX_HTTP_RESPONSE_BYTES"]
