"""Host-owned SQLite records for recovering worker executions."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from nanobot.worker.interface import (
    ExecuteRequest,
    ExecutionSnapshot,
    ExecutionStatus,
    WorkerErrorCode,
    WorkerOperationError,
)

_SCHEMA_VERSION = 1


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    execution_id: str
    working_directory: str
    timeout_seconds: float
    dispatch_state: str
    status: ExecutionStatus
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    created_at: datetime
    accepted_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    cancel_requested_at: datetime | None
    updated_at: datetime
    error_code: str | None
    error_message: str | None

    def snapshot(self) -> ExecutionSnapshot:
        return ExecutionSnapshot(
            execution_id=self.execution_id,
            status=self.status,
            exit_code=self.exit_code,
            stdout=self.stdout,
            stderr=self.stderr,
            stdout_truncated=self.stdout_truncated,
            stderr_truncated=self.stderr_truncated,
            started_at=self.started_at or self.created_at,
            finished_at=self.finished_at,
        )


class SqliteExecutionStore:
    """Small synchronous store; its path must remain on the trusted host."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise ValueError("execution database path must be absolute")
        self.path = path
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.parent.is_symlink() or not self.path.parent.is_dir():
            raise ValueError("execution database parent must be a real directory")
        if self.path.is_symlink():
            raise ValueError("execution database must not be a symlink")
        self._initialize()

    @classmethod
    def default(cls) -> SqliteExecutionStore:
        from nanobot.config.paths import get_runtime_subdir

        directory = get_runtime_subdir("worker")
        if directory.is_symlink():
            raise ValueError("worker execution data directory must not be a symlink")
        os.chmod(directory, 0o700)
        return cls(directory / "executions.sqlite3")

    def _connect(self) -> sqlite3.Connection:
        if self.path.is_symlink():
            raise RuntimeError("execution database must not be a symlink")
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in (0, _SCHEMA_VERSION):
                raise RuntimeError(f"unsupported worker execution database version: {version}")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS worker_executions (
                    execution_id TEXT PRIMARY KEY,
                    working_directory TEXT NOT NULL,
                    timeout_seconds REAL NOT NULL,
                    dispatch_state TEXT NOT NULL,
                    status TEXT NOT NULL,
                    exit_code INTEGER,
                    stdout BLOB NOT NULL DEFAULT X'',
                    stderr BLOB NOT NULL DEFAULT X'',
                    stdout_truncated INTEGER NOT NULL DEFAULT 0,
                    stderr_truncated INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    accepted_at TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    cancel_requested_at TEXT,
                    updated_at TEXT NOT NULL,
                    error_code TEXT,
                    error_message TEXT
                )"""
            )
            connection.execute("PRAGMA user_version = 1")
            connection.commit()
        os.chmod(self.path, 0o600)

    def create_intent(self, request: ExecuteRequest) -> None:
        assert request.execution_id is not None
        now = _now().isoformat()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """INSERT INTO worker_executions (
                        execution_id, working_directory,
                        timeout_seconds, dispatch_state, status, created_at, updated_at
                    ) VALUES (?, ?, ?, 'intent', 'unknown', ?, ?)""",
                    (
                        request.execution_id,
                        request.working_directory,
                        request.timeout_seconds,
                        now,
                        now,
                    ),
                )
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise WorkerOperationError(
                WorkerErrorCode.DUPLICATE_EXECUTION_ID,
                "Execution ID has already been recorded",
                execution_id=request.execution_id,
            ) from exc

    def record_snapshot(self, snapshot: ExecutionSnapshot) -> None:
        now = _now().isoformat()
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """UPDATE worker_executions SET
                    dispatch_state = 'accepted',
                    accepted_at = COALESCE(accepted_at, ?),
                    status = ?, exit_code = ?, stdout = ?, stderr = ?,
                    stdout_truncated = ?, stderr_truncated = ?,
                    started_at = ?, finished_at = ?, updated_at = ?,
                    error_code = NULL, error_message = NULL
                    WHERE execution_id = ?""",
                (
                    now,
                    snapshot.status.value,
                    snapshot.exit_code,
                    snapshot.stdout,
                    snapshot.stderr,
                    int(snapshot.stdout_truncated),
                    int(snapshot.stderr_truncated),
                    snapshot.started_at.isoformat(),
                    snapshot.finished_at.isoformat() if snapshot.finished_at else None,
                    now,
                    snapshot.execution_id,
                ),
            )
            if cursor.rowcount != 1:
                raise WorkerOperationError(
                    WorkerErrorCode.EXECUTION_NOT_FOUND,
                    "Execution intent is not present in the host record store",
                    execution_id=snapshot.execution_id,
                )

    def mark_accepted(self, execution_id: str) -> None:
        now = _now().isoformat()
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """UPDATE worker_executions SET dispatch_state = 'accepted',
                    accepted_at = COALESCE(accepted_at, ?), updated_at = ?
                    WHERE execution_id = ?""",
                (now, now, execution_id),
            )
            if cursor.rowcount != 1:
                raise WorkerOperationError(
                    WorkerErrorCode.EXECUTION_NOT_FOUND,
                    "Execution intent is not present in the host record store",
                    execution_id=execution_id,
                )

    def record_error(
        self,
        execution_id: str,
        error: WorkerOperationError,
        *,
        execute_dispatch: bool = False,
    ) -> None:
        now = _now().isoformat()
        if error.code is WorkerErrorCode.EXECUTION_UNKNOWN and execute_dispatch:
            dispatch_state = "uncertain"
            accepted_at = now
            status = ExecutionStatus.UNKNOWN.value
        elif error.code is WorkerErrorCode.WORKER_UNAVAILABLE and execute_dispatch:
            dispatch_state = "not_accepted"
            accepted_at = None
            status = ExecutionStatus.UNKNOWN.value
        elif error.code is WorkerErrorCode.EXECUTION_NOT_FOUND:
            dispatch_state = "history_missing"
            accepted_at = None
            status = ExecutionStatus.UNKNOWN.value
        else:
            dispatch_state = None
            accepted_at = None
            status = None
        with closing(self._connect()) as connection:
            connection.execute(
                """UPDATE worker_executions SET
                    dispatch_state = COALESCE(?, dispatch_state),
                    accepted_at = COALESCE(accepted_at, ?),
                    status = COALESCE(?, status),
                    finished_at = CASE WHEN ? = 'unknown' THEN NULL ELSE finished_at END,
                    updated_at = ?, error_code = ?, error_message = ?
                    WHERE execution_id = ?""",
                (
                    dispatch_state,
                    accepted_at,
                    status,
                    status,
                    now,
                    error.code.value,
                    error.message,
                    execution_id,
                ),
            )

    def mark_cancel_requested(self, execution_id: str) -> None:
        now = _now().isoformat()
        with closing(self._connect()) as connection:
            connection.execute(
                """UPDATE worker_executions SET cancel_requested_at = ?, updated_at = ?
                    WHERE execution_id = ?""",
                (now, now, execution_id),
            )

    def get(self, execution_id: str) -> ExecutionRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM worker_executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
        return _record(row) if row is not None else None


def _record(row: sqlite3.Row) -> ExecutionRecord:
    return ExecutionRecord(
        execution_id=row["execution_id"],
        working_directory=row["working_directory"],
        timeout_seconds=row["timeout_seconds"],
        dispatch_state=row["dispatch_state"],
        status=ExecutionStatus(row["status"]),
        exit_code=row["exit_code"],
        stdout=bytes(row["stdout"]),
        stderr=bytes(row["stderr"]),
        stdout_truncated=bool(row["stdout_truncated"]),
        stderr_truncated=bool(row["stderr_truncated"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        accepted_at=_optional_datetime(row["accepted_at"]),
        started_at=_optional_datetime(row["started_at"]),
        finished_at=_optional_datetime(row["finished_at"]),
        cancel_requested_at=_optional_datetime(row["cancel_requested_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        error_code=row["error_code"],
        error_message=row["error_message"],
    )


def _optional_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None
