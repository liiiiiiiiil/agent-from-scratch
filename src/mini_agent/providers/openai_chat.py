"""OpenAI-compatible Chat Completions adapter."""

from __future__ import annotations

import json
from typing import Any

from mini_agent.providers.base import (
    MAX_CONTENT_CHARS,
    MAX_TOOL_ARGUMENT_CHARS,
    ProviderHTTPError,
    ProviderProtocolError,
    ProviderResponse,
    ProviderStreamError,
    ProviderTimeoutError,
    ProviderUsage,
)
from mini_agent.providers.http import HTTPClient


def _json_bytes(payload: dict[str, Any]) -> bytes:
    try:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ProviderProtocolError("请求消息无法编码为 JSON") from error
    if len(body) > 16 * 1024 * 1024:
        raise ProviderProtocolError("请求 JSON 超过大小上限")
    return body


def _tools(tool_registry: Any) -> list[dict[str, Any]] | None:
    if tool_registry is None:
        return None
    if hasattr(tool_registry, "schemas"):
        value = tool_registry.schemas()
    else:
        value = tool_registry
    if not isinstance(value, list):
        raise ProviderProtocolError("工具 schema 必须是数组")
    return value


def _usage(payload: Any) -> ProviderUsage:
    value = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(value, dict):
        return ProviderUsage(0, 0, "estimated")
    input_tokens = value.get("prompt_tokens", value.get("input_tokens"))
    output_tokens = value.get("completion_tokens", value.get("output_tokens"))
    input_valid = not isinstance(input_tokens, bool) and isinstance(input_tokens, int) and input_tokens >= 0
    output_valid = not isinstance(output_tokens, bool) and isinstance(output_tokens, int) and output_tokens >= 0
    if not input_valid and not output_valid:
        return ProviderUsage(0, 0, "estimated")
    source = "provider" if input_valid and output_valid else "mixed"
    return ProviderUsage(
        input_tokens if input_valid else 0,
        output_tokens if output_valid else 0,
        source,
    )


def _call_from_delta(acc: dict[Any, dict[str, Any]], item: Any) -> None:
    if not isinstance(item, dict):
        index = len(acc)
        while index in acc:
            index += 1
        acc[index] = {"id": "", "type": "function", "function": item}
        return
    index = item.get("index", 0)
    try:
        hash(index)
    except TypeError:
        index = len(acc)
        while index in acc:
            index += 1
    slot = acc.setdefault(index, {
        "id": "", "type": "function", "function": {"name": "", "arguments": ""},
    })
    if item.get("id"):
        slot["id"] = item["id"]
    if "type" in item and item.get("type") is not None:
        slot["type"] = item["type"]
    if "function" not in item:
        return
    function = item.get("function")
    if not isinstance(slot.get("function"), dict):
        return
    if not isinstance(function, dict):
        slot["function"] = function
        return
    if "name" in function:
        slot["function"]["name"] = function["name"]
    if "arguments" in function:
        arguments = function["arguments"]
        if isinstance(arguments, str) and isinstance(slot["function"].get("arguments"), str):
            slot["function"]["arguments"] += arguments
        else:
            slot["function"]["arguments"] = arguments


def _validate_calls(calls: list[Any]) -> list[dict[str, Any]]:
    result = []
    for call in calls:
        if not isinstance(call, dict) or call.get("type") != "function":
            raise ProviderProtocolError("OpenAI tool call 结构不完整")
        call_id = call.get("id")
        function = call.get("function")
        if not isinstance(call_id, str) or not call_id.strip() or not isinstance(function, dict):
            raise ProviderProtocolError("OpenAI tool call 缺少 ID 或 function")
        name = function.get("name")
        arguments = function.get("arguments")
        if not isinstance(name, str) or not name.strip() or not isinstance(arguments, str):
            raise ProviderProtocolError("OpenAI tool call 参数不完整")
        if len(arguments) > MAX_TOOL_ARGUMENT_CHARS:
            raise ProviderProtocolError("OpenAI tool call arguments 超过大小上限")
        try:
            parsed = json.loads(arguments)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ProviderProtocolError("OpenAI tool call arguments 未完整闭合") from error
        if not isinstance(parsed, dict):
            raise ProviderProtocolError("OpenAI tool call arguments 必须是 JSON object")
        result.append({
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        })
    return result


def _message_from_payload(payload: dict[str, Any], *, strict_tool_calls: bool = True) -> ProviderResponse:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ProviderProtocolError("OpenAI 响应缺少 choices")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ProviderProtocolError("OpenAI 响应缺少 message")
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise ProviderProtocolError("OpenAI 响应 content 类型非法")
    result: dict[str, Any] = {"role": "assistant", "content": content}
    calls = message.get("tool_calls") or []
    if calls:
        result["tool_calls"] = _validate_calls(calls) if strict_tool_calls else calls
    return ProviderResponse(result, choice.get("finish_reason"), _usage(payload))


