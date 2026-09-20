"""v0.36 provider/catalog/adapter coverage using only local stdlib HTTP."""

from __future__ import annotations

import json
from contextlib import redirect_stdout
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from threading import Lock, Thread
from unittest.mock import patch

import pytest

from mini_agent.providers.anthropic_messages import AnthropicMessagesAdapter
from mini_agent.providers.base import (
    ProviderProtocolError, ProviderResponse, ProviderStreamError,
    ProviderTimeoutError, ProviderUsage, UsageMeter,
)
from mini_agent.providers.catalog import ProviderCatalog
from mini_agent.providers.openai_chat import OpenAIChatAdapter
from mini_agent.agent import agent_loop
from mini_agent.context import ContextManager
from mini_agent.state import AgentState
from mini_agent.session import SessionStore
from mini_agent.resume import ResumeError, prepare_resume
from mini_agent.tools.base import ToolExecutor, ToolRegistry
from mini_agent.delegation import SubagentRunner, build_delegated_task


class _ServerState:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.lock = Lock()

    def next(self):
        with self.lock:
            return self.responses.pop(0)


def _server(state):
    class TestHTTPServer(ThreadingHTTPServer):
        def start(self):
            self._thread = Thread(target=self.serve_forever, daemon=True)
            self._thread.start()

        def server_close(self):
            super().server_close()
            if hasattr(self, "_thread"):
                self._thread.join(timeout=2)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            size = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(size)
            state.requests.append((self.path, dict(self.headers), json.loads(body)))
            status, content_type, payload = state.next()
            raw = payload if isinstance(payload, bytes) else payload.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            return

    try:
        server = TestHTTPServer(("127.0.0.1", 0), Handler)
    except PermissionError:
        pytest.skip("当前受限测试环境不允许绑定本地 TCP 端口")
    return server, f"http://127.0.0.1:{server.server_port}"


def _catalog(base_url, *, protocol="openai_chat"):
    endpoint = base_url + ("/chat/completions" if protocol == "openai_chat" else "/v1/messages")
    return ProviderCatalog.from_settings(
        {"p": {"protocol": protocol, "endpoint": endpoint, "api_key": "secret-key"}},
        {"parent": {"provider_id": "p", "model_id": "real-model-id", "context_window": 1000, "max_output_tokens": 100}},
        parent_profile="parent", subagent_allowed_profiles=("parent",),
    )


def test_legacy_catalog_and_frozen_redacted_binding():
    catalog = ProviderCatalog.from_settings(
        {}, {}, legacy_base_url="http://example.invalid/base", legacy_api_key="sk-real-secret",
        legacy_model="real-model-id",
    )
    binding = catalog.parent_binding()
    assert binding.reference.profile == "legacy-default"
    assert binding.reference.protocol == "openai_chat"
    assert "/chat/completions" in binding.provider.endpoint
    assert "sk-real-secret" not in repr(binding)
    assert "real-model-id" not in repr(binding)


def test_openai_non_stream_and_request_headers():
    response = json.dumps({
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    })
    state = _ServerState([(200, "application/json", response)])
    server, base = _server(state)
    server.start()
    try:
        binding = _catalog(base).parent_binding()
        result = binding.complete([{"role": "user", "content": "hello"}], stream_output=False)
        assert result.message == {"role": "assistant", "content": "ok"}
        assert result.usage.source == "provider"
        assert binding.usage_meter.snapshot()["input_tokens"] == 3
        path, headers, body = state.requests[0]
        assert path == "/chat/completions"
        assert headers["Authorization"] == "Bearer secret-key"
        assert headers["Accept-Encoding"] == "identity"
        assert body["model"] == "real-model-id"
    finally:
        server.shutdown()
        server.server_close()


