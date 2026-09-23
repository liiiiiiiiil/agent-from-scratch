"""Offline parent Runtime integration coverage for v0.44 MCP Tools."""
from __future__ import annotations

from pathlib import Path
import json
import sys
from unittest.mock import patch

import pytest

from mini_agent import config
from mini_agent.agent import agent_loop
from mini_agent.context import ContextManager
import mini_agent.__main__ as agent_cli
from mini_agent.mcp import adapter
from mini_agent.mcp.adapter import assemble_mcp_tools
from mini_agent.mcp.schema import validate_mcp_arguments, validate_mcp_schema
from mini_agent.permission import ALLOW, DENY, PermissionGate, PermissionPolicy
from mini_agent.resume import prepare_resume
from mini_agent.session import DurableToolBoundary, SessionError, SessionStore
from mini_agent.state import AgentState
from mini_agent.tools import create_registry
from mini_agent.tools.base import ToolExecutor, ToolRegistry


FIXTURE = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"


def _server(mode: str = "normal", *, agent_enabled: bool = True) -> dict[str, object]:
    return {
        "alias": "demo",
        "command": [sys.executable, str(FIXTURE), "--mode", mode],
        "cwd": str(FIXTURE.parent.parent.parent),
        "agent_enabled": agent_enabled,
        "readonly_tools": ["echo"],
    }


def _executor(tools, policy=None):
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return ToolExecutor(
        registry,
        PermissionGate(PermissionPolicy(policy or {tool.name: ALLOW for tool in tools})),
    )


def test_config_v044_fields_default_and_validate_strictly(monkeypatch, tmp_path):
    base = [{"alias": "demo", "command": ["python", "server.py"]}]
    config.validate_mcp_servers(base)
    monkeypatch.setattr(config, "MCP_SERVERS", base)
    monkeypatch.setattr(config, "CONFIG_BASE_DIR", str(tmp_path))
    resolved = config.resolved_mcp_servers()[0]
    assert resolved["agent_enabled"] is False
    assert resolved["readonly_tools"] == []

    invalid_values = (
        {**base[0], "unknown": True},
        {**base[0], "agent_enabled": 1},
        {**base[0], "readonly_tools": ["echo", "echo"]},
        {**base[0], "readonly_tools": ["bad.name"]},
    )
    for invalid in invalid_values:
        with pytest.raises(ValueError):
            config.validate_mcp_servers([invalid])


def test_disabled_server_stays_out_of_parent_registry(monkeypatch):
    monkeypatch.setattr(config, "MCP_SERVERS", [{
        "alias": "disabled", "command": ["missing-mcp-server"],
        "agent_enabled": False,
    }])
    registry = create_registry(AgentState())
    assert not any(tool.name.startswith("mcp_") for tool in registry.list_tools())


def test_enabled_server_enters_registry_and_subagent_view_stays_fixed(monkeypatch):
    monkeypatch.setattr(config, "MCP_SERVERS", [_server()])
    registry = create_registry(AgentState())
    try:
        names = {tool.name for tool in registry.list_tools()}
        assert "mcp_demo_echo" in names
        assert registry.effect_for("mcp_demo_echo", {"text": "ok"}) == "none"
        child = registry.filtered_for_subagent({"mcp_demo_echo", "read_file"})
        assert "mcp_demo_echo" not in {tool.name for tool in child.list_tools()}
    finally:
        registry._mcp_manager.close()


def test_success_state_excerpt_has_metadata_only_and_permission_denial_sends_no_call(tmp_path):
    record = tmp_path / "methods.log"
    server = _server()
    server["command"] = [*server["command"], "--record", str(record)]
    tools, manager = assemble_mcp_tools([server])
    try:
        executor = _executor(tools, {"mcp_demo_echo": DENY})
        denied = executor.execute_result("mcp_demo_echo", {"text": "hello"})
        assert denied.outcome == "denied"
        assert "tools/call" not in record.read_text(encoding="utf-8")

        allowed = _executor(tools).execute_result("mcp_demo_echo", {"text": "secret"})
        assert allowed.outcome == "succeeded"
        assert allowed.output == "secret"
        assert "secret" not in allowed.output_excerpt
        assert '"source":"mcp"' in allowed.output_excerpt
    finally:
        manager.close()


@pytest.mark.parametrize(
    ("mode", "error_kind", "outcome"),
    [
        ("is-error", "mcp_tool_error", "failed"),
        ("remote-error", "mcp_jsonrpc_error", "failed"),
        ("call-disconnect", "mcp_disconnect", "failed"),
        ("image-result", "mcp_unsupported_content", "failed"),
        ("structured-result", "mcp_unsupported_content", "failed"),
    ],
)
def test_mcp_failures_are_controlled_results(mode, error_kind, outcome):
    tools, manager = assemble_mcp_tools([_server(mode)])
    try:
        result = _executor(tools).execute_result("mcp_demo_echo", {})
        assert result.error_kind == error_kind
        assert result.outcome == outcome
        assert result.handler_admitted is True
        assert '"alias":"demo"' in result.tool_content()
    finally:
        manager.close()


