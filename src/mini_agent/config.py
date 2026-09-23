# ===== 配置（占位值，真实配置见 config_local.py） =====
# This module intentionally keeps the reference configuration small.  The
# catalog resolves relative paths against config_local.py when that file is
# present and freezes the resulting directories at runtime construction.
import os
import re
import ipaddress
from urllib.parse import urlsplit
from typing import Any

# 提交进 git 的模板。本地真实配置请写进 config_local.py（不进 git）。
# 用法：复制 config_example.py 为 config_local.py，填入你的真实值。
BASE_URL = "https://gateway.example.invalid/v1"
API_KEY = "sk-PLACEHOLDER_API_KEY"
MODEL = "model-PLACEHOLDER"
MEMORY_DIR = "~/.mini_agent/memory"
MEMORY_RETRIEVAL_ENABLED = True
REFERENCES = []
MCP_SERVERS = []
# v0.36 provider/profile mappings.  Empty mappings intentionally select the
# legacy BASE_URL/API_KEY/MODEL compatibility path above.
PROVIDERS = {}
MODEL_PROFILES = {}
PARENT_MODEL_PROFILE = "default"
SUBAGENT_MODEL_PROFILE = None
SUBAGENT_ALLOWED_MODEL_PROFILES = ("default",)
MAX_ITERATIONS = 50
CONTEXT_WINDOW = 128_000
CONTEXT_OBSERVABILITY = True
OUTPUT_MODE = "normal"  # quiet | normal | debug；终端输出级别
MAX_FAILURE_RETRIES = 3
MAX_ATTEMPT_FINGERPRINTS = 4
MAX_RECOVERY_ACTIONS = 8
MAX_REPAIR_CYCLES = 3
MAX_CHECKPOINT_BYTES = 1_048_576
MAX_SESSION_FILE_BYTES = 16 * 1024 * 1024
MAX_REPLAN_REVISIONS = 3
MAX_NO_PROGRESS_REPLANS = 2
MAX_STAGNANT_ROUNDS = 3

MCP_ALIAS_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
MCP_TOOL_NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
MAX_MCP_SERVERS = 32
MAX_MCP_COMMAND_ARGS = 64
MAX_MCP_STRING_CHARS = 4096
MAX_MCP_HTTP_URL_CHARS = 2048
MAX_MCP_HTTP_HEADERS = 32
MAX_MCP_HTTP_HEADER_NAME_CHARS = 128
MAX_MCP_HTTP_HEADER_VALUE_CHARS = 4096
MCP_HTTP_HEADER_NAME_PATTERN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")
MCP_TRANSPORTS = {"stdio", "http"}
MCP_MANAGED_HTTP_HEADERS = {
    "accept", "accept-encoding", "content-length", "content-type", "connection",
    "host", "keep-alive", "mcp-protocol-version", "mcp-session-id",
    "proxy-connection", "transfer-encoding", "upgrade",
}

# v0.38 parent-task delegation budgets.
MAX_SUBAGENTS = 3
MAX_CONCURRENCY = 2
# Descriptive alias kept for callers that namespace the child scheduler limit.
MAX_SUBAGENT_CONCURRENCY = MAX_CONCURRENCY
MAX_TOTAL_LLM_CALLS = 24
MAX_TOTAL_TOOL_CALLS = 72
MAX_TOTAL_TOKENS = 96_000

# 本地真实配置覆盖（config_local.py 不进 git）
try:
    from . import config_local as _local_config
except ImportError:
    _local_config = None

if _local_config is not None:
    from .config_local import *  # noqa: F401,F403

CONFIG_BASE_DIR = os.path.dirname(os.path.abspath(
    getattr(_local_config, "__file__", __file__)
))


