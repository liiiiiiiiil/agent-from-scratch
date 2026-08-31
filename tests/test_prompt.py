"""Smoke test for mini_agent v0.07 system prompt.

验证 prompt.py 的分层组装：header / core_rules / environment。
可独立运行：python tests/test_prompt.py
（零第三方依赖，仅标准库）
"""

import os
import sys

# 让 tests/ 目录下也能 import 到 src 布局的包
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mini_agent.prompt import build_system_prompt, header, environment


def test_build_system_prompt():
    prompt = build_system_prompt()
    assert isinstance(prompt, str) and len(prompt) > 100
    # 三段都应在结果中
    assert "mini_agent" in prompt, "应含 header 身份文本"
    assert "<rules>" in prompt, "应含 core_rules"
    assert "<env>" in prompt, "应含 environment"
    print("PASS: build_system_prompt 组装成功，含 header/rules/env 三段")


def test_header_build():
    h = header("build")
    assert "mini_agent" in h
    print("PASS: header('build') 返回身份文本")


def test_header_unknown_fallback():
    h = header("nonexistent")
    assert h == header("build"), "未知 agent 应回退到 build"
    print("PASS: header 未知 agent 回退到 build")


def test_environment_fields():
    env = environment()
    assert "Working directory:" in env
    assert "Platform:" in env
    assert "Today's date:" in env
    assert "Is directory a git repo:" in env
    # 值非空
    assert "yes" in env or "no" in env
    print("PASS: environment 包含四项字段（工作目录/git/平台/日期）")


def test_project_instructions_section():
    prompt = build_system_prompt(project_instructions="Source: /tmp/AGENTS.md\nrule")
    assert "<project_instructions>" in prompt
    assert "rule" in prompt
    assert "<project_instructions>" not in build_system_prompt()


if __name__ == "__main__":
    test_build_system_prompt()
    test_header_build()
    test_header_unknown_fallback()
    test_environment_fields()
    test_project_instructions_section()
    print("\n全部 smoke test 通过")
