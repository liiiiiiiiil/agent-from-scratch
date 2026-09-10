import io

import pytest

from mini_agent.output import TerminalOutput


def rendered(callback, mode="normal"):
    stream = io.StringIO()
    output = TerminalOutput(mode, stream)
    callback(output)
    output.close()
    return stream.getvalue()


def test_assistant_title_is_lazy_and_streamed_once():
    text = rendered(lambda output: (
        output.assistant_end(),
        output.assistant_delta("你好"),
        output.assistant_delta("，世界"),
        output.assistant_end(),
    ))

    assert text == "助手 › 你好，世界\n\n"


def test_assistant_trailing_newlines_do_not_create_extra_blank_lines():
    text = rendered(lambda output: (
        output.assistant_delta("第一行\n"),
        output.assistant_end(),
        output.assistant_delta("第二行\n\n"),
        output.assistant_end(),
    ))

    assert text == "助手 › 第一行\n\n助手 › 第二行\n\n"


def test_split_newline_chunks_are_counted_as_one_section_boundary():
    text = rendered(lambda output: (
        output.assistant_delta("内容\n"),
        output.assistant_delta("\n"),
        output.assistant_end(),
        output.tools_start(["read_file"]),
    ))

    assert text == "助手 › 内容\n\n执行中 · read_file × 1\n\n"


def test_tool_batch_and_results_are_grouped_in_call_order():
    def write(output):
        calls = [
            {"function": {"name": "read_file", "arguments": '{"path": "a.txt"}'}},
            {"function": {"name": "read_file", "arguments": '{"path": "b.txt"}'}},
            {"function": {"name": "run_shell", "arguments": '{"command": "pytest"}'}},
        ]
        output.tools_start(calls)
        output.tool_result("read_file", {"path": "a.txt"}, "file", {"outcome": "succeeded"})
        output.tool_result("read_file", {"path": "b.txt"}, "file", {"outcome": "failed", "output_excerpt": "missing"})
        output.tool_result("run_shell", {"command": "pytest"}, "ok", None)

    text = rendered(write)
    assert "执行中 · read_file × 2，run_shell × 1" in text
    assert "完成 · read_file · path=a.txt" in text
    assert "失败 · read_file · path=b.txt · missing" in text
    assert "返回 · run_shell · command=pytest" in text
    assert text.index("path=a.txt") < text.index("path=b.txt") < text.index("command=pytest")
    assert "· file" not in text


def test_assistant_and_tool_sections_have_one_blank_line_between_them():
    def write(output):
        output.assistant_delta("先看一下。")
        output.assistant_end()
        output.tools_start([{"name": "read_file", "arguments": {"path": "x.py"}}])
        output.tool_result("read_file", {"path": "x.py"}, "body", {"outcome": "succeeded"})
        output.assistant_delta("已经看完。")
        output.assistant_end()

    assert rendered(write) == (
        "助手 › 先看一下。\n\n"
        "执行中 · read_file × 1\n"
        "  完成 · read_file · path=x.py\n\n"
        "助手 › 已经看完。\n\n"
    )


def test_normal_summary_is_single_line_bounded_and_omits_write_content():
    stream = io.StringIO()
    output = TerminalOutput(stream=stream)
    huge_path = "a" * 200 + "\nsecond"
    output.tool_result(
        "write_file",
        {"path": huge_path, "content": "SECRET BODY"},
        "written: SECRET BODY",
        {"outcome": "succeeded"},
    )
    text = stream.getvalue()
    line = text.splitlines()[0]
    assert len(line.split(" · ")[-1]) <= 100
    assert "SECRET BODY" not in text
    assert "\n" not in line


def test_normal_summary_only_exposes_file_paths_and_shell_commands():
    stream = io.StringIO()
    output = TerminalOutput(stream=stream)
    output.tool_result("list_dir", {"path": "secret"}, "entries", {"outcome": "succeeded"})
    output.tool_result("grep", {"pattern": "secret", "path": "secret"}, "matches", {"outcome": "succeeded"})
    text = stream.getvalue()
    assert "path=secret" not in text
    assert "pattern=secret" not in text


def test_structured_outcomes_and_failure_reason_are_bounded():
    stream = io.StringIO()
    output = TerminalOutput(stream=stream)
    for outcome in ("succeeded", "failed", "denied", "timeout", "invalid"):
        output.tool_result("tool", {}, "body", {"outcome": outcome, "output_excerpt": "x" * 400})
    text = stream.getvalue()
    assert "完成 · tool" in text
    assert "失败 · tool · " in text
    assert "拒绝 · tool · " in text
    assert "超时 · tool · " in text
    assert "无效 · tool · " in text
    for line in text.splitlines():
        if any(label in line for label in ("失败", "拒绝", "超时", "无效")):
            assert len(line.rsplit(" · ", 1)[-1]) <= 240


def test_debug_keeps_arguments_and_bounded_full_result():
    stream = io.StringIO()
    output = TerminalOutput("debug", stream)
    output.round_start(3)
    output.tools_start([{"name": "run_shell", "arguments": {"command": "pytest -q"}}])
    output.tool_result("run_shell", {"command": "pytest -q"}, "result\nline", {"outcome": "succeeded"})
    output.close()
    text = stream.getvalue()
    assert "[第 3 轮]" in text
    assert "工具: run_shell" in text
    assert '{"command": "pytest -q"}' in text
    assert "结果" not in text  # structured debug status remains concise
    assert "result\n    line" in text


def test_quiet_suppresses_progress_but_cli_notice_is_explicit():
    stream = io.StringIO()
    output = TerminalOutput("quiet", stream)
    output.round_start(1)
    output.assistant_delta("hidden")
    output.tools_start(["read_file"])
    output.status_notice("hidden status")
    output.cli_notice("当前任务已清空")
    assert stream.getvalue() == "当前任务已清空\n"


def test_broken_output_never_escapes_and_arguments_are_not_mutated():
    class Broken:
        def write(self, value):
            raise OSError("closed")

        def flush(self):
            raise OSError("closed")

    arguments = {"path": "x", "content": "body"}
    output = TerminalOutput(stream=Broken())
    output.assistant_delta("hello")
    output.tools_start([{"name": "write_file", "arguments": arguments}])
    output.tool_result("write_file", arguments, "done", {"outcome": "succeeded"})
    output.close()
    assert arguments == {"path": "x", "content": "body"}


def test_invalid_mode_is_rejected():
    with pytest.raises(ValueError):
        TerminalOutput("verbose")
