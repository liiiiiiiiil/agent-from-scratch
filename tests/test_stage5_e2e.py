"""Stage-five end-to-end orchestration acceptance test."""

import json
import os
import tempfile
from pathlib import Path

import mini_agent.agent as agent_module
from mini_agent.context import ContextBudget, ContextManager
from mini_agent.instructions import InstructionLoader
from mini_agent.permission import ALLOW, PermissionGate, PermissionPolicy
from mini_agent.prompt import build_system_prompt
from mini_agent.state import AgentState
from mini_agent.tools import create_registry
from mini_agent.tools.base import ToolExecutor


def _tool_call(call_id, name, arguments):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }],
    }


def test_stage5_plan_execute_replan_verify_e2e(monkeypatch, capsys):
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        (root / ".git").mkdir()
        (root / "AGENTS.md").write_text(
            "修改 app.py 的 VALUE，并运行 python -c 验证命令。\n",
            encoding="utf-8",
        )
        app = root / "app.py"
        app.write_text("VALUE = 0\n", encoding="utf-8")

        old_cwd = os.getcwd()
        os.chdir(root)
        try:
            task = "修复 app.py 的 VALUE 并验证"
            state = AgentState(task=task)
            instructions = InstructionLoader(str(root)).load()
            protected = [{
                "role": "system",
                "content": build_system_prompt(project_instructions=instructions),
            }]
            history = [{"role": "user", "content": task}]
            # Complete old rounds force compaction while preserving protocol pairs.
            for index in range(4):
                history.append(_tool_call(f"old-{index}", "read_file", {"path": "app.py"}))
                history.append({
                    "role": "tool", "tool_call_id": f"old-{index}",
                    "content": "historical output " + ("x" * 500),
                })
            events = []
            snapshots = []
            budget = ContextBudget(window=260, output_reserve_ratio=0.1, history_ratio=0.35)
            context = ContextManager(
                state,
                history,
                budget=budget,
                keep_rounds=1,
                summarizer=lambda messages: "Earlier investigation history summarized.",
                protected_messages=protected,
                observer=events.append,
            )
            registry = create_registry(state)
            policy = PermissionPolicy({name: ALLOW for name in (
                "update_todo", "read_file", "edit_file", "run_shell",
            )})
            executor = ToolExecutor(
                registry, gate=PermissionGate(policy), on_result=state.record_tool
            )
            responses = [
                _tool_call("tc1", "update_todo", {"todos": [
                    {"content": "调查 app.py", "status": "in_progress"},
                    {"content": "修改 VALUE", "status": "pending"},
                    {"content": "运行验证", "status": "pending"},
                ]}),
                _tool_call("tc2", "read_file", {"path": str(app)}),
                _tool_call("tc3", "edit_file", {
                    "path": str(app), "old_string": "VALUE = 0", "new_string": "VALUE = 1",
                }),
                _tool_call("tc4", "run_shell", {
                    "command": 'python -c "import app; assert app.VALUE == 42"',
                    "purpose": "verification",
                }),
                _tool_call("tc5", "update_todo", {"todos": [
                    {"content": "调查 app.py", "status": "completed"},
                    {"content": "修正 VALUE 为 42", "status": "in_progress"},
                    {"content": "运行验证", "status": "pending"},
                ]}),
                _tool_call("tc6", "edit_file", {
                    "path": str(app), "old_string": "VALUE = 1", "new_string": "VALUE = 42",
                }),
                _tool_call("tc7", "run_shell", {
                    "command": 'python -c "import app; assert app.VALUE == 42"',
                    "purpose": "verification",
                }),
                _tool_call("tc8", "update_todo", {"todos": [
                    {"content": "调查 app.py", "status": "completed"},
                    {"content": "修正 VALUE 为 42", "status": "completed"},
                    {"content": "运行验证", "status": "completed"},
                ]}),
                {"role": "assistant", "content": "已修复并通过验证。"},
            ]
            call_index = 0

            def scripted_llm(messages, **kwargs):
                nonlocal call_index
                snapshots.append(messages)
                result = responses[call_index]
                call_index += 1
                return result

            monkeypatch.setattr(agent_module, "call_llm", scripted_llm)
            result = agent_module.agent_loop(context, executor)
            state.status = "done"

            assert result == "已修复并通过验证。"
            assert call_index == len(responses)
            assert any(event.kind == "compacted" and not event.details.get("failed") for event in events)
            assert any("[Historical Summary]" in str(m.get("content")) for view in snapshots for m in view)
            instruction_marker = instructions.strip()
            assert any(instruction_marker in str(message.get("content", "")) for message in snapshots[0])
            assert any(
                instruction_marker in str(message.get("content", ""))
                for view in snapshots[1:] for message in view
            )

            tool_names = [item["tool"] for item in state.snapshot()["tool_history"]]
            assert tool_names.index("edit_file") < tool_names.index("run_shell")
            assert tool_names.count("edit_file") == 2
            verification_results = {
                message["tool_call_id"]: message["content"]
                for message in context.history if message.get("role") == "tool"
            }
            assert "[exit=1]" in verification_results["tc4"]
            assert "[exit=0]" in verification_results["tc7"]
            verification = [item for item in state.snapshot()["verification_evidence"]]
            assert verification[-1]["outcome"] == "passed"
            assert state.snapshot()["files_changed"] == [str(app)]
            assert state.has_verification_evidence()
            assert state.completion_reminder() is None
            assert state.snapshot()["current_goal"] == ""
            assert all(item["status"] == "completed" for item in state.snapshot()["todos"])
            assert app.read_text(encoding="utf-8") == "VALUE = 42\n"

            assistants = [m for m in context.history if m.get("role") == "assistant" and m.get("tool_calls")]
            tool_results = {m.get("tool_call_id") for m in context.history if m.get("role") == "tool"}
            assert all(tc["id"] in tool_results for m in assistants for tc in m["tool_calls"])
            assert not any(m.get("role") == "tool" and m.get("tool_call_id") not in {
                tc["id"] for a in assistants for tc in a["tool_calls"]
            } for m in context.history)
        finally:
            os.chdir(old_cwd)
            capsys.readouterr()
