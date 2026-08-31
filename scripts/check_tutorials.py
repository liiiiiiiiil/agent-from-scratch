#!/usr/bin/env python3
"""Check the minimum tutorial structure requirements."""
from __future__ import annotations
import re
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
TUTORIALS = ROOT / "docs" / "tutorials"
REQUIRED = ("本课目标","前置条件","新增与改动文件","为什么需要本版","关键流程","实现拆解","设计选择与边界","测试与验收","本版特性、下一课与代码索引")
def version_key(path: Path):
    match = re.search(r"(\d+)(?:-(\d+))?", path.name)
    return tuple(int(part) for part in match.groups(default="0")) if match else (0,)
def latest_tutorial():
    files = [p for p in TUTORIALS.glob("*.md") if re.search(r"\d", p.name)]
    if not files: raise RuntimeError("未找到版本教程")
    return max(files, key=version_key)
def check(path: Path):
    text = path.read_text(encoding="utf-8")
    errors = [f"缺少章节：## {s}" for s in REQUIRED if f"## {s}" not in text]
    if "git checkout" not in text: errors.append("缺少 git checkout 版本切换命令")
    if "git diff --stat" not in text: errors.append("缺少 git diff --stat 差异入口")
    if not re.search(r"\`\`\`(?:bash|sh|shell)[^\n]*\n[\s\S]*?(pytest|python tests/)", text): errors.append("缺少可执行测试命令")
    if not re.search(r"\[[^]]+\]\(/[^)]+\.py(?::\d+)?\)", text): errors.append("缺少实现文件索引链接")
    return errors
def main(argv):
    paths = [ROOT / arg for arg in argv] if argv else [latest_tutorial()]
    failed = False
    for path in paths:
        if not path.exists():
            print(f"ERROR {path}: 文件不存在"); failed = True; continue
        errors = check(path)
        if errors:
            failed = True
            for error in errors: print(f"ERROR {path.relative_to(ROOT)}: {error}")
        else: print(f"PASS  {path.relative_to(ROOT)}")
    return 1 if failed else 0
if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