def test_timeout_has_timeout_outcome_and_invalid_schema_never_registers(monkeypatch):
    original_connect = adapter.McpClient.connect

    def short_connect(server):
        return original_connect(server, call_timeout=0.05)

    monkeypatch.setattr(adapter.McpClient, "connect", short_connect)
    tools, manager = assemble_mcp_tools([_server("call-silent")])
    try:
        result = _executor(tools).execute_result("mcp_demo_echo", {})
        assert result.outcome == "timeout"
        assert result.error_kind == "mcp_timeout"
    finally:
        manager.close()

    with pytest.raises(ValueError):
        validate_mcp_schema({"type": "object", "properties": {"nested": {"type": "object"}}})
    with pytest.raises(ValueError):
        validate_mcp_schema({"type": "object", "properties": {"name": {"type": "string", "pattern": ".*"}}})
    schema = validate_mcp_schema({
        "type": "object",
        "properties": {"name": {"type": "string", "minLength": 2}},
        "required": ["name"],
        "additionalProperties": False,
    })
    with pytest.raises(ValueError):
        validate_mcp_arguments(schema, {"name": "x"})


def test_normalized_name_collision_closes_all_started_clients(monkeypatch):
    class FakeClient:
        def __init__(self, alias, tools):
            self.alias = alias
            self._tools = tools
            self.closed = False
            self.close_report = {"alias": alias, "closed": True}

        @property
        def tools(self):
            return tuple(self._tools)

        def list_tools(self):
            return list(self._tools)

        def close(self):
            self.closed = True

        def call_tool(self, _name, _arguments):
            return {"isError": False, "content": [{"type": "text", "text": "ok"}]}

    clients = []

    def connect(server):
        client = FakeClient(server["alias"], [
            {"name": "read-only", "inputSchema": {"type": "object"}},
            {"name": "read_only", "inputSchema": {"type": "object"}},
        ])
        clients.append(client)
        return client

    monkeypatch.setattr(adapter.McpClient, "connect", connect)
    servers = [{"alias": "one", "command": ["x"], "agent_enabled": True}]
    with pytest.raises(ValueError, match="碰撞"):
        assemble_mcp_tools(servers)
    assert all(client.closed for client in clients)


def test_interrupted_multi_server_assembly_closes_prior_clients(monkeypatch):
    class FakeClient:
        alias = "one"

        def __init__(self):
            self.closed = False

        def supports(self, _name):
            return False

        def close(self):
            self.closed = True

        @property
        def close_report(self):
            return {"alias": self.alias, "closed": self.closed}

    first = FakeClient()
    calls = iter((first, KeyboardInterrupt()))

    def connect(_server):
        result = next(calls)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(adapter.McpClient, "connect", connect)
    servers = [
        {"alias": "one", "command": ["x"], "agent_enabled": True},
        {"alias": "two", "command": ["x"], "agent_enabled": True},
    ]
    with pytest.raises(KeyboardInterrupt):
        assemble_mcp_tools(servers)
    assert first.closed is True


def _durable_mcp(tmp_path, record, *, readonly=False):
    server = _server()
    server["readonly_tools"] = ["echo", "sum"] if readonly else []
    server["command"] = [*server["command"], "--record", str(record)]
    tools, manager = assemble_mcp_tools([server])
    state = AgentState()
    state.begin_task("MCP durable boundary")
    context = ContextManager(
        state, [{"role": "user", "content": "call MCP"}], observability=False,
    )
    store = SessionStore(tmp_path / "sessions")
    envelope = store.save(None, state, context, workspace_root=tmp_path)
    executor = _executor(tools)
    executor.session_boundary = DurableToolBoundary(store, envelope["session_id"], tmp_path)
    return manager, context, store, envelope, executor


def _mcp_calls(*calls):
    return {
        "role": "assistant", "content": None,
        "tool_calls": [
            {"id": f"mcp-{index}", "type": "function", "function": {
                "name": name, "arguments": json.dumps(arguments),
            }}
            for index, (name, arguments) in enumerate(calls)
        ],
    }


def test_mcp_admission_commit_failure_sends_no_call(tmp_path):
    record = tmp_path / "methods.log"
    manager, context, store, envelope, executor = _durable_mcp(tmp_path, record)
    try:
        def fail_admission(*_args, **_kwargs):
            raise SessionError("admission commit failed")

        executor.session_boundary.record_admission = fail_admission
        response = _mcp_calls(("mcp_demo_echo", {"text": "once"}))
        with patch("mini_agent.agent.call_llm", return_value=response):
            with pytest.raises(SessionError, match="admission commit failed"):
                agent_loop(context, executor)
        assert "tools/call" not in record.read_text(encoding="utf-8")
        assert store.load(envelope["session_id"])["tool_boundary"]["calls"][0]["handler_admitted"] is False
    finally:
        manager.close()


