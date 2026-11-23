"""Validated, transport-neutral operations for the isolated worker."""

from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from nanobot.worker.execution_store import ExecutionRecord, SqliteExecutionStore

DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_TIMEOUT_SECONDS = 3600.0
MAX_COMMAND_BYTES = 32 * 1024
MAX_PATH_BYTES = 4096
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024
DEFAULT_MAX_FILE_BYTES = 1024 * 1024
MAX_DESKTOP_SCREENSHOT_BYTES = 4 * 1024 * 1024
_EXECUTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ExecutionStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class FileKind(StrEnum):
    FILE = "file"
    DIRECTORY = "directory"


class WorkerErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    WORKER_UNAVAILABLE = "worker_unavailable"
    DUPLICATE_EXECUTION_ID = "duplicate_execution_id"
    EXECUTION_NOT_FOUND = "execution_not_found"
    FILE_NOT_FOUND = "file_not_found"
    FILE_TOO_LARGE = "file_too_large"
    NOT_A_DIRECTORY = "not_a_directory"
    IS_A_DIRECTORY = "is_a_directory"
    BACKEND_PROTOCOL_ERROR = "backend_protocol_error"
    EXECUTION_UNKNOWN = "execution_unknown"


class WorkerOperationError(RuntimeError):
    """A stable, structured failure from validation or a worker backend."""

    def __init__(
        self,
        code: WorkerErrorCode,
        message: str,
        *,
        execution_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.execution_id = execution_id

    def as_dict(self) -> dict[str, str]:
        error = {"code": self.code.value, "message": self.message}
        if self.execution_id is not None:
            error["execution_id"] = self.execution_id
        return error


class WorkerUnavailableError(WorkerOperationError):
    def __init__(self, message: str = "Worker is unavailable") -> None:
        super().__init__(WorkerErrorCode.WORKER_UNAVAILABLE, message)


@dataclass(frozen=True, slots=True)
class ExecuteRequest:
    command: str
    working_directory: str = "."
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    execution_id: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionSnapshot:
    execution_id: str
    status: ExecutionStatus
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class FileEntry:
    name: str
    path: str
    kind: FileKind
    size_bytes: int


@dataclass(frozen=True, slots=True)
class FileStat:
    path: str
    kind: FileKind
    size_bytes: int


class WorkerBackend(Protocol):
    """Narrow transport contract; implementations must never execute locally."""

    async def execute(self, request: ExecuteRequest) -> ExecutionSnapshot: ...

    async def poll(self, execution_id: str) -> ExecutionSnapshot: ...

    async def terminate(self, execution_id: str) -> ExecutionSnapshot: ...

    async def read_file(self, path: str) -> bytes: ...

    async def write_file(self, path: str, content: bytes) -> FileStat: ...

    async def list_files(self, path: str) -> tuple[FileEntry, ...]: ...

    async def stat_file(self, path: str) -> FileStat: ...

    async def capture_desktop_screenshot(self) -> bytes: ...

    async def desktop_available(self) -> bool: ...

    async def desktop_control(self, operation: str) -> bool: ...


class WorkerInterface:
    """Validate and bound worker requests before passing them to an injected backend."""

    def __init__(
        self,
        backend: WorkerBackend,
        *,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        execution_store: SqliteExecutionStore | None = None,
    ) -> None:
        max_output_bytes = _size_limit(max_output_bytes, "max_output_bytes")
        max_file_bytes = _size_limit(max_file_bytes, "max_file_bytes")
        self._backend = backend
        self._max_output_bytes = max_output_bytes
        self._max_file_bytes = max_file_bytes
        self._submitted_execution_ids: set[str] = set()
        self._execution_store = execution_store

    async def execute(self, request: ExecuteRequest) -> ExecutionSnapshot:
        command = _command(request.command)
        working_directory = _relative_path(request.working_directory, allow_dot=True)
        timeout = _timeout(request.timeout_seconds)

        execution_id = (
            request.execution_id
            if request.execution_id is not None
            else uuid.uuid4().hex
        )
        validate_execution_id(execution_id)
        if execution_id in self._submitted_execution_ids:
            raise WorkerOperationError(
                WorkerErrorCode.DUPLICATE_EXECUTION_ID,
                "Execution ID has already been submitted",
                execution_id=execution_id,
            )

        # Reserve before dispatch so a transport error cannot cause a blind duplicate start.
        self._submitted_execution_ids.add(execution_id)
        normalized = replace(
            request,
            command=command,
            working_directory=working_directory,
            timeout_seconds=float(timeout),
            execution_id=execution_id,
        )
        if self._execution_store is not None:
            self._execution_store.create_intent(normalized)
        try:
            result = await self._backend.execute(normalized)
        except WorkerOperationError as exc:
            if self._execution_store is not None:
                self._execution_store.record_error(
                    execution_id, exc, execute_dispatch=True
                )
            raise
        snapshot = self._bounded_snapshot(result, expected_id=execution_id)
        if self._execution_store is not None:
            self._execution_store.record_snapshot(snapshot)
        return snapshot

    async def poll(self, execution_id: str) -> ExecutionSnapshot:
        validate_execution_id(execution_id)
        record = self._get_execution_record(execution_id)
        if self._execution_store is not None and record is None:
            raise WorkerOperationError(
                WorkerErrorCode.EXECUTION_NOT_FOUND,
                "Execution ID is not present in the host record store",
                execution_id=execution_id,
            )
        if record is not None and record.status not in {
            ExecutionStatus.RUNNING,
            ExecutionStatus.UNKNOWN,
        }:
            return record.snapshot()
        try:
            result = await self._backend.poll(execution_id)
        except WorkerOperationError as exc:
            if self._execution_store is not None:
                self._execution_store.record_error(execution_id, exc)
                if exc.code is WorkerErrorCode.EXECUTION_NOT_FOUND:
                    refreshed = self._execution_store.get(execution_id)
                    assert refreshed is not None
                    return refreshed.snapshot()
            raise
        snapshot = self._bounded_snapshot(result, expected_id=execution_id)
        if self._execution_store is not None:
            self._execution_store.record_snapshot(snapshot)
        return snapshot

    async def terminate(self, execution_id: str) -> ExecutionSnapshot:
        validate_execution_id(execution_id)
        record = self._get_execution_record(execution_id)
        if self._execution_store is not None and record is None:
            raise WorkerOperationError(
                WorkerErrorCode.EXECUTION_NOT_FOUND,
                "Execution ID is not present in the host record store",
                execution_id=execution_id,
            )
        if self._execution_store is not None:
            self._execution_store.mark_cancel_requested(execution_id)
        if record is not None and record.status not in {
            ExecutionStatus.RUNNING,
            ExecutionStatus.UNKNOWN,
        }:
            return record.snapshot()
        try:
            result = await self._backend.terminate(execution_id)
        except WorkerOperationError as exc:
            if self._execution_store is not None:
                self._execution_store.record_error(execution_id, exc)
                if exc.code is WorkerErrorCode.EXECUTION_NOT_FOUND:
                    refreshed = self._execution_store.get(execution_id)
                    assert refreshed is not None
                    return refreshed.snapshot()
            raise
        snapshot = self._bounded_snapshot(result, expected_id=execution_id)
        if self._execution_store is not None:
            self._execution_store.record_snapshot(snapshot)
        return snapshot

    async def reconcile(self, execution_id: str) -> ExecutionSnapshot:
        """Query the guest for an existing ID without resubmitting its command."""
        record = self._get_execution_record(execution_id)
        if (
            record is not None
            and record.cancel_requested_at is not None
            and record.status in {ExecutionStatus.RUNNING, ExecutionStatus.UNKNOWN}
        ):
            return await self.terminate(execution_id)
        return await self.poll(execution_id)

    def get_execution_record(self, execution_id: str) -> ExecutionRecord | None:
        """Read the host-owned durable record without contacting the worker."""
        validate_execution_id(execution_id)
        return self._get_execution_record(execution_id)

    def _get_execution_record(self, execution_id: str) -> ExecutionRecord | None:
        if self._execution_store is None:
            return None
        return self._execution_store.get(execution_id)

    async def read_file(self, path: str) -> bytes:
        normalized = _relative_path(path)
        content = await self._backend.read_file(normalized)
        if len(content) > self._max_file_bytes:
            raise WorkerOperationError(
                WorkerErrorCode.BACKEND_PROTOCOL_ERROR,
                f"Worker file response exceeds {self._max_file_bytes} bytes",
            )
        return content

    async def write_file(self, path: str, content: bytes) -> FileStat:
        normalized = _relative_path(path)
        content = _file_content(content)
        if len(content) > self._max_file_bytes:
            raise _invalid(f"content exceeds {self._max_file_bytes} bytes")
        return await self._backend.write_file(normalized, content)

    async def list_files(self, path: str) -> tuple[FileEntry, ...]:
        normalized = _relative_path(path, allow_dot=True)
        return await self._backend.list_files(normalized)

    async def stat_file(self, path: str) -> FileStat:
        normalized = _relative_path(path, allow_dot=True)
        return await self._backend.stat_file(normalized)

    async def capture_desktop_screenshot(self) -> bytes:
        """Request one fixed-display image through the authenticated worker helper."""
        content_value = cast(object, await self._backend.capture_desktop_screenshot())
        if not isinstance(content_value, bytes) or len(content_value) > MAX_DESKTOP_SCREENSHOT_BYTES:
            raise WorkerOperationError(
                WorkerErrorCode.BACKEND_PROTOCOL_ERROR,
                "Desktop screenshot exceeded the configured size limit",
            )
        _validate_desktop_png(content_value)
        return content_value

    async def desktop_available(self) -> bool:
        available = cast(object, await self._backend.desktop_available())
        if not isinstance(available, bool):
            raise WorkerOperationError(
                WorkerErrorCode.BACKEND_PROTOCOL_ERROR, "Worker returned invalid desktop readiness"
            )
        return available

    async def start_desktop(self) -> bool:
        return await self._desktop_control("start_desktop")

    async def stop_desktop(self) -> bool:
        return await self._desktop_control("stop_desktop")

    async def _desktop_control(self, operation: str) -> bool:
        result: object = await self._backend.desktop_control(operation)
        if result is not True:
            raise WorkerOperationError(
                WorkerErrorCode.BACKEND_PROTOCOL_ERROR, "Worker did not confirm the desktop state change"
            )
        return True

    def _bounded_snapshot(
        self,
        result: object,
        *,
        expected_id: str,
    ) -> ExecutionSnapshot:
        if not isinstance(result, ExecutionSnapshot) or result.execution_id != expected_id:
            raise WorkerOperationError(
                WorkerErrorCode.BACKEND_PROTOCOL_ERROR,
                "Worker returned an invalid execution snapshot",
                execution_id=expected_id,
            )
        stdout = result.stdout[: self._max_output_bytes]
        stderr = result.stderr[: self._max_output_bytes]
        return replace(
            result,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=result.stdout_truncated or len(result.stdout) > len(stdout),
            stderr_truncated=result.stderr_truncated or len(result.stderr) > len(stderr),
        )


def _relative_path(path: object, *, allow_dot: bool = False) -> str:
    if not isinstance(path, str) or not path or "\x00" in path or "\\" in path:
        raise _invalid("path must be a non-empty POSIX relative path")
    try:
        size = len(path.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise _invalid("path must be valid UTF-8 text") from exc
    if size > MAX_PATH_BYTES:
        raise _invalid(f"path exceeds {MAX_PATH_BYTES} UTF-8 bytes")
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or any(part == ".." for part in path.split("/")):
        raise _invalid("absolute paths and parent traversal are not allowed")
    normalized = str(candidate)
    if normalized == "." and not allow_dot:
        raise _invalid("a file path is required")
    return normalized


def validate_execution_id(execution_id: object) -> None:
    if not isinstance(execution_id, str) or not _EXECUTION_ID.fullmatch(execution_id):
        raise _invalid("execution_id must be 1-128 safe identifier characters")


def _command(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid("command must be a non-empty string")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise _invalid("command must be valid UTF-8 text") from exc
    if size > MAX_COMMAND_BYTES:
        raise _invalid(f"command exceeds {MAX_COMMAND_BYTES} UTF-8 bytes")
    return value


def _timeout(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 < value <= MAX_TIMEOUT_SECONDS
    ):
        raise _invalid(f"timeout_seconds must be greater than 0 and at most {MAX_TIMEOUT_SECONDS}")
    return float(value)


def _file_content(value: object) -> bytes:
    if not isinstance(value, bytes):
        raise _invalid("content must be bytes")
    return value


def _size_limit(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _validate_desktop_png(content: bytes) -> None:
    if len(content) < 24 or content[:8] != b"\x89PNG\r\n\x1a\n" or content[12:16] != b"IHDR":
        raise WorkerOperationError(
            WorkerErrorCode.BACKEND_PROTOCOL_ERROR, "Worker returned an invalid desktop image"
        )
    width = int.from_bytes(content[16:20], "big")
    height = int.from_bytes(content[20:24], "big")
    if not (1 <= width <= 1280 and 1 <= height <= 800):
        raise WorkerOperationError(
            WorkerErrorCode.BACKEND_PROTOCOL_ERROR, "Desktop image dimensions exceed the configured limit"
        )


def _invalid(message: str) -> WorkerOperationError:
    return WorkerOperationError(WorkerErrorCode.INVALID_REQUEST, message)
