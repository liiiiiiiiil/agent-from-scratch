import json
from dataclasses import dataclass
from typing import Any, Callable

from mini_agent.permission import PermissionGate


# ============================================================
# 1. Tool Definition
# ============================================================

@dataclass
class Tool:
    """
    一个 Tool 的完整定义。

    name        : 给 LLM 看的工具名称
    description : 给 LLM 看的工具说明
    parameters  : 给 LLM 看的参数 Schema
    handler     : Runtime 真正执行的 Python 函数
    """
    name: str
    description: str
    parameters: dict
    handler: Callable[..., Any]

    def to_llm_schema(self):
        """
        转换成 OpenAI Function Calling 所需要的格式。
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# ============================================================
# 2. Tool Registry
# ============================================================

class ToolRegistry:
    """
    Tool 注册中心。

    负责：
    1. 注册 Tool
    2. 根据名称查找 Tool
    3. 获取所有 Tool
    4. 生成给 LLM 的 Tool Schema
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool):
        """
        注册一个 Tool。
        """
        if tool.name in self._tools:
            raise ValueError(f"Tool 已经存在: {tool.name}")

        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        """
        根据名称获取 Tool。
        """
        if name not in self._tools:
            raise ValueError(f"未知 Tool: {name}")

        return self._tools[name]

    def list_tools(self):
        """
        获取所有 Tool。
        """
        return list(self._tools.values())

    def schemas(self):
        """
        获取所有 Tool 的 LLM Schema。
        """
        return [
            tool.to_llm_schema()
            for tool in self._tools.values()
        ]


# ============================================================
# 3. Tool Executor
# ============================================================

class ToolExecutor:
    """
    Tool 执行器。

    Registry 负责“找到 Tool”
    Executor 负责“执行 Tool”
    """

    def __init__(self, registry: ToolRegistry, gate: PermissionGate | None = None):
        self.registry = registry
        self.gate = gate or PermissionGate()

    def execute(self, name: str, arguments: dict):
        """
        根据 Tool 名称和参数执行 Tool。
        """

        # ① 从 Registry 找 Tool
        tool = self.registry.get(name)

        # ② 权限闸门：返回 None=放行，返回 str=拒绝原因
        denied = self.gate.guard(name, arguments)
        if denied:
            print(f"[Permission] {denied}")
            return denied

        print(f"[Executor] 执行 Tool: {name}")
        print(f"[Executor] 参数: {arguments}")

        # ③ 调用真正的 Python handler
        try:
            result = tool.handler(**arguments)
        except Exception as e:
            return f"Tool 执行失败: {e}"

        # ④ 返回结果
        return result