def validate_runtime_config() -> None:
    """Validate bounded runtime budgets after local configuration overrides."""
    if not isinstance(MEMORY_DIR, str) or not MEMORY_DIR.strip():
        raise ValueError("MEMORY_DIR 必须是非空字符串")
    if not isinstance(MEMORY_RETRIEVAL_ENABLED, bool):
        raise ValueError("MEMORY_RETRIEVAL_ENABLED 必须是 bool")
    if not isinstance(REFERENCES, list):
        raise ValueError("REFERENCES 必须是数组")
    for index, item in enumerate(REFERENCES):
        if not isinstance(item, dict):
            raise ValueError(f"REFERENCES[{index}] 必须是对象")
        if set(item) != {"alias", "path", "description"}:
            raise ValueError(
                f"REFERENCES[{index}] 字段必须恰为 alias、path、description"
            )
        for field in ("alias", "path", "description"):
            if not isinstance(item[field], str):
                raise ValueError(f"REFERENCES[{index}].{field} 必须是字符串")
    validate_mcp_servers(MCP_SERVERS)
    for name in (
        "MAX_ATTEMPT_FINGERPRINTS", "MAX_REPLAN_REVISIONS",
        "MAX_NO_PROGRESS_REPLANS", "MAX_STAGNANT_ROUNDS", "MAX_SUBAGENTS",
        "MAX_CONCURRENCY", "MAX_TOTAL_LLM_CALLS", "MAX_TOTAL_TOOL_CALLS",
        "MAX_TOTAL_TOKENS",
    ):
        value = globals().get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} 必须是正整数")
    if MAX_STAGNANT_ROUNDS <= 1:
        raise ValueError("MAX_STAGNANT_ROUNDS 必须大于 1")
    if MAX_ATTEMPT_FINGERPRINTS < MAX_STAGNANT_ROUNDS + 1:
        raise ValueError(
            "MAX_ATTEMPT_FINGERPRINTS 必须不小于 MAX_STAGNANT_ROUNDS + 1"
        )
    if MAX_SUBAGENTS < MAX_CONCURRENCY:
        raise ValueError("MAX_SUBAGENTS 不能小于 MAX_CONCURRENCY")

def validate_mcp_servers(servers: Any, *, config_base_dir: str | None = None) -> None:
    """Validate local MCP server definitions without starting a process."""
    if not isinstance(servers, list):
        raise ValueError("MCP_SERVERS 必须是数组")
    if len(servers) > MAX_MCP_SERVERS:
        raise ValueError(f"MCP_SERVERS 不能超过 {MAX_MCP_SERVERS} 项")
    aliases: set[str] = set()
    for index, item in enumerate(servers):
        if not isinstance(item, dict):
            raise ValueError(f"MCP_SERVERS[{index}] 必须是对象")
        if set(item) - {
            "alias", "transport", "command", "cwd", "environment", "url", "headers",
            "allow_loopback_http", "agent_enabled", "readonly_tools",
        }:
            raise ValueError(f"MCP_SERVERS[{index}] 包含未知字段")
        alias = item.get("alias")
        if (
            not isinstance(alias, str)
            or MCP_ALIAS_PATTERN.fullmatch(alias) is None
        ):
            raise ValueError(
                f"MCP_SERVERS[{index}].alias 必须以小写字母开头，只能包含小写字母、数字、_、-"
            )
        folded = alias.casefold()
        if folded in aliases:
            raise ValueError(f"MCP server alias 重复: {alias}")
        aliases.add(folded)
        transport = item.get("transport", "stdio")
        if transport not in MCP_TRANSPORTS:
            raise ValueError(f"MCP_SERVERS[{index}].transport 必须是 stdio 或 http")
        if transport == "stdio":
            if any(field in item for field in ("url", "headers", "allow_loopback_http")):
                raise ValueError(f"MCP_SERVERS[{index}] 的 stdio 配置不能包含 HTTP 字段")
            command = item.get("command")
            if (
                not isinstance(command, list)
                or not command
                or len(command) > MAX_MCP_COMMAND_ARGS
                or any(
                    not isinstance(part, str)
                    or not part
                    or len(part) > MAX_MCP_STRING_CHARS
                    or "\x00" in part
                    for part in command
                )
            ):
                raise ValueError(
                    f"MCP_SERVERS[{index}].command 必须是非空字符串 argv 列表"
                )
            cwd = item.get("cwd")
            if cwd is not None and (
                not isinstance(cwd, str) or len(cwd) > MAX_MCP_STRING_CHARS or "\x00" in cwd
            ):
                raise ValueError(f"MCP_SERVERS[{index}].cwd 必须是字符串")
            environment = item.get("environment", {})
            if not isinstance(environment, dict):
                raise ValueError(f"MCP_SERVERS[{index}].environment 必须是字符串映射")
            for key, value in environment.items():
                if (
                    not isinstance(key, str)
                    or not key
                    or len(key) > MAX_MCP_STRING_CHARS
                    or "\x00" in key
                    or not isinstance(value, str)
                    or len(value) > MAX_MCP_STRING_CHARS
                    or "\x00" in value
                ):
                    raise ValueError(
                        f"MCP_SERVERS[{index}].environment 必须是字符串映射"
                    )
        else:
            if any(field in item for field in ("command", "cwd", "environment")):
                raise ValueError(f"MCP_SERVERS[{index}] 的 http 配置不能包含 stdio 字段")
            allow_loopback_http = item.get("allow_loopback_http", False)
            if not isinstance(allow_loopback_http, bool):
                raise ValueError(f"MCP_SERVERS[{index}].allow_loopback_http 必须是 bool")
            _validate_mcp_http_url(item.get("url"), index, allow_loopback_http)
            _validate_mcp_http_headers(item.get("headers", {}), index)
        agent_enabled = item.get("agent_enabled", False)
        if not isinstance(agent_enabled, bool):
            raise ValueError(f"MCP_SERVERS[{index}].agent_enabled 必须是 bool")
        readonly_tools = item.get("readonly_tools", [])
        if not isinstance(readonly_tools, list):
            raise ValueError(f"MCP_SERVERS[{index}].readonly_tools 必须是数组")
        readonly_seen: set[str] = set()
        for tool_index, tool_name in enumerate(readonly_tools):
            if (
                not isinstance(tool_name, str)
                or MCP_TOOL_NAME_PATTERN.fullmatch(tool_name) is None
            ):
                raise ValueError(
                    f"MCP_SERVERS[{index}].readonly_tools[{tool_index}] "
                    "必须是 1 至 64 个 ASCII 字母、数字、_ 或 -"
                )
            if tool_name in readonly_seen:
                raise ValueError(
                    f"MCP_SERVERS[{index}].readonly_tools 重复工具名: {tool_name}"
                )
            readonly_seen.add(tool_name)


