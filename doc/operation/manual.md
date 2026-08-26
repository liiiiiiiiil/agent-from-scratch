# mini_agent 操作手册

## 1. 环境准备

### 1.1 依赖
- Python 3.9+（用了 `dict[str, Tool]` 等新语法）
- 零第三方依赖，仅 Python 标准库

### 1.2 安装方式

**方式一：开发模式安装（推荐）**
```bash
cd mini_agent
pip install -e .
```
安装后可从任意目录运行 `python -m mini_agent`。

**方式二：免安装，用 PYTHONPATH**
```bash
# Windows PowerShell
$env:PYTHONPATH="src"
python -m mini_agent
```
```bash
# Linux/macOS
PYTHONPATH=src python -m mini_agent
```

### 1.3 配置
配置硬编码在 `src/mini_agent/config.py`，修改后重启生效：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `BASE_URL` | `http://your-gateway-host/v3/openai/model` | LLM 网关地址 |
| `API_KEY` | `sk-...` | 网关密钥 |
| `MODEL` | `EB-GLM-5.2` | 模型名 |
| `MAX_ITERATIONS` | `10` | agent loop 最大轮数 |

> 当前不从环境变量读，改配置直接编辑 `config.py`。

---

## 2. 运行

### 2.1 单次任务模式
```bash
python -m mini_agent "读取 examples/input.txt 并总结"
```
任务完成后进入交互模式，可继续追问。

### 2.2 交互模式
```bash
python -m mini_agent
```
启动后进入 `你: ` 提示符，输入任务回车提交。输入 `exit` 或 `quit` 退出，或按 Ctrl+C/Ctrl+D。

### 2.3 相对路径约定
工具的相对路径（如 `examples/input.txt`）从**项目根目录** `mini_agent/` 起算：
```bash
# 在 mini_agent/ 目录下运行
python -m mini_agent "读取 examples/input.txt"
```

---

## 3. 工具

当前内置 3 个工具：

| 工具 | 参数 | 权限 | 说明 |
|---|---|---|---|
| `read_file` | `path: str` | allow | 读取文本文件 |
| `write_file` | `path: str, content: str` | **ASK** | 写文件，每次执行前问用户 |
| `calculate` | `expression: str` | allow | 计算数学表达式（仅数字与 `+-*/()` ） |

### 3.1 权限交互
`write_file` 执行前会提示：
```
允许执行 write_file({...})? [once/always/reject]
```
- `once`：本次允许，下次再问
- `always`：本轮运行内总是允许，不再问
- 其他输入：拒绝执行，工具返回拒绝原因给 LLM

### 3.2 工具调用流程
1. LLM 返回 `tool_calls`（一轮可含多个，代码用线程池并发执行）
2. Executor 先过权限闸门（`PermissionGate.guard`）
3. 通过则调 handler，失败则捕获异常返回错误信息给 LLM
4. 结果作为 `role=tool` 消息回灌，进入下一轮

---

## 4. 测试

### 4.1 运行 smoke test
```bash
# 需 PYTHONPATH=src（未 pip install 时）
$env:PYTHONPATH="src"; python tests/test_tools.py
```
覆盖：registry 注册、read_file、calculate、write_file 四项。

### 4.2 快速验证 import 链路
```bash
$env:PYTHONPATH="src"
python -c "from mini_agent.tools import registry, executor; print([t.name for t in registry.list_tools()])"
# 期望输出: ['read_file', 'calculate', 'write_file']
```

---

## 5. 常见问题

### Q1：运行报 502 / 连接网关失败
确认 `config.py` 的 `BASE_URL` 和 `API_KEY` 正确，且网络可达 `your-gateway-host`。
> 注意：必须用 `http.client`（代码已如此），不能用 requests/urllib——网关对 `Accept-Encoding: gzip` 响应异常。`call_llm` 已显式设 `Accept-Encoding: identity` 绕过。

### Q2：任务没完成就停了
可能触发 `MAX_ITERATIONS=10` 上限，agent 返回 `"达到最大迭代次数"`。改 `config.py` 调高即可，但注意长对话会累积上下文。

### Q3：工具失败直接报错退出
这是有意为之——agent loop 不加 try/except 兜底，保持核心逻辑清晰。工具层（`ToolExecutor.execute`）会捕获 handler 异常并返回错误信息给 LLM，但 loop 本身的异常会向上抛。

### Q4：write_file 被拒绝
检查权限交互的输入。选 `reject` 或输错字符会拒绝。重新运行即可。

### Q5：中文乱码（Windows 控制台）
`__main__.py` 已对 win32 设 `sys.stdout.reconfigure(encoding="utf-8")`。若仍乱码，PowerShell 执行 `chcp 65001` 切到 UTF-8。
