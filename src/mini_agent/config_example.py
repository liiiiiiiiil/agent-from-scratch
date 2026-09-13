# ===== 本地真实配置（不进 git） =====
# 用法：复制本文件为 config_local.py，填入你的真实值。
# config.py 会自动 import config_local.py 覆盖占位值。
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
