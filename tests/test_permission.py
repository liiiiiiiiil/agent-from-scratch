"""Focused permission-policy regression checks used by the v0.42 plan."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mini_agent.permission import ALLOW, ASK, PermissionGate, PermissionPolicy  # noqa: E402


def test_reference_default_permission_actions():
    policy = PermissionPolicy()
    assert policy.check("list_references") == ALLOW
    assert policy.check("search_reference", "docs:.") == ASK
    assert policy.check("read_reference", "docs:guide.txt") == ASK


def test_reference_permission_pattern_and_prompt_are_alias_relative():
    args = {"alias": "docs", "path": "guide.txt", "query": "needle"}
    assert PermissionGate._extract_pattern("search_reference", args) == "docs:guide.txt"
    assert PermissionGate._extract_pattern(
        "read_reference", {"alias": "docs", "path": "guide.txt"},
    ) == "docs:guide.txt"
    prompt = PermissionGate._prompt_arguments("search_reference", args)
    assert "alias=docs" in prompt and "path=guide.txt" in prompt
    assert "needle" in prompt
