"""Anthropic Messages protocol adapter."""

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


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return str(value or "")


def _parse_arguments(value: Any) -> str:
    if not isinstance(value, dict):
        raise ProviderProtocolError("Anthropic tool_use input 必须是 JSON object")
    arguments = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(arguments) > MAX_TOOL_ARGUMENT_CHARS:
        raise ProviderProtocolError("Anthropic tool_use input 超过大小上限")
    return arguments


def _usage(payload: Any) -> ProviderUsage:
    value = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(value, dict):
        return ProviderUsage(0, 0, "estimated")
    input_tokens = value.get("input_tokens")
    output_tokens = value.get("output_tokens")
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


def _tool_schema(tool_registry: Any) -> list[dict[str, Any]] | None:
    if tool_registry is None:
        return None
    value = tool_registry.schemas() if hasattr(tool_registry, "schemas") else tool_registry
    if not isinstance(value, list):
        raise ProviderProtocolError("工具 schema 必须是数组")
    result = []
    for item in value:
        if not isinstance(item, dict):
            raise ProviderProtocolError("工具 schema 结构非法")
        function = item.get("function")
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            raise ProviderProtocolError("工具 schema 缺少 function.name")
        result.append({
            "name": function["name"],
            "description": function.get("description", ""),
            "input_schema": function.get("parameters", {"type": "object", "properties": {}}),
        })
    return result


def _outgoing_messages(messages: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]]]:
    system_parts: list[str] = []
    outgoing: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            raise ProviderProtocolError("message 必须是对象")
        role = message.get("role")
        if role == "system":
            text = _content_text(message.get("content", ""))
            if text:
                system_parts.append(text)
            continue
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id.strip():
                raise ProviderProtocolError("tool result 缺少 tool_call_id")
            block = {"type": "tool_result", "tool_use_id": call_id,
                     "content": _content_text(message.get("content", ""))}
            if outgoing and outgoing[-1].get("role") == "user" and isinstance(outgoing[-1].get("content"), list):
                outgoing[-1]["content"].append(block)
            else:
                outgoing.append({"role": "user", "content": [block]})
            continue
        if role not in {"user", "assistant"}:
            raise ProviderProtocolError("Anthropic message role 不受支持")
        if role == "assistant" and message.get("tool_calls"):
            blocks: list[dict[str, Any]] = []
            text = _content_text(message.get("content", ""))
            if text:
                blocks.append({"type": "text", "text": text})
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict) or call.get("type") != "function":
                    raise ProviderProtocolError("assistant tool call 结构非法")
                call_id = call.get("id")
                function = call.get("function")
                if not isinstance(call_id, str) or not call_id.strip() or not isinstance(function, dict):
                    raise ProviderProtocolError("assistant tool call 缺少 ID 或 function")
                name = function.get("name")
                raw_args = function.get("arguments", "{}")
                if not isinstance(name, str) or not name.strip() or not isinstance(raw_args, str):
                    raise ProviderProtocolError("assistant tool call 参数非法")
                try:
                    input_value = json.loads(raw_args)
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    raise ProviderProtocolError("assistant tool call arguments 未完整闭合") from error
                if not isinstance(input_value, dict):
                    raise ProviderProtocolError("assistant tool call arguments 必须是 JSON object")
                blocks.append({"type": "tool_use", "id": call_id, "name": name, "input": input_value})
            outgoing.append({"role": "assistant", "content": blocks})
            continue
        content = message.get("content", "")
        if not isinstance(content, (str, list)):
            content = str(content)
        outgoing.append({"role": role, "content": content})
    return ("\n\n".join(system_parts) if system_parts else None), outgoing


