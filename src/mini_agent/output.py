"""Small, best-effort terminal presentation layer for the agent loop.

The output layer deliberately knows nothing about the executor implementation.  A
structured execution result is accepted through a small duck-typed interface
(``outcome`` and, for failure details, ``output_excerpt``/``output``), which keeps
this module safe to import from either the CLI or the agent loop.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any, TextIO


_MODES = {"normal", "debug", "quiet"}
_OUTCOME_LABELS = {
    "succeeded": "完成",
    "failed": "失败",
    "denied": "拒绝",
    "timeout": "超时",
    "invalid": "无效",
}
_MAX_ARGUMENT_SUMMARY = 100
_MAX_DEBUG_RESULT = 1200
_MAX_FAILURE_REASON = 240


def _text(value: Any) -> str:
    try:
        return str(value)
    except Exception:
        return "<unavailable>"


def _clip(value: Any, limit: int) -> str:
    text = _text(value)
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1] + "…"


def _single_line(value: Any) -> str:
    return " ".join(_text(value).split())


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _call_name(call: Any) -> str:
    if isinstance(call, str):
        return call
    name = _get(call, "name")
    if name is None:
        function = _get(call, "function")
        name = _get(function, "name") if function is not None else None
    return _single_line(name or "<missing>")


def _call_arguments(call: Any) -> Any:
    arguments = _get(call, "arguments")
    if arguments is not None:
        return arguments
    function = _get(call, "function")
    return _get(function, "arguments") if function is not None else None


def _json_or_text(value: Any) -> str:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return value
        value = parsed
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=_text)
    except Exception:
        return _text(value)


def _argument_dict(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, Mapping):
        return dict(arguments)
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _argument_summary(name: str, arguments: Any) -> str:
    """Return only a safe path/command summary for normal mode.

    In particular, write/edit content is intentionally ignored.  The returned
    text is a single line and is bounded independently from the surrounding
    status line.
    """
    values = _argument_dict(arguments)
    if name in {"read_file", "write_file", "edit_file"} and values.get("path") is not None:
        return _clip(f"path={_single_line(values['path'])}", _MAX_ARGUMENT_SUMMARY)
    if name == "run_shell" and values.get("command") is not None:
        return _clip(f"command={_single_line(values['command'])}", _MAX_ARGUMENT_SUMMARY)
    return ""


def _debug_arguments(arguments: Any) -> str:
    return _clip(_single_line(_json_or_text(arguments)), _MAX_DEBUG_RESULT)


def _bounded_result(value: Any) -> str:
    return _clip(value, _MAX_DEBUG_RESULT)


class TerminalOutput:
    """Render agent progress without allowing terminal failures to escape.

    ``stream`` is injectable for tests and redirected output.  Methods are safe
    to call from the loop even when the stream has been closed or is otherwise
    broken.  Input arguments are inspected only; they are never normalized in
    place.
    """

    def __init__(self, mode: str = "normal", stream: TextIO | None = None):
        mode = _text(mode).lower()
        if mode not in _MODES:
            raise ValueError(f"unknown output mode: {mode}")
        self.mode = mode
        self.stream = stream if stream is not None else sys.stdout
        self._section: str | None = None
        self._line_open = False
        self._assistant_open = False
        self._assistant_tail_newlines = 0

    def _write(self, value: Any, *, flush: bool = False) -> None:
        try:
            self.stream.write(_text(value))
            if flush:
                self.stream.flush()
        except Exception:
            # Terminal output is observational and must never break execution.
            return

    def _start_section(self, section: str) -> None:
        if self._section == section:
            return
        if self._section is not None:
            self._close_section()
        self._section = section
        self._line_open = False

    def _close_section(self) -> None:
        if self._section is None:
            return
        if self._section == "assistant" and self._assistant_open:
            # Stream chunks may already contain line endings.  Add only enough
            # to leave one blank line between the assistant and the next block.
            self._write("\n" * max(0, 2 - self._assistant_tail_newlines))
            self._section = None
            self._line_open = False
            self._assistant_open = False
            self._assistant_tail_newlines = 0
            return
        if self._line_open:
            self._write("\n")
        self._write("\n")
        self._section = None
        self._line_open = False
        self._assistant_open = False

    def _line(self, value: Any, *, flush: bool = False) -> None:
        self._write(value)
        self._write("\n", flush=flush)
        self._line_open = False

    def assistant_delta(self, delta: Any) -> None:
        """Write one streamed assistant fragment, lazily opening its title."""
        if self.mode == "quiet" or not delta:
            return
        self._start_section("assistant")
        if not self._assistant_open:
            self._write("助手 › ")
            self._assistant_open = True
            self._line_open = True
        fragment = _text(delta)
        self._write(fragment, flush=True)
        trailing = len(fragment) - len(fragment.rstrip("\n"))
        if trailing and not fragment.rstrip("\n"):
            self._assistant_tail_newlines += trailing
        else:
            self._assistant_tail_newlines = trailing
        self._line_open = self._assistant_tail_newlines == 0

    def assistant_end(self) -> None:
        if self.mode == "quiet" or not self._assistant_open:
            return
        self._close_section()

    def round_start(self, number: int) -> None:
        if self.mode != "debug":
            return
        self._start_section("round")
        self._line(f"[第 {_text(number)} 轮]")
        self._close_section()

    def tools_start(self, tool_calls: Iterable[Any] | Any) -> None:
        if self.mode == "quiet":
            return
        if isinstance(tool_calls, (str, bytes, Mapping)):
            calls = [tool_calls]
        else:
            try:
                calls = list(tool_calls)
            except Exception:
                calls = [tool_calls]
        names = [_call_name(call) for call in calls]
        if not names:
            return
        self._start_section("tools")
        counts = Counter(names)
        ordered_names = list(dict.fromkeys(names))
        summary = "，".join(f"{name} × {counts[name]}" for name in ordered_names)
        self._line(f"执行中 · {summary}", flush=True)
        if self.mode == "debug":
            for call in calls:
                self._line(f"  工具: {_call_name(call)} {_debug_arguments(_call_arguments(call))}")

    def tool_result(
        self,
        name: str,
        arguments: Any,
        content: Any,
        execution: Any = None,
    ) -> None:
        if self.mode == "quiet":
            return
        self._start_section("tools")
        tool_name = _single_line(name or "<missing>")
        summary = _argument_summary(tool_name, arguments)
        suffix = f" · {summary}" if summary else ""
        outcome = _get(execution, "outcome") if execution is not None else None
        label = _OUTCOME_LABELS.get(outcome, "返回")
        line = f"  {label} · {tool_name}{suffix}"
        if outcome in {"failed", "denied", "timeout", "invalid"}:
            reason = _get(execution, "output_excerpt")
            if reason is None:
                reason = _get(execution, "output")
            if reason is None:
                reason = content
            line += f" · {_clip(_single_line(reason), _MAX_FAILURE_REASON)}"
        if self.mode == "debug":
            self._line(line)
            self._line("    " + _bounded_result(content).replace("\n", "\n    "))
        else:
            self._line(line)

    def status_notice(self, message: Any) -> None:
        """Show a runtime status message in normal/debug modes."""
        if self.mode == "quiet" or not message:
            return
        self._notice(message)

    def cli_notice(self, message: Any) -> None:
        """Show an explicit CLI notice in every mode, including quiet."""
        if not message:
            return
        self._notice(message, force=True)

    def input_end(self) -> None:
        """Leave one blank line after the terminal echoes an input line."""
        self._write("\n", flush=True)

    def _notice(self, message: Any, *, force: bool = False) -> None:
        if self.mode == "quiet" and not force:
            return
        self._start_section("notice")
        for line in _text(message).splitlines() or [""]:
            self._line(line)

    def close(self) -> None:
        """Finish the current visual section, if any."""
        self._close_section()


__all__ = ["TerminalOutput"]
