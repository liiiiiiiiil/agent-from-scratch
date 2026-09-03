#!/usr/bin/env python3
"""Check the Chinese main README's navigation and naming conventions."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
TUTORIALS = ROOT / "docs" / "tutorials"


def main() -> int:
    text = README.read_text(encoding="utf-8")
    errors: list[str] = []
    for heading in ("阶段一 ·", "阶段二 ·", "阶段三 ·", "阶段四 ·", "阶段五 ·", "阶段六 ·"):
        if heading not in text:
            errors.append(f"缺少学习阶段标题：{heading}")
    for stale in ("阶段四 · Context Management", "阶段六 · Reliable Execution"):
        if stale in text:
            errors.append(f"阶段标题未使用中文：{stale}")
    if "docs/tutorials/README.md" not in text:
        errors.append("缺少教程索引入口：docs/tutorials/README.md")
    for tutorial in sorted(TUTORIALS.glob("[0-9][0-9]-*.md")):
        if tutorial.name not in text:
            errors.append(f"学习路径未收录教程：{tutorial.name}")
    if not re.search(r"当前状态.*v0\.\d+", text):
        errors.append("缺少当前版本状态")
    if errors:
        for error in errors:
            print(f"ERROR README.md: {error}")
        return 1
    print("PASS  README.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
