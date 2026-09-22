"""Bounded line-oriented stdio transport for a local MCP server."""
from __future__ import annotations

from dataclasses import dataclass
import os
import selectors
import subprocess
from threading import Lock, Thread
import time
from typing import Any, Mapping
from queue import Empty, Queue

from .protocol import (
    MAX_MESSAGE_BYTES,
    McpProtocolError,
    McpTimeoutError,
    McpTransportError,
    decode_message,
    encode_message,
)


MAX_STDOUT_QUEUE = 64
MAX_STDERR_BYTES = 16 * 1024
DEFAULT_STARTUP_TIMEOUT = 10.0
DEFAULT_CLOSE_TIMEOUT = 2.0


@dataclass(frozen=True)
class _StreamEnd:
    pass


class StdioTransport:
    """Start and manage one directly spawned stdio child process.

    The transport never uses a shell.  Its stdout reader parses one bounded
    JSON-RPC line at a time; stderr is drained into a bounded tail buffer and
    is never placed on the protocol queue.
    """

    def __init__(
        self,
        alias: str,
        command: list[str] | tuple[str, ...],
        *,
        cwd: str | None = None,
        environment: Mapping[str, str] | None = None,
        close_timeout: float = DEFAULT_CLOSE_TIMEOUT,
    ) -> None:
        self.alias = _safe_alias(alias)
        self.command = _copy_command(command)
        self.cwd = cwd
        self.environment = dict(environment or {})
        if close_timeout <= 0:
            raise ValueError("close_timeout must be positive")
        self.close_timeout = float(close_timeout)
        self._process: subprocess.Popen[bytes] | None = None
        self._messages: Queue[dict[str, Any] | _StreamEnd] = Queue(MAX_STDOUT_QUEUE)
        self._send_lock = Lock()
        self._state_lock = Lock()
        self._closed = False
        self._failure: McpTransportError | McpProtocolError | McpTimeoutError | None = None
        self._stderr_lock = Lock()
        self._stderr_tail = bytearray()
        self._threads: list[Thread] = []
        self._started = False
        self._close_report: dict[str, Any] = {"alias": self.alias, "closed": False}

    @classmethod
    def from_config(cls, config: Mapping[str, Any], *, close_timeout: float = DEFAULT_CLOSE_TIMEOUT) -> "StdioTransport":
        if not isinstance(config, Mapping):
            raise ValueError("MCP server configuration must be an object")
        return cls(
            config.get("alias", "unknown"),
            config.get("command", ()),
            cwd=config.get("cwd"),
            environment=config.get("environment", {}),
            close_timeout=close_timeout,
        )

    @property
    def pid(self) -> int | None:
        process = self._process
        return None if process is None else process.pid

    @property
    def returncode(self) -> int | None:
        process = self._process
        return None if process is None else process.poll()

    @property
    def is_alive(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None and not self._closed

    @property
    def close_report(self) -> dict[str, Any]:
        return dict(self._close_report)

    def stderr_tail(self) -> bytes:
        with self._stderr_lock:
            return bytes(self._stderr_tail)

    def start(self) -> "StdioTransport":
        with self._state_lock:
            if self._started:
                return self
            if self._closed:
                raise McpTransportError(f"stdio connection for alias {self.alias} is closed")
            environment = os.environ.copy()
            environment.update(self.environment)
            try:
                process = subprocess.Popen(
                    self.command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=self.cwd,
                    env=environment,
                    shell=False,
                    bufsize=0,
                )
            except (OSError, ValueError) as error:
                self._closed = True
                self._close_report = {"alias": self.alias, "closed": True}
                raise McpTransportError(f"failed to start stdio server alias {self.alias}") from error
            self._process = process
            self._started = True
            stdout_thread = Thread(target=self._read_stdout, name=f"mcp-{self.alias}-stdout", daemon=True)
            stderr_thread = Thread(target=self._read_stderr, name=f"mcp-{self.alias}-stderr", daemon=True)
            self._threads = [stdout_thread, stderr_thread]
            stdout_thread.start()
            stderr_thread.start()
        return self

    def send(self, message: Mapping[str, Any], *, timeout: float) -> None:
        payload = encode_message(message)
        self._ensure_usable()
        if timeout <= 0:
            raise McpTimeoutError(f"stdio send timed out for alias {self.alias}")
        process = self._process
        if process is None or process.stdin is None:
            self._fail(McpTransportError(f"stdio stdin is unavailable for alias {self.alias}"))
            raise self._failure  # type: ignore[misc]
        with self._send_lock:
            self._ensure_usable()
            file_descriptor = process.stdin.fileno()
            deadline = time.monotonic() + float(timeout)
            view = memoryview(payload)
            selector = selectors.DefaultSelector()
            try:
                os.set_blocking(file_descriptor, False)
                selector.register(file_descriptor, selectors.EVENT_WRITE)
                while view:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        error = McpTimeoutError(f"stdio send timed out for alias {self.alias}")
                        self._fail(error)
                        raise error
                    ready = selector.select(remaining)
                    if not ready:
                        error = McpTimeoutError(f"stdio send timed out for alias {self.alias}")
                        self._fail(error)
                        raise error
                    try:
                        count = os.write(file_descriptor, view)
                    except (BrokenPipeError, OSError) as error:
                        failure = McpTransportError(f"stdio write failed for alias {self.alias}")
                        self._fail(failure)
                        raise failure from error
                    if count <= 0:
                        failure = McpTransportError(f"stdio write failed for alias {self.alias}")
                        self._fail(failure)
                        raise failure
                    view = view[count:]
            finally:
                selector.close()
                try:
                    os.set_blocking(file_descriptor, True)
                except OSError:
                    pass

    def receive(self, *, timeout: float) -> dict[str, Any]:
        self._ensure_usable()
        if timeout <= 0:
            error = McpTimeoutError(f"stdio receive timed out for alias {self.alias}")
            self._fail(error)
            raise error
        deadline = time.monotonic() + float(timeout)
        while True:
            if self._failure is not None:
                raise self._failure
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                error = McpTimeoutError(f"stdio receive timed out for alias {self.alias}")
                self._fail(error)
                raise error
            try:
                item = self._messages.get(timeout=min(remaining, 0.05))
            except Empty:
                continue
            if isinstance(item, _StreamEnd):
                failure = self._failure or McpTransportError(
                    f"stdio server ended the connection for alias {self.alias}"
                )
                self._fail(failure)
                raise failure
            return item

    def close(self) -> None:
        """Close stdin, reap the direct child, and make repeated calls harmless."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            process = self._process
        if process is None:
            self._close_report = {"alias": self.alias, "closed": True}
            return

        reasons: list[str] = []
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                reasons.append("stdin close failed")

        deadline = time.monotonic() + self.close_timeout
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                process.terminate()
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                    process.wait(timeout=max(0.1, deadline - time.monotonic()))
                except (OSError, subprocess.TimeoutExpired):
                    reasons.append("child did not exit after termination")
        for thread in self._threads:
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(timeout=remaining)
        if process.poll() is None:
            reasons.append("child remains alive")
        self._close_report = {"alias": self.alias, "closed": not reasons}
        if reasons:
            self._close_report["reason"] = "; ".join(reasons)

    def _ensure_usable(self) -> None:
        if self._failure is not None:
            raise self._failure
        if self._closed:
            raise McpTransportError(f"stdio connection for alias {self.alias} is closed")
        if not self._started:
            raise McpTransportError(f"stdio connection for alias {self.alias} is not started")
        process = self._process
        if process is None or process.poll() is not None:
            failure = McpTransportError(f"stdio server is not running for alias {self.alias}")
            self._fail(failure)
            raise failure

    def _fail(self, error: McpTransportError | McpProtocolError | McpTimeoutError) -> None:
        with self._state_lock:
            if self._failure is None:
                self._failure = error

    def _read_stdout(self) -> None:
        process = self._process
        stream = None if process is None else process.stdout
        if stream is None:
            self._fail(McpTransportError(f"stdio stdout is unavailable for alias {self.alias}"))
            return
        while not self._closed:
            try:
                line = stream.readline(MAX_MESSAGE_BYTES + 2)
            except OSError:
                self._fail(McpTransportError(f"stdio stdout read failed for alias {self.alias}"))
                return
            if not line:
                if not self._closed:
                    try:
                        self._messages.put_nowait(_StreamEnd())
                    except Exception:
                        pass
                return
            if len(line) > MAX_MESSAGE_BYTES + 1:
                self._fail(McpProtocolError(f"stdio message exceeds the size limit for alias {self.alias}"))
                return
            try:
                message = decode_message(line)
            except (McpTransportError, McpProtocolError) as error:
                self._fail(error)
                return
            try:
                self._messages.put_nowait(message)
            except Exception:
                self._fail(McpTransportError(f"stdio stdout queue is full for alias {self.alias}"))
                return

    def _read_stderr(self) -> None:
        process = self._process
        stream = None if process is None else process.stderr
        if stream is None:
            return
        while not self._closed:
            try:
                chunk = stream.read(4096)
            except OSError:
                return
            if not chunk:
                return
            with self._stderr_lock:
                self._stderr_tail.extend(chunk)
                if len(self._stderr_tail) > MAX_STDERR_BYTES:
                    del self._stderr_tail[:-MAX_STDERR_BYTES]


def _safe_alias(value: Any) -> str:
    if isinstance(value, str) and value and len(value) <= 64 and "\x00" not in value:
        return value
    return "unknown"


def _copy_command(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("MCP command must be a non-empty argv list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or "\x00" in item:
            raise ValueError("MCP command entries must be non-empty strings")
        result.append(item)
    return result
