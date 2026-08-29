# ===== 本地真实配置（不进 git） =====
# 用法：复制本文件为 config_local.py，填入你的真实值。
# config.py 会自动 import config_local.py 覆盖占位值。
BASE_URL = "http://your-gateway-host/v3/openai/model"
API_KEY = "sk-YOUR_API_KEY_HERE"
MODEL = "EB-GLM-5.2"
MAX_ITERATIONS = 50
CONTEXT_WINDOW = 128_000
