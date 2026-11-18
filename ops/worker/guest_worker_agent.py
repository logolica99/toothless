#!/usr/bin/env python3
"""Fixed JSON worker protocol endpoint installed inside the Ubuntu guest."""

from __future__ import annotations

import base64
import json
import math
import os
import pwd
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, cast

WORKSPACE_ROOT = Path("/home/admin/workspace")
OPENCODE_WORKSPACE_ROOT = Path("/home/toothless-agent/workspace")
OPENCODE_WORKSPACE_ALIAS = "opencode-task-workspace"
STATE_ROOT = Path("/home/admin/.local/state/toothless-worker/executions")
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_COMMAND_BYTES = 32 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
MAX_FILE_BYTES = 1024 * 1024
MAX_TIMEOUT_SECONDS = 3600.0
EXECUTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_cancel_requested = False


class AgentError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentError("backend_protocol_error", "Execution state is unavailable or malformed") from exc
    if not isinstance(value, dict):
        raise AgentError("backend_protocol_error", "Execution state must be an object")
    return cast(dict[str, Any], value)


def _validate_id(value: object) -> str:
    if not isinstance(value, str) or not EXECUTION_ID.fullmatch(value):
        raise AgentError("invalid_request", "execution_id is invalid")
    return value


def _workspace_path(value: object, *, allow_dot: bool = False) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise AgentError("invalid_request", "path must be a relative POSIX path")
    parts = value.split("/")
    if value.startswith("/") or any(part == ".." for part in parts):
        raise AgentError("invalid_request", "absolute paths and parent traversal are not allowed")
    if value == "." and not allow_dot:
        raise AgentError("invalid_request", "a file path is required")
    if value == OPENCODE_WORKSPACE_ALIAS or value.startswith(f"{OPENCODE_WORKSPACE_ALIAS}/"):
        root = OPENCODE_WORKSPACE_ROOT.resolve(strict=True)
        relative_parts = parts[1:]
    else:
        root = WORKSPACE_ROOT.resolve(strict=True)
        relative_parts = parts
    target = root if value == "." or not relative_parts else root.joinpath(*relative_parts)
    resolved = target.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise AgentError("invalid_request", "path resolves outside the worker workspace")
    return resolved


def _inside_workspaces(path: Path) -> bool:
    resolved = path.resolve(strict=False)
    for root in (WORKSPACE_ROOT, OPENCODE_WORKSPACE_ROOT):
        try:
            if resolved.is_relative_to(root.resolve(strict=True)):
                return True
        except FileNotFoundError:
            continue
    return False


def _workspace_relative(path: Path) -> str:
    resolved = path.resolve(strict=True)
    try:
        opencode_root = OPENCODE_WORKSPACE_ROOT.resolve(strict=True)
        if resolved.is_relative_to(opencode_root):
            suffix = resolved.relative_to(opencode_root).as_posix()
            return OPENCODE_WORKSPACE_ALIAS if suffix == "." else f"{OPENCODE_WORKSPACE_ALIAS}/{suffix}"
    except FileNotFoundError:
        pass
    workspace_root = WORKSPACE_ROOT.resolve(strict=True)
    return "." if resolved == workspace_root else resolved.relative_to(workspace_root).as_posix()


def _snapshot(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "execution_id": state["execution_id"],
        "status": state["status"],
        "exit_code": state.get("exit_code"),
        "stdout_base64": state.get("stdout_base64", ""),
        "stderr_base64": state.get("stderr_base64", ""),
        "stdout_truncated": state.get("stdout_truncated", False),
        "stderr_truncated": state.get("stderr_truncated", False),
        "started_at": state["started_at"],
        "finished_at": state.get("finished_at"),
    }


def _start(request: dict[str, Any]) -> dict[str, Any]:
    execution_id = _validate_id(request.get("execution_id"))
    command = request.get("command")
    if not isinstance(command, str) or not command.strip():
        raise AgentError("invalid_request", "command must be non-empty text")
    try:
        command_size = len(command.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise AgentError("invalid_request", "command must be valid UTF-8") from exc
    if command_size > MAX_COMMAND_BYTES:
        raise AgentError("invalid_request", "command exceeds the worker limit")
    timeout = request.get("timeout_seconds")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or not 0 < timeout <= MAX_TIMEOUT_SECONDS
    ):
        raise AgentError("invalid_request", "timeout_seconds is outside the allowed range")
    working_directory = _workspace_path(request.get("working_directory", "."), allow_dot=True)
    if not working_directory.is_dir():
        raise AgentError("not_a_directory", "working_directory is not a directory")

    execution_dir = STATE_ROOT / execution_id
    try:
        execution_dir.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise AgentError("duplicate_execution_id", "execution_id already exists") from exc
    manifest = {
        "execution_id": execution_id,
        "command": command,
        "working_directory": str(working_directory),
        "timeout_seconds": float(timeout),
    }
    _write_json(execution_dir / "request.json", manifest)
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--runner", execution_id],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/admin", "LANG": "C.UTF-8"},
    )
    state_path = execution_dir / "state.json"
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if state_path.exists():
            state = _read_json(state_path)
            if state.get("runner_ready") is True:
                return _snapshot(state)
        time.sleep(0.02)
    raise AgentError(
        "execution_unknown",
        "Runner startup acknowledgement timed out; poll this execution_id before retrying",
    )


