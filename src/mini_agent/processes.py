"""Bounded background-process runtime used by the v0.26 start tool.

The manager owns operating-system resources.  ``AgentState`` receives only
small, serializable facts through the synchronization boundary; it never
stores a ``Popen`` object, a pipe, or a collector thread.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import codecs
import json
import os
import signal
import subprocess
from threading import Condition, Event, Lock, Thread
import time
from typing import Any


DEFAULT_MAX_ACTIVE_PROCESSES = 4
DEFAULT_MAX_STREAM_BYTES = 64 * 1024
DEFAULT_GRACE_SECONDS = 2.0
DEFAULT_READ_CHARS = 2000
MAX_READ_CHARS = 4000
MAX_PROCESS_RESULT_CHARS = 8000
DEFAULT_WAIT_MS = 1000
MAX_WAIT_MS = 30000
MAX_STDIN_BYTES = 4096
STDIN_WRITE_TIMEOUT_SECONDS = 2.0
COMMAND_SUMMARY_MAX = 240
CWD_SUMMARY_MAX = 400


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _summary(value: str, limit: int) -> str:
    value = str(value)
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


class _ByteRing:
    """A byte ring with an absolute write position."""

    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("capacity 必须大于 0")
        self.capacity = capacity
        self.data = bytearray()
        self.total_written = 0
        self.base_offset = 0
        self.lock = Lock()

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        with self.lock:
            self.total_written += len(chunk)
            self.data.extend(chunk)
            if len(self.data) > self.capacity:
                discarded = len(self.data) - self.capacity
                del self.data[:discarded]
                self.base_offset += discarded

    def snapshot(self) -> tuple[int, int, bytes]:
        with self.lock:
            return self.total_written, self.base_offset, bytes(self.data)

    def read_from(self, offset: int, max_bytes: int) -> tuple[bytes, int, bool, int]:
        """Read from an absolute offset and report whether old data was lost."""
        if max_bytes <= 0:
            return b"", offset, False, 0
        with self.lock:
            requested = max(0, int(offset))
            gap = requested < self.base_offset
            lost = max(0, self.base_offset - requested) if gap else 0
            start = max(requested, self.base_offset)
            index = start - self.base_offset
            result = bytes(self.data[index:index + max_bytes])
            return result, start + len(result), gap, lost


@dataclass(frozen=True)
class ProcessStart:
    process_id: str
    task_id: str
    pid: int
    command: str
    cwd: str
    started_at: str
    status: str = "running"
    stdin_mode: str = "closed"


@dataclass(frozen=True)
class ProcessSyncFact:
    process_id: str
    task_id: str
    status: str
    pid: int
    started_at: str
    ended_at: str | None
    exit_code: int | None
    stdout_offset: int
    stderr_offset: int
    command: str
    cwd: str
    newly_exited: bool = False
    stdin_mode: str = "closed"
    stdin_state: str = "disabled"
    write_pending: bool = False
    stdin_error: str | None = None


@dataclass(frozen=True)
class CleanupItem:
    process_id: str
    task_id: str
    pid: int
    terminated: bool
    killed: bool
    complete: bool
    reason: str
    ended_at: str | None = None
    exit_code: int | None = None
    stdout_offset: int = 0
    stderr_offset: int = 0
    stdin_mode: str = "closed"
    stdin_state: str = "disabled"
    write_pending: bool = False
    stdin_error: str | None = None


@dataclass(frozen=True)
class CleanupReport:
    task_id: str
    items: tuple[CleanupItem, ...]

    @property
    def complete(self) -> bool:
        return all(item.complete for item in self.items)

    @property
    def incomplete(self) -> tuple[CleanupItem, ...]:
        return tuple(item for item in self.items if not item.complete)

    def render(self) -> str:
        if self.complete:
            return f"任务 {self.task_id} 的后台进程已清理。"
        details = "; ".join(
            f"pid={item.pid}, process_id={item.process_id}: {item.reason}"
            for item in self.incomplete
        )
        return f"任务 {self.task_id} 的后台进程清理不完整：{details}"


class _ManagedProcess:
    def __init__(self, process_id: str, task_id: str, command: str,
                 cwd: str, proc: subprocess.Popen[bytes], started_at: str,
                 max_stream_bytes: int, stdin_mode: str = "closed") -> None:
        self.process_id = process_id
        self.task_id = task_id
        self.command = command
        self.cwd = cwd
        self.proc = proc
        self.stdin_mode = stdin_mode
        # start_new_session=True makes the child the group leader.  Capture
        # its PID even if a very short command has already exited.
        self.process_group_id = proc.pid if os.name == "posix" else None
        self.started_at = started_at
        self.stdout_ring = _ByteRing(max_stream_bytes)
        self.stderr_ring = _ByteRing(max_stream_bytes)
        self.lock = Lock()
        self.read_lock = Lock()
        self.control_lock = Lock()
        self.stdin_write_lock = Lock()
        self.output_changed = Condition()
        self.stdout_cursor = 0
        self.stderr_cursor = 0
        self.ended_at: str | None = None
        self.exit_code: int | None = None
        self.exit_reported = False
        # Once the original group disappears, its numeric ID can be reused.
        # Never interpret a later occupant as one of our descendants.
        self.group_gone_confirmed = False
        self.closed = False
        self.output_pipes_closed = False
        self.stdout_eof = False
        self.stderr_eof = False
        self.stdin_state = "open" if stdin_mode == "pipe" else "disabled"
        self.stdin_error: str | None = None
        self.stdin_pipe_closed = stdin_mode != "pipe" or proc.stdin is None
        self.stdin_write_pending = False
        self.stdin_write_thread: Thread | None = None
        self._stdin_write_result: dict[str, Any] | None = None
        self.started_collectors: list[Thread] = []
        self.stdout_thread = Thread(
            target=self._collect, args=(proc.stdout, self.stdout_ring, "stdout"),
            name=f"mini-agent-{process_id}-stdout", daemon=True,
        )
        self.stderr_thread = Thread(
            target=self._collect, args=(proc.stderr, self.stderr_ring, "stderr"),
            name=f"mini-agent-{process_id}-stderr", daemon=True,
        )

    def start_collectors(self) -> None:
        for thread in (self.stdout_thread, self.stderr_thread):
            thread.start()
            self.started_collectors.append(thread)

    def _collect(self, stream: Any, ring: _ByteRing, name: str) -> None:
        try:
            while True:
                # BufferedReader.read() may wait for all 8192 bytes or EOF.
                chunk = stream.read1(8192)
                if not chunk:
                    with self.lock:
                        setattr(self, f"{name}_eof", True)
                    break
                ring.append(chunk)
                with self.output_changed:
                    self.output_changed.notify_all()
        except (OSError, ValueError):
            # An I/O error does not establish EOF; retain the process record.
            pass

    def _close_output_pipes(self) -> None:
        with self.lock:
            if self.output_pipes_closed:
                return
            self.output_pipes_closed = True
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass

    def _close_stdin_pipe(self, *, force: bool = False) -> None:
        """Close stdin once; callers must already have bounded the wait."""
        with self.stdin_write_lock:
            if self.stdin_pipe_closed:
                return
            self.stdin_pipe_closed = True
            stream = self.proc.stdin
        if force:
            # Do not call BufferedWriter.close() while its writer thread may
            # be inside a blocking flush.  Closing the descriptor is a
            # non-blocking escape hatch; the worker will report the resulting
            # pipe error and the normal close path can finish later.
            try:
                if stream is not None:
                    os.close(stream.fileno())
            except (OSError, ValueError):
                pass
            return
        try:
            if stream is not None:
                stream.close()
        except (OSError, ValueError):
            pass

    def _close_idle_stdin_after_exit(self) -> None:
        if self.stdin_mode != "pipe":
            return
        with self.stdin_write_lock:
            if self.stdin_write_pending or self.stdin_state != "open":
                return
            self.stdin_state = "closed"
        self._close_stdin_pipe()

    def _stdin_snapshot(self) -> tuple[str, str, bool, str | None]:
        with self.stdin_write_lock:
            return (
                self.stdin_mode,
                self.stdin_state,
                self.stdin_write_pending,
                self.stdin_error,
            )

    def refresh(self) -> ProcessSyncFact:
        code = self.proc.poll()
        if code is not None:
            self._close_idle_stdin_after_exit()
        group_gone = self._group_gone()
        with self.lock:
            stable_exit = (code is not None and group_gone
                           and self.stdout_eof and self.stderr_eof)
            if stable_exit and self.exit_code is None:
                self.exit_code = int(code)
                self.ended_at = self.ended_at or _timestamp()
            ended_at = self.ended_at
            exit_code = self.exit_code
            newly_exited = stable_exit and not self.exit_reported
        stdout_offset = self.stdout_ring.snapshot()[0]
        stderr_offset = self.stderr_ring.snapshot()[0]
        status = "running" if exit_code is None else (
            "exited" if exit_code == 0 else "failed"
        )
        stdin_mode, stdin_state, write_pending, stdin_error = self._stdin_snapshot()
        return ProcessSyncFact(
            self.process_id, self.task_id, status, self.proc.pid,
            self.started_at, ended_at, exit_code, stdout_offset,
            stderr_offset, self.command, self.cwd, newly_exited,
            stdin_mode, stdin_state, write_pending, stdin_error,
        )

    def _group_gone(self) -> bool:
        if os.name != "posix":
            return True
        if self.process_group_id is None:
            return False
        with self.lock:
            if self.group_gone_confirmed:
                return True
        try:
            os.killpg(self.process_group_id, 0)
        except ProcessLookupError:
            with self.lock:
                self.group_gone_confirmed = True
            return True
        except OSError:
            # Permission or another OS error is not proof of disappearance.
            return False
        return False

    def _wait(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        try:
            self.proc.wait(timeout=max(0.0, timeout))
        except subprocess.TimeoutExpired:
            return False
        if os.name != "posix":
            return True
        # The shell can exit while a descendant remains in the captured
        # process group.  Waiting only on Popen would incorrectly report a
        # complete cleanup and leave that descendant alive.
        while time.monotonic() < deadline:
            if self._group_gone():
                return True
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        return self._group_gone()

    def _signal_group(self, sig: int) -> tuple[bool, str]:
        with self.lock:
            already_exited = self.exit_code is not None
            group_gone = self.group_gone_confirmed
        if already_exited or (os.name == "posix" and group_gone):
            return True, "受管进程组已确认退出，无需发送信号"
        try:
            if os.name == "posix" and self.process_group_id is not None:
                # Use the group captured immediately after Popen.  The shell
                # can exit before a descendant does, so checking only poll()
                # would leave that descendant behind at a task boundary.
                os.killpg(self.process_group_id, sig)
                return True, "已发送进程组信号"
            if self.proc.poll() is not None:
                return True, "进程已经退出"
            if sig == signal.SIGTERM:
                self.proc.terminate()
            else:
                self.proc.kill()
            return True, "已控制直接子进程；无法确认 shell 派生子进程树"
        except (OSError, ProcessLookupError) as error:
            if self.proc.poll() is not None:
                return True, "进程在控制前已经退出"
            return False, f"控制失败: {type(error).__name__}: {error}"

    def cleanup(self, grace_seconds: float) -> CleanupItem:
        # A previously confirmed exit must only release handles.  Sending a
        # signal to its old PGID could hit an unrelated group after ID reuse.
        fact_before = self.refresh()
        if fact_before.status == "running":
            terminated, terminate_reason = self._signal_group(signal.SIGTERM)
            waited = self._wait(grace_seconds) if terminated else False
        else:
            terminated, terminate_reason = False, "进程已确认退出"
            waited = True
        killed = False
        kill_reason = ""
        if not waited:
            killed, kill_reason = self._signal_group(getattr(signal, "SIGKILL", signal.SIGTERM))
            waited = self._wait(grace_seconds) if killed else False
        # Closing a buffered pipe while a reader may still be blocked can
        # itself block indefinitely.  Keep all handles for a later retry.
        closed, close_reason = self.close(grace_seconds) if waited else (
            False, "子进程未确认结束，管道保持登记"
        )
        # Windows can confirm the direct child and inherited pipes, but the
        # standard-library control path cannot prove its shell descendants
        # have exited.  Keep the task registration rather than claim a full
        # task-boundary cleanup.
        scope_confirmed = os.name != "nt"
        complete = waited and closed and scope_confirmed
        reason = "; ".join(part for part in (
            kill_reason if killed else terminate_reason,
            "直接子进程或受管进程组未确认结束" if not waited else "",
            close_reason if not closed else "",
        ) if part)
        if os.name == "nt":
            reason = "; ".join(part for part in (
                reason, "已确认直接子进程及管道" if waited and closed else "",
                "无法确认任意 shell 派生进程树",
            ) if part)
        fact = self.refresh()
        return CleanupItem(
            self.process_id, self.task_id, self.proc.pid,
            terminated, killed, complete, reason, fact.ended_at, fact.exit_code,
            fact.stdout_offset, fact.stderr_offset, fact.stdin_mode,
            fact.stdin_state, fact.write_pending, fact.stdin_error,
        )

    def close(self, timeout: float) -> tuple[bool, str]:
        deadline = time.monotonic() + max(0.0, timeout)
        stdin_closed, stdin_reason = self._reap_stdin(max(0.0, deadline - time.monotonic()))
        if not stdin_closed:
            return False, stdin_reason
        for thread in tuple(self.started_collectors):
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in self.started_collectors):
            return False, "stdout/stderr 收集线程尚未结束，管道保持登记"
        with self.lock:
            eof = self.stdout_eof and self.stderr_eof
        if len(self.started_collectors) == 2 and not eof:
            return False, "stdout/stderr 未确认 EOF"
        # Unstarted collectors have no reader.  Their pipes can be closed
        # after the child/group is gone; started readers have already stopped.
        self._close_output_pipes()
        self._close_stdin_pipe()
        with self.lock:
            self.closed = True
        return True, ""

    def _write_stdin_worker(self, data: bytes, close_stdin: bool, done: Event) -> None:
        written = 0
        result: dict[str, Any]
        try:
            stream = self.proc.stdin
            if stream is None:
                raise OSError("stdin 管道不可用")
            if data:
                count = stream.write(data)
                written = len(data) if count is None else int(count)
                stream.flush()
            if close_stdin:
                stream.close()
                with self.stdin_write_lock:
                    self.stdin_pipe_closed = True
                    self.stdin_state = "closed"
                result = {
                    "status": "closed", "process_id": self.process_id,
                    "written_bytes": written, "closed": True,
                    "stdin_state": "closed", "write_pending": False,
                }
            else:
                with self.stdin_write_lock:
                    self.stdin_state = "open"
                result = {
                    "status": "written", "process_id": self.process_id,
                    "written_bytes": written, "closed": False,
                    "stdin_state": "open", "write_pending": False,
                }
        except BrokenPipeError as error:
            self._close_stdin_pipe()
            detail = _summary(f"{type(error).__name__}: {error}", 240)
            with self.stdin_write_lock:
                self.stdin_state = "error"
                self.stdin_error = detail
            result = {
                "status": "error", "process_id": self.process_id,
                "written_bytes": 0, "delivery_uncertain": bool(data), "closed": False,
                "stdin_state": "error", "write_pending": False,
                "error_kind": "broken_pipe", "message": detail,
            }
        except (OSError, ValueError) as error:
            self._close_stdin_pipe()
            detail = _summary(f"{type(error).__name__}: {error}", 240)
            with self.stdin_write_lock:
                self.stdin_state = "error"
                self.stdin_error = detail
            result = {
                "status": "error", "process_id": self.process_id,
                "written_bytes": 0, "delivery_uncertain": bool(data), "closed": False,
                "stdin_state": "error", "write_pending": False,
                "error_kind": "stdin_pipe_error", "message": detail,
            }
        except Exception as error:
            self._close_stdin_pipe()
            detail = _summary(f"{type(error).__name__}: {error}", 240)
            with self.stdin_write_lock:
                self.stdin_state = "error"
                self.stdin_error = detail
            result = {
                "status": "error", "process_id": self.process_id,
                "written_bytes": 0, "delivery_uncertain": bool(data), "closed": False,
                "stdin_state": "error", "write_pending": False,
                "error_kind": "stdin_write_error", "message": detail,
            }
        finally:
            with self.stdin_write_lock:
                self.stdin_write_pending = False
                self.stdin_write_thread = None
                self._stdin_write_result = result
            done.set()

    def write_stdin(self, input_text: str, close_stdin: bool = False) -> dict[str, Any]:
        if not isinstance(input_text, str):
            raise ValueError("input 必须是字符串")
        if not isinstance(close_stdin, bool):
            raise ValueError("close_stdin 必须是布尔值")
        try:
            data = input_text.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("input 必须是有效的 UTF-8 文本") from error
        if len(data) > MAX_STDIN_BYTES:
            raise ValueError(f"input UTF-8 编码后不能超过 {MAX_STDIN_BYTES} 字节")
        if not data and not close_stdin:
            raise ValueError("input 为空时必须设置 close_stdin=true")

        with self.stdin_write_lock:
            if self.stdin_mode != "pipe":
                return {
                    "status": "error", "process_id": self.process_id,
                    "written_bytes": 0, "closed": False,
                    "stdin_state": "disabled", "write_pending": False,
                    "error_kind": "stdin_not_enabled",
                    "message": "该进程未启用 stdin pipe",
                }
            if self.stdin_write_pending:
                return {
                    "status": "write_pending", "process_id": self.process_id,
                    "written_bytes": 0, "closed": False,
                    "stdin_state": "write_pending", "write_pending": True,
                    "error_kind": "write_pending",
                    "message": "上一笔 stdin 写入仍在途，不能重复投递",
                }
            if self.proc.poll() is not None:
                self.stdin_state = "closed"
                return {
                    "status": "error", "process_id": self.process_id,
                    "written_bytes": 0, "closed": True,
                    "stdin_state": "closed", "write_pending": False,
                    "error_kind": "process_exited", "message": "进程已提前退出",
                }
            if self.stdin_state == "closed":
                return {
                    "status": "error", "process_id": self.process_id,
                    "written_bytes": 0, "closed": True,
                    "stdin_state": "closed", "write_pending": False,
                    "error_kind": "stdin_closed", "message": "stdin 已关闭",
                }
            if self.stdin_state == "error":
                return {
                    "status": "error", "process_id": self.process_id,
                    "written_bytes": 0, "closed": False,
                    "stdin_state": "error", "write_pending": False,
                    "error_kind": "stdin_error",
                    "message": self.stdin_error or "stdin 管道已出错",
                }
            done = Event()
            self.stdin_write_pending = True
            self.stdin_state = "write_pending"
            self._stdin_write_result = None
            thread = Thread(
                target=self._write_stdin_worker,
                args=(data, close_stdin, done),
                name=f"mini-agent-{self.process_id}-stdin-write",
                daemon=True,
            )
            self.stdin_write_thread = thread
        try:
            thread.start()
        except Exception as error:
            detail = _summary(f"{type(error).__name__}: {error}", 240)
            with self.stdin_write_lock:
                self.stdin_write_pending = False
                self.stdin_write_thread = None
                self.stdin_state = "error"
                self.stdin_error = detail
            return {
                "status": "error", "process_id": self.process_id,
                "written_bytes": 0, "closed": False,
                "stdin_state": "error", "write_pending": False,
                "error_kind": "stdin_write_thread_error", "message": detail,
            }
        if not done.wait(STDIN_WRITE_TIMEOUT_SECONDS):
            return {
                "status": "write_pending", "process_id": self.process_id,
                "written_bytes": 0, "closed": False,
                "stdin_state": "write_pending", "write_pending": True,
                "error_kind": "write_pending",
                "message": "stdin 写入超过 2 秒仍未确认完成",
            }
        with self.stdin_write_lock:
            return dict(self._stdin_write_result or {
                "status": "error", "process_id": self.process_id,
                "written_bytes": 0, "closed": False,
                "stdin_state": "error", "write_pending": False,
                "error_kind": "stdin_write_error", "message": "stdin 写入结果不可用",
            })

    def write_preflight(self) -> dict[str, Any] | None:
        """Return a non-mutating rejection for a write that cannot start."""
        with self.stdin_write_lock:
            if self.stdin_mode != "pipe":
                return {
                    "status": "error", "process_id": self.process_id,
                    "written_bytes": 0, "closed": False,
                    "stdin_state": "disabled", "write_pending": False,
                    "error_kind": "stdin_not_enabled",
                    "message": "该进程未启用 stdin pipe",
                }
            if self.stdin_write_pending:
                return {
                    "status": "write_pending", "process_id": self.process_id,
                    "written_bytes": 0, "closed": False,
                    "stdin_state": "write_pending", "write_pending": True,
                    "error_kind": "write_pending",
                    "message": "上一笔 stdin 写入仍在途，不能重复投递",
                }
            if self.proc.poll() is not None:
                return {
                    "status": "error", "process_id": self.process_id,
                    "written_bytes": 0, "closed": True,
                    "stdin_state": "closed", "write_pending": False,
                    "error_kind": "process_exited", "message": "进程已提前退出",
                }
            if self.stdin_state == "closed":
                return {
                    "status": "error", "process_id": self.process_id,
                    "written_bytes": 0, "closed": True,
                    "stdin_state": "closed", "write_pending": False,
                    "error_kind": "stdin_closed", "message": "stdin 已关闭",
                }
            if self.stdin_state == "error":
                return {
                    "status": "error", "process_id": self.process_id,
                    "written_bytes": 0, "closed": False,
                    "stdin_state": "error", "write_pending": False,
                    "error_kind": "stdin_error",
                    "message": self.stdin_error or "stdin 管道已出错",
                }
        return None

    def _reap_stdin(self, timeout: float) -> tuple[bool, str]:
        if self.stdin_mode != "pipe":
            return True, ""
        deadline = time.monotonic() + max(0.0, timeout)
        with self.stdin_write_lock:
            thread = self.stdin_write_thread
        if thread is not None and thread.is_alive():
            # Closing the read end is what releases a writer blocked in flush;
            # the bounded join below decides whether cleanup can be confirmed.
            self._close_stdin_pipe(force=True)
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if thread is not None and thread.is_alive():
            return False, "stdin 写入线程尚未结束，管道保持登记"
        self._close_stdin_pipe()
        with self.stdin_write_lock:
            if self.stdin_write_pending:
                return False, "stdin 写入状态仍在途，管道保持登记"
            if self.stdin_state == "open":
                self.stdin_state = "closed"
        return True, ""


class ProcessManager:
    """Own all background process handles for one CLI lifecycle."""

    def __init__(self, max_active_processes: int = DEFAULT_MAX_ACTIVE_PROCESSES,
                 max_stream_bytes: int = DEFAULT_MAX_STREAM_BYTES,
                 grace_seconds: float = DEFAULT_GRACE_SECONDS) -> None:
        if max_active_processes <= 0:
            raise ValueError("max_active_processes 必须大于 0")
        if max_stream_bytes <= 0:
            raise ValueError("max_stream_bytes 必须大于 0")
        if grace_seconds < 0:
            raise ValueError("grace_seconds 不能小于 0")
        self.max_active_processes = max_active_processes
        self.max_stream_bytes = max_stream_bytes
        self.grace_seconds = grace_seconds
        self._lock = Lock()
        self._next_process = 1
        self._starting: dict[str, int] = {}
        self._processes: dict[str, _ManagedProcess] = {}

    def _allocate_id(self) -> str:
        with self._lock:
            process_id = f"proc-{self._next_process}"
            self._next_process += 1
            return process_id

    def start(self, command: str, cwd: str | None, task_id: str,
              stdin_mode: str = "closed") -> ProcessStart:
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command 必须是非空字符串")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task_id 必须是非空字符串")
        if stdin_mode not in ("closed", "pipe"):
            raise ValueError("stdin_mode 必须是 closed 或 pipe")
        resolved_cwd = os.getcwd() if cwd is None else os.path.abspath(os.path.expanduser(cwd))
        if not os.path.isdir(resolved_cwd):
            raise ValueError(f"cwd 不是存在的目录: {cwd}")
        with self._lock:
            active = sum(
                1 for item in self._processes.values()
                if item.task_id == task_id and item.refresh().status == "running"
            )
            starting = self._starting.get(task_id, 0)
            if active + starting >= self.max_active_processes:
                raise ValueError(f"活动后台进程已达到上限 {self.max_active_processes}")
            self._starting[task_id] = starting + 1
            process_id = f"proc-{self._next_process}"
            self._next_process += 1
        kwargs: dict[str, Any] = {
            "shell": True,
            "cwd": resolved_cwd,
            "stdin": subprocess.PIPE if stdin_mode == "pipe" else subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
        }
        if os.name == "posix":
            kwargs["start_new_session"] = True
        elif os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            proc = subprocess.Popen(command, **kwargs)
        except Exception:
            # The process id is intentionally consumed: IDs are never reused
            # after an unsuccessful start attempt.
            with self._lock:
                self._starting[task_id] = self._starting.get(task_id, 1) - 1
                if self._starting[task_id] <= 0:
                    self._starting.pop(task_id, None)
            raise
        managed = _ManagedProcess(
            process_id, task_id, _summary(command, COMMAND_SUMMARY_MAX),
            _summary(resolved_cwd, CWD_SUMMARY_MAX), proc, _timestamp(),
            self.max_stream_bytes, stdin_mode,
        )
        with self._lock:
            self._processes[process_id] = managed
            self._starting[task_id] = self._starting.get(task_id, 1) - 1
            if self._starting[task_id] <= 0:
                self._starting.pop(task_id, None)
        # Start both collectors before returning to the executor.
        try:
            managed.start_collectors()
        except Exception as error:
            # A thread-start failure must not leave the child outside the
            # State/CLI cleanup path.
            item = managed.cleanup(self.grace_seconds)
            if item.complete:
                with self._lock:
                    self._processes.pop(process_id, None)
            raise RuntimeError(
                f"收集线程启动失败: process_id={process_id}, pid={proc.pid}; "
                f"{type(error).__name__}: {error}; 清理: {item.reason}; "
                f"complete={item.complete}"
            ) from error
        return ProcessStart(
            process_id, task_id, proc.pid, managed.command, managed.cwd,
            managed.started_at, stdin_mode=stdin_mode,
        )

    def sync_processes(self, task_id: str) -> list[ProcessSyncFact]:
        with self._lock:
            processes = [item for item in self._processes.values() if item.task_id == task_id]
        return [item.refresh() for item in processes]

    def acknowledge_exit(self, process_id: str) -> None:
        """Mark an exit as committed by State after the atomic event write."""
        with self._lock:
            managed = self._processes.get(process_id)
        if managed is not None:
            with managed.lock:
                managed.exit_reported = True

    def active_process_ids(self, task_id: str) -> tuple[str, ...]:
        return tuple(
            fact.process_id for fact in self.sync_processes(task_id)
            if fact.status == "running"
        )

    def get_owned(self, task_id: str, process_id: str) -> _ManagedProcess | None:
        with self._lock:
            managed = self._processes.get(process_id)
        return managed if managed is not None and managed.task_id == task_id else None

    def control(self, task_id: str, process_id: str, *, kill: bool) -> dict[str, Any]:
        """Signal only a task-owned process and confirm the stable exit boundary."""
        managed = self.get_owned(task_id, process_id)
        if managed is None:
            raise ValueError("未知、过期或跨任务 process_id")
        with managed.control_lock:
            fact = managed.refresh()
            if fact.status != "running":
                stdin_closed, stdin_reason = managed._reap_stdin(self.grace_seconds)
                result = {"process_id": process_id, "status": "already_exited",
                          "exit_code": fact.exit_code}
                if not stdin_closed:
                    result["stdin_cleanup"] = "incomplete"
                    result["stdin_cleanup_reason"] = stdin_reason
                return result
            sent, reason = managed._signal_group(
                getattr(signal, "SIGKILL", signal.SIGTERM) if kill else signal.SIGTERM
            )
            if not sent:
                return {"process_id": process_id, "status": "error",
                        "error_kind": "control_failed", "message": reason}
            deadline = time.monotonic() + self.grace_seconds
            while True:
                fact = managed.refresh()
                if fact.status != "running":
                    stdin_closed, stdin_reason = managed._reap_stdin(self.grace_seconds)
                    result = {
                        "process_id": process_id,
                        "status": "killed" if kill else "terminated",
                        "exit_code": fact.exit_code,
                        "reason": reason + ("; 无法确认任意 shell 派生进程树"
                                           if os.name == "nt" else ""),
                    }
                    if not stdin_closed:
                        result["stdin_cleanup"] = "incomplete"
                        result["stdin_cleanup_reason"] = stdin_reason
                    return result
                if time.monotonic() >= deadline:
                    return {"process_id": process_id, "status": "still_running",
                            "reason": reason}
                time.sleep(min(0.01, deadline - time.monotonic()))

    def write_process(self, task_id: str, process_id: str, input_text: str,
                      close_stdin: bool = False) -> dict[str, Any]:
        """Write one bounded UTF-8 text payload to a task-owned stdin pipe."""
        managed = self.get_owned(task_id, process_id)
        if managed is None:
            raise ValueError("未知、过期或跨任务 process_id")
        result = managed.write_stdin(input_text, close_stdin)
        if result.get("error_kind") == "process_exited":
            # write_stdin discovers this race under its lock. Close outside
            # that lock so cleanup cannot mistake an open pipe for a closed one.
            managed._close_stdin_pipe()
        return result

    def write_preflight(self, task_id: str, process_id: str) -> dict[str, Any] | None:
        managed = self.get_owned(task_id, process_id)
        if managed is None:
            raise ValueError("未知、过期或跨任务 process_id")
        return managed.write_preflight()

    @staticmethod
    def _stream_fragment(ring: _ByteRing, cursor: int, char_limit: int,
                         json_budget: int, final: bool) -> tuple[str, int, bool, int, int]:
        total, base, data = ring.snapshot()
        gap = cursor < base
        lost = max(0, base - cursor)
        start = max(cursor, base)
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        chars: list[str] = []
        next_offset = start
        spent = 0
        for index, byte in enumerate(data[start - base:], start=start):
            produced = decoder.decode(bytes((byte,)), final=False)
            if not produced:
                continue
            cost = sum(len(json.dumps(char, ensure_ascii=False)) - 2 for char in produced)
            if len(chars) + len(produced) > char_limit or spent + cost > json_budget:
                break
            chars.extend(produced)
            spent += cost
            next_offset = index + 1
        else:
            if final and next_offset < total:
                produced = decoder.decode(b"", final=True)
                cost = sum(len(json.dumps(char, ensure_ascii=False)) - 2 for char in produced)
                if produced and len(chars) + len(produced) <= char_limit and spent + cost <= json_budget:
                    chars.extend(produced)
                    spent += cost
                    next_offset = total
        return "".join(chars), next_offset, gap, lost, spent

    def read_process(self, task_id: str, process_id: str,
                     max_chars: int = DEFAULT_READ_CHARS) -> dict[str, Any]:
        managed = self.get_owned(task_id, process_id)
        if managed is None:
            raise ValueError("未知、过期或跨任务 process_id")
        if not 1 <= max_chars <= MAX_READ_CHARS:
            raise ValueError(f"max_chars 必须在 1..{MAX_READ_CHARS} 之间")
        with managed.read_lock:
            fact = managed.refresh()
            # Reserve room for metadata and escaping; both streams share one
            # response budget.  Reserve half the first pass for stderr so a
            # chatty stdout cannot indefinitely hide its errors.
            budget = MAX_PROCESS_RESULT_CHARS - 1000
            stdout, out_next, out_gap, out_lost, cost = self._stream_fragment(
                managed.stdout_ring, managed.stdout_cursor, (max_chars + 1) // 2,
                budget // 2, fact.status != "running",
            )
            stderr, err_next, err_gap, err_lost, _ = self._stream_fragment(
                managed.stderr_ring, managed.stderr_cursor, max_chars - len(stdout),
                budget - cost, fact.status != "running",
            )
            result = {
                "process_id": process_id, "status": fact.status,
                "exit_code": fact.exit_code, "stdout": stdout, "stderr": stderr,
                "next_stdout_offset": out_next, "next_stderr_offset": err_next,
                "output_gap": out_gap or err_gap,
                "stdout_output_gap": out_gap, "stderr_output_gap": err_gap,
                "stdout_lost_bytes": out_lost, "stderr_lost_bytes": err_lost,
            }
            if len(json.dumps(result, ensure_ascii=False)) > MAX_PROCESS_RESULT_CHARS:
                raise RuntimeError("进程读取结果超过协议长度上限")
            managed.stdout_cursor = out_next
            managed.stderr_cursor = err_next
            return result

    def wait_process(self, task_id: str, process_id: str,
                     timeout_ms: int = DEFAULT_WAIT_MS) -> dict[str, Any]:
        managed = self.get_owned(task_id, process_id)
        if managed is None:
            raise ValueError("未知、过期或跨任务 process_id")
        if not 0 <= timeout_ms <= MAX_WAIT_MS:
            raise ValueError(f"timeout_ms 必须在 0..{MAX_WAIT_MS} 之间")
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            fact = managed.refresh()
            with managed.read_lock:
                stdout, _, stdout_gap, _, _ = self._stream_fragment(
                    managed.stdout_ring, managed.stdout_cursor, 1, 10, False,
                )
                stderr, _, stderr_gap, _, _ = self._stream_fragment(
                    managed.stderr_ring, managed.stderr_cursor, 1, 10, False,
                )
                output_available = bool(stdout or stderr or stdout_gap or stderr_gap)
            if fact.status != "running":
                reason = "exited"
            elif output_available:
                reason = "output_available"
            elif time.monotonic() >= deadline:
                reason = "still_running"
            else:
                with managed.output_changed:
                    managed.output_changed.wait(timeout=min(0.05, max(0, deadline - time.monotonic())))
                continue
            return {
                "process_id": process_id, "reason": reason,
                "status": fact.status, "exit_code": fact.exit_code,
                "stdout_offset": fact.stdout_offset,
                "stderr_offset": fact.stderr_offset,
            }

    def read_output(self, process_id: str, stdout_offset: int = 0,
                    stderr_offset: int = 0, max_bytes: int = 8000) -> dict[str, Any]:
        """Internal/future observation API; v0.26 does not register a tool."""
        with self._lock:
            managed = self._processes.get(process_id)
        if managed is None:
            raise ValueError("未知 process_id")
        stdout, next_stdout, stdout_gap, stdout_lost = managed.stdout_ring.read_from(stdout_offset, max_bytes)
        stderr, next_stderr, stderr_gap, stderr_lost = managed.stderr_ring.read_from(stderr_offset, max_bytes)
        fact = managed.refresh()
        return {
            "process_id": process_id,
            "status": fact.status,
            "exit_code": fact.exit_code,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "next_stdout_offset": next_stdout,
            "next_stderr_offset": next_stderr,
            "output_gap": stdout_gap or stderr_gap,
            "stdout_lost_bytes": stdout_lost,
            "stderr_lost_bytes": stderr_lost,
        }

    def cleanup(self, task_id: str) -> CleanupReport:
        with self._lock:
            processes = [item for item in self._processes.values() if item.task_id == task_id]
        items = []
        for managed in processes:
            item = managed.cleanup(self.grace_seconds)
            if item.complete:
                with self._lock:
                    self._processes.pop(managed.process_id, None)
            items.append(item)
        return CleanupReport(task_id, tuple(items))

    def close(self, task_id: str | None = None) -> CleanupReport | None:
        """Alias used by lifecycle owners; always scopes cleanup by task."""
        if task_id is None:
            return None
        return self.cleanup(task_id)
