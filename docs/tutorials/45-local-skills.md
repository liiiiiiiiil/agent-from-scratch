# 第 45 课：本地 Skills 发现与按需加载

上一课：[MCP Tool 接入父 Agent Runtime](44-mcp-tools-runtime.md) · [教程总览](README.md) · 下一课：v0.46 MCP 能力扩展（规划中）

代码快照：`v0.45` · 相邻差异：`v0.44..v0.45`

本课示例命令使用 Bash/zsh。v0.45 tag 由维护者在交付后固定；阅读者切换前请确认本地已有该 tag。

## 本课目标

上一课把 MCP Tool 接入了父 Agent，但还有一类能力不适合做成 Tool：它们不增加新的执行动作，只告诉模型怎样安排已经存在的读取、修改、验证和 MCP Tool。把所有说明预先放进 system prompt 会浪费上下文，也会让模型难以分辨“建议”与“授权”。

本课建立一个更窄的入口：父 Agent 平时只看到本地 Skill 的 ID、来源级别和简短说明；模型明确调用 skill(name)，并通过权限检查后，才会收到对应 SKILL.md 正文。正文进入普通 role=tool 历史，仍然是不可信资料。Skill 可以建议运行测试，但它不能替模型批准 run_shell、写文件、推进 Plan 或进入 Subagent。

## 前置条件

需要 Python 3.10+、基础 Python、Bash/zsh 和 Git 知识。建议先阅读第 44 课，因为本课沿用它的 Registry、ToolExecutor、PermissionGate、Context 和父子工具隔离边界。

查看相邻版本时，可以执行：

~~~bash
git checkout v0.44
git diff --stat v0.44..v0.45
git checkout v0.45
~~~

第一条命令切到没有本地 Skills 的基线，第二条命令显示本课改动范围，最后一条命令切到本课源码。阅读结束后回到原来的分支：

~~~bash
git checkout -
~~~

## 新增与改动文件

本版的插入点在“父 Runtime 建立目录”和“Context 准备模型请求”之间。Catalog 负责冻结和重新核查文件，Tool 负责按需加载，Executor 负责摘要和错误边界，Context 只展示元数据。

| 文件 | 作用 |
|---|---|
| [skills.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.45/src/mini_agent/skills.py) | 扫描固定项目级/全局目录，解析受限 frontmatter，冻结文件身份并用目录 fd 安全加载。 |
| [tools/skill.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.45/src/mini_agent/tools/skill.py) | 注册父侧 skill(name)；返回有界 JSON，Skill 不进入固定 Subagent 工具面。 |
| [tools/base.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.45/src/mini_agent/tools/base.py) | 为正文保留有界完整工具结果，并把 State/Trace 摘要收缩到 ID、来源和字节数。 |
| [permission.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.45/src/mini_agent/permission.py) | 给 skill 默认 ask，用 Skill ID 作为权限 pattern，always 只记住精确 ID。 |
| [context.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.45/src/mini_agent/context.py) | 每次准备请求时按当前 PermissionPolicy 生成有界、不可信的目录提示，并计入输入预算。 |
| [prompt.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.45/src/mini_agent/prompt.py) | 明确 Skill 正文不能覆盖用户、项目指令、Plan、verification 或权限。 |
| [tools/__init__.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.45/src/mini_agent/tools/__init__.py) | 只给任务 Registry 装配 Skill Catalog；模块级兼容 Registry 不扫描 Skill。 |
| [test_skills_v045.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.45/tests/test_skills_v045.py) | 覆盖发现、覆盖优先级、格式限制、权限、替换、预算、历史和子代理边界。 |

## 版本变更定位

先看上一版的调用链。v0.44 的 MCP 目录在 Registry 中变成模型可调用的 Tool，正文和权限都由普通执行链负责。

~~~text
[旧] create_registry(state)
  → [旧] MCP / 内置 Tool Registry
  → [旧] ContextManager.prepare_messages()
  → [旧] AgentRuntime → ToolExecutor → role=tool
~~~

本版增加的是一个只提供工作流资料的旁路：目录提示进入请求视图，Skill 正文仍在普通 Tool 回合中出现。

