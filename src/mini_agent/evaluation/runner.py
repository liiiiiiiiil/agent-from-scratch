"""Isolated trial orchestration, independent grading, and atomic artifacts."""

from __future__ import annotations

from datetime import datetime, timezone
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Any
from urllib.parse import urlsplit

from mini_agent.evaluation.schema import (
    MAX_FIXTURE_BYTES, MAX_FIXTURE_FILE_BYTES, MAX_RESULT_BYTES, Case,
    TrialRequest, TrialResult, load_case, validate_fixture_directory,
    validate_trial_result,
)


MAX_DIFF_BYTES = 1024 * 1024
MAX_AGENT_LOG_BYTES = 64 * 1024
MAX_GRADER_LOG_BYTES = 64 * 1024
MAX_GRADER_RESULT_BYTES = 64 * 1024
MAX_WORKER_RESULT_BYTES = 64 * 1024
MAX_WORKSPACE_FILES = 256
MAX_WORKSPACE_BYTES = 8 * 1024 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _load_live_binding(case: Case):
    from mini_agent import config
    from mini_agent.providers.catalog import ProviderCatalog

    catalog = ProviderCatalog.from_module(config)
    profile_name = catalog.resolve_parent_profile(case.model_profile)
    binding = catalog.bind(profile_name)
    provider = binding.provider
    profile = binding.profile
    endpoint_host = (urlsplit(provider.endpoint).hostname or "").casefold()
    if (
        not provider.api_key
        or "placeholder" in provider.api_key.casefold()
        or not profile.model_id
        or "placeholder" in profile.model_id.casefold()
        or "example.invalid" in endpoint_host
        or ".invalid" in endpoint_host
    ):
        raise ValueError("live 配置仍包含模板值；请先配置本地模型 provider/profile")
    redactions = {provider.api_key, provider.endpoint, profile.model_id}
    for configured_provider in catalog.providers.values():
        if configured_provider.api_key:
            redactions.add(configured_provider.api_key)
        if configured_provider.endpoint:
            redactions.add(configured_provider.endpoint)
        for key, value in configured_provider.extra_headers.items():
            if value and any(term in key.casefold() for term in ("auth", "key", "token", "secret", "password")):
                redactions.add(str(value))
    for configured_profile in catalog.profiles.values():
        if configured_profile.model_id:
            redactions.add(configured_profile.model_id)
    return binding.reference.to_dict(), tuple(sorted((item for item in redactions if len(item) >= 3), key=len, reverse=True))


def _redact(text: str, values: tuple[str, ...]) -> str:
    rendered = text
    for value in values:
        rendered = rendered.replace(value, "<redacted>")
    rendered = re.sub(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+", r"\1<redacted>", rendered)
    rendered = re.sub(r"(?i)(api[_-]?key\s*[=:]\s*)[^\s,;\"']+", r"\1<redacted>", rendered)
    rendered = re.sub(r"\bsk-[A-Za-z0-9_./+=-]{8,}", "<redacted-key>", rendered)
    return rendered


def _read_limited(path: Path, limit: int) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raw = raw[:limit] + b"\n[truncated]\n"
    return raw.decode("utf-8", errors="replace")


def _process_environment(home: Path, *, include_proxy: bool) -> dict[str, str]:
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "TMPDIR": str(home / "tmp"),
        "TEMP": str(home / "tmp"),
        "TMP": str(home / "tmp"),
        "PYTHONUTF8": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    }
    if os.name == "nt" and os.environ.get("SystemRoot"):
        env["SystemRoot"] = os.environ["SystemRoot"]
    if include_proxy:
        for key, value in os.environ.items():
            if key.casefold() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}:
                env[key] = value
        for key in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
            if key in os.environ:
                env[key] = os.environ[key]
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    return env


def _terminate_group(process: subprocess.Popen[Any], grace_seconds: float = 1.5) -> tuple[bool, str | None]:
    if process.poll() is not None:
        return True, None
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        pass
    except OSError as error:
        return False, f"pid={process.pid} terminate={type(error).__name__}"
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        except OSError as error:
            return False, f"pid={process.pid} kill={type(error).__name__}"
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            return False, f"pid={process.pid} did not exit after kill"
    return process.poll() is not None, None


