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
import json
import threading

# ============================================================
# 1. 动作常量
# ============================================================

ALLOW, DENY, ASK = "allow", "deny", "ask"

# ============================================================
# 2. 权限规则配置（硬编码，与 config.py 的风格一致）
# ============================================================

PERMISSION_RULES = {
    "begin_plan": ALLOW,
    "cancel_planning": ALLOW,
    "commit_plan": ALLOW,
    "update_plan_progress": ALLOW,
    "request_replan": ALLOW,
    "delegate_task": ALLOW,
    "read_file": ALLOW,
    "calculate": ALLOW,
    "list_dir": ALLOW,  # 只读，放行
    "grep": ALLOW,  # 只读，放行
    "list_memories": ALLOW,
    "read_memory": ALLOW,
    "search_memories": ALLOW,
    "list_references": ALLOW,
    "search_reference": ASK,
    "read_reference": ASK,
    "remember": ASK,
    "revise_memory": ASK,
    "forget_memory": ASK,
    "skill": ASK,
    "write_file": ASK,  # 有副作用，每次问一下
    "edit_file": ASK,  # 有副作用，同 write_file
    "rollback_checkpoint": ASK,  # 仅 RecoveryRuntime 可调用的受限文件恢复
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
    # Background execution has its own command rules.  An approval for
    # run_shell must never silently authorize a long-lived process.
    "start_process": {
        "*": ASK,
    },
    "get_process": ALLOW,
    "read_process": ALLOW,
    "list_processes": ALLOW,
    "wait_process": ALLOW,
    "write_process": ASK,
    "terminate_process": ASK,
    "kill_process": ASK,
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
            pattern_matches = (
                pattern == rule["pattern"]
                if rule.get("literal_pattern")
                else fnmatch.fnmatch(pattern, rule["pattern"])
            )
            if fnmatch.fnmatch(tool_name, rule["permission"]) and pattern_matches:
                return rule["action"]
        return ASK

    def approve(self, tool_name: str, pattern: str = "*"):
        """用户选 'always' 时调用，存 (tool_name, pattern) 到 approved。

        approved 追加在末尾，被 check() 的 findLast 覆盖前面的 ask 规则。
        """
        self._approved.append({
            "permission": tool_name, "pattern": pattern, "action": ALLOW,
            # Reference approvals name one alias-relative target.  A literal
            # '*' or '[' in that target must not widen an ``always`` decision
            # into a glob rule.  Other tools retain the established pattern
            # approval behavior.
            "literal_pattern": tool_name in {"search_reference", "read_reference", "skill"},
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

    def guard(self, tool_name: str, args: dict, *, display_context: dict | None = None) -> str | None:
        """
        返回 None 表示放行，返回 str 表示拒绝原因。
        """
        pattern = self._extract_pattern(tool_name, args)
        action = self.policy.check(tool_name, pattern)

        if action == DENY:
            return f"权限拒绝: 规则禁止调用 {tool_name}({pattern})"

        if action == ASK:
            with self._ask_lock:
                try:
                    prompt_args = self._prompt_arguments(
                        tool_name, args, display_context=display_context,
                    )
                    choice = input(
                        f"\n授权确认\n允许执行 {tool_name}({prompt_args})? [once/always/reject] "
                    ).strip().lower()
                finally:
                    try:
                        print()
                    except Exception:
                        pass
                if choice == "always":
                    self.policy.approve(tool_name, pattern)
                elif choice != "once":
                    return f"权限拒绝: 用户拒绝执行 {tool_name}"

        return None

    @staticmethod
    def _prompt_arguments(
        tool_name: str, args: dict, *, display_context: dict | None = None,
    ) -> str:
        """Render only non-sensitive authorization facts for stdin writes."""
        if tool_name == "skill":
            skill_id = str((args or {}).get("name", "<missing>"))[:64]
            source = str((display_context or {}).get("source", "<unknown>"))[:16]
            return f"skill_id={skill_id}, source={source}"
        if display_context is not None:
            alias = str(display_context.get("alias", "<unknown>"))[:64]
            raw_tool = str(display_context.get("tool", "<unknown>"))[:64]
            facts: dict[str, object] = {}
            sensitive_markers = (
                "token", "secret", "password", "passwd", "credential", "authorization",
                "api_key", "apikey", "access_key", "private_key", "cookie", "auth",
            )
            for key, value in sorted((args or {}).items(), key=lambda item: str(item[0])):
                key_text = str(key)[:64]
                lowered = key_text.casefold()
                if any(marker in lowered for marker in sensitive_markers):
                    facts[key_text] = "<redacted>"
                elif isinstance(value, str):
                    try:
                        value.encode("utf-8")
                    except UnicodeEncodeError:
                        facts[key_text] = "<invalid utf-8>"
                    else:
                        if len(value) > 128:
                            facts[key_text] = f"<{len(value)} chars>"
                        else:
                            facts[key_text] = value
                elif isinstance(value, (list, dict)):
                    facts[key_text] = f"<{type(value).__name__} {len(value)} items>"
                else:
                    facts[key_text] = value
            try:
                rendered = json.dumps(facts, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            except (TypeError, ValueError):
                rendered = "<unrenderable>"
            rendered = rendered[:900]
            return f"alias={alias}, tool={raw_tool}, arguments={rendered}"
        if tool_name == "write_process":
            input_text = args.get("input") if isinstance(args, dict) else ""
            try:
                byte_count = len(input_text.encode("utf-8")) if isinstance(input_text, str) else 0
            except UnicodeEncodeError:
                byte_count = 0
            return (
                f"process_id={args.get('process_id', '<missing>')}, "
                f"bytes={byte_count}, close_stdin={bool(args.get('close_stdin', False))}"
            )
        if tool_name in {"remember", "revise_memory", "forget_memory"}:
            # Memory bodies are already bounded by the tool schema.  Show the
            # actual proposed content and optimistic target revision so an
            # ``ask`` decision is reviewable; never use the generic repr here
            # because it is easy to lose the distinction between fields.
            try:
                return json.dumps(args, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError):
                return str(args)
        if tool_name in {"search_reference", "read_reference"}:
            # Reference authorization is alias-scoped.  Keep the prompt
            # useful without ever rendering a configured filesystem root.
            if tool_name == "search_reference":
                return (
                    f"alias={args.get('alias', '<missing>')}, "
                    f"path={args.get('path', '.')}, "
                    f"query={args.get('query', '<missing>')}"
                )
            return (
                f"alias={args.get('alias', '<missing>')}, "
                f"path={args.get('path', '<missing>')}, "
                f"offset={args.get('offset', 0)}, limit={args.get('limit', 200)}"
            )
        return str(args)

    @staticmethod
    def _extract_pattern(tool_name: str, args: dict) -> str:
        """从工具参数中提取权限匹配 pattern。

        对 run_shell：返回命令字符串（v0.10 接入 BashArity 后改为泛化模式）
        对 read_file/write_file/edit_file：返回文件路径（支持按文件名模式控制权限）
        对其他工具：返回 "*"（行为不变，一维兼容）
        """
        if tool_name == "run_shell":
            return args.get("command", "*")
        if tool_name == "start_process":
            return args.get("command", "*")
        if tool_name in ("read_file", "write_file", "edit_file", "rollback_checkpoint"):
            return args.get("path", "*")
        if tool_name in {"search_reference", "read_reference"}:
            alias = args.get("alias", "*")
            path = args.get("path", ".")
            return f"{alias}:{path}"
        if tool_name == "skill":
            return args.get("name", "*")
        return "*"
