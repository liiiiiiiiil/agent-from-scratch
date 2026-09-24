"""Interactive terminal input with an optional multiline editor."""

from __future__ import annotations

from queue import Empty, Queue
from threading import Thread
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

        def insert_newline(event):
            event.current_buffer.insert_text("\n")

        # prompt_toolkit versions and terminals encode Shift+Enter differently.
        # Use the named binding when available, then fall back to the common
        # Escape+Enter sequence instead of making enhanced input fatal.
        try:
            bindings.add("s-enter")(insert_newline)
        except ValueError:
            bindings.add("escape", "enter")(insert_newline)

        return self.session.prompt(
            prompt,
            multiline=True,
            key_bindings=bindings,
        )

    def read_while_polling(self, prompt: str, poll: Any, *, interval: float = 0.05) -> str:
        """Keep CLI lifecycle collection on the caller thread while input waits."""
        completed: Queue[tuple[bool, Any]] = Queue(maxsize=1)

        def reader() -> None:
            try:
                completed.put((True, self.read(prompt)))
            except BaseException as error:
                completed.put((False, error))

        Thread(target=reader, name="mini-agent-cli-input", daemon=True).start()
        while True:
            try:
                success, value = completed.get(timeout=interval)
            except Empty:
                poll()
                continue
            poll()
            if success:
                return value
            raise value