def _poll(execution_id: str) -> dict[str, Any]:
    execution_dir = STATE_ROOT / _validate_id(execution_id)
    if not (execution_dir / "state.json").is_file():
        raise AgentError("execution_not_found", "Guest execution history is missing")
    state = _read_json(execution_dir / "state.json")
    if state.get("status") == "running":
        runner_pid = state.get("runner_pid")
        if isinstance(runner_pid, int) and not _pid_exists(runner_pid):
            state["status"] = "unknown"
            state["finished_at"] = _utcnow()
            _write_json(execution_dir / "state.json", state)
    return _snapshot(state)


def _terminate(execution_id: str) -> dict[str, Any]:
    execution_dir = STATE_ROOT / _validate_id(execution_id)
    state_path = execution_dir / "state.json"
    if not state_path.is_file():
        raise AgentError("execution_not_found", "Guest execution history is missing")
    state = _read_json(state_path)
    if state.get("status") != "running":
        return _snapshot(state)
    runner_pid = state.get("runner_pid")
    if not isinstance(runner_pid, int) or not _pid_exists(runner_pid):
        state["status"] = "unknown"
        state["finished_at"] = _utcnow()
        _write_json(state_path, state)
        return _snapshot(state)
    try:
        os.kill(runner_pid, signal.SIGUSR1)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        current = _read_json(state_path)
        if current.get("status") != "running":
            return _snapshot(current)
        time.sleep(0.05)
    raise AgentError("execution_unknown", "Cancellation was requested but not acknowledged")


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _read_file(path_value: object) -> dict[str, Any]:
    path = _workspace_path(path_value)
    if not path.is_file():
        if path.is_dir():
            raise AgentError("is_a_directory", "Path is a directory")
        raise AgentError("file_not_found", "File was not found")
    with path.open("rb") as stream:
        content = stream.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise AgentError("file_too_large", "File exceeds the worker transfer limit")
    return {"content_base64": base64.b64encode(content).decode("ascii")}


def _write_file(request: dict[str, Any]) -> dict[str, Any]:
    path = _workspace_path(request.get("path"))
    encoded = request.get("content_base64")
    if not isinstance(encoded, str):
        raise AgentError("invalid_request", "content_base64 must be text")
    try:
        content = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise AgentError("invalid_request", "content_base64 is invalid") from exc
    if len(content) > MAX_FILE_BYTES:
        raise AgentError("invalid_request", "file content exceeds the worker limit")
    if path.is_dir():
        raise AgentError("is_a_directory", "Path is a directory")
    path.parent.mkdir(parents=True, exist_ok=True)
    resolved = path.resolve(strict=False)
    if not _inside_workspaces(resolved):
        raise AgentError("invalid_request", "path resolves outside the worker workspace")
    descriptor, temporary = tempfile.mkstemp(prefix=".toothless-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return _stat_path(path)


def _list_files(path_value: object) -> dict[str, Any]:
    path = _workspace_path(path_value, allow_dot=True)
    if not path.is_dir():
        if path.exists():
            raise AgentError("not_a_directory", "Path is not a directory")
        raise AgentError("file_not_found", "Directory was not found")
    entries: list[dict[str, Any]] = []
    for child in sorted(path.iterdir(), key=lambda item: item.name):
        resolved = child.resolve(strict=False)
        if not _inside_workspaces(resolved):
            raise AgentError("invalid_request", "A listed entry resolves outside the workspace")
        info = child.stat()
        kind = "directory" if child.is_dir() else "file"
        entries.append(
            {
                "name": child.name,
                "path": _workspace_relative(child),
                "kind": kind,
                "size_bytes": 0 if kind == "directory" else info.st_size,
            }
        )
    return {"entries": entries}


def _stat_path(path: Path) -> dict[str, Any]:
    info = path.stat()
    if path.is_dir():
        kind = "directory"
        size = 0
    elif path.is_file():
        kind = "file"
        size = info.st_size
    else:
        raise AgentError("invalid_request", "Only regular files and directories are supported")
    return {
        "path": _workspace_relative(path),
        "kind": kind,
        "size_bytes": size,
    }


def _stat_file(path_value: object) -> dict[str, Any]:
    path = _workspace_path(path_value, allow_dot=True)
    if not path.exists():
        raise AgentError("file_not_found", "Path was not found")
    return _stat_path(path)


def _dispatch(request: object) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise AgentError("invalid_request", "Request must be a JSON object")
    data = cast(dict[str, Any], request)
    operation = data.get("operation")
    if operation == "execute":
        result = _start(data)
    elif operation == "poll":
        result = _poll(_validate_id(data.get("execution_id")))
    elif operation == "terminate":
        result = _terminate(_validate_id(data.get("execution_id")))
    elif operation == "read_file":
        result = _read_file(data.get("path"))
    elif operation == "write_file":
        result = _write_file(data)
    elif operation == "list_files":
        result = _list_files(data.get("path"))
    elif operation == "stat_file":
        result = _stat_file(data.get("path"))
    else:
        raise AgentError("invalid_request", "Unsupported worker operation")
    return {"ok": True, "result": result}


def _capture_child(request: dict[str, Any], child: subprocess.Popen[bytes]) -> None:
    global _cancel_requested
    stdout_data = bytearray()
    stderr_data = bytearray()
    stdout_truncated = False
    stderr_truncated = False
    selector = selectors.DefaultSelector()
    assert child.stdout is not None and child.stderr is not None
    for stream_value, name in ((child.stdout, "stdout"), (child.stderr, "stderr")):
        stream = cast(BinaryIO, stream_value)
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)

    status = "succeeded"
    deadline = time.monotonic() + float(request["timeout_seconds"])
    stopped_at: float | None = None
    while selector.get_map():
        if _cancel_requested and status == "succeeded":
            _stop_child_group(child)
            status = "cancelled"
            stopped_at = time.monotonic()
        elif time.monotonic() >= deadline and status == "succeeded":
            _stop_child_group(child)
            status = "timed_out"
            stopped_at = time.monotonic()
        if stopped_at is not None and time.monotonic() - stopped_at >= 2.0:
            for key in list(selector.get_map().values()):
                stream = cast(BinaryIO, key.fileobj)
                selector.unregister(stream)
                stream.close()
            break
        events = selector.select(0.05)
        for key, _mask in events:
            stream = cast(BinaryIO, key.fileobj)
            name = key.data
            try:
                chunk = os.read(stream.fileno(), 64 * 1024)
            except BlockingIOError:
                continue
            if not chunk:
                selector.unregister(stream)
                stream.close()
                continue
            target = stdout_data if name == "stdout" else stderr_data
            available = max(0, MAX_OUTPUT_BYTES - len(target))
            target.extend(chunk[:available])
            if len(chunk) > available:
                if name == "stdout":
                    stdout_truncated = True
                else:
                    stderr_truncated = True
        if child.poll() is not None and not selector.get_map():
            break
    selector.close()
    exit_code = child.wait()
    if status == "succeeded" and exit_code != 0:
        status = "failed"
    state_path = STATE_ROOT / str(request["execution_id"]) / "state.json"
    state = _read_json(state_path)
    state.update(
        {
            "status": status,
            "exit_code": exit_code if status in {"succeeded", "failed"} else None,
            "stdout_base64": base64.b64encode(stdout_data).decode("ascii"),
            "stderr_base64": base64.b64encode(stderr_data).decode("ascii"),
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "finished_at": _utcnow(),
        }
    )
    _write_json(state_path, state)
    _cancel_requested = False