~~~text
[+] workspace/skills/<name>/SKILL.md
    ~/.mini_agent/skills/<name>/SKILL.md
      → [+] SkillCatalog：frontmatter + 名称 + 文件身份冻结
      → [+] Context：按 PermissionPolicy 过滤 ID/来源/说明，按预算裁剪
      → [C] 模型提交 skill(name)
      → [C] PermissionGate(pattern=name)
      → [+] 目录 fd + O_NOFOLLOW + 身份/时间/大小复核
      → [C] ToolExecutor → 完整 role=tool 正文
           ├─ [B] deny、未知 ID、替换、符号链接、坏编码：安全失败
           └─ [B] 正文仍不能授权后续写文件、shell、Plan 或验证
~~~

图例：[旧] v0.44 已有，[+] v0.45 新增，[C] 主要消费者，[B] 本版边界。Catalog 的真实根目录和文件身份只留在当前进程，State/Trace 只保留安全摘要。

## 关键流程

### 1. 发现阶段只产生元数据

目录必须是工作区根下的 skills/<name>/SKILL.md 或用户目录下的 ~/.mini_agent/skills/<name>/SKILL.md。Catalog 不递归扫描，也不执行 Skill 目录里的脚本。项目级同名项覆盖全局项；如果项目级同名目录存在但 frontmatter 无效，全局项不会悄悄补位。

SKILL.md 只接受下面这种受限 frontmatter：

~~~text
---
name: verify-change
description: 按读取、修改、验证的顺序完成一次变更
---
# 正文从这里开始
~~~

name 必须以小写字母开头，只能包含小写字母、数字、_ 和 -，长度不超过 64，并且必须和目录名相同。frontmatter 只解析 name 和 description 两个单行字段；未知字段、重复字段、控制字符、坏 UTF-8、缺失字段和格式错误都会让该候选失效。单文件最多 32 KiB，说明最多 240 字符，两处根目录合计最多扫描 64 个直属目录项。项目级目录超限时 Catalog 留空，以免无法确定它是否遮蔽全局同名项。

发现结果会按名称排序。模型看到的目录提示最多 8 KiB，而且只含 skill_id、name、description 和 source。目录提示以独立的用户级资料消息放在原始任务前，不进入受保护的 system 消息；说明文字不能改变系统规则。

### 2. 加载阶段才返回正文

模型调用 skill({"name": "verify-change"}) 后，调用先经过 Registry 参数校验和 PermissionGate。默认权限是 ask；选择 always 时，策略只保存 verify-change 这个字面 ID，不会扩大到其他 Skill。当前规则为 deny 的 Skill 不会出现在目录提示中，但直接调用仍会在 Executor 中再次经过 Gate。

授权通过后，Catalog 从冻结根目录 fd 逐段打开 Skill 目录和 SKILL.md，拒绝符号链接，并在读取前后复核目录/文件身份、修改时间和大小。文件被替换、超限、失去访问权限或编码改变时，工具返回有界的 skill_access_error；真实路径和正文不会进入错误摘要。

成功工具结果的形状类似下面这样：

~~~json
{"status":"ok","skill_id":"verify-change","source":"project","bytes":412,"content":"# ..."}
~~~

content 只作为当前工具调用对应的普通 role=tool 内容提供给模型。ExecutionResult.output_excerpt、State 的工具历史和 Trace 只保留 ID、来源和字节数，因此不会把 Skill 正文复制到结构化任务状态中。

### 3. Skill 建议和 Tool 授权分开

正文可以写“修改后运行 PYTHONPATH=src python -m pytest -q”，但这只是工作流建议。真正的命令仍然是 run_shell 调用，需要自己的参数校验、Plan gate、PermissionGate、generation 和 verification 规则；正文不能伪造一次授权，也不会自动产生 verification evidence。

子代理继续只得到 calculate、read_file、list_dir、grep 四个固定工具。Skill Catalog、skill Tool、父权限和父 Context 都不会进入 Subagent。

## 实现拆解

SkillCatalog 在任务 Registry 创建时冻结。模块级 registry 仍用于旧的标准库 smoke test，因此不会因为导入 mini_agent.tools 就扫描用户目录。新任务、/new、/reset 和恢复任务都重新创建 Catalog；恢复不会从 session 信任旧的目录快照，也不会因为历史中已有正文而重新读文件。

Context 在每次 prepare_messages() 时重新读取当前策略的判定结果，只把仍然可见的元数据放进请求视图。这个消息不是 history 的一部分，也不会写进 Context/session 导出；正文如果已经作为工具结果进入 history，则遵循既有普通工具历史和 /save 规则。

下面的实现索引展示了两个边界。Catalog 的 load() 才返回正文，而 Executor 的 Skill 分支单独生成摘要：

