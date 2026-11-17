"""Pinned-key SSH transport for the fixed guest worker helper."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable, cast

from nanobot.worker.interface import (
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_OUTPUT_BYTES,
    ExecuteRequest,
    ExecutionSnapshot,
    ExecutionStatus,
    FileEntry,
    FileKind,
    FileStat,
    WorkerErrorCode,
    WorkerInterface,
    WorkerOperationError,
    WorkerUnavailableError,
    validate_execution_id,
)

if TYPE_CHECKING:
    from nanobot.worker.execution_store import SqliteExecutionStore

REMOTE_HELPER = "sudo -n /usr/local/libexec/toothless/worker-agent"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 15.0
MAX_WIRE_RESPONSE_BYTES = (DEFAULT_MAX_FILE_BYTES * 4 // 3) + 128 * 1024
MAX_SSH_STDERR_BYTES = 64 * 1024
_USERNAME = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


@dataclass(frozen=True, slots=True)
class SSHWorkerConfig:
    host: str
    username: str
    identity_file: Path
    known_hosts_file: Path
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    ssh_binary: str = "/usr/bin/ssh"


class SSHWorkerBackend:
    """WorkerBackend implementation using strict key-only OpenSSH and JSON stdin."""

    def __init__(
        self,
        config: SSHWorkerConfig,
        *,
        accepted_callback: Callable[[str], None] | None = None,
    ) -> None:
        _validate_config(config)
        self._config = config
        self._accepted_callback = accepted_callback

    async def execute(self, request: ExecuteRequest) -> ExecutionSnapshot:
        execution_id = request.execution_id
        if execution_id is None:
            raise WorkerOperationError(
                WorkerErrorCode.INVALID_REQUEST,
                "WorkerInterface must assign an execution ID before SSH dispatch",
            )
        payload: dict[str, object] = {
            "operation": "execute",
            "execution_id": execution_id,
            "command": request.command,
            "working_directory": request.working_directory,
            "timeout_seconds": request.timeout_seconds,
        }
        response = await self._request(payload, execution_id=execution_id, dispatch=True)
        return _snapshot(response)

    async def poll(self, execution_id: str) -> ExecutionSnapshot:
        validate_execution_id(execution_id)
        response = await self._request({"operation": "poll", "execution_id": execution_id})
        return _snapshot(response)

    async def terminate(self, execution_id: str) -> ExecutionSnapshot:
        validate_execution_id(execution_id)
        response = await self._request({"operation": "terminate", "execution_id": execution_id})
        return _snapshot(response)

    async def read_file(self, path: str) -> bytes:
        response = await self._request({"operation": "read_file", "path": path})
        content = _string(response, "content_base64")
        try:
            data = base64.b64decode(content, validate=True)
        except ValueError as exc:
            raise _protocol_error("Worker returned invalid file encoding") from exc
        if len(data) > DEFAULT_MAX_FILE_BYTES:
            raise _protocol_error("Worker file response exceeds the configured limit")
        return data

    async def write_file(self, path: str, content: bytes) -> FileStat:
        response = await self._request(
            {
                "operation": "write_file",
                "path": path,
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
        )
        return _file_stat(response)

    async def list_files(self, path: str) -> tuple[FileEntry, ...]:
        response = await self._request({"operation": "list_files", "path": path})
        entries_value = response.get("entries")
        if not isinstance(entries_value, list):
            raise _protocol_error("Worker returned an invalid file list")
        entries = cast(list[object], entries_value)
        return tuple(_file_entry(entry) for entry in entries)

    async def stat_file(self, path: str) -> FileStat:
        response = await self._request({"operation": "stat_file", "path": path})
        return _file_stat(response)

    async def _request(
        self,
        payload: dict[str, object],
        *,
        execution_id: str | None = None,
        dispatch: bool = False,
    ) -> dict[str, object]:
        config = self._config
        deadline = asyncio.get_running_loop().time() + config.request_timeout_seconds
        try:
            process = await asyncio.create_subprocess_exec(
                config.ssh_binary,
                "-F",
                "/dev/null",
                "-T",
                "-i",
                str(config.identity_file),
                "-p",
                "22",
                "-o",
                f"UserKnownHostsFile={config.known_hosts_file}",
                "-o",
                "GlobalKnownHostsFile=/dev/null",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "HostKeyAlgorithms=ssh-ed25519",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "IdentityAgent=none",
                "-o",
                "ForwardAgent=no",
                "-o",
                "ClearAllForwardings=yes",
                "-o",
                "BatchMode=yes",
                "-o",
                "PasswordAuthentication=no",
                "-o",
                "KbdInteractiveAuthentication=no",
                "-o",
                "PreferredAuthentications=publickey",
                "-o",
                "RequestTTY=no",
                "-o",
                "ControlMaster=no",
                "-o",
                "ControlPersist=no",
                "-o",
                "PermitLocalCommand=no",
                "-o",
                "ProxyCommand=none",
                "-o",
                "ProxyJump=none",
                "-o",
                "ConnectTimeout=5",
                "-o",
                "ServerAliveInterval=5",
                "-o",
                "ServerAliveCountMax=1",
                f"{config.username}@{config.host}",
                REMOTE_HELPER,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=MAX_WIRE_RESPONSE_BYTES + 1,
                env={"PATH": "/usr/bin:/bin", "HOME": "/var/empty", "LANG": "C.UTF-8"},
            )
        except OSError as exc:
            raise WorkerUnavailableError("Could not start the SSH transport") from exc

        if process.stdin is None or process.stdout is None or process.stderr is None:
            process.kill()
            await process.wait()
            raise _protocol_error("SSH transport did not provide the required streams")

        request_bytes = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8") + b"\n"
        accepted = False
        stderr_task = asyncio.create_task(_read_bounded(process.stderr, MAX_SSH_STDERR_BYTES))
        try:
            process.stdin.write(request_bytes)
            await asyncio.wait_for(process.stdin.drain(), timeout=_remaining(deadline))
            process.stdin.close()
            acknowledgement = await asyncio.wait_for(
                process.stdout.readline(),
                timeout=_remaining(deadline),
            )
            accepted = acknowledgement == b'{"accepted":true}\n'
            if accepted:
                if dispatch and execution_id is not None and self._accepted_callback is not None:
                    try:
                        self._accepted_callback(execution_id)
                    except Exception as exc:
                        await _stop_ssh(process, stderr_task)
                        raise WorkerOperationError(
                            WorkerErrorCode.EXECUTION_UNKNOWN,
                            "Worker accepted the request but host acceptance could not be persisted",
                            execution_id=execution_id,
                        ) from exc
                stdout = await asyncio.wait_for(
                    _read_bounded(process.stdout, MAX_WIRE_RESPONSE_BYTES),
                    timeout=_remaining(deadline),
                )
            else:
                remainder = await asyncio.wait_for(
                    _read_bounded(process.stdout, MAX_WIRE_RESPONSE_BYTES),
                    timeout=_remaining(deadline),
                )
                stdout = acknowledgement + remainder
            _stderr = await asyncio.wait_for(
                stderr_task,
                timeout=_remaining(deadline),
            )
            return_code = await asyncio.wait_for(
                process.wait(), timeout=_remaining(deadline)
            )
        except asyncio.CancelledError:
            await _stop_ssh(process, stderr_task)
            raise
        except (BrokenPipeError, ConnectionError, OSError, asyncio.TimeoutError, ValueError) as exc:
            failure_stderr = _completed_task_result(stderr_task)
            await _stop_ssh(process, stderr_task)
            if dispatch and (
                accepted or not _is_definitive_pre_dispatch_failure(failure_stderr)
            ):
                raise WorkerOperationError(
                    WorkerErrorCode.EXECUTION_UNKNOWN,
                    "SSH ended without a confirmed pre-dispatch failure; reconcile this execution ID before retrying",
                    execution_id=execution_id,
                ) from exc
            raise WorkerUnavailableError("SSH worker request failed") from exc

        if return_code != 0:
            if dispatch and (
                accepted or not _is_definitive_pre_dispatch_failure(_stderr)
            ):
                raise WorkerOperationError(
                    WorkerErrorCode.EXECUTION_UNKNOWN,
                    "SSH exited without a confirmed pre-dispatch failure; reconcile this execution ID before retrying",
                    execution_id=execution_id,
                )
            raise WorkerUnavailableError("SSH worker is unavailable or host-key verification failed")

        if not accepted:
            if dispatch:
                raise WorkerOperationError(
                    WorkerErrorCode.EXECUTION_UNKNOWN,
                    "Worker response omitted its acceptance acknowledgement; reconcile this execution ID before retrying",
                    execution_id=execution_id,
                )
            raise _protocol_error("Worker did not acknowledge request acceptance")

        try:
            response_lines = stdout.splitlines()
            if len(response_lines) != 1:
                raise _protocol_error("Worker returned an invalid response line count")
            response_value = json.loads(response_lines[0])
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise _protocol_error("Worker returned malformed JSON") from exc
        if not isinstance(response_value, dict):
            raise _protocol_error("Worker returned a non-object response")
        response = cast(dict[str, object], response_value)
        if response.get("ok") is False:
            error = response.get("error")
            if not isinstance(error, dict):
                raise _protocol_error("Worker returned malformed structured error")
            error_data = cast(dict[str, object], error)
            code_value = error_data.get("code")
            message_value = error_data.get("message")
            if not isinstance(code_value, str) or not isinstance(message_value, str):
                raise _protocol_error("Worker returned malformed structured error")
            try:
                code = WorkerErrorCode(code_value)
            except ValueError as exc:
                raise _protocol_error("Worker returned an unknown error code") from exc
            raise WorkerOperationError(code, message_value, execution_id=execution_id)
        result_value = response.get("result")
        if response.get("ok") is not True or not isinstance(result_value, dict):
            raise _protocol_error("Worker returned an invalid response envelope")
        return cast(dict[str, object], result_value)


async def _read_bounded(reader: asyncio.StreamReader, limit: int) -> bytes:
    output = bytearray()
    while chunk := await reader.read(64 * 1024):
        if len(output) + len(chunk) > limit:
            raise ValueError("SSH response exceeded its size limit")
        output.extend(chunk)
    return bytes(output)


def _completed_task_result(task: asyncio.Task[bytes]) -> bytes:
    if not task.done() or task.cancelled():
        return b""
    try:
        return task.result()
    except (OSError, ValueError):
        return b""


def _is_definitive_pre_dispatch_failure(stderr: bytes) -> bool:
    normalized = stderr.lower()
    return any(
        marker in normalized
        for marker in (
            b"connection refused",
            b"no route to host",
            b"connection timed out",
            b"operation timed out",
            b"host key verification failed",
            b"remote host identification has changed",
            b"permission denied (publickey",
            b"could not resolve hostname",
        )
    )


async def _stop_ssh(
    process: asyncio.subprocess.Process,
    stderr_task: asyncio.Task[bytes],
) -> None:
    if process.returncode is None:
        process.kill()
        await process.wait()
    if not stderr_task.done():
        stderr_task.cancel()
    await asyncio.gather(stderr_task, return_exceptions=True)


def _validate_config(config: SSHWorkerConfig) -> None:
    try:
        ipaddress.IPv4Address(config.host)
    except ipaddress.AddressValueError as exc:
        raise ValueError("SSH worker host must be a literal IPv4 address") from exc
    if not _USERNAME.fullmatch(config.username):
        raise ValueError("SSH username is invalid")
    if config.ssh_binary != "/usr/bin/ssh":
        raise ValueError("SSH binary must be the system OpenSSH client at /usr/bin/ssh")
    for label, path in (
        ("identity_file", config.identity_file),
        ("known_hosts_file", config.known_hosts_file),
    ):
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise ValueError(f"{label} must be an existing absolute regular file")
    timeout = config.request_timeout_seconds
    if (
        isinstance(timeout, bool)
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("request_timeout_seconds must be finite and positive")


def _remaining(deadline: float) -> float:
    return max(0.001, deadline - asyncio.get_running_loop().time())


def _snapshot(value: dict[str, object]) -> ExecutionSnapshot:
    execution_id = _string(value, "execution_id")
    validate_execution_id(execution_id)
    try:
        status = ExecutionStatus(_string(value, "status"))
    except ValueError as exc:
        raise _protocol_error("Worker returned an unknown execution status") from exc
    exit_code_value = value.get("exit_code")
    if exit_code_value is not None and (
        isinstance(exit_code_value, bool) or not isinstance(exit_code_value, int)
    ):
        raise _protocol_error("Worker returned an invalid exit code")
    stdout = _decode_bytes(value, "stdout_base64")
    stderr = _decode_bytes(value, "stderr_base64")
    if len(stdout) > DEFAULT_MAX_OUTPUT_BYTES or len(stderr) > DEFAULT_MAX_OUTPUT_BYTES:
        raise _protocol_error("Worker returned output over the configured limit")
    return ExecutionSnapshot(
        execution_id=execution_id,
        status=status,
        exit_code=exit_code_value,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=_bool(value, "stdout_truncated"),
        stderr_truncated=_bool(value, "stderr_truncated"),
        started_at=_datetime(value, "started_at"),
        finished_at=_optional_datetime(value, "finished_at"),
    )


def _file_stat(value: dict[str, object]) -> FileStat:
    try:
        kind = FileKind(_string(value, "kind"))
    except ValueError as exc:
        raise _protocol_error("Worker returned an invalid file kind") from exc
    size = value.get("size_bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise _protocol_error("Worker returned an invalid file size")
    return FileStat(_string(value, "path"), kind, size)


def _file_entry(value: object) -> FileEntry:
    if not isinstance(value, dict):
        raise _protocol_error("Worker returned an invalid file entry")
    data = cast(dict[str, object], value)
    stat = _file_stat(data)
    return FileEntry(_string(data, "name"), stat.path, stat.kind, stat.size_bytes)


def _decode_bytes(value: dict[str, object], key: str) -> bytes:
    try:
        return base64.b64decode(_string(value, key), validate=True)
    except ValueError as exc:
        raise _protocol_error(f"Worker returned invalid {key}") from exc


def _string(value: dict[str, object], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str):
        raise _protocol_error(f"Worker response field {key} must be text")
    return result


def _bool(value: dict[str, object], key: str) -> bool:
    result = value.get(key)
    if not isinstance(result, bool):
        raise _protocol_error(f"Worker response field {key} must be boolean")
    return result


def _datetime(value: dict[str, object], key: str) -> datetime:
    result = value.get(key)
    if not isinstance(result, str):
        raise _protocol_error(f"Worker response field {key} must be a timestamp")
    try:
        return datetime.fromisoformat(result)
    except ValueError as exc:
        raise _protocol_error(f"Worker response field {key} is not an ISO timestamp") from exc


def _optional_datetime(value: dict[str, object], key: str) -> datetime | None:
    result = value.get(key)
    if result is None:
        return None
    if not isinstance(result, str):
        raise _protocol_error(f"Worker response field {key} must be a timestamp or null")
    try:
        return datetime.fromisoformat(result)
    except ValueError as exc:
        raise _protocol_error(f"Worker response field {key} is not an ISO timestamp") from exc


def _protocol_error(message: str) -> WorkerOperationError:
    return WorkerOperationError(WorkerErrorCode.BACKEND_PROTOCOL_ERROR, message)


def create_persistent_worker(
    config: SSHWorkerConfig,
    *,
    execution_store: SqliteExecutionStore | None = None,
    accepted_callback: Callable[[str], None] | None = None,
) -> WorkerInterface:
    """Compose SSH with the host-owned default execution record store."""
    from nanobot.worker.execution_store import SqliteExecutionStore

    store = execution_store if execution_store is not None else SqliteExecutionStore.default()
    def persist_acceptance(execution_id: str) -> None:
        store.mark_accepted(execution_id)
        if accepted_callback is not None:
            accepted_callback(execution_id)

    backend = SSHWorkerBackend(config, accepted_callback=persist_acceptance)
    return WorkerInterface(backend, execution_store=store)
