"""文件操作工具：read_file / write_file / edit_file / list_dir / grep。

v0.03：read_file / write_file
v0.08：read_file 加 offset/limit + 新增 edit_file / list_dir / grep
"""

import fnmatch
import os
import re

from mini_agent.tools.base import Tool


# ============================================================
# read_file：读取文本文件（支持 offset/limit 分段读取）
# ============================================================

def read_file(path: str, offset: int = 0, limit: int = 2000):
    """读取一个文本文件的内容，支持从指定行开始、限制行数。"""
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    total = len(lines)
    start = max(0, offset)
    end = min(start + limit, total)

    # 带行号前缀输出，便于 LLM 定位 edit_file 的 old_string
    numbered = [f"{i + 1:05d}| {lines[i]}" for i in range(start, end)]

    if end < total:
        suffix = f"\n(共 {total} 行，已读 {end - start} 行，还有 {total - end} 行未读)"
    else:
        suffix = f"\n(End of file - 共 {total} 行)" if total > 0 else ""

    return "".join(numbered) + suffix


read_file_tool = Tool(
    name="read_file",
    description="读取一个文本文件的内容，支持 offset/limit 分段读取大文件",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要读取的文件路径",
            },
            "offset": {
                "type": "integer",
                "description": "起始行号（0-based），默认 0",
            },
            "limit": {
                "type": "integer",
                "description": "读取行数，默认 2000",
            },
        },
        "required": ["path"],
    },
    handler=read_file,
)


# ============================================================
# write_file：写入文本文件（完整覆盖）
# ============================================================

def write_file(path: str, content: str):
    """将内容写入文本文件（覆盖写）。"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"已写入文件: {path}"


write_file_tool = Tool(
    name="write_file",
    description="将内容写入文本文件（完整覆盖）",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要写入的文件路径",
            },
            "content": {
                "type": "string",
                "description": "要写入的内容",
            },
        },
        "required": ["path", "content"],
    },
    handler=write_file,
    effect_class="possible",
)


# ============================================================
# edit_file：精确字符串替换（局部编辑）
# ============================================================

def edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False):
    """对文件做精确字符串替换。多匹配时需指定 replace_all 或提供更长的唯一上下文。"""
    if old_string == new_string:
        raise ValueError("old_string 与 new_string 不能相同")

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    count = content.count(old_string)
    if count == 0:
        raise ValueError(f"未找到匹配内容: {old_string[:50]}...")

    if count > 1 and not replace_all:
        raise ValueError(
            f"找到 {count} 处匹配，需指定 replace_all=true 或提供更长的唯一上下文"
        )

    if replace_all:
        new_content = content.replace(old_string, new_string)
        replaced = count
    else:
        new_content = content.replace(old_string, new_string, 1)
        replaced = 1

    with open(path, "w", encoding="utf-8") as f:
        f.write(new_content)

    return f"已替换 {replaced} 处: {path}"


edit_file_tool = Tool(
    name="edit_file",
    description="对文件做精确字符串替换（局部编辑），多匹配时需指定 replace_all 或提供更长上下文",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要编辑的文件路径",
            },
            "old_string": {
                "type": "string",
                "description": "要替换的文本",
            },
            "new_string": {
                "type": "string",
                "description": "替换后的文本（必须与 old_string 不同）",
            },
            "replace_all": {
                "type": "boolean",
                "description": "是否替换所有匹配处，默认 false",
            },
        },
        "required": ["path", "old_string", "new_string"],
    },
    handler=edit_file,
    effect_class="possible",
)


# ============================================================
# list_dir：列出目录内容
# ============================================================

def list_dir(path: str = "."):
    """列出目录内容，目录加 / 后缀。"""
    entries = []
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        kind = "/" if os.path.isdir(full) else ""
        entries.append(f"{name}{kind}")

    if not entries:
        return f"目录为空: {path}"

    # 上限 200 条防爆
    truncated = len(entries) > 200
    result = "\n".join(entries[:200])
    if truncated:
        result += f"\n(共 {len(entries)} 条，仅显示前 200 条)"
    return result


list_dir_tool = Tool(
    name="list_dir",
    description="列出目录内容，目录加 / 后缀",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要列出的目录路径，默认当前目录",
            },
        },
        "required": [],
    },
    handler=list_dir,
)


# ============================================================
# grep：正则搜索文件内容（纯标准库实现）
# ============================================================

def grep(pattern: str, path: str = ".", include: str = "*"):
    """用正则搜索目录下文件内容，返回 file:line: content 格式。"""
    regex = re.compile(pattern)
    results = []
    max_results = 100

    for root, dirs, files in os.walk(path):
        for fname in sorted(files):
            if not fnmatch.fnmatch(fname, include):
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    for i, line in enumerate(f, 1):
                        if regex.search(line):
                            results.append(f"{fpath}:{i}: {line.rstrip()}")
                            if len(results) >= max_results:
                                return "\n".join(results) + f"\n(结果已达 {max_results} 条上限)"
            except (PermissionError, OSError):
                continue

    if not results:
        return "无匹配"

    return "\n".join(results)


grep_tool = Tool(
    name="grep",
    description="用正则搜索目录下文件内容，返回 file:line: content 格式",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "正则表达式",
            },
            "path": {
                "type": "string",
                "description": "要搜索的目录路径，默认当前目录",
            },
            "include": {
                "type": "string",
                "description": "文件名 glob 过滤模式，例如 *.py，默认 *",
            },
        },
        "required": ["pattern"],
    },
    handler=grep,
)
