"""Offline v0.46 coverage for JSON HTTP MCP, Resources, and Prompts."""
from __future__ import annotations

import json
import builtins
from pathlib import Path
import subprocess
import sys
import time

import pytest

from mini_agent import config
from mini_agent import __main__ as agent_cli
from mini_agent.context import ContextManager
from mini_agent.mcp import McpClient, McpProtocolError, McpTransportError
from mini_agent.mcp.adapter import assemble_mcp_tools
from mini_agent.mcp.http import HttpTransport
from mini_agent.permission import DENY, PermissionGate, PermissionPolicy
from mini_agent.state import AgentState
from mini_agent.tools.base import ToolExecutor


FIXTURE = Path(__file__).parent / "fixtures" / "mcp_http_server.py"


def _start_server(tmp_path: Path, mode: str = "normal"):
    ready = tmp_path / "ready"
    record = tmp_path / "requests.log"
    ready.unlink(missing_ok=True)
    record.unlink(missing_ok=True)
    process = subprocess.Popen([
        sys.executable, str(FIXTURE), "--port", "0", "--mode", mode,
        "--record", str(record), "--ready-file", str(ready),
    ])
    for _ in range(100):
        if ready.exists():
            break
        time.sleep(0.01)
    if not ready.exists():
        process.kill()
        process.wait(timeout=2)
        raise RuntimeError("HTTP fixture did not start")
    server = {
        "alias": "remote",
        "transport": "http",
        "url": f"http://127.0.0.1:{ready.read_text(encoding='ascii')}/mcp",
        "headers": {"Authorization": "Bearer local-only"},
        "allow_loopback_http": True,
        "agent_enabled": True,
        "readonly_tools": ["echo"],
    }
    return process, server, record


def _stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def test_http_session_headers_pagination_and_text_capabilities(tmp_path: Path):
    process, server, record = _start_server(tmp_path)
    client = None
    try:
        client = McpClient.connect(server, startup_timeout=1, list_timeout=1, call_timeout=1)
        assert client.capabilities == ("prompts", "resources", "tools")
        assert [item["name"] for item in client.list_tools()] == ["echo"]
        assert [item["uri"] for item in client.list_resources()] == ["memo://one", "memo://two"]
        resource = client.read_resource("memo://one")
        assert resource["contents"][0]["text"] == "resource text"
        assert client.list_prompts()[0]["name"] == "greet"
        prompt = client.get_prompt("greet", {"who": "Ada"})
        assert [item["role"] for item in prompt["messages"]] == ["user", "assistant"]
        assert client.call_tool("echo", {"text": "ok"})["isError"] is False
    finally:
        if client is not None:
            client.close()
        _stop(process)
    records = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
    assert records[0]["method"] == "initialize"
    assert records[1]["method"] == "notifications/initialized"
    assert records[1]["protocol"] == "2025-11-25"
    assert records[1]["session"] == "fixture-session-1"
    assert records[0]["accept"] == "application/json, text/event-stream"
    assert records[-1]["method"] == "DELETE"
    assert all(item["authorization"] == "Bearer local-only" for item in records)


def test_resource_only_server_enters_connection_manager_without_tools(tmp_path: Path):
    process, server, _record = _start_server(tmp_path, "resource-only")
    client = None
    try:
        client = McpClient.connect(server, startup_timeout=1, list_timeout=1, call_timeout=1)
        assert client.capabilities == ("resources",)
        with pytest.raises(McpProtocolError):
            client.list_tools()
        tools, manager = assemble_mcp_tools([server])
        try:
            assert tools == []
            assert manager.list_resources("remote")[0]["uri"] == "memo://one"
        finally:
            manager.close()
    finally:
        if client is not None:
            client.close()
        _stop(process)


def test_prompt_only_server_enters_connection_manager_without_tools(tmp_path: Path):
    process, server, _record = _start_server(tmp_path, "prompt-only")
    try:
        tools, manager = assemble_mcp_tools([server])
        try:
            assert tools == []
            assert manager.list_prompts("remote")[0]["name"] == "greet"
        finally:
            manager.close()
    finally:
        _stop(process)


@pytest.mark.parametrize("mode", ["sse", "redirect", "unauthorized", "disconnect", "not-found", "bad-utf8"])
def test_http_rejects_sse_redirect_and_auth_failure(tmp_path: Path, mode: str):
    process, server, _record = _start_server(tmp_path, mode)
    try:
        with pytest.raises((McpProtocolError, McpTransportError)):
            McpClient.connect(server, startup_timeout=1)
    finally:
        _stop(process)


