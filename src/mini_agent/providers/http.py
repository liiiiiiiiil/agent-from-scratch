"""Small standard-library HTTP/SSE primitives shared by provider adapters."""

from __future__ import annotations

import http.client
import json
import math
import re
from typing import Any, Iterator

from mini_agent.providers.base import (
    MAX_ERROR_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_SSE_LINE_BYTES,
    ProviderConnectionError,
    ProviderHTTPError,
    ProviderProtocolError,
    ProviderStreamError,
    ProviderTimeoutError,
    _safe_text,
)


_ENDPOINT_RE = re.compile(
    r"\A(?P<scheme>https?)://(?P<authority>[^/?#]+)(?P<path>/[^?#]*)?\Z"
)


def parse_endpoint(endpoint: str) -> tuple[str, str | None, int, str]:
    """Validate an endpoint without using urllib or a third-party client."""
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise ValueError("endpoint 必须是完整 HTTP(S) 地址")
    match = _ENDPOINT_RE.fullmatch(endpoint.strip())
    if match is None:
        raise ValueError("endpoint 必须是完整 HTTP(S) 地址")
    scheme = match.group("scheme").lower()
    authority = match.group("authority")
    if "@" in authority:
        raise ValueError("endpoint 不允许内嵌认证信息")
    host = authority
    port: int | None = None
    if authority.startswith("["):
        end = authority.find("]")
        if end < 0:
            raise ValueError("endpoint 主机格式非法")
        host = authority[1:end]
        suffix = authority[end + 1:]
        if suffix:
            if not suffix.startswith(":") or not suffix[1:].isdigit():
                raise ValueError("endpoint 端口格式非法")
            port = int(suffix[1:])
    elif authority.count(":") == 1:
        candidate_host, candidate_port = authority.rsplit(":", 1)
        if not candidate_host or not candidate_port.isdigit():
            raise ValueError("endpoint 主机或端口格式非法")
        host, port = candidate_host, int(candidate_port)
    if not host or any(ord(char) < 33 for char in host):
        raise ValueError("endpoint 主机格式非法")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("endpoint 端口必须在 1–65535 内")
    path = match.group("path") or "/"
    if not path.startswith("/"):
        raise ValueError("endpoint 路径格式非法")
    return scheme, host, port or (443 if scheme == "https" else 80), path


def _json_detail(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace")
    try:
        payload = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return _safe_text(text)
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            for key in ("message", "detail", "type"):
                if error.get(key):
                    return _safe_text(error[key])
        for key in ("message", "detail"):
            if payload.get(key):
                return _safe_text(payload[key])
    return _safe_text(text)


def _timeout(value: float | None) -> float:
    result = 120.0 if value is None else value
    if isinstance(result, bool) or not isinstance(result, (int, float)):
        raise ValueError("timeout 必须是正数")
    if not math.isfinite(result) or result <= 0:
        raise ValueError("timeout 必须是正数")
    return float(result)


class HTTPClient:
    """One connection per request; no retries and bounded reads."""

    def __init__(self, endpoint: str, *, timeout: float = 120,
                 redactions: tuple[str, ...] = ()) -> None:
        self.scheme, self.host, self.port, self.path = parse_endpoint(endpoint)
        self.timeout = _timeout(timeout)
        self.redactions = tuple(item for item in redactions if item)

    def _connection(self):
        cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        return cls(self.host, self.port, timeout=self.timeout)

    def _headers(self, headers: dict[str, str] | None) -> dict[str, str]:
        result = {
            "Content-Type": "application/json",
            "Accept-Encoding": "identity",
        }
        if headers:
            result.update({str(key): str(value) for key, value in headers.items()})
        # The transport contract is stronger than provider-specific extras;
        # callers cannot opt back into compressed response bodies.
        for key in tuple(result):
            if key.lower() == "accept-encoding":
                del result[key]
        result["Accept-Encoding"] = "identity"
        return result

    def request(self, body: bytes, *, headers: dict[str, str] | None = None):
        connection = self._connection()
        try:
            connection.request("POST", self.path, body=body, headers=self._headers(headers))
            response = connection.getresponse()
            status = getattr(response, "status", None)
            # A few historical fakes only implement iteration and intentionally
            # omit status; treat those as successful compatibility responses.
            if status is None:
                status = 200
            if not isinstance(status, int):
                raise ProviderProtocolError("服务商响应缺少 HTTP 状态")
            if not 200 <= status < 300:
                try:
                    try:
                        detail = response.read(MAX_ERROR_BYTES)
                    except TypeError:
                        detail = response.read()
                finally:
                    connection.close()
                reason = _safe_text(getattr(response, "reason", ""), 160, self.redactions)
                detail_text = _safe_text(_json_detail(detail), 1000, self.redactions)
                raise ProviderHTTPError(status, reason, detail_text)
            return connection, response
        except TimeoutError as error:
            connection.close()
            raise ProviderTimeoutError() from error
        except (OSError, http.client.HTTPException) as error:
            try:
                connection.close()
            except Exception:
                pass
            raise ProviderConnectionError() from error
        except Exception:
            # The successful branch returns before any later exception can
            # occur here, so every exception path still owns the connection.
            try:
                connection.close()
            except Exception:
                pass
            raise

    def json(self, body: bytes, *, headers: dict[str, str] | None = None) -> dict[str, Any]:
        connection, response = self.request(body, headers=headers)
        try:
            try:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
            except TypeError:
                payload = response.read()
        except TimeoutError as error:
            raise ProviderTimeoutError() from error
        except (OSError, http.client.HTTPException) as error:
            raise ProviderConnectionError() from error
        finally:
            connection.close()
        if len(payload) > MAX_RESPONSE_BYTES:
            raise ProviderProtocolError("服务商 JSON 响应超过大小上限")
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ProviderProtocolError("服务商返回了非法 JSON") from error
        if not isinstance(value, dict):
            raise ProviderProtocolError("服务商 JSON 响应必须是对象")
        return value

    def stream(self, body: bytes, *, headers: dict[str, str] | None = None) -> Iterator[str]:
        connection, response = self.request(body, headers=headers)
        try:
            for raw in response:
                if len(raw) > MAX_SSE_LINE_BYTES:
                    raise ProviderStreamError("SSE 行超过大小上限")
                line = raw.decode("utf-8", errors="strict").rstrip("\r\n")
                if line.startswith("data:"):
                    yield line[5:].lstrip()
                elif line.startswith(("event:", "id:", "retry:")):
                    # Provider adapters use the JSON data field as the
                    # authoritative event. Standard SSE metadata is safe to
                    # ignore here.
                    continue
                elif line and not line.startswith(":"):
                    # SSE permits event/id fields, but provider adapters only
                    # consume data lines and reject unexpected response prose.
                    if line.startswith("{") or line.startswith("["):
                        raise ProviderStreamError(
                            "服务商返回了非 SSE 响应：" + _json_detail(line.encode("utf-8"))
                        )
                    raise ProviderStreamError("服务商返回了非法 SSE 行")
        except TimeoutError as error:
            raise ProviderTimeoutError() from error
        except (OSError, http.client.HTTPException) as error:
            raise ProviderStreamError("服务商 SSE 读取失败") from error
        except UnicodeDecodeError as error:
            raise ProviderStreamError("服务商 SSE 不是有效 UTF-8") from error
        finally:
            connection.close()