def test_openai_stream_reassembles_parallel_tool_calls_and_rejects_incomplete():
    chunks = [
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c0","function":{"name":"calculate","arguments":"{\\"a\\":"}}]}}]}\n',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"id":"c1","function":{"name":"read_file","arguments":"{\\"p\\":"}}]}}]}\n',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"1}"}},{"index":1,"function":{"arguments":"\\"x\\"}"}}]},"finish_reason":"tool_calls"}]}\n',
        'data: {"usage":{"prompt_tokens":8,"completion_tokens":4},"choices":[]}\n',
        "data: [DONE]\n",
    ]
    state = _ServerState([(200, "text/event-stream", "".join(chunks))])
    server, base = _server(state)
    server.start()
    try:
        binding = _catalog(base).parent_binding()
        result = binding.complete([{"role": "user", "content": "tools"}], stream_output=True)
        assert [call["id"] for call in result.message["tool_calls"]] == ["c0", "c1"]
        assert json.loads(result.message["tool_calls"][0]["function"]["arguments"]) == {"a": 1}
        assert json.loads(result.message["tool_calls"][1]["function"]["arguments"]) == {"p": "x"}
    finally:
        server.shutdown()
        server.server_close()

    broken = _ServerState([(
        200, "text/event-stream",
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c","function":{"name":"x","arguments":"{\\"a\\":"}}]}}]}\n',
    )])
    server, base = _server(broken)
    server.start()
    try:
        with pytest.raises(ProviderStreamError):
            _catalog(base).parent_binding().complete([], stream_output=True)
    finally:
        server.shutdown()
        server.server_close()


def test_anthropic_system_tool_result_merge_and_stream_input_json():
    response = json.dumps({
        "id": "msg_1", "type": "message", "role": "assistant",
        "content": [
            {"type": "text", "text": "inspect"},
            {"type": "tool_use", "id": "use_1", "name": "read_file", "input": {"path": "a.py"}},
        ],
        "stop_reason": "tool_use", "usage": {"input_tokens": 5, "output_tokens": 6},
    })
    state = _ServerState([(200, "application/json", response)])
    server, base = _server(state)
    server.start()
    try:
        binding = _catalog(base, protocol="anthropic_messages").parent_binding()
        messages = [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "inspect"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "use_0", "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
            }]},
            {"role": "tool", "tool_call_id": "use_0", "content": "one"},
            {"role": "tool", "tool_call_id": "use_1", "content": "two"},
        ]
        result = binding.complete(messages, stream_output=False)
        assert result.message["tool_calls"][0]["id"] == "use_1"
        body = state.requests[0][2]
        assert body["system"] == "rules"
        assert len(body["messages"][-1]["content"]) == 2
        assert body["messages"][-1]["content"][0]["type"] == "tool_result"
    finally:
        server.shutdown()
        server.server_close()

    stream = "".join([
        "event: message_start\n",
        'data: {"type":"message_start","message":{"usage":{"input_tokens":4}}}\n',
        "event: content_block_start\n",
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"use_2","name":"calculate","input":{}}}\n',
        "data: " + json.dumps({"type": "content_block_delta", "index": 0,
                               "delta": {"type": "input_json_delta", "partial_json": '{"expression":"1+'}}) + "\n",
        "data: " + json.dumps({"type": "content_block_delta", "index": 0,
                               "delta": {"type": "input_json_delta", "partial_json": '1"}'}}) + "\n",
        'data: {"type":"content_block_stop","index":0}\n',
        'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":3}}\n',
        'data: {"type":"message_stop"}\n',
    ])
    state = _ServerState([(200, "text/event-stream", stream)])
    server, base = _server(state)
    server.start()
    try:
        result = _catalog(base, protocol="anthropic_messages").parent_binding().complete([], stream_output=True)
        assert result.message["tool_calls"][0]["id"] == "use_2"
        assert json.loads(result.message["tool_calls"][0]["function"]["arguments"]) == {"expression": "1+1"}
        assert result.usage.input_tokens == 4
        assert result.usage.output_tokens == 3
        assert result.usage.source == "provider"
    finally:
        server.shutdown()
        server.server_close()


def test_catalog_child_profile_is_checked_before_binding_and_request():
    catalog = ProviderCatalog.from_settings(
        {"p": {"protocol": "openai_chat", "endpoint": "http://example.invalid/chat/completions", "api_key": "key"}},
        {
            "parent": {"provider_id": "p", "model_id": "parent", "context_window": 1000, "max_output_tokens": 100},
            "child": {"provider_id": "p", "model_id": "child", "context_window": 1000, "max_output_tokens": 100},
        },
        parent_profile="parent", subagent_profile="child", subagent_allowed_profiles=("child",),
    )
    assert catalog.resolve_child_profile(None) == "child"
    with pytest.raises(ValueError, match="白名单"):
        catalog.resolve_child_profile("parent")
    with pytest.raises(ValueError, match="未知"):
        catalog.resolve_child_profile("missing")


