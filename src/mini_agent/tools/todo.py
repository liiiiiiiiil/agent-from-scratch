"""State-bound Todo update tool."""
from mini_agent.state import AgentState
from mini_agent.tools.base import Tool

def make_update_todo_tool(state: AgentState) -> Tool:
    def update_todo(todos=None):
        try:
            state.update_todos(todos)
        except (TypeError, ValueError) as error:
            return f"Todo 更新失败: {error}"
        snapshot = state.snapshot()
        return f"Todo 已更新：共 {len(snapshot['todos'])} 项；当前目标：{snapshot['current_goal'] or '(无)'}"
    return Tool(name="update_todo", description="创建或更新任务 Todo 列表。每次提交完整列表。", parameters={"type":"object","properties":{"todos":{"type":"array","maxItems":50,"items":{"type":"object","properties":{"content":{"type":"string"},"status":{"type":"string","enum":["pending","in_progress","completed"],"default":"pending"}},"required":["content"]}}},"required":["todos"]}, handler=update_todo)