def resolved_mcp_servers() -> list[dict[str, Any]]:
    """Return a detached config snapshot with relative cwd resolved locally."""
    validate_mcp_servers(MCP_SERVERS)
    base = CONFIG_BASE_DIR
    result: list[dict[str, Any]] = []
    for item in MCP_SERVERS:
        copied = {
            "alias": item["alias"],
            "transport": item.get("transport", "stdio"),
            "agent_enabled": item.get("agent_enabled", False),
            "readonly_tools": list(item.get("readonly_tools", [])),
        }
        if copied["transport"] == "stdio":
            copied.update({
                "command": list(item["command"]),
                "cwd": item.get("cwd"),
                "environment": dict(item.get("environment", {})),
            })
            if copied["cwd"] is not None and not os.path.isabs(copied["cwd"]):
                copied["cwd"] = os.path.abspath(os.path.join(base, copied["cwd"]))
        else:
            copied.update({
                "url": item["url"],
                "headers": dict(item.get("headers", {})),
                "allow_loopback_http": item.get("allow_loopback_http", False),
            })
        result.append(copied)
    return result


def _validate_mcp_http_url(value: Any, index: int, allow_loopback_http: bool) -> None:
    if not isinstance(value, str) or not value or len(value) > MAX_MCP_HTTP_URL_CHARS or "\x00" in value:
        raise ValueError(f"MCP_SERVERS[{index}].url 必须是有界非空字符串")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"MCP_SERVERS[{index}].url 含控制字符")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"MCP_SERVERS[{index}].url 端口无效") from error
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError(f"MCP_SERVERS[{index}].url 只支持 https 或 http URL")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ValueError(f"MCP_SERVERS[{index}].url 不能包含用户信息")
    if parsed.fragment:
        raise ValueError(f"MCP_SERVERS[{index}].url 不能包含片段")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f"MCP_SERVERS[{index}].url 端口无效")
    if parsed.scheme == "http":
        if not allow_loopback_http:
            raise ValueError(f"MCP_SERVERS[{index}] 必须显式开启 allow_loopback_http")
        if not _is_loopback_host(parsed.hostname):
            raise ValueError(f"MCP_SERVERS[{index}] 的 http URL 必须是回环地址")


def _is_loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_mcp_http_headers(value: Any, index: int) -> None:
    if not isinstance(value, dict) or len(value) > MAX_MCP_HTTP_HEADERS:
        raise ValueError(f"MCP_SERVERS[{index}].headers 必须是有界字符串映射")
    seen: set[str] = set()
    for name, header_value in value.items():
        if (
            not isinstance(name, str)
            or not name
            or len(name) > MAX_MCP_HTTP_HEADER_NAME_CHARS
            or MCP_HTTP_HEADER_NAME_PATTERN.fullmatch(name) is None
            or name.casefold() in MCP_MANAGED_HTTP_HEADERS
        ):
            raise ValueError(f"MCP_SERVERS[{index}].headers 含不受控或无效 Header 名")
        folded = name.casefold()
        if folded in seen:
            raise ValueError(f"MCP_SERVERS[{index}].headers 含重复 Header 名")
        seen.add(folded)
        if (
            not isinstance(header_value, str)
            or len(header_value) > MAX_MCP_HTTP_HEADER_VALUE_CHARS
            or any(ord(char) < 32 or ord(char) == 127 for char in header_value)
            or _not_latin1(header_value)
        ):
            raise ValueError(f"MCP_SERVERS[{index}].headers 的值必须是无控制字符字符串")


def _not_latin1(value: str) -> bool:
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        return True
    return False


validate_runtime_config()
