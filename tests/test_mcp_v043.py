"""Offline protocol, configuration, CLI, and child lifecycle coverage for v0.43."""
from __future__ import annotations

import io
import json
from pathlib import Path
import sys

import pytest

from mini_agent import config
from mini_agent.mcp import (
    McpClient,
    McpProtocolError,
    McpRemoteError,
    McpTimeoutError,
    McpTransportError,
)
from mini_agent.mcp.__main__ import main as mcp_main
from mini_agent.mcp.client import MAX_NOTIFICATIONS
from mini_agent.mcp.protocol import classify_message, decode_message, make_notification, make_request
from mini_agent.mcp.stdio import StdioTransport


FIXTURE = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"


def _server(mode: str = "normal", record: Path | None = None) -> dict[str, object]:
    command = [sys.executable, str(FIXTURE), "--mode", mode]
    if record is not None:
        command += ["--record", str(record)]
    return {"alias": "demo", "command": command, "cwd": str(FIXTURE.parent.parent.parent)}


def _methods(record: Path) -> list[str]:
    return record.read_text(encoding="utf-8").splitlines() if record.exists() else []


def test_handshake_pagination_call_and_close_are_ordered(tmp_path: Path):
    record = tmp_path / "methods.log"
    client = McpClient.connect(_server(record=record))
    try:
        tools = client.list_tools()
        assert [item["name"] for item in tools] == ["echo", "sum", "third"]
        result = client.call_tool("echo", {"text": "hello"})
        assert result["isError"] is False
        assert result["content"][0]["text"] == "hello"
    finally:
        client.close()
        client.close()
    assert _methods(record) == ["initialize", "notifications/initialized", "tools/list", "tools/list", "tools/call"]
    assert client.transport.returncode is not None


def test_notifications_are_distinguished_and_ignored(tmp_path: Path):
    client = McpClient.connect(_server("notify"))
    try:
        client.list_tools()
        assert "notifications/tools/list_changed" in client.notifications
    finally:
        client.close()


def test_tools_directory_is_frozen_after_first_list(tmp_path: Path):
    record = tmp_path / "methods.log"
    client = McpClient.connect(_server(record=record))
    try:
        first = client.list_tools()
        first[0]["name"] = "changed"
        second = client.list_tools()
        assert [tool["name"] for tool in second] == ["echo", "sum", "third"]
        assert _methods(record).count("tools/list") == 2
    finally:
        client.close()


def test_notification_history_is_bounded():
    client = McpClient.connect(_server("notify"))
    try:
        client.list_tools()
        for _ in range(MAX_NOTIFICATIONS + 10):
            client.call_tool("echo", {"text": "ok"})
        assert len(client.notifications) == MAX_NOTIFICATIONS
        assert all(item == "notifications/tools/list_changed" for item in client.notifications)
    finally:
        client.close()


@pytest.mark.parametrize(
    ("mode", "error_type"),
    [
        ("wrong-id", McpProtocolError),
        ("version-mismatch", McpProtocolError),
        ("missing-capability", McpProtocolError),
        ("bad-json", McpProtocolError),
        ("invalid-utf8", McpTransportError),
        ("huge", McpProtocolError),
        ("early-exit", McpTransportError),
        ("server-request", McpProtocolError),
    ],
)
def test_handshake_and_transport_failures_close_the_connection(mode: str, error_type: type[Exception]):
    with pytest.raises(error_type):
        McpClient.connect(_server(mode))


def test_response_with_result_and_error_is_rejected():
    client = McpClient.connect(_server("both-result-error"))
    try:
        with pytest.raises(McpProtocolError):
            client._request("tools/list", {}, timeout=1)
    finally:
        client.close()


def test_duplicate_cursor_rejects_whole_directory():
    client = McpClient.connect(_server("duplicate-cursor"))
    try:
        with pytest.raises(McpProtocolError):
            client.list_tools()
        assert not client.connected
    finally:
        client.close()


def test_is_error_is_a_valid_tool_result_and_json_rpc_error_is_distinct():
    client = McpClient.connect(_server("is-error"))
    try:
        client.list_tools()
        result = client.call_tool("echo", {})
        assert result["isError"] is True
    finally:
        client.close()

    client = McpClient.connect(_server("remote-error"))
    try:
        client.list_tools()
        with pytest.raises(McpRemoteError) as raised:
            client.call_tool("echo", {})
        assert raised.value.code == -32001
        assert "fixture failure" not in str(raised.value)
    finally:
        client.close()


def test_timeouts_stdout_queue_and_stderr_tail_are_bounded():
    with pytest.raises(McpTimeoutError):
        McpClient.connect(_server("silent"), startup_timeout=0.05, close_timeout=0.2)

    with pytest.raises((McpProtocolError, McpTransportError)):
        McpClient.connect(_server("stdout-flood"), startup_timeout=1, close_timeout=0.2)

    client = McpClient.connect(_server("stderr-flood"))
    try:
        assert len(client.transport.stderr_tail()) <= 16 * 1024
        assert client.list_tools()
    finally:
        client.close()


def test_request_timeout_is_one_deadline_shared_by_send_and_receive(monkeypatch):
    class FakeTransport:
        def __init__(self):
            self.send_timeout = None
            self.receive_timeout = None
            self.closed = False

        def send(self, _message, *, timeout):
            self.send_timeout = timeout

        def receive(self, *, timeout):
            self.receive_timeout = timeout
            return {"jsonrpc": "2.0", "id": 1, "result": {}}

        def close(self):
            self.closed = True

    client = McpClient(_server())
    transport = FakeTransport()
    client.transport = transport
    clock = iter((100.0, 103.0, 106.0))
    monkeypatch.setattr("mini_agent.mcp.client.time.monotonic", lambda: next(clock))

    assert client._request("tools/list", {}, timeout=10.0) == {}
    assert transport.send_timeout == pytest.approx(7.0)
    assert transport.receive_timeout == pytest.approx(4.0)