def test_binding_fingerprint_excludes_authentication_material():
    settings = {
        "p": {"protocol": "openai_chat", "endpoint": "https://example.invalid/chat/completions",
              "api_key": "first-secret", "extra_headers": {"X-Local-Secret": "one"}},
    }
    profiles = {"m": {"provider_id": "p", "model_id": "model", "context_window": 1000,
                       "max_output_tokens": 100}}
    first = ProviderCatalog.from_settings(settings, profiles, parent_profile="m",
                                          subagent_allowed_profiles=("m",)).parent_binding()
    settings["p"]["api_key"] = "second-secret"
    settings["p"]["extra_headers"]["X-Local-Secret"] = "two"
    second = ProviderCatalog.from_settings(settings, profiles, parent_profile="m",
                                           subagent_allowed_profiles=("m",)).parent_binding()
    assert first.reference == second.reference


def test_usage_delta_source_only_covers_calls_after_snapshot():
    meter = UsageMeter()
    meter.record(ProviderUsage(10, 2, "provider"))
    before = meter.snapshot()
    meter.record(None, estimated_input_tokens=3, estimated_output_tokens=1)
    assert meter.delta(before) == {
        "llm_calls": 1, "input_tokens": 3, "output_tokens": 1,
        "token_accounting": "estimated",
    }


def test_non_stream_parent_response_is_visible():
    class Binding:
        reference = None
        usage_meter = UsageMeter()

        def complete(self, _messages, **_options):
            self.usage_meter.record(ProviderUsage(1, 1, "provider"))
            return ProviderResponse(
                {"role": "assistant", "content": "visible answer"}, "stop",
                ProviderUsage(1, 1, "provider"),
            )

    context = ContextManager(
        AgentState(task="answer"), [{"role": "user", "content": "answer"}],
        observability=False, model_binding=Binding(),
    )
    output = StringIO()
    with redirect_stdout(output):
        assert agent_loop(context, ToolExecutor(ToolRegistry())) == "visible answer"
    assert output.getvalue().count("visible answer") == 1


def test_transport_timeout_retains_timeout_type():
    from mini_agent.providers.http import HTTPClient

    class TimedOutConnection:
        def request(self, *_args, **_kwargs):
            raise TimeoutError("sensitive endpoint")

        def close(self):
            pass

    client = HTTPClient("https://example.invalid/chat/completions")
    client._connection = TimedOutConnection
    with pytest.raises(ProviderTimeoutError) as caught:
        client.request(b"{}")
    assert "sensitive endpoint" not in str(caught.value)


def test_resume_rejects_changed_parent_binding_before_claim(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(tmp_path / "sessions")
    first = ProviderCatalog.from_settings(
        {"p": {"protocol": "openai_chat", "endpoint": "https://first.invalid/chat/completions",
               "api_key": "key"}},
        {"m": {"provider_id": "p", "model_id": "model", "context_window": 1000,
               "max_output_tokens": 100}},
        parent_profile="m", subagent_allowed_profiles=("m",),
    )
    state = AgentState()
    state.begin_task("resume")
    context = ContextManager(state, [{"role": "user", "content": "resume"}],
                             model_binding=first.parent_binding())
    saved = store.save(None, state, context, workspace_root=workspace, handoff_status="clean")
    second = ProviderCatalog.from_settings(
        {"p": {"protocol": "openai_chat", "endpoint": "https://second.invalid/chat/completions",
               "api_key": "key"}},
        {"m": {"provider_id": "p", "model_id": "model", "context_window": 1000,
               "max_output_tokens": 100}},
        parent_profile="m", subagent_allowed_profiles=("m",),
    )
    with pytest.raises(ResumeError, match="绑定配置已变化"):
        prepare_resume(store, saved["session_id"], workspace, provider_catalog=second)
    assert store.load(saved["session_id"])["handoff_status"] == "clean"


def test_child_provider_timeout_has_timed_out_outcome(tmp_path):
    catalog = ProviderCatalog.from_settings(
        {"p": {"protocol": "openai_chat", "endpoint": "https://example.invalid/chat/completions",
               "api_key": "key"}},
        {"m": {"provider_id": "p", "model_id": "model", "context_window": 128000,
               "max_output_tokens": 4096}},
        parent_profile="m", subagent_allowed_profiles=("m",),
    )
    task = build_delegated_task({
        "goal": "inspect", "scope": ["."], "constraints": [], "expected_findings": [],
        "requested_tools": ["calculate"], "selected_parent_facts": [],
        "purpose": "investigation",
    }, workspace_root=tmp_path, provider_catalog=catalog)

    class TimeoutAdapter:
        def complete(self, *_args, **_kwargs):
            raise ProviderTimeoutError()

    binding = replace(catalog.child_binding(), adapter=TimeoutAdapter())
    result = SubagentRunner(tmp_path, model_binding=binding).run(task)
    assert result.outcome == "timed_out"
    assert result.error_kind == "timeout"