def _run_child(
    argv: list[str], *, cwd: Path, env: dict[str, str], timeout: float,
    stdout_path: Path, stderr_path: Path,
) -> dict[str, Any]:
    process = None
    started = time.monotonic()
    try:
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            kwargs: dict[str, Any] = {}
            if os.name == "posix":
                kwargs["start_new_session"] = True
            elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            process = subprocess.Popen(
                argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                stdout=stdout, stderr=stderr, **kwargs,
            )
            try:
                process.wait(timeout=timeout)
                timed_out = False
                interrupted = False
                cleanup_complete, cleanup_issue = True, None
            except (KeyboardInterrupt, SystemExit):
                timed_out = False
                interrupted = True
                cleanup_complete, cleanup_issue = _terminate_group(process)
            except subprocess.TimeoutExpired:
                timed_out = True
                interrupted = False
                cleanup_complete, cleanup_issue = _terminate_group(process)
        return {
            "started": True,
            "returncode": process.returncode,
            "timed_out": timed_out,
            "interrupted": interrupted,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "cleanup_complete": cleanup_complete,
            "cleanup_issue": cleanup_issue,
            "pid": process.pid,
            "launch_error": None,
        }
    except Exception as error:
        cleanup_complete, cleanup_issue = (True, None)
        if process is not None:
            cleanup_complete, cleanup_issue = _terminate_group(process)
        return {
            "started": process is not None,
            "returncode": process.poll() if process is not None else None,
            "timed_out": False,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "cleanup_complete": cleanup_complete,
            "cleanup_issue": cleanup_issue,
            "pid": process.pid if process is not None else None,
            "launch_error": type(error).__name__,
        }


