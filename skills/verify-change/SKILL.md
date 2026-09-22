---
name: verify-change
description: 按读取、修改、验证的顺序完成一次有界代码变更
---
# Verify a change

1. 先用 `read_file`、`list_dir` 或 `grep` 阅读相关实现、测试和项目指令，确认现有边界。
2. 用已有的修改工具完成最小变更；需要运行命令时，把命令交给 `run_shell`，并说明它是执行还是 verification。
3. 修改后用 `run_shell` 独立运行与改动相关的 verification，观察退出码和关键输出。
4. 如果验证失败，回到读取和诊断步骤，修正后再次独立验证。

这份 Skill 只描述工作顺序。它不会批准任何工具调用，也不会自动执行这里提到的命令；每个工具仍须经过自己的参数校验、Plan gate 和 PermissionGate。
