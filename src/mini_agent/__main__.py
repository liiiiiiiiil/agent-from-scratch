import os
import sys

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
from mini_agent.state import AgentState
from mini_agent.tools import create_registry, registry
from mini_agent.tools.base import ToolExecutor
from mini_agent.output import TerminalOutput


def _single_line_notice(value, limit=240):
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


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

    def cli_notice(message):
        cli_output.cli_notice(message)
        cli_output.close()

    def status_notice(message):
        cli_output.status_notice(message)
        cli_output.close()

    def run_task(user_input):
        if not state.task:
            if hasattr(state, "begin_task"):
                state.begin_task(user_input)
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
        elif state.status == "running":
            state.status = "done"

    # 命令行首条任务（可选）：与交互循环走同一套路径，
    # 保证 argv 分支后 history 状态完整，后续追问上下文不丢。
    if len(sys.argv) > 1:
        run_task(sys.argv[1])

    while True:
        try:
            prompt = "你 › "
            user_input = input_session.read(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not user_input or user_input.lower() in ("exit", "quit"):
            break
        cli_output.input_end()
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
