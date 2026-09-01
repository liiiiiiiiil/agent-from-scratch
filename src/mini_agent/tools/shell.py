"""Shell 执行工具：run_shell。

v0.10：用 subprocess 执行命令，超时 30s，输出截断到 2000 字符防爆上下文。
权限由二维权限闸门控制（permission.py 的 run_shell 规则按命令模式匹配）。
"""

import os
import subprocess

from mini_agent.tools.base import Tool


# ============================================================
# run_shell：执行 shell 命令
# ============================================================

_TIMEOUT = 30
_MAX_OUTPUT = 2000


def run_shell(command: str, purpose: str = "execution"):
    """执行 shell 命令，返回合并的 stdout+stderr 输出。

    - 超时 30s，超时后终止进程并返回错误信息
    - 输出截断到 2000 字符，尾部标注实际长度
    - 工作目录为当前 os.getcwd()
    """
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=os.getcwd(),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
        output = proc.stdout + proc.stderr
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        return f"[timeout] 命令超时（{_TIMEOUT}s），已终止: {command}"

    # 截断防爆
    total = len(output)
    if total > _MAX_OUTPUT:
        output = output[:_MAX_OUTPUT] + f"\n(输出已截断，共 {total} 字符)"
        # 保留尾部更有用，但教学简洁性优先：截头部留尾部
        # output = output[-_MAX_OUTPUT:] + f"\n(输出已截断，共 {total} 字符)"

    prefix = f"[exit={exit_code}] "
    return prefix + output if output else f"[exit={exit_code}] (无输出)"


run_shell_tool = Tool(
    name="run_shell",
    description="执行 shell 命令，返回 stdout+stderr 合并输出；可标记为 execution 或 verification。",
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的 shell 命令",
            },
            "purpose": {
                "type": "string", "enum": ["execution", "verification"],
                "default": "execution", "description": "命令用途",
            },
        },
        "required": ["command"],
    },
    handler=run_shell,
    effect_class="possible",
)
