"""Tests for optional terminal input without requiring prompt_toolkit."""

import os
import sys
from threading import Event
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mini_agent.input_session import InputSession


def test_falls_back_to_builtin_input_without_optional_dependency():
    with patch("mini_agent.input_session._load_prompt_toolkit", return_value=None):
        session = InputSession()
    with patch("builtins.input", return_value="line one") as read_input:
        assert session.read("you: ") == "line one"
    read_input.assert_called_once_with("you: ")


def test_prompt_toolkit_mode_binds_enter_and_shift_enter():
    class FakeBindings:
        def __init__(self):
            self.handlers = {}

        def add(self, *keys):
            key = keys[0] if len(keys) == 1 else tuple(keys)
            def decorator(handler):
                self.handlers[key] = handler
                return handler
            return decorator

    class FakeBuffer:
        def __init__(self):
            self.submitted = False
            self.text = ""

        def validate_and_handle(self):
            self.submitted = True

        def insert_text(self, text):
            self.text += text

    class FakeSession:
        def __init__(self):
            self.buffer = FakeBuffer()
            self.kwargs = None

        def prompt(self, prompt, **kwargs):
            self.kwargs = kwargs
            bindings = kwargs["key_bindings"]
            event = type("Event", (), {"current_buffer": self.buffer})()
            handler = bindings.handlers.get("s-enter") or bindings.handlers.get(("escape", "enter"))
            handler(event)
            bindings.handlers["enter"](event)
            return "first\nsecond"

    fake_session = FakeSession()
    with patch("mini_agent.input_session._load_prompt_toolkit", return_value=(object, FakeBindings)):
        session = InputSession(session=fake_session)
        assert session.read("you: ") == "first\nsecond"
    assert fake_session.kwargs["multiline"] is True
    assert fake_session.buffer.text == "\n"
    assert fake_session.buffer.submitted is True


def test_polling_keeps_cli_thread_available_until_input_arrives():
    with patch("mini_agent.input_session._load_prompt_toolkit", return_value=None):
        session = InputSession()
    release_input = Event()
    polled = Event()

    def delayed_input(_prompt):
        assert release_input.wait(2)
        return "continue"

    def poll():
        polled.set()
        release_input.set()

    with patch("builtins.input", side_effect=delayed_input):
        assert session.read_while_polling("you: ", poll, interval=0.001) == "continue"
    assert polled.is_set()


if __name__ == "__main__":
    test_falls_back_to_builtin_input_without_optional_dependency()
    test_prompt_toolkit_mode_binds_enter_and_shift_enter()
    print("input session tests passed")
