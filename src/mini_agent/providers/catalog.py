"""Local provider/profile catalog and frozen model bindings."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from types import MappingProxyType
from typing import Any, Literal, Mapping

from mini_agent.providers.base import ProviderAdapter, ProviderUsage, UsageMeter, validate_limit
from mini_agent.providers.http import parse_endpoint


SUPPORTED_PROTOCOLS = frozenset({"openai_chat", "anthropic_messages"})
_MAX_NAME = 120


def _name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > _MAX_NAME:
        raise ValueError(f"{label} 必须是 1–{_MAX_NAME} 个字符的别名")
    if any(ord(char) < 33 or char in "<>\\\"'" for char in value):
        raise ValueError(f"{label} 含非法字符")
    return value.strip()


def _endpoint_from_legacy(base_url: str) -> str:
    endpoint = str(base_url or "").strip().rstrip("/")
    if endpoint.endswith("/chat/completions"):
        return endpoint
    return endpoint + "/chat/completions"


@dataclass(frozen=True, repr=False)
class ProviderConfig:
    provider_id: str
    protocol: Literal["openai_chat", "anthropic_messages"]
    endpoint: str
    api_key: str
    timeout_seconds: float = 120
    extra_headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        provider_id = _name(self.provider_id, "provider_id")
        protocol = str(self.protocol)
        if protocol not in SUPPORTED_PROTOCOLS:
            raise ValueError(f"不支持的 provider protocol: {protocol}")
        endpoint = str(self.endpoint or "").strip()
        parse_endpoint(endpoint)
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise ValueError("provider api_key 必须是非空字符串")
        validate_limit(self.timeout_seconds, "timeout_seconds")
        if not isinstance(self.extra_headers, Mapping):
            raise ValueError("extra_headers 必须是映射")
        headers: dict[str, str] = {}
        for key, value in self.extra_headers.items():
            if not isinstance(key, str) or not key.strip() or not isinstance(value, str):
                raise ValueError("extra_headers 必须是字符串到字符串的映射")
            if key.lower() in {"authorization", "x-api-key", "cookie"}:
                raise ValueError("认证头只能由 adapter 根据 api_key 生成")
            headers[key] = value
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "protocol", protocol)
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "extra_headers", MappingProxyType(headers))

    def __repr__(self) -> str:
        return (
            f"ProviderConfig(provider_id={self.provider_id!r}, protocol={self.protocol!r}, "
            "endpoint='<redacted>', api_key='<redacted>')"
        )


@dataclass(frozen=True, repr=False)
class ModelProfile:
    name: str
    provider_id: str
    model_id: str
    context_window: int
    max_output_tokens: int
    supports_tools: bool = True
    supports_streaming: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _name(self.name, "profile name"))
        object.__setattr__(self, "provider_id", _name(self.provider_id, "profile provider_id"))
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise ValueError("profile model_id 必须是非空字符串")
        validate_limit(self.context_window, "context_window", integer=True)
        validate_limit(self.max_output_tokens, "max_output_tokens", integer=True)
        if self.max_output_tokens >= self.context_window:
            raise ValueError("max_output_tokens 必须小于 context_window")
        if not isinstance(self.supports_tools, bool) or not isinstance(self.supports_streaming, bool):
            raise ValueError("profile 能力字段必须是布尔值")

    def __repr__(self) -> str:
        return (
            f"ModelProfile(name={self.name!r}, provider_id={self.provider_id!r}, "
            "model_id='<redacted>', context_window="
            f"{self.context_window}, max_output_tokens={self.max_output_tokens})"
        )


@dataclass(frozen=True)
class ModelBindingRef:
    profile: str
    provider: str
    protocol: str
    fingerprint: str

    def __post_init__(self) -> None:
        _name(self.profile, "binding profile")
        _name(self.provider, "binding provider")
        if self.protocol not in SUPPORTED_PROTOCOLS:
            raise ValueError("binding protocol 不受支持")
        if len(self.fingerprint) != 64 or any(char not in "0123456789abcdef" for char in self.fingerprint):
            raise ValueError("binding fingerprint 必须是 SHA-256")

    def to_dict(self) -> dict[str, str]:
        return {
            "profile": self.profile,
            "provider": self.provider,
            "protocol": self.protocol,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True, repr=False)
class ModelBinding:
    profile: ModelProfile
    provider: ProviderConfig
    adapter: ProviderAdapter
    reference: ModelBindingRef
    usage_meter: UsageMeter = field(compare=False, repr=False)

    def __repr__(self) -> str:
        return f"ModelBinding(reference={self.reference!r})"

    def complete(self, messages: list[dict[str, Any]], **options: Any):
        """Invoke the frozen adapter and account this call on its meter."""
        from mini_agent.context import count_tokens

        estimated_input = count_tokens(messages)
        try:
            response = self.adapter.complete(messages, **options)
        except Exception:
            self.usage_meter.record(
                None,
                estimated_input_tokens=estimated_input,
                estimated_output_tokens=0,
            )
            raise
        estimated_output = count_tokens(response.message)
        self.usage_meter.record(
            response.usage,
            estimated_input_tokens=estimated_input,
            estimated_output_tokens=estimated_output,
        )
        return response


@dataclass(frozen=True, repr=False)
class ProviderCatalog:
    providers: Mapping[str, ProviderConfig]
    profiles: Mapping[str, ModelProfile]
    parent_profile: str
    subagent_profile: str | None
    subagent_allowed_profiles: tuple[str, ...]

    def __repr__(self) -> str:
        return (
            f"ProviderCatalog(providers={tuple(self.providers)}, profiles={tuple(self.profiles)}, "
            f"parent_profile={self.parent_profile!r}, subagent_profile={self.subagent_profile!r})"
        )

    @classmethod
    def from_settings(
        cls,
        providers: Mapping[str, Any] | None,
        profiles: Mapping[str, Any] | None,
        *,
        parent_profile: str | None = None,
        subagent_profile: str | None = None,
        subagent_allowed_profiles: tuple[str, ...] | list[str] | None = None,
        legacy_base_url: str | None = None,
        legacy_api_key: str | None = None,
        legacy_model: str | None = None,
        legacy_context_window: int = 128_000,
    ) -> "ProviderCatalog":
        provider_map = {} if providers is None else dict(providers)
        profile_map = {} if profiles is None else dict(profiles)
        if bool(provider_map) != bool(profile_map):
            raise ValueError("PROVIDERS 与 MODEL_PROFILES 必须同时定义，不能局部混用")
        if not provider_map:
            if not legacy_base_url or not legacy_api_key or not legacy_model:
                raise ValueError("旧配置缺少 BASE_URL、API_KEY 或 MODEL")
            legacy_provider = ProviderConfig(
                "legacy-default", "openai_chat", _endpoint_from_legacy(legacy_base_url),
                legacy_api_key, 120, {},
            )
            legacy_profile = ModelProfile(
                "legacy-default", "legacy-default", legacy_model,
                legacy_context_window, min(8192, legacy_context_window), True, True,
            )
            return cls(
                MappingProxyType({legacy_provider.provider_id: legacy_provider}),
                MappingProxyType({legacy_profile.name: legacy_profile}),
                "legacy-default", None, ("legacy-default",),
            )

        converted_providers: dict[str, ProviderConfig] = {}
        for key, value in provider_map.items():
            provider_id = _name(key, "provider_id")
            if provider_id in converted_providers:
                raise ValueError("provider_id 重复")
            if isinstance(value, ProviderConfig):
                config = value
                if config.provider_id != provider_id:
                    raise ValueError("provider 映射键与 provider_id 不一致")
            elif isinstance(value, Mapping):
                config = ProviderConfig(provider_id=provider_id, **dict(value))
            else:
                raise ValueError("provider 配置必须是对象")
            converted_providers[provider_id] = config

        converted_profiles: dict[str, ModelProfile] = {}
        for key, value in profile_map.items():
            name = _name(key, "profile name")
            if name in converted_profiles:
                raise ValueError("profile name 重复")
            if isinstance(value, ModelProfile):
                profile = value
                if profile.name != name:
                    raise ValueError("profile 映射键与 name 不一致")
            elif isinstance(value, Mapping):
                profile = ModelProfile(name=name, **dict(value))
            else:
                raise ValueError("model profile 配置必须是对象")
            if profile.provider_id not in converted_providers:
                raise ValueError(f"profile {name} 引用了未知 provider")
            converted_profiles[name] = profile

        parent = _name(parent_profile or "default", "PARENT_MODEL_PROFILE")
        if parent not in converted_profiles:
            raise ValueError("PARENT_MODEL_PROFILE 不存在")
        child = None if subagent_profile in (None, "") else _name(subagent_profile, "SUBAGENT_MODEL_PROFILE")
        if child is not None and child not in converted_profiles:
            raise ValueError("SUBAGENT_MODEL_PROFILE 不存在")
        allowed_raw = subagent_allowed_profiles
        if allowed_raw is None:
            allowed = (parent,)
        else:
            allowed = tuple(_name(item, "SUBAGENT_ALLOWED_MODEL_PROFILES") for item in allowed_raw)
        if not allowed:
            raise ValueError("SUBAGENT_ALLOWED_MODEL_PROFILES 不能为空")
        if len(set(allowed)) != len(allowed):
            raise ValueError("SUBAGENT_ALLOWED_MODEL_PROFILES 不能重复")
        if any(item not in converted_profiles for item in allowed):
            raise ValueError("子代理白名单引用了不存在的 profile")
        if child is not None and child not in allowed:
            raise ValueError("SUBAGENT_MODEL_PROFILE 必须位于子代理白名单")
        if child is None and parent not in allowed:
            raise ValueError("子代理回退父 profile 时，父 profile 必须位于白名单")
        selected = {parent, child, *allowed}
        for name in selected:
            if name is not None and not converted_profiles[name].supports_tools:
                raise ValueError(f"profile {name} 的模型不支持工具调用")
        return cls(
            MappingProxyType(converted_providers), MappingProxyType(converted_profiles),
            parent, child, allowed,
        )

    @classmethod
    def from_module(cls, module: Any) -> "ProviderCatalog":
        return cls.from_settings(
            getattr(module, "PROVIDERS", None),
            getattr(module, "MODEL_PROFILES", None),
            parent_profile=getattr(module, "PARENT_MODEL_PROFILE", None),
            subagent_profile=getattr(module, "SUBAGENT_MODEL_PROFILE", None),
            subagent_allowed_profiles=getattr(module, "SUBAGENT_ALLOWED_MODEL_PROFILES", None),
            legacy_base_url=getattr(module, "BASE_URL", None),
            legacy_api_key=getattr(module, "API_KEY", None),
            legacy_model=getattr(module, "MODEL", None),
            legacy_context_window=getattr(module, "CONTEXT_WINDOW", 128_000),
        )

    def profile(self, name: str) -> ModelProfile:
        if name not in self.profiles:
            raise ValueError(f"未知 model_profile: {name}")
        return self.profiles[name]

    def resolve_parent_profile(self, requested: str | None = None) -> str:
        selected = self.parent_profile if requested is None else _name(requested, "parent model_profile")
        if selected not in self.profiles:
            raise ValueError(f"未知 parent model_profile: {selected}")
        return selected

    def resolve_child_profile(self, requested: str | None = None) -> str:
        if requested is not None:
            selected = _name(requested, "model_profile")
            if selected not in self.profiles:
                raise ValueError(f"未知 model_profile: {selected}")
            if selected not in self.subagent_allowed_profiles:
                raise ValueError(f"model_profile {selected} 不在子代理白名单")
            return selected
        selected = self.subagent_profile or self.parent_profile
        if selected not in self.subagent_allowed_profiles:
            raise ValueError(f"子代理默认 model_profile {selected} 不在白名单")
        return selected

    def bind(self, profile_name: str) -> ModelBinding:
        profile = self.profile(profile_name)
        provider = self.providers[profile.provider_id]
        if not profile.supports_tools:
            raise ValueError(f"profile {profile.name} 的模型不支持工具调用")
        fingerprint_payload = {
            "provider_id": provider.provider_id,
            "protocol": provider.protocol,
            "endpoint": provider.endpoint,
            "timeout_seconds": provider.timeout_seconds,
            # Authentication material and arbitrary headers must not become
            # an offline-verifiable public digest in a tool result/session.
            "profile": profile.name,
            "model_id": profile.model_id,
            "context_window": profile.context_window,
            "max_output_tokens": profile.max_output_tokens,
            "supports_tools": profile.supports_tools,
            "supports_streaming": profile.supports_streaming,
        }
        fingerprint = hashlib.sha256(json.dumps(
            fingerprint_payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        reference = ModelBindingRef(profile.name, provider.provider_id, provider.protocol, fingerprint)
        if provider.protocol == "openai_chat":
            from mini_agent.providers.openai_chat import OpenAIChatAdapter
            adapter = OpenAIChatAdapter(provider, profile)
        elif provider.protocol == "anthropic_messages":
            from mini_agent.providers.anthropic_messages import AnthropicMessagesAdapter
            adapter = AnthropicMessagesAdapter(provider, profile)
        else:  # defensive: ProviderConfig already validates this branch
            raise ValueError(f"不支持的 provider protocol: {provider.protocol}")
        return ModelBinding(profile, provider, adapter, reference, UsageMeter())

    def parent_binding(self) -> ModelBinding:
        return self.bind(self.resolve_parent_profile())

    def child_binding(self, requested: str | None = None) -> ModelBinding:
        return self.bind(self.resolve_child_profile(requested))


def legacy_binding(
    *, base_url: str, api_key: str, model: str, context_window: int = 128_000,
) -> ModelBinding:
    """Build a frozen compatibility binding for callers outside the CLI."""
    catalog = ProviderCatalog.from_settings(
        {}, {}, legacy_base_url=base_url, legacy_api_key=api_key,
        legacy_model=model, legacy_context_window=context_window,
    )
    return catalog.parent_binding()


def load_provider_catalog() -> ProviderCatalog:
    from mini_agent import config
    return ProviderCatalog.from_module(config)