def test_interrupted_connect_closes_started_transport():
    class InterruptedTransport:
        closed = False

        def start(self):
            return self

        def send(self, _message, *, timeout):
            raise KeyboardInterrupt

        def close(self):
            self.closed = True

    client = McpClient(_server())
    transport = InterruptedTransport()
    client.transport = transport

    with pytest.raises(KeyboardInterrupt):
        client.connect()
    assert transport.closed is True


def test_protocol_helpers_build_and_reject_unpaired_messages():
    assert make_request(1, "initialize", {})["id"] == 1
    assert "id" not in make_notification("notifications/initialized")
    with pytest.raises(McpProtocolError):
        classify_message(json.loads(b'{"jsonrpc":"2.0","id":1,"result":{},"error":{}}'))
    with pytest.raises(McpTransportError):
        decode_message(b"\xff\n")
    for code in (True, 1.5, "\x1b[2J"):
        with pytest.raises(McpProtocolError):
            classify_message({
                "jsonrpc": "2.0", "id": 1,
                "error": {"code": code, "message": "bad"},
            })


def test_configuration_validation_and_relative_cwd(monkeypatch, tmp_path: Path):
    valid = [{"alias": "demo", "command": ["python", "server.py"], "cwd": ".", "environment": {"X": "1"}}]
    config.validate_mcp_servers(valid)
    monkeypatch.setattr(config, "MCP_SERVERS", valid)
    monkeypatch.setattr(config, "CONFIG_BASE_DIR", str(tmp_path))
    resolved = config.resolved_mcp_servers()
    assert resolved[0]["cwd"] == str(tmp_path)
    for invalid in (
        [{"alias": "Demo", "command": ["python"]}],
        [{"alias": "demo", "command": []}],
        [{"alias": "demo", "command": ["python"], "environment": {"X": 1}}],
        [{"alias": "demo", "command": ["python"], "extra": 1}],
    ):
        with pytest.raises(ValueError):
            config.validate_mcp_servers(invalid)


class _TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def _run_cli(monkeypatch, tmp_path: Path, mode: str, action: str, answer: str = "", *, interactive: bool = True):
    record = tmp_path / f"{mode}-{action}.log"
    server = _server(mode, record)
    monkeypatch.setattr(config, "MCP_SERVERS", [server])
    monkeypatch.setattr(config, "CONFIG_BASE_DIR", str(tmp_path))
    input_stream: io.StringIO = _TTY(answer) if interactive else io.StringIO(answer)
    output = io.StringIO()
    if action == "list":
        code = mcp_main(["demo", "list"], stdin=input_stream, stdout=output)
    else:
        code = mcp_main(["demo", "call", "echo", '{"text":"hi"}'], stdin=input_stream, stdout=output)
    return code, output.getvalue(), _methods(record)


def test_cli_rejects_eof_decline_and_noninteractive_without_tools_call(monkeypatch, tmp_path: Path):
    for answer, interactive in (("", True), ("no\n", True), ("yes\n", False)):
        code, output, methods = _run_cli(monkeypatch, tmp_path, "normal", "call", answer, interactive=interactive)
        assert code == 2
        assert "未发送 tools/call" in output
        assert "tools/call" not in methods


def test_cli_confirmation_sends_exactly_one_call(monkeypatch, tmp_path: Path):
    code, output, methods = _run_cli(monkeypatch, tmp_path, "normal", "call", "yes\n")
    assert code == 0
    assert "alias=demo" in output and "tool=echo" in output
    assert methods.count("tools/call") == 1


def test_cli_rejects_arguments_that_cannot_be_fully_previewed(monkeypatch, tmp_path: Path):
    record = tmp_path / "long-call.log"
    monkeypatch.setattr(config, "MCP_SERVERS", [_server(record=record)])
    output = io.StringIO()
    arguments = json.dumps({"text": "x" * (64 * 1024) + "unseen-tail"})
    code = mcp_main(
        ["demo", "call", "echo", arguments], stdin=_TTY("yes\n"), stdout=output,
    )
    assert code == 2
    assert "无法完整展示" in output.getvalue()
    assert "tools/call" not in _methods(record)


def test_cli_reports_incomplete_child_cleanup(monkeypatch, tmp_path: Path):
    record = tmp_path / "cleanup.log"
    monkeypatch.setattr(config, "MCP_SERVERS", [_server(record=record)])
    original_close = StdioTransport.close

    def report_failure(transport):
        original_close(transport)
        transport._close_report = {
            "alias": transport.alias, "closed": False, "reason": "child remains alive",
        }

    monkeypatch.setattr(StdioTransport, "close", report_failure)
    output = io.StringIO()
    code = mcp_main(["demo", "list"], stdin=io.StringIO(), stdout=output)
    assert code == 1
    assert "清理未完成" in output.getvalue()
    assert "child remains alive" in output.getvalue()


def test_cli_list_is_available_without_confirmation(monkeypatch, tmp_path: Path):
    code, output, methods = _run_cli(monkeypatch, tmp_path, "normal", "list", interactive=False)
    payload = json.loads(output)
    assert code == 0
    assert payload["total"] == 3
    assert "tools/call" not in methods