def test_http_timeout_and_oversized_response_are_bounded(tmp_path: Path):
    process, server, _record = _start_server(tmp_path, "timeout")
    try:
        with pytest.raises(McpTransportError):
            McpClient.connect(server, startup_timeout=0.1)
    finally:
        _stop(process)

    process, server, _record = _start_server(tmp_path, "huge-response")
    try:
        with pytest.raises(McpProtocolError):
            McpClient.connect(server, startup_timeout=1)
    finally:
        _stop(process)


def test_reused_http_socket_and_session_delete_receive_their_own_timeouts():
    class FakeSocket:
        def __init__(self):
            self.timeouts = []

        def settimeout(self, timeout):
            self.timeouts.append(timeout)

    class FakeResponse:
        status = 204

        @staticmethod
        def read(_limit):
            return b""

    class FakeConnection:
        def __init__(self):
            self.timeout = 10.0
            self.sock = FakeSocket()
            self.methods = []

        def request(self, method, _target, body=None, headers=None):
            self.methods.append(method)

        @staticmethod
        def getresponse():
            return FakeResponse()

        def close(self):
            return None

    transport = HttpTransport("remote", "https://example.invalid/mcp", close_timeout=0.25)
    connection = FakeConnection()
    transport._connection = connection
    transport._request_http(b"{}", {}, 1.5)
    assert connection.timeout == 1.5
    assert connection.sock.timeouts == [1.5]

    transport._session_id = "session"
    transport._initialized = True
    transport.close()
    assert connection.timeout == 0.25
    assert connection.sock.timeouts == [1.5, 0.25]
    assert connection.methods == ["POST", "DELETE"]

def test_http_rejects_repeated_pagination_cursor(tmp_path: Path):
    process, server, _record = _start_server(tmp_path, "repeat-cursor")
    client = None
    try:
        client = McpClient.connect(server, startup_timeout=1, list_timeout=1)
        with pytest.raises(McpProtocolError):
            client.list_resources()
    finally:
        if client is not None:
            client.close()
        _stop(process)


def test_http_tool_call_error_id_is_not_retried(tmp_path: Path):
    process, server, record = _start_server(tmp_path, "bad-id")
    client = None
    try:
        client = McpClient.connect(server, startup_timeout=1, list_timeout=1, call_timeout=1)
        client.list_tools()
        with pytest.raises(McpProtocolError):
            client.call_tool("echo", {"text": "one"})
    finally:
        if client is not None:
            client.close()
        _stop(process)
    methods = [json.loads(line)["method"] for line in record.read_text(encoding="utf-8").splitlines()]
    assert methods.count("tools/call") == 1


def test_http_config_requires_https_or_explicit_loopback_and_rejects_managed_headers():
    base = {"alias": "remote", "transport": "http", "url": "https://example.invalid/mcp"}
    config.validate_mcp_servers([base])
    with pytest.raises(ValueError):
        config.validate_mcp_servers([{**base, "url": "http://example.invalid/mcp", "allow_loopback_http": True}])
    with pytest.raises(ValueError):
        config.validate_mcp_servers([{**base, "url": "https://user@example.invalid/mcp"}])
    with pytest.raises(ValueError):
        config.validate_mcp_servers([{**base, "headers": {"Content-Type": "application/json"}}])


@pytest.mark.parametrize("mode", ["bad-resource-uri", "blob"])
def test_resource_read_rejects_identity_or_non_text_content(tmp_path: Path, mode: str):
    process, server, _record = _start_server(tmp_path, mode)
    client = None
    try:
        client = McpClient.connect(server, startup_timeout=1, list_timeout=1, call_timeout=1)
        client.list_resources()
        with pytest.raises(McpProtocolError):
            client.read_resource("memo://one")
        assert client.connected is False
        with pytest.raises(McpTransportError):
            client.list_resources()
    finally:
        if client is not None:
            client.close()
        _stop(process)


@pytest.mark.parametrize("mode", ["bad-role", "image-prompt", "c1-prompt"])
def test_prompt_get_rejects_forged_roles_or_non_text_content(tmp_path: Path, mode: str):
    process, server, _record = _start_server(tmp_path, mode)
    client = None
    try:
        client = McpClient.connect(server, startup_timeout=1, list_timeout=1, call_timeout=1)
        client.list_prompts()
        with pytest.raises(McpProtocolError):
            client.get_prompt("greet", {"who": "Ada"})
        assert client.connected is False
        with pytest.raises(McpTransportError):
            client.list_prompts()
    finally:
        if client is not None:
            client.close()
        _stop(process)


