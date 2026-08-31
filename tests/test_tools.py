"""Smoke test for mini_agent v0.08 tools.

验证 Tool/ToolRegistry/ToolExecutor + calculate/read_file/write_file/edit_file/list_dir/grep + 权限闸门。
可独立运行：python tests/test_tools.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mini_agent.tools import create_registry, registry, executor
from mini_agent.state import AgentState
from mini_agent.tools.base import Tool, ToolRegistry, ToolExecutor
from mini_agent.permission import PermissionGate, PermissionPolicy, ALLOW, DENY, ASK


def test_registry():
    names = [t.name for t in registry.list_tools()]
    expected = ["calculate", "read_file", "write_file", "edit_file", "list_dir", "grep", "run_shell"]
    assert names == expected, names
    print(f"PASS: registry 包含 {len(expected)} 个工具")


def test_state_registry_todo_isolation():
    first_state, second_state = AgentState(), AgentState()
    first = ToolExecutor(create_registry(first_state), gate=PermissionGate(PermissionPolicy({"update_todo": ALLOW})))
    second = ToolExecutor(create_registry(second_state), gate=PermissionGate(PermissionPolicy({"update_todo": ALLOW})))
    first.execute("update_todo", {"todos": [{"content": "first"}]})
    assert first_state.snapshot()["todos"] == [{"content": "first", "status": "pending"}]
    assert second_state.snapshot()["todos"] == []
    assert first_state.snapshot()["tool_history"] == []
    print("PASS: Todo registry 与执行状态按 AgentState 隔离")


def test_calculate():
    result = executor.execute("calculate", {"expression": "3 + 5 * 2"})
    assert result == "13", result
    print("PASS: calculate 3 + 5 * 2 == 13")


def test_calculate_invalid():
    result = executor.execute("calculate", {"expression": "import os"})
    assert "失败" in result, result
    print("PASS: calculate 拒绝非法字符")


def test_read_file():
    path = os.path.join(os.path.dirname(__file__), "..", "examples", "input.txt")
    result = executor.execute("read_file", {"path": path})
    assert isinstance(result, str) and "3 + 5 * 2" in result, result
    print("PASS: read_file 读取 examples/input.txt 成功")


def test_read_file_offset_limit():
    # 写一个 5 行临时文件，测 offset/limit
    gate = PermissionGate(PermissionPolicy({"write_file": ALLOW}))
    exec_allow = ToolExecutor(registry, gate=gate)
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "lines.txt")
        exec_allow.execute("write_file", {"path": target, "content": "L0\nL1\nL2\nL3\nL4\n"})
        # offset=1, limit=2 → 读 L1, L2
        result = executor.execute("read_file", {"path": target, "offset": 1, "limit": 2})
        assert "L1" in result and "L2" in result, result
        assert "L0" not in result, f"offset 未生效: {result}"
        assert "L3" not in result, f"limit 未生效: {result}"
        assert "还有" in result, f"未提示剩余行: {result}"
    print("PASS: read_file offset/limit 分段读取生效")


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
    print("PASS: write_file 写入临时文件成功（放行策略）")


def test_edit_file():
    gate = PermissionGate(PermissionPolicy({"write_file": ALLOW, "edit_file": ALLOW}))
    exec_allow = ToolExecutor(registry, gate=gate)
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "edit.txt")
        exec_allow.execute("write_file", {"path": target, "content": "hello world\nfoo bar\n"})
        # 精确替换单处
        result = exec_allow.execute("edit_file", {
            "path": target, "old_string": "foo bar", "new_string": "BAZ",
        })
        assert "已替换 1 处" in result, result
        with open(target, "r", encoding="utf-8") as f:
            assert "BAZ" in f.read()
    print("PASS: edit_file 精确替换单处成功")


def test_edit_file_no_match():
    gate = PermissionGate(PermissionPolicy({"write_file": ALLOW, "edit_file": ALLOW}))
    exec_allow = ToolExecutor(registry, gate=gate)
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "nomatch.txt")
        exec_allow.execute("write_file", {"path": target, "content": "abc\n"})
        result = exec_allow.execute("edit_file", {
            "path": target, "old_string": "xyz", "new_string": "123",
        })
        assert "失败" in result, result
    print("PASS: edit_file 无匹配时报错")


def test_edit_file_multi_match():
    gate = PermissionGate(PermissionPolicy({"write_file": ALLOW, "edit_file": ALLOW}))
    exec_allow = ToolExecutor(registry, gate=gate)
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "multi.txt")
        exec_allow.execute("write_file", {"path": target, "content": "foo\nfoo\nfoo\n"})
        # 多匹配不传 replace_all → 应报错
        result = exec_allow.execute("edit_file", {
            "path": target, "old_string": "foo", "new_string": "bar",
        })
        assert "失败" in result and "3 处" in result, result
        # 多匹配传 replace_all=true → 应全部替换
        result = exec_allow.execute("edit_file", {
            "path": target, "old_string": "foo", "new_string": "bar", "replace_all": True,
        })
        assert "已替换 3 处" in result, result
        with open(target, "r", encoding="utf-8") as f:
            content = f.read()
            assert "foo" not in content
            assert content.count("bar") == 3
    print("PASS: edit_file 多匹配安全检查 + replace_all 生效")


def test_list_dir():
    examples = os.path.join(os.path.dirname(__file__), "..", "examples")
    result = executor.execute("list_dir", {"path": examples})
    assert "input.txt" in result, result
    print("PASS: list_dir 列出 examples 目录成功")


def test_grep():
    examples = os.path.join(os.path.dirname(__file__), "..", "examples")
    result = executor.execute("grep", {"pattern": r"3 \+ 5", "path": examples})
    assert "input.txt" in result and "3 + 5" in result, result
    print("PASS: grep 搜索 examples 目录成功")


def test_grep_no_match():
    examples = os.path.join(os.path.dirname(__file__), "..", "examples")
    result = executor.execute("grep", {"pattern": "ZZZ_NOT_EXIST", "path": examples})
    assert "无匹配" in result, result
    print("PASS: grep 无匹配时返回提示")


def test_permission_deny():
    # DENY 策略：write_file 被拒绝
    gate = PermissionGate(PermissionPolicy({"write_file": DENY}))
    exec_deny = ToolExecutor(registry, gate=gate)
    result = exec_deny.execute("write_file", {"path": "/tmp/x.txt", "content": "x"})
    assert "权限拒绝" in result, result
    print("PASS: write_file 被 DENY 策略拒绝")


def test_permission_pattern_allow():
    """二维权限：特定 pattern allow 覆盖默认 ask"""
    # 规则按顺序匹配，后出现的优先级更高（findLast 语义）
    # 所以通配 * 放前面，具体 *.txt 放后面覆盖
    policy = PermissionPolicy({"write_file": {"*": "ask", "*.txt": "allow"}})
    gate = PermissionGate(policy)
    exec_test = ToolExecutor(registry, gate=gate)
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "test.txt")
        result = exec_test.execute("write_file", {"path": target, "content": "ok"})
        assert "已写入" in result, result
    print("PASS: 二维权限 *.txt allow 覆盖默认 ask")


def test_permission_pattern_deny():
    """二维权限：特定 pattern deny 覆盖默认 allow"""
    # 通配 * 放前面，具体 *.env 放后面覆盖
    policy = PermissionPolicy({"read_file": {"*": "allow", "*.env": "deny"}})
    gate = PermissionGate(policy)
    exec_test = ToolExecutor(registry, gate=gate)
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "secret.env")
        with open(target, "w", encoding="utf-8") as f:
            f.write("SECRET=xxx")
        result = exec_test.execute("read_file", {"path": target})
        assert "权限拒绝" in result, result
    print("PASS: 二维权限 *.env deny 覆盖默认 allow")


def test_permission_always_pattern():
    """always 回复存 pattern，同类免问"""
    policy = PermissionPolicy({"write_file": "ask"})
    policy.approve("write_file", "*.txt")
    assert policy.check("write_file", "test.txt") == ALLOW, "approved *.txt 应放行"
    assert policy.check("write_file", "test.py") == ASK, "未 approved *.py 仍 ask"
    print("PASS: always 存 pattern，同类免问、异类仍 ask")


def test_permission_findlast_priority():
    """后出现的规则优先级更高（findLast 语义）"""
    policy = PermissionPolicy({"write_file": "ask"})
    policy.approve("write_file", "*")
    assert policy.check("write_file", "anything") == ALLOW, "approved 追加在末尾应覆盖 ask"
    print("PASS: findLast 后出现规则优先级更高")


def test_registry_duplicate():
    reg = ToolRegistry()
    reg.register(Tool(
        name="dummy", description="d", parameters={},
        handler=lambda: None,
    ))
    try:
        reg.register(Tool(
            name="dummy", description="d", parameters={},
            handler=lambda: None,
        ))
        assert False, "应抛 ValueError"
    except ValueError:
        pass
    print("PASS: registry 拒绝重复注册")


def test_executor_unknown():
    try:
        executor.execute("nonexistent", {})
        assert False, "应抛 ValueError"
    except ValueError:
        pass
    print("PASS: executor 对未知工具抛 ValueError")


def test_run_shell():
    """run_shell 执行简单命令"""
    result = executor.execute("run_shell", {"command": "echo hello_world"})
    assert "hello_world" in result, result
    print("PASS: run_shell 执行 echo 成功")


def test_run_shell_exit_code():
    """run_shell 返回非零退出码时带 [exit=N] 前缀"""
    # false/python 不在默认 allow 清单（落到 ask 会交互式提问），用放行策略绕过
    gate = PermissionGate(PermissionPolicy({"run_shell": ALLOW}))
    exec_allow = ToolExecutor(registry, gate=gate)
    # Windows 和 Linux 的 false 命令不同
    if sys.platform == "win32":
        cmd = "python -c \"exit(1)\""
    else:
        cmd = "false"
    result = exec_allow.execute("run_shell", {"command": cmd})
    assert "[exit=1]" in result, result
    print("PASS: run_shell 非零退出码带前缀")


def test_run_shell_permission():
    """二维权限：git * allow，rm * deny"""
    policy = PermissionPolicy({"run_shell": {"git *": "allow", "rm *": "deny", "*": "ask"}})
    gate = PermissionGate(policy)
    exec_test = ToolExecutor(registry, gate=gate)
    # git 命令放行
    result = exec_test.execute("run_shell", {"command": "git --version"})
    assert "git version" in result.lower() or "git" in result.lower(), result
    # rm 命令拒绝
    result = exec_test.execute("run_shell", {"command": "rm -rf /tmp/nonexist"})
    assert "权限拒绝" in result, result
    print("PASS: run_shell 二维权限 git allow / rm deny 生效")


if __name__ == "__main__":
    test_registry()
    test_state_registry_todo_isolation()
    test_calculate()
    test_calculate_invalid()
    test_read_file()
    test_read_file_offset_limit()
    test_write_file()
    test_edit_file()
    test_edit_file_no_match()
    test_edit_file_multi_match()
    test_list_dir()
    test_grep()
    test_grep_no_match()
    test_permission_deny()
    test_permission_pattern_allow()
    test_permission_pattern_deny()
    test_permission_always_pattern()
    test_permission_findlast_priority()
    test_registry_duplicate()
    test_executor_unknown()
    test_run_shell()
    test_run_shell_exit_code()
    test_run_shell_permission()
    print("\n全部 smoke test 通过")
