"""Typed contracts for work delegated to an isolated worker."""

from nanobot.worker.execution_store import ExecutionRecord, SqliteExecutionStore
from nanobot.worker.interface import (
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
)
from nanobot.worker.ssh_backend import (
    SSHWorkerBackend,
    SSHWorkerConfig,
    create_persistent_worker,
)

__all__ = [
    "ExecuteRequest",
    "ExecutionSnapshot",
    "ExecutionRecord",
    "ExecutionStatus",
    "FileEntry",
    "FileKind",
    "FileStat",
    "SSHWorkerBackend",
    "SSHWorkerConfig",
    "SqliteExecutionStore",
    "WorkerErrorCode",
    "WorkerInterface",
    "WorkerOperationError",
    "WorkerUnavailableError",
    "create_persistent_worker",
]
