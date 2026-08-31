"""权限策略：tool 执行前的三态闸门（allow/deny/ask）。

v0.09 版：从一维 (tool_name -> action) 升级为二维 (tool_name, pattern) -> action。
- 规则内部存扁平 list[dict]（Rule 三元组：permission + pattern + action）
- 构造函数兼容旧版简单 dict 格式（{"write_file": "ask"}）和新版复杂格式（{"run_shell": {"git *": "allow"}}）
- check() 用 fnmatch 做 wildcard 匹配，findLast 取最后匹配（后出现优先级更高）
- approve() 存 (tool_name, pattern) 而非只存 tool_name，实现"同类命令免问"
- 未匹配任何规则时默认 ask（安全优先）

借签 OpenCode PermissionNext 的 evaluate / fromConfig / findLast 设计，做最小版：
去掉事件总线、pending 队列、持久化、CorrectedError——CLI 同步交互不需要。
"""

from __future__ import annotations

import fnmatch
import threading

# ============================================================
# 1. 动作常量
# ============================================================

ALLOW, DENY, ASK = "allow", "deny", "ask"

# ============================================================
# 2. 权限规则配置（硬编码，与 config.py 的风格一致）
# ============================================================

PERMISSION_RULES = {
    "update_todo": ALLOW,
    "read_file": ALLOW,
    "calculate": ALLOW,
    "list_dir": ALLOW,  # 只读，放行
    "grep": ALLOW,  # 只读，放行
    "write_file": ASK,  # 有副作用，每次问一下
    "edit_file": ASK,  # 有副作用，同 write_file
    # v0.10：run_shell 二维权限——安全命令放行，其他每次问
    "run_shell": {
        "git *": ALLOW,  # git 操作放行
        "python *": ALLOW,  # python 脚本/测试放行
        "pip *": ALLOW,  # pip 安装放行
        "ls *": ALLOW,  # 只读命令放行
        "cat *": ALLOW,  # 只读命令放行
        "echo *": ALLOW,  # 只读命令放行
        "*": ASK,  # 其他命令每次问
    },
}


# ============================================================
# 3. PermissionPolicy
# ============================================================


class PermissionPolicy:
    """
    二维权限策略：(tool_name, pattern) -> action。

    内部存扁平 list[dict]（Rule 三元组），构造函数兼容两种格式：
    - 简单格式：{"write_file": "ask"}  →  pattern="*"
    - 复杂格式：{"run_shell": {"git *": "allow", "rm *": "deny"}}

    check() 用 fnmatch 做 wildcard 匹配，从后往前找第一个匹配的规则
    （后出现的优先级更高）。未匹配时默认 ask（安全优先）。
    """

    def __init__(self, rules: dict | None = None):
        self._rules = self._from_config(rules if rules is not None else PERMISSION_RULES)
        self._approved: list[dict] = []  # 运行时 approved，追加在末尾，优先级更高

    @staticmethod
    def _from_config(config: dict) -> list[dict]:
        """把配置 dict 转为扁平 Rule list。

        简单格式 "read_file": "allow"  →  {permission, pattern="*", action}
        复杂格式 "run_shell": {"git *": "allow"}  →  多条 Rule

        对复杂格式，通配符 "*" 排在该工具规则块最前面（优先级最低），
        具体模式排在后面（优先级更高），与 findLast 语义配合：
        从后往前找，先碰具体模式，匹配不到才落到 "*" 兜底。
        """
        ruleset: list[dict] = []
        for key, value in config.items():
            if isinstance(value, str):
                ruleset.append({"permission": key, "pattern": "*", "action": value})
            elif isinstance(value, dict):
                # "*" 排最前（优先级最低），具体模式排后面（优先级更高）
                # findLast 从后往前找，先碰具体模式，匹配不到才落到 "*" 兜底
                items = sorted(value.items(), key=lambda kv: kv[0] != "*")
                for pattern, action in items:
                    ruleset.append({"permission": key, "pattern": pattern, "action": action})
            else:
                raise ValueError(f"不支持的权限规则格式: {key}={value!r}")
        return ruleset

    def check(self, tool_name: str, pattern: str = "*") -> str:
        """检查 (tool_name, pattern) 的权限动作。

        从后往前找第一个匹配的规则（findLast 语义），后出现的优先级更高。
        未匹配时默认 ask（安全优先）。
        """
        merged = self._rules + self._approved
        for rule in reversed(merged):
            if fnmatch.fnmatch(tool_name, rule["permission"]) and fnmatch.fnmatch(pattern, rule["pattern"]):
                return rule["action"]
        return ASK

    def approve(self, tool_name: str, pattern: str = "*"):
        """用户选 'always' 时调用，存 (tool_name, pattern) 到 approved。

        approved 追加在末尾，被 check() 的 findLast 覆盖前面的 ask 规则。
        """
        self._approved.append({
            "permission": tool_name, "pattern": pattern, "action": ALLOW,
        })


# ============================================================
# 4. PermissionGate —— Executor 调用的闸门入口
# ============================================================


class PermissionGate:
    """
    封装"检查 + 交互 + Lock"。
    Executor 只需调 gate.guard(name, args)，返回 None=放行 / str=拒绝原因。
    """

    def __init__(self, policy: PermissionPolicy | None = None):
        self.policy = policy or PermissionPolicy()
        self._ask_lock = threading.Lock()

    def guard(self, tool_name: str, args: dict) -> str | None:
        """
        返回 None 表示放行，返回 str 表示拒绝原因。
        """
        pattern = self._extract_pattern(tool_name, args)
        action = self.policy.check(tool_name, pattern)

        if action == DENY:
            return f"权限拒绝: 规则禁止调用 {tool_name}({pattern})"

        if action == ASK:
            with self._ask_lock:
                choice = input(
                    f"允许执行 {tool_name}({args})? [once/always/reject] "
                ).strip().lower()
                if choice == "always":
                    self.policy.approve(tool_name, pattern)
                elif choice != "once":
                    return f"权限拒绝: 用户拒绝执行 {tool_name}"

        return None

    @staticmethod
    def _extract_pattern(tool_name: str, args: dict) -> str:
        """从工具参数中提取权限匹配 pattern。

        对 run_shell：返回命令字符串（v0.10 接入 BashArity 后改为泛化模式）
        对 read_file/write_file/edit_file：返回文件路径（支持按文件名模式控制权限）
        对其他工具：返回 "*"（行为不变，一维兼容）
        """
        if tool_name == "run_shell":
            return args.get("command", "*")
        if tool_name in ("read_file", "write_file", "edit_file"):
            return args.get("path", "*")
        return "*"
