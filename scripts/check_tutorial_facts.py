#!/usr/bin/env python3
"""Validate tutorial version metadata and tag-pinned source links."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TUTORIALS = ROOT / "docs" / "tutorials"
REPO_URL = "https://github.com/liiiiiiiiil/agent-from-scratch/blob/"

EXPECTED = {
    "01-minimal-loop.md": ("v0.01", None),
    "02-first-tool.md": ("v0.02", "v0.01..v0.02"),
    "03-file-tools.md": ("v0.03", "v0.02..v0.03"),
    "04-permission-gate.md": ("v0.04", "v0.03..v0.04"),
    "05-streaming.md": ("v0.05", "v0.04..v0.05"),
    "06-concurrent-tool-calls.md": ("v0.06", "v0.05..v0.06"),
    "07-system-prompt.md": ("v0.07", "v0.06.1..v0.07"),
    "08-file-operations.md": ("v0.08", "v0.07..v0.08"),
    "09-permission-upgrade.md": ("v0.09", "v0.08..v0.09"),
    "10-shell-execution.md": ("v0.10", "v0.09..v0.10"),
    "11-context-architecture.md": ("v0.11", "v0.10..v0.11"),
    "12-token-budget-trimming.md": ("v0.12", "v0.11..v0.12"),
    "13-context-compaction.md": ("v0.13", "v0.12..v0.13"),
    "14-project-instructions.md": ("v0.14", "v0.13.1..v0.14"),
    "15-task-state.md": ("v0.15", "v0.14..v0.15"),
    "16-plan-driven-execution.md": ("v0.16", "v0.15..v0.16"),
}
PATCHES = {
    "06-concurrent-tool-calls.md": ("v0.06.1", "v0.06..v0.06.1"),
    "13-context-compaction.md": ("v0.13.1", "v0.13..v0.13.1"),
}
META_RE = re.compile(
    r"代码快照：`(?P<tag>v[0-9.]+)` · 相邻差异："
    r"(?:(?:`(?P<diff>v[0-9.]+\.\.v[0-9.]+)`)|无（首版）)"
)
LINK_RE = re.compile(re.escape(REPO_URL) + r"(?P<tag>v[0-9.]+)/(?P<path>[^)\s]+)")


def git_exists(spec: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", spec], cwd=ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    return result.returncode == 0


def check_tutorial(name: str, expected: tuple[str, str | None]) -> list[str]:
    text = (TUTORIALS / name).read_text(encoding="utf-8")
    metadata = [(m.group("tag"), m.group("diff")) for m in META_RE.finditer(text)]
    required = [expected]
    if name in PATCHES:
        required.append(PATCHES[name])
    errors = [f"缺少或错误的版本声明：{item}" for item in required if item not in metadata]

    declared_tags = {tag for tag, _ in required}
    for tag in declared_tags:
        if not git_exists(f"refs/tags/{tag}^{{commit}}"):
            errors.append(f"tag 不存在：{tag}")
    for _, baseline in required:
        if baseline:
            left, right = baseline.split("..", 1)
            if not git_exists(f"refs/tags/{left}^{{commit}}") or not git_exists(f"refs/tags/{right}^{{commit}}"):
                errors.append(f"diff 基线不存在：{baseline}")

    links = list(LINK_RE.finditer(text))
    if not links:
        errors.append("缺少 tag 固定源码链接")
    linked_tags = {match.group("tag") for match in links}
    for patch_tag in declared_tags:
        if patch_tag not in linked_tags:
            errors.append(f"代码快照没有源码链接：{patch_tag}")
    for match in links:
        tag, path = match.group("tag"), match.group("path")
        if tag not in declared_tags:
            errors.append(f"源码链接 tag 未在本课声明：{tag}/{path}")
        if not git_exists(f"{tag}:{path}"):
            errors.append(f"tag 中路径不存在：{tag}:{path}")
    return errors


def main() -> int:
    failed = False
    for name, expected in EXPECTED.items():
        errors = check_tutorial(name, expected)
        if errors:
            failed = True
            for error in errors:
                print(f"ERROR docs/tutorials/{name}: {error}")
        else:
            print(f"PASS  docs/tutorials/{name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
