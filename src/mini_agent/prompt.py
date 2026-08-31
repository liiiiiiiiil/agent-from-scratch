"""System Prompt 工程化：分层组装 agent 的系统提示词。

借鉴 OpenCode 的分层思路（header/environment/custom），按 mini_agent
渐进生长原则做最小版：header（身份）+ core_rules（行为规范）+ environment（环境）。
 项目指令（AGENTS.md）由 v0.14 的 InstructionLoader 提供。
"""

import os
import sys
from datetime import date


# ============================================================
# 1. header —— 身份层：告诉模型它是 mini_agent 的哪个 agent
# ============================================================

def header(agent_name: str = "build") -> str:
    """身份层：告诉模型它是 mini_agent 的哪个 agent。

    为多 agent/sub-agent 预留参数，当前只实现 build。
    后续加 explore/plan 时在此分支即可。
    """
    agents = {
        "build": (
            "你是 mini_agent，一个编程 agent。"
            "你通过调用工具完成编程任务：当前能读写改文件、跑命令、做数学计算。"
            "你的目标是独立完成基础的编程任务，不只是聊天。"
        ),
        # 预留，v0.07 不实现：
        # "explore": "你是 mini_agent 的 explore 子 agent，只负责只读探索代码库...",
        # "plan": "你是 mini_agent 的 plan agent，只负责规划不执行...",
    }
    return agents.get(agent_name, agents["build"])


# ============================================================
# 2. environment —— 环境层：动态注入运行时上下文
# ============================================================

def environment() -> str:
    """环境层：动态注入运行时上下文。

    纯标准库获取四项：工作目录 / git 状态 / 平台 / 日期。
    让模型能正确解析相对路径、选对平台命令、感知时间。
    """
    cwd = os.getcwd()
    is_git = _detect_git(cwd)

    return "\n".join([
        "<env>",
        f"  Working directory: {cwd}",
        f"  Is directory a git repo: {'yes' if is_git else 'no'}",
        f"  Platform: {sys.platform}",
        f"  Today's date: {date.today().isoformat()}",
        "</env>",
    ])


def _detect_git(cwd: str) -> bool:
    """向上遍历目录树查找 .git，判断是否在 git 仓库内。

    纯目录遍历，不依赖 git 可执行文件，符合 mini_agent 自包含原则。
    worktree/submodule 场景可能漏判，后续按需升级。
    """
    p = os.path.abspath(cwd)
    while True:
        if os.path.isdir(os.path.join(p, ".git")):
            return True
        parent = os.path.dirname(p)
        if parent == p:
            return False
        p = parent


# ============================================================
# 3. core_rules —— 静态行为规范（所有 agent 共享）
# ============================================================

_CORE_RULES = """<rules>
# Tone and style
- 简洁直接，不啰嗦。输出会显示在命令行，用 GitHub 风格 Markdown。
- 不用 emoji，除非用户明确要求。
- 工具结果已回灌给你，无需在回复中复述工具输出。
- 完成代码修改或文件操作后，不主动总结你做了什么，除非用户问起。

# Professional objectivity
- 优先技术准确性和真实性，而非迎合用户假设。
- 不确定时先调查（读文件、查代码）再下结论，不要凭猜测附和用户。
- 发现用户理解有误时如实指出，客观纠正比盲目同意更有价值。

# Tool usage
- 优先用工具完成任务，不要只靠对话。
- 工具调用的参数要完整、合法，路径用绝对路径或相对工作目录的路径。
- 同一轮可发起多个无依赖的 tool_calls，它们会并发执行。
- 涉及多个步骤、多个文件或需要验证的复杂任务，先用 update_todo 建立简短计划；简单任务无需创建 Todo。
- update_todo 每次提交完整列表，最多一个 Todo 处于 in_progress；Todo 是计划意图，不代表任务已验证完成。

# Safety
- 写文件前会被权限闸门拦截询问，这是预期行为。
- 不要猜测 URL，除非确信对编程有帮助。
- 工具失败会直接抛异常终止循环，这是有意为之——保持核心逻辑清晰。
</rules>"""


# ============================================================
# 4. build_system_prompt —— 组装入口
# ============================================================

def build_system_prompt(agent_name: str = "build", project_instructions: str = "") -> str:
    """组装完整 system prompt，并可附加项目级指令。"""
    sections = [
        header(agent_name),
        _CORE_RULES,
        environment(),
    ]
    if project_instructions.strip():
        sections.append("<project_instructions>\n" + project_instructions.strip() + "\n</project_instructions>")
    return "\n\n".join(sections)
