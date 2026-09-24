"""Frozen, bounded definitions for named synchronous Subagent roles."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from types import MappingProxyType
import unicodedata
from typing import Any


AGENT_PROFILE_ID_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
AGENT_PROFILE_TOOLS = frozenset({"calculate", "read_file", "list_dir", "grep"})
BUILTIN_AGENT_PROFILE_IDS = frozenset({"explorer", "reviewer", "tester", "general"})
MAX_AGENT_PROFILES = 32
MAX_PROFILE_DESCRIPTION = 240
MAX_PROFILE_PROMPT = 4000
MAX_PROFILE_SKILLS = 32


@dataclass(frozen=True)
class AgentProfile:
    """Immutable, validated role definition. Prompt text is runtime-only."""

    profile_id: str
    description: str
    prompt: str
    model_profile: str | None
    tools: tuple[str, ...]
    permissions: tuple[tuple[str, str], ...]
    skills: tuple[str, ...]
    fingerprint: str
    builtin: bool = False

    def permission_for(self, tool: str) -> str | None:
        return dict(self.permissions).get(tool)


def _builtin_specs() -> dict[str, dict[str, Any]]:
    read_tools = ["read_file", "list_dir", "grep"]
    return {
        "explorer": {
            "description": "在给定范围内定位文件、定义和实现关系。",
            "prompt": "围绕任务目标探索给定范围，指出最相关的文件、符号和调用关系；区分直接观察与推断。",
            "tools": read_tools,
        },
        "reviewer": {
            "description": "审阅给定范围中的正确性、边界和回归风险。",
            "prompt": "以代码审阅方式检查给定范围，优先报告具体缺陷、触发条件和影响；没有证据的问题不要写成事实。",
            "tools": read_tools,
        },
        "tester": {
            "description": "分析测试覆盖并建议可执行的验证步骤。",
            "prompt": (
                "分析已有测试和实现，指出覆盖缺口并建议具体测试命令或用例。你只能建议测试，"
                "不得执行测试；不得声称测试已运行或通过。最终 limitations 必须明确写明本次未执行测试。"
            ),
            "tools": read_tools,
        },
        "general": {
            "description": "使用只读文件调查和纯计算完成一般调查。",
            "prompt": "针对目标开展一般只读调查，汇总有依据的发现，并明确范围和证据限制。",
            "tools": ["calculate", *read_tools],
        },
    }


def _has_control(value: str) -> bool:
    return any(
        unicodedata.category(char).startswith("C") and char not in "\r\n\t"
        for char in value
    )


def _freeze_profile(
    profile_id: str,
    raw: dict[str, Any],
    *,
    provider_catalog: Any = None,
    builtin: bool = False,
) -> AgentProfile:
    allowed_fields = {"description", "prompt", "model_profile", "tools", "permissions", "skills"}
    if set(raw) - allowed_fields:
        raise ValueError(f"AGENT_PROFILES[{profile_id}] 含未知字段")
    description, prompt = raw.get("description"), raw.get("prompt")
    if (not isinstance(description, str) or not description.strip()
            or len(description) > MAX_PROFILE_DESCRIPTION or _has_control(description)):
        raise ValueError(f"AGENT_PROFILES[{profile_id}].description 无效")
    if (not isinstance(prompt, str) or not prompt.strip()
            or len(prompt) > MAX_PROFILE_PROMPT or _has_control(prompt)):
        raise ValueError(f"AGENT_PROFILES[{profile_id}].prompt 无效")

    tools = raw.get("tools")
    if (not isinstance(tools, list) or not tools or len(tools) > len(AGENT_PROFILE_TOOLS)
            or any(not isinstance(item, str) or item not in AGENT_PROFILE_TOOLS for item in tools)
            or len(set(tools)) != len(tools)):
        raise ValueError(f"AGENT_PROFILES[{profile_id}].tools 含未知、重复或越权工具")

    permissions = raw.get("permissions", {})
    if not isinstance(permissions, dict) or len(permissions) > len(AGENT_PROFILE_TOOLS):
        raise ValueError(f"AGENT_PROFILES[{profile_id}].permissions 必须是有界对象")
    frozen_permissions: list[tuple[str, str]] = []
    for tool, action in permissions.items():
        if tool not in tools or not isinstance(action, str) or action not in {"allow", "deny"}:
            raise ValueError(
                f"AGENT_PROFILES[{profile_id}].permissions 只能为角色工具指定 allow 或 deny"
            )
        frozen_permissions.append((tool, action))
    frozen_permissions.sort()

    skills = raw.get("skills", [])
    if (not isinstance(skills, list) or len(skills) > MAX_PROFILE_SKILLS
            or any(not isinstance(item, str) or AGENT_PROFILE_ID_PATTERN.fullmatch(item) is None
                   for item in skills)
            or len(set(skills)) != len(skills)):
        raise ValueError(f"AGENT_PROFILES[{profile_id}].skills 含非法或重复 Skill ID")

    requested_model = raw.get("model_profile")
    if requested_model is not None and (
            not isinstance(requested_model, str) or not requested_model.strip()
            or len(requested_model) > 120 or _has_control(requested_model)):
        raise ValueError(f"AGENT_PROFILES[{profile_id}].model_profile 无效")
    if provider_catalog is None:
        if requested_model is not None:
            raise ValueError(f"AGENT_PROFILES[{profile_id}] 指定了 model_profile，但没有 ProviderCatalog")
        resolved_model = None
    else:
        try:
            resolved_model = provider_catalog.resolve_child_profile(requested_model)
        except ValueError as error:
            raise ValueError(f"AGENT_PROFILES[{profile_id}].model_profile 无效: {error}") from error

    payload = {
        "id": profile_id,
        "description": description,
        "prompt": prompt,
        "model_profile": resolved_model,
        "tools": list(tools),
        "permissions": dict(frozen_permissions),
        "skills": list(skills),
        "builtin": builtin,
    }
    fingerprint = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return AgentProfile(
        profile_id, description.strip(), prompt.strip(), resolved_model,
        tuple(tools), tuple(frozen_permissions), tuple(skills), fingerprint, builtin,
    )


class AgentProfileCatalog:
    """Validate and freeze built-in plus local role definitions for one Runtime."""

    def __init__(self, configured: Any = None, *, provider_catalog: Any = None) -> None:
        if configured is None:
            configured = {}
        if not isinstance(configured, dict):
            raise ValueError("AGENT_PROFILES 必须是对象")
        if len(configured) > MAX_AGENT_PROFILES:
            raise ValueError(f"AGENT_PROFILES 不能超过 {MAX_AGENT_PROFILES} 项")
        profiles = {
            name: _freeze_profile(name, spec, provider_catalog=provider_catalog, builtin=True)
            for name, spec in _builtin_specs().items()
        }
        for profile_id, spec in configured.items():
            if not isinstance(profile_id, str) or AGENT_PROFILE_ID_PATTERN.fullmatch(profile_id) is None:
                raise ValueError("AGENT_PROFILES 的角色 ID 格式非法")
            if profile_id in BUILTIN_AGENT_PROFILE_IDS:
                raise ValueError(f"内置角色不可覆盖: {profile_id}")
            if not isinstance(spec, dict):
                raise ValueError(f"AGENT_PROFILES[{profile_id}] 必须是对象")
            profiles[profile_id] = _freeze_profile(
                profile_id, spec, provider_catalog=provider_catalog,
            )
        self._profiles = MappingProxyType(profiles)

    @property
    def profiles(self) -> tuple[AgentProfile, ...]:
        return tuple(self._profiles[name] for name in sorted(self._profiles))

    def resolve(self, profile_id: str) -> AgentProfile:
        try:
            return self._profiles[profile_id]
        except (KeyError, TypeError) as error:
            raise ValueError(f"未知 agent_profile: {profile_id}") from error


def validate_agent_profiles_config(configured: Any) -> None:
    """Validate config shape before the provider catalog is assembled."""
    if configured is None:
        configured = {}
    if not isinstance(configured, dict):
        raise ValueError("AGENT_PROFILES 必须是对象")
    if len(configured) > MAX_AGENT_PROFILES:
        raise ValueError(f"AGENT_PROFILES 不能超过 {MAX_AGENT_PROFILES} 项")
    for profile_id, raw in configured.items():
        if not isinstance(profile_id, str) or AGENT_PROFILE_ID_PATTERN.fullmatch(profile_id) is None:
            raise ValueError("AGENT_PROFILES 的角色 ID 格式非法")
        if profile_id in BUILTIN_AGENT_PROFILE_IDS:
            raise ValueError(f"内置角色不可覆盖: {profile_id}")
        if not isinstance(raw, dict):
            raise ValueError(f"AGENT_PROFILES[{profile_id}] 必须是对象")
        if "model_profile" in raw:
            model = raw["model_profile"]
            if model is not None and (
                    not isinstance(model, str) or not model.strip()
                    or len(model) > 120 or _has_control(model)):
                raise ValueError(f"AGENT_PROFILES[{profile_id}].model_profile 无效")
        # Alias existence and subagent authorization are checked against the
        # frozen ProviderCatalog when a Runtime is assembled.
        shape_only = dict(raw)
        shape_only.pop("model_profile", None)
        _freeze_profile(profile_id, shape_only, provider_catalog=None)


__all__ = [
    "AGENT_PROFILE_ID_PATTERN", "AGENT_PROFILE_TOOLS", "AgentProfile",
    "AgentProfileCatalog", "BUILTIN_AGENT_PROFILE_IDS", "validate_agent_profiles_config",
]