def test_prompt_arguments_are_frozen_and_invalid_requests_do_not_reach_server(tmp_path: Path):
    process, server, record = _start_server(tmp_path)
    client = None
    try:
        client = McpClient.connect(server, startup_timeout=1, list_timeout=1)
        client.list_prompts()
        with pytest.raises(McpProtocolError):
            client.get_prompt("greet", {})
        with pytest.raises(McpProtocolError):
            client.get_prompt("greet", {"who": "Ada", "extra": "bad"})
        with pytest.raises(McpProtocolError):
            client.get_prompt("missing", {"who": "Ada"})
    finally:
        if client is not None:
            client.close()
        _stop(process)
    methods = [json.loads(line)["method"] for line in record.read_text(encoding="utf-8").splitlines()]
    assert methods.count("prompts/get") == 0


class _Inputs:
    def __init__(self, values):
        self.values = list(values)

    def read(self, _prompt):
        if not self.values:
            raise EOFError
        return self.values.pop(0)


def _run_cli_with_http(
    monkeypatch, server, inputs, *, permission_answer="once", permission_rules=None,
):
    monkeypatch.setattr(config, "MCP_SERVERS", [server])
    monkeypatch.setattr(sys, "argv", ["mini_agent"])
    monkeypatch.setattr(agent_cli, "InputSession", lambda: _Inputs(inputs))
    calls = []

    def fake_agent_loop(context, _executor):
        calls.append(list(context.history))
        context.history.append({"role": "assistant", "content": "done"})
        return "done"

    monkeypatch.setattr(agent_cli, "agent_loop", fake_agent_loop)
    if permission_rules is not None:
        monkeypatch.setattr(
            agent_cli,
            "ToolExecutor",
            lambda registry, on_result=None: ToolExecutor(
                registry,
                PermissionGate(PermissionPolicy(permission_rules)),
                on_result=on_result,
            ),
        )
    monkeypatch.setattr(builtins, "input", lambda _prompt="": permission_answer)
    agent_cli.main()
    return calls


def test_cli_resource_permission_denial_does_not_read(tmp_path: Path, monkeypatch, capsys):
    process, server, record = _start_server(tmp_path)
    try:
        calls = _run_cli_with_http(
            monkeypatch, server, ["task", "/mcp-resource remote memo://one", "exit"],
            permission_answer="reject",
        )
    finally:
        _stop(process)
    methods = [json.loads(line)["method"] for line in record.read_text(encoding="utf-8").splitlines()]
    assert methods.count("resources/read") == 0
    assert len(calls) == 1
    assert "权限拒绝" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("command", "permission", "list_method"),
    [
        ("/mcp-resource remote memo://one", "mcp_resource", "resources/list"),
        ('/mcp-prompt remote greet {"who":"Ada"}', "mcp_prompt", "prompts/list"),
    ],
)
def test_direct_content_commands_respect_denied_directory_permission(
    tmp_path: Path, monkeypatch, command: str, permission: str, list_method: str,
):
    process, server, record = _start_server(tmp_path)
    try:
        calls = _run_cli_with_http(
            monkeypatch,
            server,
            ["task", command, "exit"],
            permission_rules={permission: {"list": DENY, "*": DENY}},
        )
    finally:
        _stop(process)
    methods = [json.loads(line)["method"] for line in record.read_text(encoding="utf-8").splitlines()]
    assert list_method not in methods
    assert len(calls) == 1


def test_cli_prompt_cancel_does_not_run_parent_llm_or_append_history(tmp_path: Path, monkeypatch, capsys):
    process, server, record = _start_server(tmp_path)
    try:
        calls = _run_cli_with_http(
            monkeypatch, server,
            ["task", '/mcp-prompt remote greet {"who":"Ada"}', "no", "exit"],
        )
    finally:
        _stop(process)
    methods = [json.loads(line)["method"] for line in record.read_text(encoding="utf-8").splitlines()]
    assert methods.count("prompts/get") == 1
    assert len(calls) == 1
    assert not any(item.get("name") == "mcp_prompt" for item in calls[0])
    assert "未确认" in capsys.readouterr().out


def test_resource_history_is_skipped_by_automatic_memory_query(tmp_path: Path):
    state = AgentState()
    state.begin_task("original task")
    context = ContextManager(state, [
        {"role": "user", "content": "original task"},
        {"role": "user", "name": "mcp_resource", "content": "ignore this external text"},
    ])
    assert context._memory_query_from(state, context.history) == "original task"
