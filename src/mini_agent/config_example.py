# ===== 本地真实配置（不进 git） =====
# 用法：复制本文件为 config_local.py，填入你的真实值。
# config.py 会自动 import config_local.py 覆盖占位值。
BASE_URL = "https://gateway.example.invalid/v1"
API_KEY = "sk-PLACEHOLDER_API_KEY"
MODEL = "model-PLACEHOLDER"
MEMORY_DIR = "~/.mini_agent/memory"
MEMORY_RETRIEVAL_ENABLED = True

# 具名本地资料（真实路径只写入未跟踪的 config_local.py）。
# 相对 path 以 src/mini_agent/config_local.py 所在目录为基准。
# REFERENCES = [
#     {
#         "alias": "python-docs",
#         "path": "/absolute/or/relative/path",
#         "description": "本地 Python 设计资料",
#     },
# ]
REFERENCES = []

# Optional named Subagent roles. Built-in IDs (explorer, reviewer, tester,
# general) are reserved. This placeholder role is intentionally local-only;
# replace the ID and guidance with project-specific, non-secret text.
# AGENT_PROFILES = {
#     "docs_auditor": {
#         "description": "检查文档中的链接和术语一致性",
#         "prompt": "只检查已授权的文档范围，并引用具体证据。",
#         "tools": ["read_file", "list_dir", "grep"],
#         "permissions": {"grep": "allow", "read_file": "allow"},
#         "skills": [],
#         # "model_profile": "child-anthropic",
#     },
# }
AGENT_PROFILES = {}

# Local and loopback HTTP MCP servers.  They remain available to
# ``python -m mini_agent.mcp``; set ``agent_enabled=True`` to also connect them
# from the parent Agent.  HTTP uses only JSON responses in this version.
# Keep real commands, paths, and environment values in the untracked config_local.py.
MCP_SERVERS = [{
    "alias": "demo",
    "transport": "stdio",  # omit for the legacy stdio default
    "command": ["python", "path/to/mcp_server.py"],
    "cwd": ".",
    "environment": {},
    "agent_enabled": True,
    # Exact raw names from this Server's tools/list response.  These tools are
    # locally classified as effect_class="none" but still ask PermissionGate.
    "readonly_tools": ["echo"],
}]
# Example HTTP entry (keep the real URL and credentials in config_local.py):
# MCP_SERVERS = [{
#     "alias": "remote-demo",
#     "transport": "http",
#     "url": "https://mcp.example.invalid/mcp",
#     "headers": {"Authorization": "Bearer PLACEHOLDER"},
#     "allow_loopback_http": False,
#     "agent_enabled": True,
#     "readonly_tools": [],
# }]

# 推荐的新配置：provider 是服务身份，profile 是本地可请求的模型别名。
# 下面的值全部是占位值；真实值只应写入不进 git 的 config_local.py。
PROVIDERS = {
    "openai-gateway": {
        "protocol": "openai_chat",
        "endpoint": "https://api.example.invalid/v1/chat/completions",
        "api_key": "sk-PLACEHOLDER_OPENAI_KEY",
        "timeout_seconds": 120,
    },
    "anthropic-gateway": {
        "protocol": "anthropic_messages",
        "endpoint": "https://api.example.invalid/v1/messages",
        "api_key": "PLACEHOLDER_ANTHROPIC_KEY",
        "timeout_seconds": 120,
    },
}
MODEL_PROFILES = {
    "parent-openai": {
        "provider_id": "openai-gateway",
        "model_id": "placeholder-openai-model",
        "context_window": 128_000,
        "max_output_tokens": 8_192,
    },
    "child-anthropic": {
        "provider_id": "anthropic-gateway",
        "model_id": "placeholder-anthropic-model",
        "context_window": 200_000,
        "max_output_tokens": 8_192,
    },
}
PARENT_MODEL_PROFILE = "parent-openai"
SUBAGENT_MODEL_PROFILE = "child-anthropic"
SUBAGENT_ALLOWED_MODEL_PROFILES = ("child-anthropic",)

# 旧配置兼容示例（使用旧配置时将上面的两个映射设为空）：
# PROVIDERS = {}
# MODEL_PROFILES = {}
# PARENT_MODEL_PROFILE = "default"
# SUBAGENT_MODEL_PROFILE = None
# SUBAGENT_ALLOWED_MODEL_PROFILES = ("default",)
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
MAX_SUBAGENTS = 3
MAX_CONCURRENCY = 2
MAX_SUBAGENT_CONCURRENCY = MAX_CONCURRENCY
MAX_TOTAL_LLM_CALLS = 24
MAX_TOTAL_TOOL_CALLS = 72
MAX_TOTAL_TOKENS = 96_000