~~~python
# SkillCatalog.load() 的结果会作为普通 Tool 输出
return {
    "status": "ok",
    "skill_id": definition.name,
    "source": definition.source,
    "bytes": len(raw),
    "content": body,
}
~~~

工具正文有独立的有界结果路径，避免现有通用 4 KiB 格式化器截断合法 Skill；State/Trace 仍只得到 metadata excerpt。这种拆分让模型能读到完整工作流，同时让结构化状态保持小而稳定。

## 为什么这样设计

Skill 和 Tool 分开，是因为二者解决的问题不同。Tool 代表一次可执行动作，必须经过权限和运行时状态；Skill 代表一段可以被核查的工作流说明，不能自行增加动作。把正文预先放进 system prompt 会扩大受保护提示词，也会模糊资料和规则的边界；把 Skill 做成可执行脚本则会绕过现有工具授权。

固定两层目录和受限 frontmatter 限制了发现能力，却带来稳定排序、确定性覆盖和容易检查的安全边界。文件身份在 Runtime 创建时冻结，意味着编辑 Skill 后当前任务不会热刷新；完成修改后开始新任务即可获得新目录。这也避免模型在一个任务中看到前后不一致的工作流。

本版刻意不读取 Skill 目录中的附属脚本、模板或其他文件，不支持远程 Skill、自动执行脚本、Marketplace、完整 YAML 或 Skill 间依赖。需要这些内容时，模型仍要明确调用现有文件工具，并重新经过对应权限和计划规则。

## 设计边界

- 目录提示是低信任元数据，Skill 正文是低信任工具资料；二者都不能覆盖用户要求、AGENTS.md、Plan、PermissionGate 或完成判定。
- 目录只在父 Context 中出现，Subagent 不继承 Skill 目录、正文、授权或父 history。
- /save 不额外复制 Skill 目录；但已经进入普通工具 history 的正文可以随会话保存，这是工具协议完整性的结果。
- Skill 是 effect_class="none"，因为读取本地说明本身不修改工作区；这不改变正文所建议的后续动作仍需独立授权的事实。
- 本版不新增 State/session schema 字段，也不把已加载 Skill 记录成 verification evidence。

## 运行与观察

可以在一个临时工作区创建最小 Skill，再用标准库调用观察“目录先有元数据、授权后才有正文”：

~~~bash
tmp_dir="$(mktemp -d)"
mkdir -p "$tmp_dir/skills/verify-change"
python - <<'PY' "$tmp_dir"
from pathlib import Path
import sys

root = Path(sys.argv[1])
(root / "skills/verify-change/SKILL.md").write_text(
    "---\nname: verify-change\ndescription: inspect, change, verify\n---\n"
    "# Workflow\n1. read_file\n2. edit_file\n3. run_shell for verification\n",
    encoding="utf-8",
)
PY
PYTHONPATH=src python - <<'PY' "$tmp_dir"
from pathlib import Path
import sys
from mini_agent.skills import SkillCatalog

catalog = SkillCatalog(Path(sys.argv[1]), global_root=Path(sys.argv[1]) / "no-global-skills")
print(catalog.directory_prompt())
PY
~~~

第一次输出只有 verify-change 的 ID、来源和说明，不会出现 # Workflow。在父 Runtime 中把 skill 的权限选择为 once 后，模型才会收到正文；随后若它请求 edit_file 或 run_shell，终端仍会显示对应工具自己的授权/阶段结果。这个现象证明 Skill 提供的是建议，不是执行批准。

## 本版特性、下一课与代码索引

v0.45 完成了本地 Skill 的固定目录发现、项目级覆盖、受限 frontmatter、按权限过滤的目录提示和获准后的安全按需加载。正文沿用普通工具回灌和 Context 裁剪，结构化 State/Trace 只记录无正文摘要，父子运行时能力边界保持不变。

下一课 v0.46 计划在现有 MCP Client 上增加受限远程 HTTP、文本 Resource 和用户选择的 Prompt；这些能力不会改变本课确立的本地 Skill 与执行授权边界。

固定代码索引：[skills.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.45/src/mini_agent/skills.py)、[tools/skill.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.45/src/mini_agent/tools/skill.py)、[context.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.45/src/mini_agent/context.py)、[permission.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.45/src/mini_agent/permission.py)、[test_skills_v045.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.45/tests/test_skills_v045.py)。
