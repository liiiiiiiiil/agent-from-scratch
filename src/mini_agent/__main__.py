import os
import sys
import unicodedata

# Enable terminal line editing (including reliable Backspace handling) when
# the platform provides Python's readline support.
try:
    import readline  # noqa: F401
except ImportError:
    pass

from mini_agent.agent import LLMResponseError, agent_loop
from mini_agent.context import ContextManager
from mini_agent.instructions import InstructionLoader
from mini_agent.input_session import InputSession
from mini_agent.config import OUTPUT_MODE
from mini_agent.prompt import build_system_prompt
from mini_agent.state import AgentState, PlanRejected
from mini_agent.tools import create_registry, registry
from mini_agent.tools.base import ToolExecutor
from mini_agent.output import TerminalOutput
from mini_agent.trace import TraceQueryError, build_trace, render_trace


def _single_line_notice(value, limit=240):
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _plan_display_text(value):
    """Keep plan text complete while making terminal controls visible."""
    chars = []
    for char in value:
        if char == "\n":
            chars.append(char)
        elif unicodedata.category(char).startswith("C") or char in "\u2028\u2029":
            chars.append(char.encode("unicode_escape").decode("ascii"))
        else:
            chars.append(char)
    return "".join(chars)


def _render_plan_for_approval(state):
    """Render the current State revision and its CLI decisions without side effects."""
    snapshot = state.snapshot()
    revision_id = snapshot["planning_state"]["active_revision_id"]
    plan = snapshot["active_plan"]
    if not plan or plan.get("revision_id") != revision_id:
        return f"计划状态异常：待批 revision {revision_id} 的 active_plan 缺失或不匹配，无法展示审批内容。"

    def field(label, value, indent="  "):
        rendered = _plan_display_text(value).replace("\n", "\n" + indent)
        return f"{label}{rendered}"

    lines = [f"计划 revision {revision_id}"]
    if plan["parent_revision_id"] is not None:
        lines.append(f"父 revision：{plan['parent_revision_id']}")
    if plan["trigger_id"] is not None:
        lines.append(f"修订触发 ID：{plan['trigger_id']}")
    lines.extend((field("目标：", plan["goal"]), field("制订原因：", plan["reason"]), "限制："))
    lines.extend(field("- ", item) for item in plan["constraints"])
    if not plan["constraints"]:
        lines.append("- 无")
    lines.append("任务验收标准：")
    lines.extend(field("- ", item) for item in plan["success_criteria"])
    lines.append("步骤：")
    for index, step in enumerate(plan["steps"], 1):
        lines.append(field(
            f"{index}. [{step['status']}] {step['step_id']}：", step["content"], "   "))
        lines.append(f"   依赖：{', '.join(step['depends_on']) or '无'}")
        if step["replaces"]:
            lines.append(f"   替换：{', '.join(step['replaces'])}")
        lines.append("   本步骤验收：")
        lines.extend(field("   - ", item, "     ") for item in step["success_criteria"])
    lines.extend(("", f"计划 revision {revision_id} 等待决定：",
                  f"/approve {revision_id}", f"/reject {revision_id} <反馈>",
                  f"/continue {revision_id} <反馈>"))
    return "\n".join(lines)


