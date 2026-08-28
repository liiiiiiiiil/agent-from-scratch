"""Tool 定义、注册中心、执行器。

v0.04 版：ToolExecutor 加权限闸门（PermissionGate）。
Executor 先过闸门再调 handler，异常捕获返回错误信息给 LLM。
"""

from __future__ import annotations

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
        """转换成 OpenAI Function Calling 所需要的格式。"""
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
        """注册一个 Tool。"""
        if tool.name in self._tools:
            raise ValueError(f"Tool 已经存在: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        """根据名称获取 Tool。"""
        if name not in self._tools:
            raise ValueError(f"未知 Tool: {name}")
        return self._tools[name]

    def list_tools(self):
        """获取所有 Tool。"""
        return list(self._tools.values())

    def schemas(self):
        """获取所有 Tool 的 LLM Schema。"""
        return [tool.to_llm_schema() for tool in self._tools.values()]


# ============================================================
# 3. Tool Executor
# ============================================================

RESULT_BRIEF_MAX_LENGTH = 200
RESULT_BRIEF_FALLBACK = "<unavailable>"
ResultCallback = Callable[[str, dict[str, Any], bool, str], None]


class ToolExecutor:
    """
    Tool 执行器。

    Registry 负责"找到 Tool"
    Executor 负责"执行 Tool"（先过权限闸门）
    """

    def __init__(
        self,
        registry: ToolRegistry,
        gate: PermissionGate | None = None,
        on_result: ResultCallback | None = None,
    ):
        self.registry = registry
        self.gate = gate or PermissionGate()
        self.on_result: ResultCallback | None = on_result

    def _notify_result(
        self,
        name: str,
        arguments: dict[str, Any],
        ok: bool,
        result: Any,
    ) -> None:
        if self.on_result is None:
            return

        try:
            try:
                brief = str(result)[:RESULT_BRIEF_MAX_LENGTH]
            except Exception:
                brief = RESULT_BRIEF_FALLBACK[:RESULT_BRIEF_MAX_LENGTH]
            self.on_result(name, arguments, ok, brief)
        except Exception as error:
            # Result callbacks are observational and must not affect execution.
            try:
                print(f"[Executor] 结果回调失败: {type(error).__name__}")
            except Exception:
                pass

    def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        """根据 Tool 名称和参数执行 Tool。"""
        tool = self.registry.get(name)

        # ① 权限闸门：返回 None=放行，返回 str=拒绝原因
        denied = self.gate.guard(name, arguments)
        if denied:
            print(f"[Permission] {denied}")
            self._notify_result(name, arguments, False, denied)
            return denied

        print(f"[Executor] 执行 Tool: {name}")
        print(f"[Executor] 参数: {arguments}")

        try:
            result = tool.handler(**arguments)
        except Exception as e:
            result = f"Tool 执行失败: {e}"
            self._notify_result(name, arguments, False, result)
            return result

        self._notify_result(name, arguments, True, result)
        return result