def _stop_child_group(child: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        child.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _run(execution_id: str) -> int:
    global _cancel_requested
    if not EXECUTION_ID.fullmatch(execution_id):
        return 2
    execution_dir = STATE_ROOT / execution_id
    request = _read_json(execution_dir / "request.json")
    _cancel_requested = False
    signal.signal(signal.SIGUSR1, lambda _signum, _frame: _set_cancel())
    state_path = execution_dir / "state.json"
    started_at = _utcnow()
    _write_json(
        state_path,
        {
            "execution_id": execution_id,
            "status": "running",
            "exit_code": None,
            "stdout_base64": "",
            "stderr_base64": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
            "started_at": started_at,
            "finished_at": None,
            "runner_pid": os.getpid(),
            "runner_ready": True,
            "child_pid": None,
        },
    )
    if _cancel_requested:
        state = _read_json(state_path)
        state.update({"status": "cancelled", "finished_at": _utcnow()})
        _write_json(state_path, state)
        return 0
    command = request["command"]
    working_directory = request["working_directory"]
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/admin", "LANG": "C.UTF-8"}
    child = subprocess.Popen(
        ["/bin/sh", "-c", command],
        cwd=working_directory,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
        start_new_session=True,
        env=env,
    )
    state = _read_json(state_path)
    state["child_pid"] = child.pid
    _write_json(state_path, state)
    _capture_child(request, child)
    return 0


def _set_cancel() -> None:
    global _cancel_requested
    _cancel_requested = True


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--runner":
        return _run(sys.argv[2])
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        response = {"ok": False, "error": {"code": "invalid_request", "message": "Request is too large"}}
        sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        return 0
    accepted = False
    request_received = False
    try:
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise AgentError("invalid_request", "Request must be a JSON object")
        request = cast(dict[str, Any], request)
        request_received = True
        account = pwd.getpwnam("admin")
        if os.geteuid() == 0:
            os.initgroups(account.pw_name, account.pw_gid)
            os.setgid(account.pw_gid)
            os.setuid(account.pw_uid)
        response = _dispatch(request)
        accepted = True
    except AgentError as exc:
        accepted = request_received
        response = {"ok": False, "error": {"code": exc.code, "message": exc.message}}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        accepted = request_received
        response = {
            "ok": False,
            "error": {"code": "backend_protocol_error", "message": type(exc).__name__},
        }
    if accepted:
        sys.stdout.write('{"accepted":true}\n')
        sys.stdout.flush()
    sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
