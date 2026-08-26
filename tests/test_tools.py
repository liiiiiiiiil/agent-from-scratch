"""Smoke test for mini_agent tools.

可独立运行：python tests/test_tools.py
（零第三方依赖，仅标准库）
"""

import os
import sys
import tempfile

# 让 tests/ 目录下也能 import 到 src 布局的包
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mini_agent.tools import registry, executor
from mini_agent.tools.base import ToolExecutor
from mini_agent.permission import PermissionGate, PermissionPolicy, ALLOW


def test_registry():
    names = [t.name for t in registry.list_tools()]
    assert names == ["read_file", "calculate", "write_file"], names
    print("PASS: registry 包含 read_file/calculate/write_file")


def test_read_file():
    path = os.path.join(os.path.dirname(__file__), "..", "examples", "input.txt")
    result = executor.execute("read_file", {"path": path})
    assert isinstance(result, str) and result.strip(), result
    print("PASS: read_file 读取 examples/input.txt 成功")


def test_calculate():
    result = executor.execute("calculate", {"expression": "3 + 5 * 2"})
    assert result == "13", result
    print("PASS: calculate 3 + 5 * 2 == 13")


def test_write_file():
    # write_file 默认是 ASK 权限（交互式），smoke test 用放行策略绕过
    gate = PermissionGate(PermissionPolicy({"write_file": ALLOW}))
    exec_allow = ToolExecutor(registry, gate=gate)
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "out.txt")
        result = exec_allow.execute("write_file", {"path": target, "content": "hello"})
        assert "已写入" in result, result
        with open(target, "r", encoding="utf-8") as f:
            assert f.read() == "hello"
    print("PASS: write_file 写入临时文件成功")


if __name__ == "__main__":
    test_registry()
    test_read_file()
    test_calculate()
    test_write_file()
    print("\n全部 smoke test 通过")