def _parse_blocks(blocks: Any, *, strict: bool = True) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(blocks, list):
        raise ProviderProtocolError("Anthropic 响应 content 必须是数组")
    text_parts: list[str] = []
    calls: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            raise ProviderProtocolError("Anthropic content block 结构非法")
        kind = block.get("type")
        if kind == "text":
            if not isinstance(block.get("text"), str):
                raise ProviderProtocolError("Anthropic text block 非法")
            text_parts.append(block["text"])
        elif kind == "tool_use":
            call_id = block.get("id")
            name = block.get("name")
            if not isinstance(call_id, str) or not call_id.strip() or not isinstance(name, str) or not name.strip():
                raise ProviderProtocolError("Anthropic tool_use 缺少 ID 或 name")
            calls.append({
                "id": call_id, "type": "function",
                "function": {"name": name, "arguments": _parse_arguments(block.get("input"))},
            })
        elif strict:
            raise ProviderProtocolError("Anthropic content block 类型不受支持")
    if sum(len(item) for item in text_parts) > MAX_CONTENT_CHARS:
        raise ProviderProtocolError("Anthropic 响应 content 超过大小上限")
    result: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
    if calls:
        result["tool_calls"] = calls
    return result, calls


class AnthropicMessagesAdapter:
    """Encode/decode the Anthropic Messages endpoint."""

    def __init__(self, provider: Any, profile: Any) -> None:
        self.provider = provider
        self.profile = profile

    def _client(self, timeout: float | None) -> HTTPClient:
        return HTTPClient(
            self.provider.endpoint,
            timeout=self.provider.timeout_seconds if timeout is None else timeout,
            redactions=(self.provider.api_key, self.provider.endpoint, self.profile.model_id),
        )

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
        system, converted = _outgoing_messages(messages)
        payload: dict[str, Any] = {
            "model": self.profile.model_id,
            "messages": converted,
            "max_tokens": self.profile.max_output_tokens,
            "stream": stream,
        }
        if system is not None:
            payload["system"] = system
        if include_tools:
            tools = _tool_schema(tool_registry)
            if tools:
                payload["tools"] = tools
        try:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ProviderProtocolError("请求消息无法编码为 JSON") from error
        headers = dict(self.provider.extra_headers)
        headers.update({
            "x-api-key": self.provider.api_key,
            "anthropic-version": headers.get("anthropic-version", "2023-06-01"),
            "Accept": "text/event-stream" if stream else "application/json",
        })
        client = HTTPClient(
            self.provider.endpoint,
            timeout=self.provider.timeout_seconds if timeout is None else timeout,
            redactions=(self.provider.api_key, self.provider.endpoint, self.profile.model_id),
        )
        if not stream:
            response = client.json(body, headers=headers)
            if response.get("type") == "error" or response.get("error"):
                raise ProviderProtocolError("Anthropic 返回错误")
            message, _ = _parse_blocks(response.get("content"), strict=strict_tool_calls)
            return ProviderResponse(message, response.get("stop_reason"), _usage(response))

        blocks: dict[int, dict[str, Any]] = {}
        block_order: list[int] = []
        finish_reason = None
        usage_input = None
        usage_output = None
        saw_start = False
        saw_stop = False
        try:
            for data in client.stream(body, headers=headers):
                try:
                    event = json.loads(data)
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    raise ProviderStreamError("Anthropic SSE data 不是合法 JSON") from error
                if not isinstance(event, dict):
                    raise ProviderStreamError("Anthropic SSE data 必须是对象")
                kind = event.get("type")
                if kind == "message_start":
                    saw_start = True
                    message_usage = event.get("message", {}).get("usage", {})
                    if (isinstance(message_usage, dict)
                            and isinstance(message_usage.get("input_tokens"), int)
                            and not isinstance(message_usage.get("input_tokens"), bool)
                            and message_usage["input_tokens"] >= 0):
                        usage_input = message_usage["input_tokens"]
                elif kind == "content_block_start":
                    index = event.get("index")
                    block = event.get("content_block")
                    if isinstance(index, bool) or not isinstance(index, int) or not isinstance(block, dict):
                        raise ProviderStreamError("Anthropic content_block_start 结构非法")
                    block_type = block.get("type")
                    if block_type not in {"text", "tool_use"}:
                        raise ProviderStreamError("Anthropic content block 类型不受支持")
                    current = {
                        "type": block_type, "text": block.get("text", ""),
                        "id": block.get("id"), "name": block.get("name"),
                        "arguments": "", "started": True, "stopped": False,
                    }
                    if block_type == "text" and not isinstance(current["text"], str):
                        raise ProviderStreamError("Anthropic text block 非法")
                    blocks[index] = current
                    if index not in block_order:
                        block_order.append(index)
                elif kind == "content_block_delta":
                    index = event.get("index")
                    delta = event.get("delta")
                    if index not in blocks or not isinstance(delta, dict):
                        raise ProviderStreamError("Anthropic content_block_delta 缺少 block")
                    delta_type = delta.get("type")
                    current = blocks[index]
                    if delta_type == "text_delta":
                        text = delta.get("text")
                        if not isinstance(text, str):
                            raise ProviderStreamError("Anthropic text delta 非法")
                        current["text"] += text
                        if on_content is not None:
                            try:
                                on_content(text)
                            except Exception:
                                pass
                    elif delta_type == "input_json_delta":
                        partial = delta.get("partial_json")
                        if not isinstance(partial, str):
                            raise ProviderStreamError("Anthropic input_json_delta 非法")
                        current["arguments"] += partial
                        if len(current["arguments"]) > MAX_TOOL_ARGUMENT_CHARS:
                            raise ProviderStreamError("Anthropic tool arguments 超过大小上限")
                    else:
                        raise ProviderStreamError("Anthropic content delta 类型不受支持")
                elif kind == "content_block_stop":
                    index = event.get("index")
                    if index not in blocks:
                        raise ProviderStreamError("Anthropic content_block_stop 缺少 block")
                    blocks[index]["stopped"] = True
                elif kind == "message_delta":
                    delta = event.get("delta")
                    if not isinstance(delta, dict):
                        raise ProviderStreamError("Anthropic message_delta 结构非法")
                    if delta.get("stop_reason") is not None:
                        finish_reason = delta.get("stop_reason")
                    event_usage = event.get("usage")
                    if (isinstance(event_usage, dict)
                            and isinstance(event_usage.get("output_tokens"), int)
                            and not isinstance(event_usage.get("output_tokens"), bool)
                            and event_usage["output_tokens"] >= 0):
                        usage_output = event_usage["output_tokens"]
                elif kind == "message_stop":
                    saw_stop = True
                    break
                elif kind == "error":
                    raise ProviderStreamError("Anthropic 流式响应返回错误")
                else:
                    raise ProviderStreamError("Anthropic 返回了未知 SSE 事件")
        except ProviderStreamError:
            raise
        except (ProviderHTTPError, ProviderTimeoutError):
            raise
        except Exception as error:
            raise ProviderStreamError("Anthropic SSE 读取失败") from error
        if not saw_start or not saw_stop:
            raise ProviderStreamError("Anthropic SSE 在 message_stop 前中断")
        if any(not blocks[index]["stopped"] for index in block_order):
            raise ProviderStreamError("Anthropic content block 在结束前中断")
        normalized_blocks: list[dict[str, Any]] = []
        for index in block_order:
            current = blocks[index]
            if current["type"] == "text":
                normalized_blocks.append({"type": "text", "text": current["text"]})
            else:
                call_id = current.get("id")
                name = current.get("name")
                raw_arguments = current.get("arguments", "") or "{}"
                if not isinstance(call_id, str) or not call_id.strip() or not isinstance(name, str) or not name.strip():
                    raise ProviderStreamError("Anthropic tool_use 缺少 ID 或 name")
                try:
                    parsed = json.loads(raw_arguments)
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    raise ProviderStreamError("Anthropic input_json_delta 未完整闭合") from error
                if not isinstance(parsed, dict):
                    raise ProviderStreamError("Anthropic tool_use input 必须是 JSON object")
                normalized_blocks.append({"type": "tool_use", "id": call_id, "name": name, "input": parsed})
        message, _ = _parse_blocks(normalized_blocks, strict=strict_tool_calls)
        if usage_input is not None and usage_output is not None:
            usage = ProviderUsage(usage_input, usage_output, "provider")
        elif usage_input is not None or usage_output is not None:
            usage = ProviderUsage(usage_input or 0, usage_output or 0, "mixed")
        else:
            usage = ProviderUsage(0, 0, "estimated")
        return ProviderResponse(message, finish_reason, usage)
