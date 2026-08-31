#!/usr/bin/env python3
"""file_search - 并发文件搜索 CLI 工具（纯标准库，零第三方依赖）

用法示例:
    python file_search.py "*.py" .                     # 按文件名通配符搜
    python file_search.py "*.py" src --content "TODO"  # 文件名 + 内容正则双条件
    python file_search.py "*" . --ignore ".git,node_modules" --workers 8

设计要点:
- 遍历: os.walk 原地剪枝被忽略的目录, 不进入 .git 等大目录
- 并发: ThreadPoolExecutor, 每个文件一个任务, IO 等待重叠
- 匹配: 文件名用 fnmatch 通配符, 内容用 re 正则(可选)
- 边界: 二进制/编码错误静默跳过; max-results 上限; 结果按路径排序保证稳定输出
"""

import argparse
import fnmatch
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass


@dataclass
class Match:
    path: str
    line_no: int = 0
    snippet: str = ""


def walk_files(root: str, ignore: list[str]):
    """递归产出所有文件路径, 原地剪枝被忽略的目录, 避免遍历 .git 等。"""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _ignored(d, ignore)]
        for f in filenames:
            if not _ignored(f, ignore):
                yield os.path.join(dirpath, f)


def _ignored(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in patterns)


def search_one(path: str, name_pattern: str, content_re: "re.Pattern | None") -> list[Match]:
    """单个文件的搜索任务(worker 函数)。"""
    if not fnmatch.fnmatch(os.path.basename(path), name_pattern):
        return []
    if content_re is None:
        return [Match(path)]

    results: list[Match] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line_no, line in enumerate(fh, 1):
                if content_re.search(line):
                    results.append(Match(path, line_no, line.rstrip()[:120]))
    except OSError:
        pass  # 权限/路径问题静默跳过
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description="并发文件搜索工具(标准库)")
    ap.add_argument("pattern", help="文件名匹配模式, fnmatch 通配符, 如 *.py")
    ap.add_argument("root", nargs="?", default=".", help="搜索根目录, 默认当前目录")
    ap.add_argument("--content", help="可选: 在文件内容中搜索的正则表达式")
    ap.add_argument("--ignore", default=".git,node_modules,__pycache__,.venv,*.pyc",
                    help="忽略的名称(逗号分隔, 支持通配符)")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 4,
                    help="并发 worker 数, 默认 CPU 核数")
    ap.add_argument("--max-results", type=int, default=100, help="最多输出结果数")
    args = ap.parse_args()

    content_re = re.compile(args.content) if args.content else None
    ignore = [p for p in args.ignore.split(",") if p]

    files = list(walk_files(args.root, ignore))
    if not files:
        print("未找到任何文件", file=sys.stderr)
        return 1

    found: list[Match] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(search_one, f, args.pattern, content_re): f for f in files}
        for fut in as_completed(futures):
            found.extend(fut.result())
            if len(found) >= args.max_results:
                break

    found = found[:args.max_results]
    for m in sorted(found, key=lambda m: (m.path, m.line_no)):
        print(f"{m.path}:{m.line_no}: {m.snippet}" if m.line_no else m.path)

    print(f"[done] {len(found)}/{len(files)} files scanned, {len(found)} results", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