def main():
    global registry
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    state = AgentState()
    run_registry = create_registry(state)
    registry = run_registry
    instructions = InstructionLoader(os.getcwd()).load()
    history = []
    system_prompt = build_system_prompt(project_instructions=instructions) if instructions else build_system_prompt()
    protected_messages = [{
        "role": "system",
        "content": system_prompt,
    }]
    context = ContextManager(state, history)
    context.protected_messages = protected_messages
    # Kept as a compatibility observer for callers using ToolExecutor.execute().
    # The agent loop's structured path suppresses this legacy callback.
    tool_executor = ToolExecutor(run_registry, on_result=state.record_tool)
    input_session = InputSession()
    cli_output = TerminalOutput(OUTPUT_MODE)
    argv = sys.argv[1:]
    if argv and argv[0] == "--plan":
        if len(argv) != 2 or not argv[1].strip():
            print('用法: python -m mini_agent --plan "<任务>"')
            return
        first_task = argv[1]
        first_mode = "plan_only"
    else:
        first_task = argv[0] if argv else None
        first_mode = "auto"

    def cli_notice(message):
        cli_output.cli_notice(message)
        cli_output.close()

    def status_notice(message):
        cli_output.status_notice(message)
        cli_output.close()

    def run_task(user_input, mode="auto"):
        if not state.task:
            if hasattr(state, "begin_task"):
                state.begin_task(user_input, mode=mode)
            else:
                state.task = user_input
        state.status = "running"
        context.history.append({"role": "user", "content": user_input})
        try:
            result = agent_loop(context, tool_executor)
        except LLMResponseError as error:
            state.status = "failed"
            cli_notice(f"服务商错误：{error}")
            return
        except Exception:
            state.status = "failed"
            raise
        if result == "达到最大迭代次数":
            if state.status not in ("blocked", "failed"):
                state.status = "failed"
            status_notice("达到最大迭代次数。")
        elif state.status == "blocked":
            reason = _single_line_notice(getattr(state, "terminal_reason", ""))
            status_notice(f"任务已阻塞：{reason}" if reason else "任务已阻塞：完成条件尚未满足。")
        elif state.status == "failed":
            reason = _single_line_notice(getattr(state, "terminal_reason", ""))
            status_notice(f"任务执行失败：{reason}" if reason else "任务执行失败。")
        elif getattr(getattr(state, "planning_state", None), "phase", None) == "awaiting_approval":
            cli_notice(_render_plan_for_approval(state))
        elif getattr(getattr(state, "planning_state", None), "phase", None) == "exploring":
            revision_id = state.planning_state.active_revision_id
            if revision_id is not None:
                status_notice(f"仍在调查 revision {revision_id}；若原计划可用，输入 /review {revision_id}。")
            else:
                status_notice("仍在只读调查阶段，请继续调查并提交计划。")
        elif state.status == "running":
            state.status = "done"

    # 命令行首条任务（可选）：与交互循环走同一套路径，
    # 保证 argv 分支后 history 状态完整，后续追问上下文不丢。
    if first_task is not None:
        run_task(first_task, mode=first_mode)

    while True:
        try:
            prompt = "你 › "
            user_input = input_session.read(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not user_input or user_input.lower() in ("exit", "quit"):
            break
        cli_output.input_end()
        if user_input.split(maxsplit=1)[0] in ("/approve", "/reject", "/continue", "/review"):
            parts = user_input.split(maxsplit=2)
            command = parts[0]
            needs_feedback = command in ("/reject", "/continue")
            if (len(parts) < 2 or (needs_feedback and len(parts) != 3)
                    or (not needs_feedback and len(parts) != 2)):
                cli_notice(f"用法: {command} <revision_id>" + (" <反馈>" if needs_feedback else ""))
                continue
            try:
                revision_id = int(parts[1])
                if command == "/review":
                    state.review_current_plan(revision_id)
                    cli_notice(_render_plan_for_approval(state))
                else:
                    decision = {
                        "/approve": "approved", "/reject": "rejected",
                        "/continue": "continue_exploring",
                    }[command]
                    state.decide_plan(decision, revision_id,
                                      parts[2] if needs_feedback else None)
                    if decision == "approved":
                        run_task(f"用户已批准当前计划 revision {revision_id}，请继续执行。")
                    elif decision == "continue_exploring":
                        run_task(f"用户要求继续只读调查 revision {revision_id}；反馈：{parts[2]}。必要时提交引用当前 trigger 的修订；若原计划仍合适，请说明，用户可用 /review 重新交付审批。")
                    else:
                        run_task(f"用户驳回计划 revision {revision_id}；反馈：{parts[2]}。请先只读调查，再提交引用当前 trigger 的修订计划。")
            except (ValueError, PlanRejected) as error:
                cli_notice(f"计划决定无效：{error}")
            continue
        if (getattr(getattr(state, "planning_state", None), "phase", None) == "awaiting_approval"
                and user_input not in ("/reset",) and not user_input.startswith(("/new ", "/trace"))):
            cli_notice("当前计划等待用户决定，请使用 /approve、/reject 或 /continue。")
            continue
        if user_input == "/trace" or user_input.startswith("/trace "):
            if not state.task:
                cli_notice("当前没有活动任务，无法回放。")
                continue
            parts = user_input.split()
            if len(parts) > 2:
                cli_notice("用法: /trace [generation_id]")
                continue
            requested_generation = None
            if len(parts) == 2:
                try:
                    requested_generation = int(parts[1])
                except ValueError:
                    cli_notice("用法: /trace [generation_id]，generation_id 必须是非负整数。")
                    continue
            try:
                report = build_trace(state.snapshot(), requested_generation)
            except TraceQueryError as error:
                cli_notice(f"Trace 查询失败：{error}")
                continue
            cli_notice(render_trace(report))
            continue
        if user_input == "/reset":
            context.reset_task()
            if hasattr(state, "reset_task"):
                state.reset_task()
            else:
                state.task = ""
                state.status = "idle"
            cli_notice("当前任务已清空。输入任务开始，或使用 /new <任务>。")
            continue
        if user_input == "/new" or user_input.startswith("/new "):
            task = user_input[4:].strip()
            if not task:
                cli_notice("用法: /new <任务>")
                continue
            context.reset_task()
            if hasattr(state, "begin_task"):
                state.begin_task(task)
            else:
                state.task = task
                state.status = "running"
            cli_notice("已开始新任务。")
            run_task(task)
            continue
        run_task(user_input)


if __name__ == "__main__":
    main()
