import os
import sys
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mini_agent import __main__ as cli
import mini_agent.agent as agent_module
from mini_agent.output import TerminalOutput
from mini_agent.permission import ALLOW, DENY, PermissionGate, PermissionPolicy


class _Session:
    def __init__(self, values):
        self.values = iter(values)
        self.prompts = []

    def read(self, prompt):
        self.prompts.append(prompt)
        value = next(self.values)
        # Simulate the terminal echo that input()/prompt_toolkit normally emits.
        sys.stdout.write(value + "\n")
        return value


def _run_cli(values, loop, mode="normal"):
    session = _Session(values)
    with patch.object(cli, "InputSession", return_value=session), \
            patch.object(cli, "agent_loop", side_effect=loop), \
            patch.object(cli, "OUTPUT_MODE", mode), \
            patch.object(sys, "argv", ["mini_agent"]):
        output = StringIO()
        with redirect_stdout(output):
            cli.main()
    return session, output.getvalue()


def test_prompt_is_short_and_user_echo_has_a_blank_before_assistant():
    def loop(context, executor):
        rendered = TerminalOutput("normal")
        rendered.assistant_delta("回答")
        rendered.assistant_end()
        return "回答"

    session, text = _run_cli(["hello", "exit"], loop)
    assert session.prompts == ["你 › ", "你 › "]
    assert "hello\n\n助手 › 回答\n\n" in text
    assert "当前任务:" not in text


def test_new_reset_and_followup_keep_existing_state_boundaries():
    calls = []

    def loop(context, executor):
        calls.append((context.state.task, [m["content"] for m in context.history if m["role"] == "user"]))
        return "完成"

    _, text = _run_cli(["task A", "follow up", "/new task B", "/reset", "exit"], loop)
    assert calls == [
        ("task A", ["task A"]),
        ("task A", ["task A", "follow up"]),
        ("task B", ["task B"]),
    ]
    assert "已开始新任务。" in text
    assert "当前任务已清空。输入任务开始，或使用 /new <任务>。" in text

    _, usage_text = _run_cli(["/new", "exit"], loop)
    assert "用法: /new <任务>" in usage_text


def test_quiet_keeps_explicit_cli_notice_visible():
    def loop(context, executor):
        return "完成"

    _, text = _run_cli(["/reset", "exit"], loop, mode="quiet")
    assert "当前任务已清空。输入任务开始，或使用 /new <任务>。" in text


def test_quiet_permission_prompt_still_shows_prompt_and_answer_echo():
    output = StringIO()

    def echoed_input(prompt):
        sys.stdout.write(prompt + "always\n")
        return "always"

    gate = PermissionGate(PermissionPolicy({"write_file": "ask"}))
    with patch.object(cli, "OUTPUT_MODE", "quiet"), \
            patch("builtins.input", side_effect=echoed_input), redirect_stdout(output):
        assert gate.guard("write_file", {"path": "a.py"}) is None

    text = output.getvalue()
    assert "\n授权确认\n允许执行 write_file({'path': 'a.py'})? [once/always/reject] always\n\n" in text


def test_cli_status_notices_cover_max_blocked_and_failed():
    def max_loop(context, executor):
        return "达到最大迭代次数"

    _, max_text = _run_cli(["max", "exit"], max_loop)
    assert "达到最大迭代次数。" in max_text

    def blocked_loop(context, executor):
        context.state.status = "blocked"
        context.state.terminal_reason = "原因\n" + ("x" * 400)
        return "blocked"

    _, blocked_text = _run_cli(["blocked", "exit"], blocked_loop)
    assert "任务已阻塞：原因 " in blocked_text
    blocked_line = next(line for line in blocked_text.splitlines() if line.startswith("任务已阻塞："))
    assert "\n" not in blocked_line

    def failed_loop(context, executor):
        context.state.status = "failed"
        context.state.terminal_reason = "执行器失败"
        return "failed"

    _, failed_text = _run_cli(["failed", "exit"], failed_loop)
    assert "任务执行失败：执行器失败" in failed_text


def test_cli_real_agent_loop_streams_one_final_response_without_replay():
    def fake_call_llm(messages, **kwargs):
        kwargs["on_content"]("最终回答")
        return {"role": "assistant", "content": "最终回答"}

    session = _Session(["hello", "exit"])
    output = StringIO()
    with patch.object(cli, "InputSession", return_value=session), \
            patch.object(cli, "OUTPUT_MODE", "normal"), \
            patch.object(agent_module, "call_llm", side_effect=fake_call_llm), \
            patch.object(sys, "argv", ["mini_agent"]), redirect_stdout(output):
        cli.main()

    text = output.getvalue()
    assert text.count("助手 › 最终回答") == 1
    assert "hello\n\n助手 › 最终回答\n\n" in text


def test_permission_prompt_is_separate_and_preserves_always_once_reject_policy():
    output = StringIO()
    policy = PermissionPolicy({"write_file": "ask"})
    gate = PermissionGate(policy)
    with patch("builtins.input", side_effect=["always"]) as ask, redirect_stdout(output):
        assert gate.guard("write_file", {"path": "a.py", "content": "secret"}) is None
        assert gate.guard("write_file", {"path": "a.py", "content": "secret"}) is None
    text = output.getvalue()
    ask.assert_called_once_with(
        "\n授权确认\n允许执行 write_file({'path': 'a.py', 'content': 'secret'})? [once/always/reject] "
    )
    assert text == "\n"
    assert policy.check("write_file", "a.py") == ALLOW

    policy = PermissionPolicy({"write_file": "ask"})
    gate = PermissionGate(policy)
    with patch("builtins.input", side_effect=["once", "reject"]):
        assert gate.guard("write_file", {"path": "a.py"}) is None
        assert gate.guard("write_file", {"path": "b.py"}) is not None
    assert policy.check("write_file", "a.py") != ALLOW
    assert policy.check("write_file", "b.py") != ALLOW

    deny = PermissionGate(PermissionPolicy({"write_file": DENY}))
    with patch("builtins.input") as ask:
        assert deny.guard("write_file", {"path": "a.py"}) is not None
    ask.assert_not_called()
