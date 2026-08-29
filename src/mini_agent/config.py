# ===== 配置（占位值，真实配置见 config_local.py） =====
# 提交进 git 的模板。本地真实配置请写进 config_local.py（不进 git）。
# 用法：复制 config_example.py 为 config_local.py，填入你的真实值。
BASE_URL = "http://your-gateway-host/v3/openai/model"
API_KEY = "sk-YOUR_API_KEY_HERE"
MODEL = "EB-GLM-5.2"
MAX_ITERATIONS = 10
CONTEXT_WINDOW = 128_000

# 本地真实配置覆盖（config_local.py 不进 git）
try:
    from .config_local import *  # noqa: F401,F403
except ImportError:
    pass
