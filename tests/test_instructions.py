"""Tests for project AGENTS.md discovery and loading."""

import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mini_agent.instructions import InstructionLoader


def test_discover_root_to_cwd_and_load_sources():
    with tempfile.TemporaryDirectory() as root:
        os.mkdir(os.path.join(root, ".git"))
        child = os.path.join(root, "src", "pkg")
        os.makedirs(child)
        open(os.path.join(root, "AGENTS.md"), "w", encoding="utf-8").write("root rule")
        open(os.path.join(root, "src", "AGENTS.md"), "w", encoding="utf-8").write("src rule")
        open(os.path.join(child, "AGENTS.md"), "w", encoding="utf-8").write("child rule")
        loader = InstructionLoader(child)
        paths = loader.discover()
        assert paths == [os.path.join(root, "AGENTS.md"), os.path.join(root, "src", "AGENTS.md"), os.path.join(child, "AGENTS.md")]
        loaded = loader.load()
        assert loaded.index("root rule") < loaded.index("src rule") < loaded.index("child rule")
        assert loaded.count("Source:") == 3


def test_non_git_directory_only_checks_cwd():
    with tempfile.TemporaryDirectory() as root:
        child = os.path.join(root, "child")
        os.mkdir(child)
        open(os.path.join(root, "AGENTS.md"), "w", encoding="utf-8").write("parent")
        assert InstructionLoader(child).discover() == []


def test_missing_and_unreadable_files_are_non_fatal():
    with tempfile.TemporaryDirectory() as root:
        os.mkdir(os.path.join(root, ".git"))
        assert InstructionLoader(root).discover() == []
        path = os.path.join(root, "AGENTS.md")
        open(path, "w", encoding="utf-8").write("rule")
        loader = InstructionLoader(root)
        assert "rule" in loader.load()

def test_empty_file_keeps_source_marker():
    with tempfile.TemporaryDirectory() as root:
        os.mkdir(os.path.join(root, ".git"))
        path = os.path.join(root, "AGENTS.md")
        open(path, "w", encoding="utf-8").close()
        loaded = InstructionLoader(root).load()
        assert f"Source: {path}" in loaded

def test_oserror_is_marked_and_does_not_abort():
    with tempfile.TemporaryDirectory() as root:
        os.mkdir(os.path.join(root, ".git"))
        path = os.path.join(root, "AGENTS.md")
        open(path, "w", encoding="utf-8").write("rule")
        real_open = open
        def failing_open(name, *args, **kwargs):
            if name == path:
                raise OSError("simulated read failure")
            return real_open(name, *args, **kwargs)
        with patch("builtins.open", side_effect=failing_open):
            loaded = InstructionLoader(root).load()
        assert "Source:" in loaded and "读取失败" in loaded


def test_load_is_truncated_at_limit_with_marker():
    with tempfile.TemporaryDirectory() as root:
        os.mkdir(os.path.join(root, ".git"))
        open(os.path.join(root, "AGENTS.md"), "w", encoding="utf-8").write("x" * 500)
        loaded = InstructionLoader(root, max_chars=80).load()
        assert len(loaded) <= 80
        assert "指令已截断" in loaded


if __name__ == "__main__":
    for name, value in list(globals().items()):
        if name.startswith("test_"):
            value()
    print("全部 instructions test 通过")
