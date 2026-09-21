# ===== 配置（占位值，真实配置见 config_local.py） =====
# This module intentionally keeps the reference configuration small.  The
# catalog resolves relative paths against config_local.py when that file is
# present and freezes the resulting directories at runtime construction.
import os

# 提交进 git 的模板。本地真实配置请写进 config_local.py（不进 git）。
# 用法：复制 config_example.py 为 config_local.py，填入你的真实值。
BASE_URL = "https://gateway.example.invalid/v1"
API_KEY = "sk-PLACEHOLDER_API_KEY"
MODEL = "model-PLACEHOLDER"
MEMORY_DIR = "~/.mini_agent/memory"
MEMORY_RETRIEVAL_ENABLED = True
REFERENCES = []
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


validate_runtime_config()
