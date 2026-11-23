"""Deterministic in-memory worker backend; it never invokes a host command."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from nanobot.worker.interface import (
    ExecuteRequest,
    ExecutionSnapshot,
    ExecutionStatus,
    FileEntry,
    FileKind,
    FileStat,
    WorkerErrorCode,
    WorkerOperationError,
    WorkerUnavailableError,
)


@dataclass(frozen=True, slots=True)
class FakeCommandPlan:
    stdout: bytes = b""
    stderr: bytes = b""
    exit_code: int = 0
    runtime_seconds: float = 0.0
    stays_running: bool = False


class FakeWorkerBackend:
    """Scripted results and an in-memory file tree for deterministic contract tests."""

    def __init__(
        self,
        *,
        command_plans: dict[str, FakeCommandPlan] | None = None,
        files: dict[str, bytes] | None = None,
        available: bool = True,
        desktop_screenshot: bytes | None = None,
    ) -> None:
        self.command_plans = dict(command_plans or {})
        self.files = dict(files or {})
        self.available = available
        self.desktop_screenshot = desktop_screenshot
        self.submitted_requests: list[ExecuteRequest] = []
        self.executions: dict[str, ExecutionSnapshot] = {}
        self._clock_ticks = 0

    async def execute(self, request: ExecuteRequest) -> ExecutionSnapshot:
        self._require_available()
        assert request.execution_id is not None
        if request.execution_id in self.executions:
            raise WorkerOperationError(
                WorkerErrorCode.DUPLICATE_EXECUTION_ID,
                "Execution ID already exists in fake worker",
                execution_id=request.execution_id,
            )
        self.submitted_requests.append(request)
        now = self._next_timestamp()
        plan = self.command_plans.get(request.command, FakeCommandPlan())
        if plan.runtime_seconds > request.timeout_seconds:
            status = ExecutionStatus.TIMED_OUT
            exit_code = None
            finished_at = now
        elif plan.stays_running:
            status = ExecutionStatus.RUNNING
            exit_code = None
            finished_at = None
        else:
            status = ExecutionStatus.SUCCEEDED if plan.exit_code == 0 else ExecutionStatus.FAILED
            exit_code = plan.exit_code
            finished_at = now
        snapshot = ExecutionSnapshot(
            execution_id=request.execution_id,
            status=status,
            exit_code=exit_code,
            stdout=plan.stdout,
            stderr=plan.stderr,
            stdout_truncated=False,
            stderr_truncated=False,
            started_at=now,
            finished_at=finished_at,
        )
        self.executions[request.execution_id] = snapshot
        return snapshot

    async def poll(self, execution_id: str) -> ExecutionSnapshot:
        self._require_available()
        try:
            return self.executions[execution_id]
        except KeyError as exc:
            raise WorkerOperationError(
                WorkerErrorCode.EXECUTION_NOT_FOUND,
                "Execution ID was not found",
                execution_id=execution_id,
            ) from exc

    async def terminate(self, execution_id: str) -> ExecutionSnapshot:
        self._require_available()
        snapshot = await self.poll(execution_id)
        if snapshot.status is ExecutionStatus.RUNNING:
            snapshot = replace(
                snapshot,
                status=ExecutionStatus.CANCELLED,
                finished_at=self._next_timestamp(),
            )
            self.executions[execution_id] = snapshot
        return snapshot

    async def read_file(self, path: str) -> bytes:
        self._require_available()
        if path not in self.files:
            if self._is_directory(path):
                raise WorkerOperationError(WorkerErrorCode.IS_A_DIRECTORY, "Path is a directory")
            raise WorkerOperationError(WorkerErrorCode.FILE_NOT_FOUND, "File was not found")
        return self.files[path]

    async def write_file(self, path: str, content: bytes) -> FileStat:
        self._require_available()
        if self._is_directory(path):
            raise WorkerOperationError(WorkerErrorCode.IS_A_DIRECTORY, "Path is a directory")
        self.files[path] = content
        return FileStat(path=path, kind=FileKind.FILE, size_bytes=len(content))

    async def list_files(self, path: str) -> tuple[FileEntry, ...]:
        self._require_available()
        if path in self.files:
            raise WorkerOperationError(WorkerErrorCode.NOT_A_DIRECTORY, "Path is not a directory")
        if path != "." and not self._is_directory(path):
            raise WorkerOperationError(WorkerErrorCode.FILE_NOT_FOUND, "Directory was not found")
        prefix = "" if path == "." else f"{path}/"
        children: dict[str, FileEntry] = {}
        for file_path, content in self.files.items():
            if not file_path.startswith(prefix):
                continue
            remainder = file_path[len(prefix):]
            child_name, separator, _ = remainder.partition("/")
            child_path = f"{prefix}{child_name}" if prefix else child_name
            if separator:
                children[child_name] = FileEntry(child_name, child_path, FileKind.DIRECTORY, 0)
            else:
                children[child_name] = FileEntry(child_name, child_path, FileKind.FILE, len(content))
        return tuple(children[name] for name in sorted(children))

    async def stat_file(self, path: str) -> FileStat:
        self._require_available()
        if path in self.files:
            return FileStat(path=path, kind=FileKind.FILE, size_bytes=len(self.files[path]))
        if path == "." or self._is_directory(path):
            return FileStat(path=path, kind=FileKind.DIRECTORY, size_bytes=0)
        raise WorkerOperationError(WorkerErrorCode.FILE_NOT_FOUND, "Path was not found")

    async def capture_desktop_screenshot(self) -> bytes:
        self._require_available()
        if self.desktop_screenshot is None:
            raise WorkerOperationError(WorkerErrorCode.WORKER_UNAVAILABLE, "Desktop session is unavailable")
        return self.desktop_screenshot

    async def desktop_available(self) -> bool:
        self._require_available()
        return self.desktop_screenshot is not None

    async def desktop_control(self, operation: str) -> bool:
        self._require_available()
        if operation not in {"start_desktop", "stop_desktop"}:
            raise WorkerOperationError(WorkerErrorCode.INVALID_REQUEST, "Unsupported desktop control")
        return True

    def _is_directory(self, path: str) -> bool:
        prefix = f"{path}/"
        return any(file_path.startswith(prefix) for file_path in self.files)

    def _next_timestamp(self) -> datetime:
        timestamp = datetime(2000, 1, 1, tzinfo=UTC) + timedelta(seconds=self._clock_ticks)
        self._clock_ticks += 1
        return timestamp

    def _require_available(self) -> None:
        if not self.available:
            raise WorkerUnavailableError("Fake worker is unavailable")
