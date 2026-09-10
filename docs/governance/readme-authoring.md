# 主 README 编写规范

本文件定义中文主 README（`README.md`）的职责；英文说明维护在 `README_EN.md`，两者须保持入口、课程链接、阶段和版本状态一致。

## 角色与边界

- `README.md` 是项目总入口：说明项目、当前状态、运行入口、完整学习路径、主要目录、设计约束和文档入口。
- `docs/tutorials/README.md` 是学习指南：说明前置条件、学习路线、课程阅读方法和版本工作流；课程细节留在各课。
- `docs/operation/` 是最新版运行手册；`docs/plans/` 记录路线和意图；治理文档记录写作与决策规范。README 只链接它们，不复制其正文。

## 结构与命名

- 首屏说明项目是什么、适合谁、当前版本和阅读方式；主体保持快速开始、学习路径、项目结构、设计约束和文档入口。
- 学习路径按“大阶段 → 版本课程”组织，课程表、教程索引、计划和 CHANGELOG 的版本与链接保持一致。
- 阶段名和版本主题优先使用通俗中文；标题短而具体，避免营销口号和同义重复。必要的协议字段、代码标识和公认技术名词可保留英文。
- 命令必须可复制；配置示例不得包含真实 `BASE_URL`、`API_KEY` 或 `MODEL`。

## 更新与验收

新增版本或调整入口时，检查 `README.md`、`README_EN.md`、`docs/tutorials/README.md`、操作手册和 CHANGELOG 的版本、链接与状态，并运行：

```bash
PYTHONPATH=src python scripts/check_readme.py
```
