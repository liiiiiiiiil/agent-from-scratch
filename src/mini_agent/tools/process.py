"""Task-owned background process tools."""
from __future__ import annotations

from dataclasses import asdict
import json

from mini_agent.processes import (DEFAULT_READ_CHARS, DEFAULT_WAIT_MS,
                                  MAX_READ_CHARS, MAX_WAIT_MS, ProcessManager)
from mini_agent.state import AgentState
from mini_agent.tools.base import Tool


def make_start_process_tool(state: AgentState, manager: ProcessManager) -> Tool:
    def start_process(command: str, cwd: str | None = None):
        task_id = state.ensure_task_id()
        started = manager.start(command, cwd, task_id)
        return {
            "process_id": started.process_id,
            "pid": started.pid,
            "status": started.status,
            "task_id": started.task_id,
            "started_at": started.started_at,
            "command": started.command,
            "cwd": started.cwd,
        }

    return Tool(
        name="start_process",
        description=(
            "在当前任务中启动长期运行的 shell 命令，立即返回任务专属 process_id；"
            "创建成功不代表命令最终成功。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string", "minLength": 1, "maxLength": 4000,
                    "pattern": r"\S",
                    "description": "沿用 run_shell shell 语义的命令字符串",
                },
                "cwd": {
                    "type": "string", "minLength": 1, "maxLength": 1000,
                    "description": "存在的工作目录；缺省为当前工作目录",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        handler=start_process,
        effect_class="possible",
    )


def _observation(state: AgentState, manager: ProcessManager, process_id: str):
    managed = manager.get_owned(state.task_id, process_id)
    if managed is None:
        return None
    state.sync_processes(manager.sync_processes(state.task_id))
    return next((item for item in state.process_records
                 if item.task_id == state.task_id and item.process_id == process_id), None)


def _error(process_id: str) -> str:
    return json.dumps({"status": "error", "process_id": process_id,
                       "error_kind": "unknown_process_id",
                       "message": "未知、过期或跨任务 process_id"}, ensure_ascii=False)


def _process_id_schema() -> dict:
    return {"type": "string", "minLength": 1, "maxLength": 80}


def make_get_process_tool(state: AgentState, manager: ProcessManager) -> Tool:
    def get_process(process_id: str):
        record = _observation(state, manager, process_id)
        return _error(process_id) if record is None else json.dumps(asdict(record), ensure_ascii=False)
    return Tool("get_process", "查询当前任务后台进程的状态、退出码和输出字节位置；不读取日志。",
                {"type": "object", "properties": {"process_id": _process_id_schema()},
                 "required": ["process_id"], "additionalProperties": False}, get_process)


def make_list_processes_tool(state: AgentState, manager: ProcessManager) -> Tool:
    def list_processes():
        if state.task_id:
            state.sync_processes(manager.sync_processes(state.task_id))
        records = [asdict(item) for item in state.process_records if item.task_id == state.task_id]
        visible = []
        for record in reversed(records):
            candidate = [record] + visible
            payload = {"processes": candidate, "omitted_count": len(records) - len(candidate)}
            if len(json.dumps(payload, ensure_ascii=False)) > 8000:
                break
            visible = candidate
        return json.dumps({"processes": visible,
                           "omitted_count": len(records) - len(visible)}, ensure_ascii=False)
    return Tool("list_processes", "列出当前任务登记的后台进程，不包含日志正文。",
                {"type": "object", "properties": {}, "additionalProperties": False}, list_processes)


def make_read_process_tool(state: AgentState, manager: ProcessManager) -> Tool:
    def read_process(process_id: str, max_chars: int = DEFAULT_READ_CHARS):
        if _observation(state, manager, process_id) is None:
            return _error(process_id)
        result = manager.read_process(state.task_id, process_id, max_chars)
        state.sync_processes(manager.sync_processes(state.task_id))
        return json.dumps(result, ensure_ascii=False)
    return Tool("read_process", "读取当前任务后台进程 stdout/stderr 的新增内容和逐流缺口；按字节推进游标。",
                {"type": "object", "properties": {
                    "process_id": _process_id_schema(),
                    "max_chars": {"type": "integer", "minimum": 1, "maximum": MAX_READ_CHARS,
                                  "default": DEFAULT_READ_CHARS},
                }, "required": ["process_id"], "additionalProperties": False}, read_process)


def make_wait_process_tool(state: AgentState, manager: ProcessManager) -> Tool:
    def wait_process(process_id: str, timeout_ms: int = DEFAULT_WAIT_MS):
        if _observation(state, manager, process_id) is None:
            return _error(process_id)
        result = manager.wait_process(state.task_id, process_id, timeout_ms)
        state.sync_processes(manager.sync_processes(state.task_id))
        return json.dumps(result, ensure_ascii=False)
    return Tool("wait_process", "有界等待未读输出或进程退出；超时返回 still_running，不消费日志。",
                {"type": "object", "properties": {
                    "process_id": _process_id_schema(),
                    "timeout_ms": {"type": "integer", "minimum": 0, "maximum": MAX_WAIT_MS,
                                   "default": DEFAULT_WAIT_MS},
                }, "required": ["process_id"], "additionalProperties": False}, wait_process)
