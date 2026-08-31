import os
import sys

# Enable terminal line editing (including reliable Backspace handling) when
# the platform provides Python's readline support.
try:
    import readline  # noqa: F401
except ImportError:
    pass

from mini_agent.agent import agent_loop
from mini_agent.context import ContextManager
from mini_agent.instructions import InstructionLoader
from mini_agent.prompt import build_system_prompt
from mini_agent.state import AgentState
from mini_agent.tools import registry
from mini_agent.tools.base import ToolExecutor


def main():
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    state = AgentState()
    instructions = InstructionLoader(os.getcwd()).load()
    history = []
    system_prompt = build_system_prompt(project_instructions=instructions) if instructions else build_system_prompt()
    protected_messages = [{
        "role": "system",
        "content": system_prompt,
    }]
    context = ContextManager(state, history)
    context.protected_messages = protected_messages
    tool_executor = ToolExecutor(registry, on_result=state.record_tool)

    def run_task(user_input):
        state.status = "running"
        state.task = user_input
        context.history.append({"role": "user", "content": user_input})
        try:
            result = agent_loop(context, tool_executor)
        except Exception:
            state.status = "failed"
            raise
        state.status = "failed" if result == "达到最大迭代次数" else "done"

    # 命令行首条任务（可选）：与交互循环走同一套路径，
    # 保证 argv 分支后 history 状态完整，后续追问上下文不丢。
    if len(sys.argv) > 1:
        run_task(sys.argv[1])

    while True:
        try:
            user_input = input("\n你: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not user_input or user_input.lower() in ("exit", "quit"):
            break
        run_task(user_input)


if __name__ == "__main__":
    main()