def _restore_reference_permissions(root: Path) -> None:
    """Restore only runner-owned reference directories, without following links."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def restore(fd: int) -> None:
        os.fchmod(fd, 0o700)
        for name in os.listdir(fd):
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                continue
            child = os.open(name, flags, dir_fd=fd)
            try:
                restore(child)
            finally:
                os.close(child)

    fd = os.open(root, flags)
    try:
        restore(fd)
    finally:
        os.close(fd)


def _copy_fixture(source: Path, destination: Path) -> None:
    count, total = validate_fixture_directory(source)
    if count > 256 or total > MAX_FIXTURE_BYTES:
        raise ValueError("fixture_limit_exceeded")
    destination.mkdir(parents=True, mode=0o700, exist_ok=True)
    for current, dirs, files in os.walk(source, topdown=True, followlinks=False):
        dirs[:] = [directory for directory in dirs if directory != "__pycache__"]
        relative = Path(current).relative_to(source)
        target_dir = destination / relative
        target_dir.mkdir(parents=True, exist_ok=True)
        for directory in dirs:
            source_dir = Path(current) / directory
            if source_dir.is_symlink():
                raise ValueError("fixture_symlink_race")
            (target_dir / directory).mkdir(exist_ok=True)
        for name in files:
            if name.endswith((".pyc", ".pyo")):
                continue
            origin = Path(current) / name
            if origin.is_symlink() or not origin.is_file():
                raise ValueError("fixture_file_changed_during_copy")
            if origin.stat().st_size > MAX_FIXTURE_FILE_BYTES:
                raise ValueError("fixture_file_limit_exceeded")
            shutil.copyfile(origin, target_dir / name)


def _snapshot(root: Path) -> tuple[dict[str, bytes], int, int]:
    files: dict[str, bytes] = {}
    total = 0
    nodes = 0
    for current, dirs, names in os.walk(root, topdown=True, followlinks=False):
        for name in dirs:
            entry = Path(current) / name
            nodes += 1
            if entry.is_symlink():
                raise ValueError("workspace_symlink_detected")
        for name in names:
            entry = Path(current) / name
            nodes += 1
            if entry.is_symlink() or not entry.is_file():
                raise ValueError("workspace_special_file_detected")
            size = entry.stat().st_size
            total += size
            files[entry.relative_to(root).as_posix()] = entry.read_bytes()
    if len(files) > MAX_WORKSPACE_FILES or total > MAX_WORKSPACE_BYTES or nodes > MAX_WORKSPACE_FILES * 2:
        raise ValueError("workspace_output_limit_exceeded")
    return files, len(files), total


def _build_diff(before: dict[str, bytes], after: dict[str, bytes]) -> tuple[str, list[str]]:
    chunks: list[str] = []
    changed: list[str] = []
    size = 0
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name), after.get(name)
        if old == new:
            continue
        changed.append(name[:256])
        try:
            old_text = old.decode("utf-8") if old is not None else ""
            new_text = new.decode("utf-8") if new is not None else ""
        except UnicodeDecodeError:
            chunk = f"Binary file changed: {name} ({len(old or b'')} -> {len(new or b'')} bytes)\n"
        else:
            chunk = "".join(difflib.unified_diff(
                old_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
                fromfile=f"a/{name}" if old is not None else "/dev/null",
                tofile=f"b/{name}" if new is not None else "/dev/null",
            ))
        amount = len(chunk.encode("utf-8"))
        if size + amount > MAX_DIFF_BYTES:
            remaining = MAX_DIFF_BYTES - size
            if remaining > 0:
                chunks.append(chunk.encode("utf-8")[:remaining].decode("utf-8", errors="ignore"))
            chunks.append("\n[diff truncated]\n")
            break
        chunks.append(chunk)
        size += amount
    return "".join(chunks), changed[:256]


def _sanitize_json(value: Any, redactions: tuple[str, ...], *, budget: list[int]) -> Any:
    if budget[0] <= 0:
        return "<truncated>"
    budget[0] -= 1
    if isinstance(value, str):
        return _redact(value[:4000], redactions)
    if isinstance(value, dict):
        return {str(key)[:100]: _sanitize_json(item, redactions, budget=budget) for key, item in list(value.items())[:128]}
    if isinstance(value, list):
        return [_sanitize_json(item, redactions, budget=budget) for item in value[:128]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(type(value).__name__)


def _prepare_grader_result(stdout_path: Path, stderr_path: Path, redactions: tuple[str, ...]):
    stdout = _read_limited(stdout_path, MAX_GRADER_RESULT_BYTES)
    stderr = _read_limited(stderr_path, MAX_GRADER_RESULT_BYTES)
    sanitized_log = _redact((stdout + ("\n[stderr]\n" + stderr if stderr else ""))[:MAX_GRADER_LOG_BYTES], redactions)
    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, "invalid_result", sanitized_log
    if not isinstance(payload, dict) or not isinstance(payload.get("passed"), bool):
        return None, "invalid_result", sanitized_log
    safe = _sanitize_json(payload, redactions, budget=[1024])
    encoded = json.dumps(safe, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_GRADER_RESULT_BYTES:
        return None, "result_too_large", sanitized_log
    return safe, None, sanitized_log


def _atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            os.chmod(temporary, mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _code_revision() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[3],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=2, check=True, text=True,
        )
        revision = result.stdout.strip()
        return revision if re.fullmatch(r"[0-9a-f]{40,64}", revision) else None
    except Exception:
        return None


def _worker_payload(stdout_path: Path) -> dict[str, Any] | None:
    if not stdout_path.exists() or stdout_path.stat().st_size > MAX_WORKER_RESULT_BYTES:
        return None
    try:
        text = stdout_path.read_text(encoding="utf-8").strip()
        payload = json.loads(text)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return None
    required = {
        "trial_id", "agent_started", "agent_stop_reason", "agent_state_status", "agent_error_kind",
        "agent_duration_ms", "agent_llm_calls", "successful_model_responses",
        "permission_denials", "tool_calls", "input_tokens", "output_tokens",
        "token_accounting", "usage_source", "model_binding_ref", "cleanup_complete",
        "cleanup_issue",
    }
    return payload if required <= set(payload) else None


class EvaluationRunner:
    def validate(self, case_path: str | os.PathLike[str]) -> Case:
        return load_case(case_path)

    def validate_live_configuration(self, case: Case) -> tuple[dict[str, str], tuple[str, ...]]:
        return _load_live_binding(case)

    def run_suite(self, suite, output, **options) -> Path:
        from mini_agent.evaluation.benchmark import run_suite

        return run_suite(suite, output, runner=self, **options)

    def run_case(
        self,
        case: Case,
        output: str | os.PathLike[str],
        *,
        run_kind: str,
        live_confirmed: bool = False,
        responses: tuple[dict[str, Any], ...] = (),
        suite_metadata: dict[str, Any] | None = None,
        expected_model_binding_ref: dict[str, str] | None = None,
    ) -> Path:
        if run_kind not in {"live", "fixture"}:
            raise ValueError("run_kind must be live or fixture")
        if run_kind == "live" and not live_confirmed:
            raise ValueError("真实模型调用需要显式 --live")
        fixture_root = Path(case.case_dir) / case.fixture_dir
        grader_script = Path(case.case_dir) / case.grader_script
        validate_fixture_directory(fixture_root)
        if not grader_script.is_file() or grader_script.is_symlink():
            raise ValueError("grader_script 无效")
        descriptor = os.open(grader_script, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            grader_stat = os.fstat(descriptor)
            if not stat.S_ISREG(grader_stat.st_mode):
                raise ValueError("grader_script 必须是普通文件")
            with os.fdopen(descriptor, "rb", closefd=False) as grader_stream:
                grader_source = grader_stream.read(64 * 1024 + 1)
        finally:
            os.close(descriptor)
        if len(grader_source) > 64 * 1024:
            raise ValueError("grader_script 超过 64 KiB")
        grader_sha256 = hashlib.sha256(grader_source).hexdigest()
        if suite_metadata is not None:
            from mini_agent.evaluation.benchmark import runtime_fingerprint, tree_sha256

            if suite_metadata.get("grader_sha256") != grader_sha256:
                raise ValueError("suite grader 摘要在预检后发生变化")
            if tree_sha256(fixture_root) != suite_metadata.get("initial_fixture_sha256"):
                raise ValueError("suite initial fixture 摘要在预检后发生变化")
            if runtime_fingerprint() != suite_metadata.get("runtime_fingerprint"):
                raise ValueError("Agent runtime source changed before trial")
        model_ref: dict[str, str] | None = None
        redactions: tuple[str, ...] = ()
        if run_kind == "live":
            model_ref, redactions = self.validate_live_configuration(case)
            if expected_model_binding_ref is not None and model_ref != expected_model_binding_ref:
                raise ValueError("model_binding_changed_before_trial")

        output_root = Path(output).expanduser().absolute()
        if output_root.resolve(strict=False).is_relative_to(Path(case.case_dir).resolve()):
            raise ValueError("output 不能位于题目目录内")
        output_root.mkdir(parents=True, exist_ok=True)
        if not output_root.is_dir():
            raise ValueError("output 必须是目录")
        trial_id = str(uuid.uuid4())
        final_dir = output_root / f"trial-{case.case_id}-{trial_id[:8]}"
        if final_dir.exists():
            raise FileExistsError("trial result directory already exists")
        stage_dir = Path(tempfile.mkdtemp(prefix=".evaluation-trial-", dir=output_root))
        os.chmod(stage_dir, 0o700)
        tmp_root = Path(tempfile.mkdtemp(prefix=f"mini-agent-eval-{trial_id[:8]}-"))
        os.chmod(tmp_root, 0o700)
        started_at = _now()
        result_payload: dict[str, Any] | None = None
        worker_info: dict[str, Any] | None = None
        grader_info: dict[str, Any] | None = None
        grader_result = None
        grader_error = None
        grader_log = ""
        grader_passed = None
        changed_files: list[str] = []
        diff_text = ""
        workspace_limit_error = None
        cleanup_complete = True
        cleanup_issue = None
        runtime_fingerprint_end = None
        runtime_fingerprint_changed = False
        grader_reference = None
        workspace = tmp_root / "workspace"
        frozen_grader = tmp_root / "grader.py"
        frozen_grader.write_bytes(grader_source)
        os.chmod(frozen_grader, 0o400)
        home = tmp_root / "home"
        home.mkdir(mode=0o700)
        (home / "tmp").mkdir(mode=0o700)
        (home / ".mini_agent").mkdir(mode=0o700)
        (home / ".mini_agent" / "memory").mkdir(mode=0o700)
        try:
            _copy_fixture(fixture_root, workspace)
            if suite_metadata is not None:
                from mini_agent.evaluation.benchmark import tree_sha256

                if tree_sha256(workspace) != suite_metadata["initial_fixture_sha256"]:
                    raise ValueError("复制后的 trial workspace 与冻结 initial fixture 不一致")
                grader_reference = tmp_root / "grader-reference"
                _copy_fixture(workspace, grader_reference / "initial")
                for current, dirs, files in os.walk(grader_reference, topdown=False):
                    current_path = Path(current)
                    for name in files:
                        os.chmod(current_path / name, 0o400)
                    os.chmod(current_path, 0o500)
                os.chmod(grader_reference, 0o500)
            before, _before_count, _before_size = _snapshot(workspace)
            if run_kind == "fixture" and len(responses) > case.max_rounds:
                raise ValueError("fixture responses exceed case max_rounds")
            request = TrialRequest(
                trial_id=trial_id, run_kind=run_kind, case=case.to_dict(),
                workspace=str(workspace), responses=responses,
            )
            request_bytes = json.dumps(request.to_dict(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(request_bytes) > MAX_RESULT_BYTES:
                raise ValueError("trial request exceeds the 256 KiB limit")
            request_path = tmp_root / "worker-request.json"
            request_path.write_bytes(request_bytes)
            os.chmod(request_path, 0o600)
            start_marker = tmp_root / "agent-started.marker"
            worker_stdout = tmp_root / "worker.stdout"
            worker_stderr = tmp_root / "worker.stderr"
            worker_env = _process_environment(home, include_proxy=run_kind == "live")
            worker_info = _run_child(
                [sys.executable, "-m", "mini_agent.evaluation.worker", str(request_path), str(start_marker)],
                cwd=workspace, env=worker_env, timeout=case.agent_timeout_seconds,
                stdout_path=worker_stdout, stderr_path=worker_stderr,
            )
            if suite_metadata is not None:
                from mini_agent.evaluation.benchmark import runtime_fingerprint

                runtime_fingerprint_end = runtime_fingerprint()
                runtime_fingerprint_changed = runtime_fingerprint_end != suite_metadata["runtime_fingerprint"]
            result_payload = None if worker_info["timed_out"] else _worker_payload(worker_stdout)
            if result_payload is not None and result_payload.get("trial_id") != trial_id:
                result_payload = None
            if worker_info["timed_out"]:
                agent_stop = "timeout"
            elif worker_info.get("interrupted"):
                agent_stop = "interrupted"
            elif result_payload is None:
                agent_stop = "worker_error"
            else:
                agent_stop = result_payload.get("agent_stop_reason")
            try:
                after, _after_count, _after_size = _snapshot(workspace)
                diff_text, changed_files = _build_diff(before, after)
            except Exception as error:
                workspace_limit_error = type(error).__name__
            if worker_info["started"]:
                grader_stdout = tmp_root / "grader.stdout"
                grader_stderr = tmp_root / "grader.stderr"
                grader_env = _process_environment(home, include_proxy=False)
                grader_env["MINI_AGENT_EVALUATION_REFERENCE_ROOT"] = str(grader_reference or case.case_dir)
                grader_info = _run_child(
                    [sys.executable, str(frozen_grader), str(workspace)],
                    cwd=workspace, env=grader_env, timeout=case.grader_timeout_seconds,
                    stdout_path=grader_stdout, stderr_path=grader_stderr,
                )
                if grader_info.get("interrupted"):
                    grader_error = "interrupted"
                elif grader_info["timed_out"]:
                    grader_error = "timeout"
                    grader_log = _redact(_read_limited(grader_stdout, MAX_GRADER_LOG_BYTES), redactions)
                elif not grader_info["started"]:
                    grader_error = "launch_error"
                    grader_log = _redact(_read_limited(grader_stderr, MAX_GRADER_LOG_BYTES), redactions)
                elif grader_info["returncode"] != 0:
                    grader_error = "nonzero_exit"
                    _payload, _error, grader_log = _prepare_grader_result(grader_stdout, grader_stderr, redactions)
                else:
                    grader_result, grader_error, grader_log = _prepare_grader_result(
                        grader_stdout, grader_stderr, redactions,
                    )
                    if grader_error is None:
                        grader_passed = bool(grader_result["passed"])
            agent_cleanup_ok = bool(worker_info["cleanup_complete"])
            grader_cleanup_ok = bool(grader_info is None or grader_info["cleanup_complete"])
            cleanup_complete = agent_cleanup_ok and grader_cleanup_ok
            cleanup_issue = worker_info.get("cleanup_issue") or (grader_info or {}).get("cleanup_issue")
            if not cleanup_complete and cleanup_issue is None:
                pid = worker_info.get("pid") if not agent_cleanup_ok else (grader_info or {}).get("pid")
                cleanup_issue = f"子进程清理未确认 pid={pid}"
            if worker_info["timed_out"]:
                agent_duration_ms = worker_info["duration_ms"]
                failure_kind = "agent_timeout"
            elif worker_info.get("interrupted"):
                agent_duration_ms = worker_info["duration_ms"]
                failure_kind = "agent_interrupted"
            elif not worker_info["started"]:
                agent_duration_ms = 0
                failure_kind = "infrastructure_error"
            elif result_payload is None:
                agent_duration_ms = worker_info["duration_ms"]
                failure_kind = "infrastructure_error"
            else:
                agent_duration_ms = max(0, int(result_payload.get("agent_duration_ms", worker_info["duration_ms"])))
                if result_payload.get("agent_error_kind"):
                    failure_kind = "agent_error"
                elif result_payload.get("agent_stop_reason") != "text" or result_payload.get("agent_state_status") != "done":
                    failure_kind = "task_failed"
                else:
                    failure_kind = "none"
            if workspace_limit_error is not None:
                failure_kind = "agent_output_limit"
            if grader_error and failure_kind == "none":
                failure_kind = "grader_infrastructure_error"
            if not cleanup_complete:
                failure_kind = "infrastructure_error"
            if runtime_fingerprint_changed:
                failure_kind = "infrastructure_error"
            observed_binding = result_payload.get("model_binding_ref") if result_payload else None
            if (
                expected_model_binding_ref is not None
                and observed_binding is not None
                and observed_binding != expected_model_binding_ref
            ):
                failure_kind = "infrastructure_error"
            success = bool(
                failure_kind == "none"
                and grader_passed is True
                and cleanup_complete
                and result_payload is not None
                and int(result_payload.get("successful_model_responses", 0)) > 0
            )
            if not success and failure_kind == "none":
                failure_kind = "task_failed"
            agent_log_payload = {
                "worker_returncode": worker_info["returncode"],
                "worker_pid": worker_info["pid"],
                "worker_timed_out": worker_info["timed_out"],
                "worker_stderr": _redact(_read_limited(worker_stderr, MAX_AGENT_LOG_BYTES), redactions),
            }
            agent_log = json.dumps(agent_log_payload, ensure_ascii=False, sort_keys=True)[:MAX_AGENT_LOG_BYTES]
            agent_log = _redact(agent_log, redactions)
            diff_text = _redact(diff_text, redactions)
            ended_at = _now()
            agent_result = result_payload or {}
            if workspace_limit_error:
                agent_result = dict(agent_result)
                agent_result["workspace_output_error"] = workspace_limit_error
            trial = {
                "schema_version": 1,
                "trial_id": trial_id,
                "case_id": case.case_id,
                "case_version": case.version,
                "run_kind": run_kind,
                "started_at": started_at,
                "ended_at": ended_at,
                "agent_started": bool(result_payload.get("agent_started")) if result_payload else start_marker.is_file(),
                "agent_stop_reason": agent_stop,
                "agent_state_status": result_payload.get("agent_state_status") if result_payload else None,
                "agent_error_kind": result_payload.get("agent_error_kind") if result_payload else (
                    "worker_protocol_error" if worker_info["started"] and not worker_info["timed_out"] and not worker_info.get("interrupted") else None
                ),
                "agent_duration_ms": agent_duration_ms,
                "agent_exit_code": worker_info["returncode"],
                "agent_llm_calls": result_payload.get("agent_llm_calls") if result_payload else None,
                "successful_model_responses": result_payload.get("successful_model_responses") if result_payload else None,
                "tool_calls": result_payload.get("tool_calls") if result_payload else None,
                "permission_denials": result_payload.get("permission_denials") if result_payload else None,
                "subagent_tool_calls": 0,
                "subagent_count": 0,
                "input_tokens": result_payload.get("input_tokens") if result_payload else None,
                "output_tokens": result_payload.get("output_tokens") if result_payload else None,
                "token_accounting": result_payload.get("token_accounting", "unavailable") if result_payload else "unavailable",
                "usage_source": result_payload.get("usage_source", "unavailable") if result_payload else "unavailable",
                "model_binding_ref": result_payload.get("model_binding_ref", model_ref) if result_payload else model_ref,
                "grader_passed": grader_passed,
                "grader_error_kind": grader_error,
                "grader_duration_ms": int((grader_info or {}).get("duration_ms", 0)),
                "grader_exit_code": (grader_info or {}).get("returncode"),
                "success": success,
                "failure_kind": failure_kind,
                "cleanup_complete": cleanup_complete,
                "cleanup_issue": (str(cleanup_issue)[:300] if cleanup_issue else None),
                "recovery_success_rate": None,
                "invalid_repeat_count": None,
                "recovery_metrics_note": "本版不适用/未采集",
                "grader_result": grader_result,
                "artifacts": {"diff": "diff.patch", "agent_log": "agent.log", "grader_log": "grader.log"},
                "changed_files": changed_files,
                "code_revision": _code_revision(),
                "cost_usd": None,
                "price_snapshot": None,
                "grader_sha256": grader_sha256,
            }
            if suite_metadata is not None:
                trial.update(suite_metadata)
                trial["runtime_fingerprint_end"] = runtime_fingerprint_end
                trial["schema_version"] = 2
            validate_trial_result(trial)
            (stage_dir / "diff.patch").write_text(diff_text, encoding="utf-8")
            (stage_dir / "agent.log").write_text(agent_log, encoding="utf-8")
            (stage_dir / "grader.log").write_text(_redact(grader_log, redactions), encoding="utf-8")
            result_bytes = json.dumps(trial, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
            if len(result_bytes) > MAX_RESULT_BYTES:
                trial["changed_files"] = changed_files[:64]
                trial["grader_result"] = {"passed": grader_passed, "detail": "result output limit"} if grader_passed is not None else None
                result_bytes = json.dumps(trial, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
                if len(result_bytes) > MAX_RESULT_BYTES:
                    raise ValueError("trial_result_too_large")
            _atomic_write(stage_dir / "trial.json", result_bytes)
        finally:
            try:
                if grader_reference is not None:
                    _restore_reference_permissions(grader_reference)
                shutil.rmtree(tmp_root)
            except OSError as error:
                cleanup_complete = False
                cleanup_issue = f"runner_temp_cleanup:{type(error).__name__}"
            if stage_dir.exists() and (stage_dir / "trial.json").exists():
                if not cleanup_complete:
                    try:
                        raw = json.loads((stage_dir / "trial.json").read_text(encoding="utf-8"))
                        raw["cleanup_complete"] = False
                        raw["cleanup_issue"] = str(cleanup_issue)[:300]
                        raw["success"] = False
                        raw["failure_kind"] = "infrastructure_error"
                        validate_trial_result(raw)
                        _atomic_write(stage_dir / "trial.json", json.dumps(raw, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n")
                    except Exception:
                        pass
                os.replace(stage_dir, final_dir)
            elif stage_dir.exists():
                shutil.rmtree(stage_dir, ignore_errors=True)
        return final_dir


def run_grader_for_workspace(
    case: Case, workspace: Path, *, timeout: int = 30,
    reference_root: Path | None = None,
) -> tuple[bool | None, str | None]:
    """Run the case's frozen grader against a prepared baseline workspace."""
    grader = Path(case.case_dir) / case.grader_script
    home = Path(tempfile.mkdtemp(prefix="mini-agent-eval-grader-home-"))
    os.chmod(home, 0o700)
    (home / "tmp").mkdir(mode=0o700)
    out, err = home / "stdout", home / "stderr"
    try:
        info = _run_child(
            [sys.executable, str(grader), str(workspace)], cwd=workspace,
            env={
                **_process_environment(home, include_proxy=False),
                "MINI_AGENT_EVALUATION_REFERENCE_ROOT": str(reference_root or case.case_dir),
            }, timeout=timeout,
            stdout_path=out, stderr_path=err,
        )
        if info["timed_out"] or info["returncode"] != 0:
            return False, info["launch_error"] or "grader_failed"
        try:
            data = json.loads(out.read_text(encoding="utf-8"))
        except Exception:
            return None, "grader_invalid_result"
        if not isinstance(data, dict) or not isinstance(data.get("passed"), bool):
            return None, "grader_invalid_result"
        return data["passed"], None
    finally:
        shutil.rmtree(home, ignore_errors=True)
