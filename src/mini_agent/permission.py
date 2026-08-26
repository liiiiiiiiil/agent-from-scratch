"""权限策略：tool 执行前的三态闸门（allow/deny/ask）。"""

import threading

# ============================================================
# 1. 动作常量
# ============================================================

ALLOW, DENY, ASK = "allow", "deny", "ask"

# ============================================================
# 2. 权限规则配置（硬编码，与 agent.py 的 BASE_URL/API_KEY 风格一致）
# ============================================================

PERMISSION_RULES = {
    "read_file": ALLOW,
    "calculate": ALLOW,
    "write_file": ASK,  # 有副作用，每次问一下
}


# ============================================================
# 3. PermissionPolicy
# ============================================================


class PermissionPolicy:
    """
    一维权限策略：tool_name -> action。
    留 args 参数口子，以后升到 (tool_name, pattern) 二维。
    """

    def __init__(self, rules: dict | None = None):
        self.rules = rules if rules is not None else PERMISSION_RULES
        self._approved = set()  # 运行时"本轮已批准"的工具名

    def check(self, tool_name: str, args: dict | None = None) -> str:
        action = self.rules.get(tool_name, ALLOW)
        if action == ALLOW:
            return ALLOW
        if action == DENY:
            return DENY
        if action == ASK:
            if tool_name in self._approved:
                return ALLOW  # 本轮已批准，免再问
            return ASK
        return ALLOW

    def approve(self, tool_name: str):
        """用户选 'a' 时调用，标记本轮内总是允许。"""
        self._approved.add(tool_name)


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
        action = self.policy.check(tool_name, args)

        if action == DENY:
            return f"权限拒绝: 规则禁止调用 {tool_name}"

        if action == ASK:
            with self._ask_lock:
                choice = input(
                    f"允许执行 {tool_name}({args})? [once/always/reject] "
                ).strip().lower()
                if choice == "always":
                    self.policy.approve(tool_name)
                elif choice != "once":
                    return f"权限拒绝: 用户拒绝执行 {tool_name}"

        return None
