"""Interactive terminal input with an optional multiline editor."""

from __future__ import annotations

from typing import Any


def _load_prompt_toolkit() -> tuple[Any, Any] | None:
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.key_binding import KeyBindings
    except ImportError:
        return None
    return PromptSession, KeyBindings


class InputSession:
    """Read one user message, using prompt_toolkit when available."""

    def __init__(self, session: Any | None = None) -> None:
        loaded = _load_prompt_toolkit()
        self.enabled = loaded is not None
        if session is not None:
            self.session = session
        elif loaded is None:
            self.session = None
        else:
            prompt_session, _ = loaded
            self.session = prompt_session()

    def read(self, prompt: str) -> str:
        if self.session is None:
            return input(prompt)

        loaded = _load_prompt_toolkit()
        if loaded is None:
            return input(prompt)
        _, key_bindings = loaded
        bindings = key_bindings()

        @bindings.add("enter")
        def submit(event):
            event.current_buffer.validate_and_handle()

        @bindings.add("s-enter")
        def insert_newline(event):
            event.current_buffer.insert_text("\n")

        return self.session.prompt(
            prompt,
            multiline=True,
            key_bindings=bindings,
        )
