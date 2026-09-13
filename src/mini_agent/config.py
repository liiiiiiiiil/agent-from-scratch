# ===== 配置（占位值，真实配置见 config_local.py） =====
# 提交进 git 的模板。本地真实配置请写进 config_local.py（不进 git）。
# 用法：复制 config_example.py 为 config_local.py，填入你的真实值。
BASE_URL = "http://your-gateway-host/v3/openai/model"
API_KEY = "sk-YOUR_API_KEY_HERE"
MODEL = "EB-GLM-5.2"
MAX_ITERATIONS = 50
CONTEXT_WINDOW = 128_000
CONTEXT_OBSERVABILITY = True
OUTPUT_MODE = "normal"  # quiet | normal | debug；终端输出级别
MAX_FAILURE_RETRIES = 3
MAX_ATTEMPT_FINGERPRINTS = 4
MAX_RECOVERY_ACTIONS = 8
MAX_REPAIR_CYCLES = 3
MAX_CHECKPOINT_BYTES = 1_048_576
MAX_REPLAN_REVISIONS = 3
MAX_NO_PROGRESS_REPLANS = 2
MAX_STAGNANT_ROUNDS = 3

# 本地真实配置覆盖（config_local.py 不进 git）
try:
    from .config_local import *  # noqa: F401,F403
except ImportError:
    pass


def validate_runtime_config() -> None:
    """Validate bounded runtime budgets after local configuration overrides."""
    for name in (
        "MAX_ATTEMPT_FINGERPRINTS", "MAX_REPLAN_REVISIONS",
        "MAX_NO_PROGRESS_REPLANS", "MAX_STAGNANT_ROUNDS",
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


validate_runtime_config()