def test_mcp_result_commit_failure_recovers_without_replay(tmp_path):
    record = tmp_path / "methods.log"
    manager, context, store, envelope, executor = _durable_mcp(tmp_path, record)
    try:
        def fail_result(*_args, **_kwargs):
            raise SessionError("result commit failed")

        executor.session_boundary.record_execution_result = fail_result
        response = _mcp_calls(("mcp_demo_echo", {"text": "once"}))
        with patch("mini_agent.agent.call_llm", return_value=response):
            with pytest.raises(SessionError, match="result commit failed"):
                agent_loop(context, executor)
        before = record.read_text(encoding="utf-8").splitlines().count("tools/call")
        assert before == 1
        pending = store.load(envelope["session_id"])["tool_boundary"]["calls"][0]
        assert pending["handler_admitted"] is True
        assert pending["status"] == "pending"
    finally:
        manager.close()

    with prepare_resume(store, envelope["session_id"], tmp_path) as candidate:
        resumed = candidate.claim()
    assert resumed.recovery_mode == "crash_recovery"
    assert resumed.state.crash_issues[0].classification != "not_executed"
    assert record.read_text(encoding="utf-8").splitlines().count("tools/call") == before
    resumed.registry._mcp_manager.close()


def test_mcp_parallel_results_return_in_model_order(tmp_path):
    record = tmp_path / "methods.log"
    manager, context, store, envelope, executor = _durable_mcp(tmp_path, record, readonly=True)
    try:
        responses = [
            _mcp_calls(
                ("mcp_demo_echo", {"text": "first"}),
                ("mcp_demo_sum", {"a": 1, "b": 2}),
            ),
            {"role": "assistant", "content": "done"},
        ]
        with patch("mini_agent.agent.call_llm", side_effect=lambda *_a, **_k: responses.pop(0)):
            assert agent_loop(context, executor) == "done"
        saved = store.load(envelope["session_id"])
        assert [call["tool_call_id"] for call in saved["tool_boundary"]["calls"]] == [
            "mcp-0", "mcp-1",
        ]
        assert [message["tool_call_id"] for message in saved["context"]["history"][-2:]] == [
            "mcp-0", "mcp-1",
        ]
        assert all(call["status"] == "committed" for call in saved["tool_boundary"]["calls"])
    finally:
        manager.close()


@pytest.mark.parametrize("inputs", [[EOFError()], ["/reset", EOFError()]])
def test_idle_cli_exit_closes_every_mcp_connection(monkeypatch, inputs):
    monkeypatch.setattr(config, "MCP_SERVERS", [_server()])
    monkeypatch.setattr(sys, "argv", ["mini_agent"])
    real_create = agent_cli.create_registry
    registries = []

    def capture_registry(*args, **kwargs):
        registry = real_create(*args, **kwargs)
        registries.append(registry)
        return registry

    monkeypatch.setattr(agent_cli, "create_registry", capture_registry)
    def next_input(_self, _prompt):
        value = inputs.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(agent_cli.InputSession, "read", next_input)
    agent_cli.main()
    assert len(registries) >= 1
    assert all(registry._mcp_manager.closed for registry in registries)
    assert all(
        client.transport.returncode is not None
        for registry in registries for client in registry._mcp_manager.clients
    )


def test_invalid_parent_registry_configuration_does_not_start_mcp(monkeypatch):
    monkeypatch.setattr(config, "MCP_SERVERS", [_server()])
    monkeypatch.setattr(config, "REFERENCES", [{"alias": "bad"}])
    started = []

    def unexpected_connect(_server):
        started.append(True)
        raise AssertionError("MCP must not start before local registry assembly")

    monkeypatch.setattr(adapter.McpClient, "connect", unexpected_connect)
    with pytest.raises((KeyError, ValueError)):
        create_registry(AgentState())
    assert started == []


def test_abandoned_resume_candidate_can_close_its_mcp_clients(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MCP_SERVERS", [_server()])
    state = AgentState()
    state.begin_task("resume candidate")
    context = ContextManager(state, [{"role": "user", "content": "task"}], observability=False)
    store = SessionStore(tmp_path / "sessions")
    envelope = store.save(None, state, context, workspace_root=tmp_path, handoff_status="clean")
    candidate = prepare_resume(store, envelope["session_id"], tmp_path)
    clients = candidate._runtime.registry._mcp_manager.clients
    assert clients and clients[0].connected
    assert candidate.close()["closed"] is True
    assert all(client.transport.returncode is not None for client in clients)
    assert candidate.close()["closed"] is True