class OpenAIChatAdapter:
    """Encode/decode one OpenAI-compatible Chat Completions endpoint."""

    def __init__(self, provider: Any, profile: Any) -> None:
        self.provider = provider
        self.profile = profile

    def _client(self, timeout: float | None) -> HTTPClient:
        return HTTPClient(
            self.provider.endpoint,
            timeout=self.provider.timeout_seconds if timeout is None else timeout,
            redactions=(self.provider.api_key, self.provider.endpoint, self.profile.model_id),
        )

    def _request(self, messages, *, include_tools, tool_registry, stream, timeout):
        payload: dict[str, Any] = {
            "model": self.profile.model_id,
            "messages": messages,
            "stream": stream,
            "max_tokens": self.profile.max_output_tokens,
        }
        if include_tools:
            tools = _tools(tool_registry)
            if tools:
                payload["tools"] = tools
        headers = dict(self.provider.extra_headers)
        headers.update({
            "Authorization": f"Bearer {self.provider.api_key}",
            "Accept": "text/event-stream" if stream else "application/json",
        })
        return self._client(timeout), _json_bytes(payload), headers

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        include_tools: bool = True,
        tool_registry: Any = None,
        stream_output: bool = False,
        on_content: Any = None,
        timeout: float | None = None,
        strict_tool_calls: bool = True,
    ) -> ProviderResponse:
        if not isinstance(messages, list):
            raise ProviderProtocolError("messages 必须是数组")
        stream = bool(stream_output and self.profile.supports_streaming)
        client, body, headers = self._request(
            messages, include_tools=include_tools, tool_registry=tool_registry,
            stream=stream, timeout=timeout,
        )
        if not stream:
            return _message_from_payload(
                client.json(body, headers=headers), strict_tool_calls=strict_tool_calls,
            )

        content: list[str] = []
        calls: dict[Any, dict[str, Any]] = {}
        finish_reason = None
        usage = ProviderUsage(0, 0, "estimated")
        saw_event = False
        saw_done = False
        stream_error = None
        try:
            for data in client.stream(body, headers=headers):
                if data == "[DONE]":
                    saw_done = True
                    break
                saw_event = True
                try:
                    chunk = json.loads(data)
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    raise ProviderStreamError("OpenAI SSE data 不是合法 JSON") from error
                if not isinstance(chunk, dict):
                    raise ProviderStreamError("OpenAI SSE data 必须是对象")
                error = chunk.get("error")
                if error:
                    stream_error = "provider stream error"
                    continue
                choices = chunk.get("choices") or []
                if choices:
                    if not isinstance(choices[0], dict):
                        raise ProviderStreamError("OpenAI SSE choices 结构非法")
                    choice = choices[0]
                    if choice.get("finish_reason") is not None:
                        finish_reason = choice.get("finish_reason")
                    delta = choice.get("delta") or {}
                    if not isinstance(delta, dict):
                        raise ProviderStreamError("OpenAI SSE delta 结构非法")
                    piece = delta.get("content")
                    if piece is not None:
                        if not isinstance(piece, str):
                            raise ProviderStreamError("OpenAI SSE content 类型非法")
                        content.append(piece)
                        if on_content is not None:
                            try:
                                on_content(piece)
                            except Exception:
                                pass
                    for item in delta.get("tool_calls") or []:
                        _call_from_delta(calls, item)
                candidate_usage = _usage(chunk)
                if candidate_usage.source != "estimated":
                    usage = candidate_usage
            if not saw_done:
                raise ProviderStreamError("OpenAI SSE 在 [DONE] 前中断")
        except ProviderStreamError:
            raise
        except (ProviderHTTPError, ProviderTimeoutError):
            raise
        except Exception as error:
            raise ProviderStreamError("OpenAI SSE 读取失败") from error
        if stream_error:
            raise ProviderStreamError("服务商流式响应返回错误")
        if not saw_event:
            raise ProviderStreamError("服务商返回了空 SSE 响应")
        if len("".join(content)) > MAX_CONTENT_CHARS:
            raise ProviderStreamError("OpenAI 响应 content 超过大小上限")
        def call_index(value: Any):
            return (0, value) if isinstance(value, int) and not isinstance(value, bool) else (1, str(value))

        normalized_calls = list(calls[index] for index in sorted(calls, key=call_index))
        if strict_tool_calls:
            normalized_calls = _validate_calls(normalized_calls)
        result: dict[str, Any] = {"role": "assistant", "content": "".join(content) or None}
        if normalized_calls:
            result["tool_calls"] = normalized_calls
        return ProviderResponse(result, finish_reason, usage)
