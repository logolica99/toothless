"""Durable host-owned Telegram task records."""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

TASK_ID_PATTERN = re.compile(r"^task-[0-9a-f]{32}$")
MAX_REQUEST_CHARS = 8_000
MAX_INSTRUCTION_CHARS = 4_000


class TaskStatus(StrEnum):
    QUEUED = "queued"
    PAUSED = "paused"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TaskRecord:
    task_id: str
    request: str
    instructions: str
    status: TaskStatus
    progress: str
    error: str | None
    created_at: datetime
    updated_at: datetime
    execution_ids: tuple[str, ...]
    source_update_id: int
    result: str | None
    approved_at: datetime | None
    cancellation_requested_at: datetime | None
    notification_error: str | None
    attachments: tuple[TaskAttachment, ...] = ()


@dataclass(frozen=True, slots=True)
class TaskAttachment:
    filename: str
    mime_type: str
    size_bytes: int
    storage_key: str
    telegram_file_id: str
    caption: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TaskFileDelivery:
    guest_path: str
    filename: str
    mime_type: str
    size_bytes: int
    status: str
    telegram_message_id: int | None
    error: str | None


@dataclass(frozen=True, slots=True)
class TaskEvent:
    event_type: str
    detail: str
    created_at: datetime


class TaskStore:
    """Own task creation, lifecycle and execution associations on the host."""

    def __init__(self, path: Path):
        if not path.is_absolute():
            raise ValueError("task database path must be absolute")
        self.path = path
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.parent.is_symlink() or not self.path.parent.is_dir():
            raise ValueError("task database parent must be a real directory")
        parent = self.path.parent.stat()
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077:
            raise ValueError("task database directory must be owner-only and owned by this user")
        if self.path.is_symlink():
            raise ValueError("task database must not be a symlink")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        if self.path.is_symlink():
            raise RuntimeError("task database must not be a symlink")
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    request TEXT NOT NULL,
                    instructions TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    progress TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    source_update_id INTEGER NOT NULL UNIQUE,
                    result TEXT,
                    approved_at TEXT,
                    cancellation_requested_at TEXT,
                    notification_error TEXT
                );
                CREATE TABLE IF NOT EXISTS task_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_executions (
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    execution_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, execution_id)
                );
                CREATE TABLE IF NOT EXISTS task_attachments (
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    telegram_file_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    storage_key TEXT NOT NULL,
                    caption TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, filename)
                );
                CREATE TABLE IF NOT EXISTS task_file_deliveries (
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    guest_path TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    telegram_message_id INTEGER,
                    error TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, guest_path)
                );
                CREATE INDEX IF NOT EXISTS task_created_idx ON tasks(created_at DESC, task_id DESC);
                CREATE INDEX IF NOT EXISTS task_events_idx ON task_events(task_id, event_id);"""
            )
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(tasks)")}
            for name in ("result", "approved_at", "cancellation_requested_at", "notification_error"):
                if name not in columns:
                    connection.execute(f"ALTER TABLE tasks ADD COLUMN {name} TEXT")
        os.chmod(self.path, 0o600)

    def create_from_update(self, update_id: int, request: str) -> tuple[TaskRecord, bool]:
        if isinstance(update_id, bool) or update_id < 0:
            raise ValueError("Telegram update ID must be a non-negative integer")
        request = request.strip()
        if not request or len(request) > MAX_REQUEST_CHARS:
            raise ValueError(f"task request must contain 1 to {MAX_REQUEST_CHARS} characters")
        now = _now().isoformat()
        task_id = f"task-{secrets.token_hex(16)}"
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """INSERT INTO tasks (
                        task_id, request, status, progress, created_at, updated_at, source_update_id
                    ) VALUES (?, ?, 'queued', 'AI execution has not started.', ?, ?, ?)""",
                    (task_id, request, now, now, update_id),
                )
                connection.execute(
                    "INSERT INTO task_events(task_id, event_type, detail, created_at) VALUES (?, 'created', ?, ?)",
                    (task_id, "Task accepted; AI execution is disabled.", now),
                )
                created = True
            except sqlite3.IntegrityError:
                row = connection.execute(
                    "SELECT task_id FROM tasks WHERE source_update_id = ?", (update_id,)
                ).fetchone()
                if row is None:
                    connection.rollback()
                    raise
                task_id = str(row["task_id"])
                created = False
            connection.commit()
        record = self.get(task_id)
        if record is None:
            raise RuntimeError("created task could not be read back")
        return record, created

    def get(self, task_id: str) -> TaskRecord | None:
        validate_task_id(task_id)
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                return None
            execution_ids = connection.execute(
                "SELECT execution_id FROM task_executions WHERE task_id = ? ORDER BY created_at, execution_id",
                (task_id,),
            ).fetchall()
            attachments = connection.execute(
                "SELECT filename, mime_type, size_bytes, storage_key, telegram_file_id, caption, created_at "
                "FROM task_attachments WHERE task_id = ? ORDER BY filename", (task_id,)
            ).fetchall()
        return _record(
            row,
            tuple(str(item["execution_id"]) for item in execution_ids),
            tuple(_attachment(item) for item in attachments),
        )

    def add_attachment(
        self, task_id: str, *, telegram_file_id: str, filename: str, mime_type: str,
        size_bytes: int, storage_key: str, caption: str,
    ) -> bool:
        validate_task_id(task_id)
        if not telegram_file_id or len(telegram_file_id) > 512 or size_bytes < 1:
            raise ValueError("invalid attachment metadata")
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO task_attachments VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, telegram_file_id, filename, mime_type[:120], size_bytes, storage_key, caption[:4000], _now().isoformat()),
            )
        return cursor.rowcount == 1

    def begin_file_delivery(
        self, task_id: str, *, guest_path: str, filename: str, mime_type: str, size_bytes: int,
    ) -> bool:
        """Persist a one-shot delivery intent; interrupted sends remain uncertain."""
        validate_task_id(task_id)
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO task_file_deliveries "
                "(task_id, guest_path, filename, mime_type, size_bytes, status, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'sending', ?)",
                (task_id, guest_path, filename, mime_type, size_bytes, _now().isoformat()),
            )
        return cursor.rowcount == 1

    def finish_file_delivery(
        self, task_id: str, guest_path: str, *, status: str,
        telegram_message_id: int | None = None, error: str | None = None,
    ) -> None:
        if status not in {"delivered", "failed", "uncertain"}:
            raise ValueError("invalid file delivery status")
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE task_file_deliveries SET status = ?, telegram_message_id = ?, error = ?, updated_at = ? "
                "WHERE task_id = ? AND guest_path = ? AND status = 'sending'",
                (status, telegram_message_id, (error or "")[:500] or None, _now().isoformat(), task_id, guest_path),
            )

    def list_file_deliveries(self, task_id: str) -> tuple[TaskFileDelivery, ...]:
        validate_task_id(task_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT guest_path, filename, mime_type, size_bytes, status, telegram_message_id, error "
                "FROM task_file_deliveries WHERE task_id = ? ORDER BY filename", (task_id,)
            ).fetchall()
        return tuple(TaskFileDelivery(**dict(row)) for row in rows)

    def resolve_interrupted_file_deliveries(self) -> int:
        """Mark pre-restart sends uncertain; never intentionally replay them."""
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "UPDATE task_file_deliveries SET status = 'uncertain', "
                "error = 'Controller restarted before Telegram delivery was confirmed.', updated_at = ? "
                "WHERE status = 'sending'",
                (_now().isoformat(),),
            )
        return cursor.rowcount

    def list_latest(self, limit: int = 10) -> tuple[TaskRecord, ...]:
        if isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("task record limit must be between 1 and 100")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT task_id FROM tasks ORDER BY created_at DESC, task_id DESC LIMIT ?", (limit,)
            ).fetchall()
        records = (self.get(str(row["task_id"])) for row in rows)
        return tuple(record for record in records if record is not None)

    def transition(self, task_id: str, target: TaskStatus) -> tuple[TaskRecord | None, bool]:
        validate_task_id(task_id)
        now = _now().isoformat()
        allowed = {
            TaskStatus.PAUSED: {TaskStatus.QUEUED},
            TaskStatus.QUEUED: {TaskStatus.PAUSED},
            TaskStatus.CANCELLED: {TaskStatus.QUEUED, TaskStatus.PAUSED},
        }
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                connection.rollback()
                return None, False
            current = TaskStatus(row["status"])
            if current is target:
                connection.commit()
                return self.get(task_id), False
            if target not in allowed or current not in allowed[target]:
                connection.commit()
                return self.get(task_id), False
            detail = f"Status changed from {current.value} to {target.value}."
            connection.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?",
                (target.value, now, task_id),
            )
            connection.execute(
                "INSERT INTO task_events(task_id, event_type, detail, created_at) VALUES (?, ?, ?, ?)",
                (task_id, target.value, detail, now),
            )
            connection.commit()
        return self.get(task_id), True

    def claim_for_dispatch(self, task_id: str, execution_id: str) -> tuple[TaskRecord | None, bool]:
        """Persist explicit /run authorization and dispatch intent atomically."""
        validate_task_id(task_id)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", execution_id):
            raise ValueError("invalid worker execution ID")
        now = _now().isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                connection.rollback()
                return None, False
            current = TaskStatus(row["status"])
            if current is not TaskStatus.QUEUED:
                connection.commit()
                return self.get(task_id), False
            active = connection.execute(
                "SELECT task_id FROM tasks WHERE status IN ('dispatching', 'running', 'cancelling') LIMIT 1"
            ).fetchone()
            if active is not None:
                connection.commit()
                return self.get(task_id), False
            connection.execute(
                "UPDATE tasks SET status = 'dispatching', progress = ?, approved_at = ?, updated_at = ? WHERE task_id = ?",
                ("Execution approved; dispatch intent recorded, awaiting worker acknowledgement.", now, now, task_id),
            )
            connection.execute(
                "INSERT INTO task_executions(task_id, execution_id, created_at) VALUES (?, ?, ?)",
                (task_id, execution_id, now),
            )
            connection.execute(
                "INSERT INTO task_events(task_id, event_type, detail, created_at) VALUES (?, 'dispatching', ?, ?)",
                (task_id, f"Explicit run approved; worker dispatch intent {execution_id} recorded.", now),
            )
            connection.commit()
        return self.get(task_id), True

    def mark_worker_accepted(self, execution_id: str) -> None:
        now = _now().isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT task_id, status FROM tasks JOIN task_executions USING(task_id) WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if row is not None and row["status"] == TaskStatus.DISPATCHING.value:
                task_id = str(row["task_id"])
                connection.execute(
                    "UPDATE tasks SET status = 'running', progress = ?, updated_at = ? WHERE task_id = ?",
                    ("Worker acknowledged the OpenCode task; model execution is in progress.", now, task_id),
                )
                connection.execute(
                    "INSERT INTO task_events(task_id, event_type, detail, created_at) VALUES (?, 'running', ?, ?)",
                    (task_id, "Worker acknowledged task execution.", now),
                )
            connection.commit()

    def record_progress(self, task_id: str, progress: str) -> bool:
        validate_task_id(task_id)
        progress = progress.strip()
        if not progress or len(progress) > 320:
            raise ValueError("progress must contain 1 to 320 characters")
        now = _now().isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None or row["status"] != TaskStatus.RUNNING.value:
                connection.rollback()
                return False
            connection.execute(
                "UPDATE tasks SET progress = ?, updated_at = ? WHERE task_id = ?",
                (progress, now, task_id),
            )
            connection.execute(
                "INSERT INTO task_events(task_id, event_type, detail, created_at) VALUES (?, 'progress', ?, ?)",
                (task_id, progress, now),
            )
            connection.commit()
        return True

    def request_task_cancellation(self, task_id: str) -> tuple[TaskRecord | None, bool]:
        validate_task_id(task_id)
        now = _now().isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                connection.rollback()
                return None, False
            status = TaskStatus(row["status"])
            if status in {TaskStatus.QUEUED, TaskStatus.PAUSED}:
                connection.execute(
                    "UPDATE tasks SET status = 'cancelled', progress = ?, updated_at = ? WHERE task_id = ?",
                    ("Cancelled before execution.", now, task_id),
                )
                event = "Task cancelled before execution."
                target = TaskStatus.CANCELLED
            elif status in {TaskStatus.DISPATCHING, TaskStatus.RUNNING}:
                connection.execute(
                    "UPDATE tasks SET status = 'cancelling', progress = ?, cancellation_requested_at = ?, updated_at = ? WHERE task_id = ?",
                    ("Cancellation requested; waiting for worker confirmation.", now, now, task_id),
                )
                event = "Cancellation intent persisted before worker termination request."
                target = TaskStatus.CANCELLING
            else:
                connection.commit()
                return self.get(task_id), False
            connection.execute(
                "INSERT INTO task_events(task_id, event_type, detail, created_at) VALUES (?, ?, ?, ?)",
                (task_id, target.value, event, now),
            )
            connection.commit()
        return self.get(task_id), True

    def finish_execution(
        self, execution_id: str, status: TaskStatus, *, result: str | None = None, error: str | None = None
    ) -> TaskRecord | None:
        if status not in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.UNKNOWN}:
            raise ValueError("execution result must be terminal or unknown")
        now = _now().isoformat()
        progress = {
            TaskStatus.SUCCEEDED: "OpenCode completed and the result was saved.",
            TaskStatus.FAILED: "OpenCode execution failed.",
            TaskStatus.CANCELLED: "Worker confirmed execution cancellation.",
            TaskStatus.UNKNOWN: "Execution outcome is unknown; it was not resubmitted.",
        }[status]
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT tasks.task_id, tasks.status FROM tasks JOIN task_executions USING(task_id) WHERE task_executions.execution_id = ?",
                (execution_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            task_id = str(row["task_id"])
            current = TaskStatus(row["status"])
            if current in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.UNKNOWN}:
                connection.commit()
                return self.get(task_id)
            connection.execute(
                "UPDATE tasks SET status = ?, progress = ?, error = ?, result = ?, updated_at = ? WHERE task_id = ?",
                (status.value, progress, error, result, now, task_id),
            )
            connection.execute(
                "INSERT INTO task_events(task_id, event_type, detail, created_at) VALUES (?, ?, ?, ?)",
                (task_id, status.value, error or progress, now),
            )
            connection.commit()
        return self.get(task_id)

    def record_recovered_partial_result(
        self, task_id: str, result: str, *, progress: str, detail: str
    ) -> bool:
        """Attach independently verified output to a timed-out task without changing its failure state."""
        validate_task_id(task_id)
        if not result.strip() or len(result) > 48_000 or len(progress) > 320 or len(detail) > 800:
            raise ValueError("recovered result metadata exceeds the allowed limits")
        now = _now().isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, error, result FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if (
                row is None
                or row["status"] != TaskStatus.FAILED.value
                or not str(row["error"] or "").startswith("timed_out:")
                or row["result"] is not None
            ):
                connection.rollback()
                return False
            connection.execute(
                "UPDATE tasks SET result = ?, progress = ?, updated_at = ? WHERE task_id = ?",
                (result, progress, now, task_id),
            )
            connection.execute(
                "INSERT INTO task_events(task_id, event_type, detail, created_at) VALUES (?, 'partial_result_recovered', ?, ?)",
                (task_id, detail, now),
            )
            connection.commit()
        return True

    def active_tasks(self) -> tuple[TaskRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT task_id FROM tasks WHERE status IN ('dispatching', 'running', 'cancelling') ORDER BY updated_at"
            ).fetchall()
        return tuple(record for row in rows if (record := self.get(str(row["task_id"]))) is not None)

    def record_notification_error(self, task_id: str, message: str) -> None:
        now = _now().isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE tasks SET notification_error = ?, updated_at = ? WHERE task_id = ?",
                (message[:800], now, task_id),
            )
            connection.execute(
                "INSERT INTO task_events(task_id, event_type, detail, created_at) VALUES (?, 'delivery_failed', ?, ?)",
                (task_id, "Telegram result delivery failed; the result remains stored on the host." , now),
            )
            connection.commit()

    def mark_unresolved(self, task_id: str, message: str) -> None:
        """Record an honest unknown terminal state after loss of execution evidence."""
        validate_task_id(task_id)
        now = _now().isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                connection.rollback()
                return
            if TaskStatus(row["status"]) not in {TaskStatus.DISPATCHING, TaskStatus.RUNNING, TaskStatus.CANCELLING}:
                connection.commit()
                return
            connection.execute(
                "UPDATE tasks SET status = 'unknown', progress = ?, error = ?, updated_at = ? WHERE task_id = ?",
                ("Execution outcome is unknown; the operation was not resubmitted.", message[:800], now, task_id),
            )
            connection.execute(
                "INSERT INTO task_events(task_id, event_type, detail, created_at) VALUES (?, 'unknown', ?, ?)",
                (task_id, message[:800], now),
            )
            connection.commit()

    def finish_without_execution(self, task_id: str, status: TaskStatus, message: str) -> None:
        validate_task_id(task_id)
        if status not in {TaskStatus.FAILED, TaskStatus.UNKNOWN}:
            raise ValueError("dispatch resolution must be failed or unknown")
        now = _now().isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE tasks SET status = ?, progress = ?, error = ?, updated_at = ? WHERE task_id = ? AND status = 'dispatching'",
                (status.value, "Task dispatch ended before a confirmed worker result.", message[:800], now, task_id),
            )
            if cursor.rowcount == 1:
                connection.execute(
                    "INSERT INTO task_events(task_id, event_type, detail, created_at) VALUES (?, ?, ?, ?)",
                    (task_id, status.value, message[:800], now),
                )
            connection.commit()

    def append_instruction(self, task_id: str, instruction: str) -> tuple[TaskRecord | None, bool]:
        validate_task_id(task_id)
        instruction = instruction.strip()
        if not instruction or len(instruction) > MAX_INSTRUCTION_CHARS:
            raise ValueError(f"instructions must contain 1 to {MAX_INSTRUCTION_CHARS} characters")
        now = _now().isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT status, instructions FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                connection.rollback()
                return None, False
            if TaskStatus(row["status"]) not in {TaskStatus.QUEUED, TaskStatus.PAUSED}:
                connection.commit()
                return self.get(task_id), False
            old = str(row["instructions"])
            combined = f"{old}\n{instruction}" if old else instruction
            if len(combined) > MAX_REQUEST_CHARS:
                connection.rollback()
                raise ValueError("combined task instructions exceed the storage limit")
            connection.execute(
                "UPDATE tasks SET instructions = ?, updated_at = ? WHERE task_id = ?",
                (combined, now, task_id),
            )
            connection.execute(
                "INSERT INTO task_events(task_id, event_type, detail, created_at) VALUES (?, 'instruction_added', ?, ?)",
                (task_id, instruction, now),
            )
            connection.commit()
        return self.get(task_id), True

    def associate_execution(self, task_id: str, execution_id: str) -> None:
        validate_task_id(task_id)
        execution_id_pattern = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
        if not re.fullmatch(execution_id_pattern, execution_id):
            raise ValueError("invalid worker execution ID")
        now = _now().isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)).fetchone() is None:
                connection.rollback()
                raise KeyError(task_id)
            connection.execute(
                "INSERT OR IGNORE INTO task_executions(task_id, execution_id, created_at) VALUES (?, ?, ?)",
                (task_id, execution_id, now),
            )
            connection.commit()

    def events(self, task_id: str, limit: int = 50) -> tuple[TaskEvent, ...]:
        validate_task_id(task_id)
        if not 1 <= limit <= 100:
            raise ValueError("task event limit must be between 1 and 100")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT event_type, detail, created_at FROM task_events WHERE task_id = ? ORDER BY event_id DESC LIMIT ?",
                (task_id, limit),
            ).fetchall()
        return tuple(
            TaskEvent(str(row["event_type"]), str(row["detail"]), datetime.fromisoformat(row["created_at"]))
            for row in reversed(rows)
        )


def validate_task_id(task_id: object) -> None:
    if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
        raise ValueError("invalid task ID")


def _now() -> datetime:
    return datetime.now(UTC)


def _record(
    row: sqlite3.Row, execution_ids: tuple[str, ...], attachments: tuple[TaskAttachment, ...] = ()
) -> TaskRecord:
    return TaskRecord(
        task_id=str(row["task_id"]),
        request=str(row["request"]),
        instructions=str(row["instructions"]),
        status=TaskStatus(row["status"]),
        progress=str(row["progress"]),
        error=row["error"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        execution_ids=execution_ids,
        source_update_id=int(row["source_update_id"]),
        result=row["result"],
        approved_at=datetime.fromisoformat(row["approved_at"]) if row["approved_at"] else None,
        cancellation_requested_at=(
            datetime.fromisoformat(row["cancellation_requested_at"])
            if row["cancellation_requested_at"] else None
        ),
        notification_error=row["notification_error"],
        attachments=attachments,
    )


def _attachment(row: sqlite3.Row) -> TaskAttachment:
    return TaskAttachment(
        filename=str(row["filename"]), mime_type=str(row["mime_type"]),
        size_bytes=int(row["size_bytes"]), storage_key=str(row["storage_key"]),
        telegram_file_id=str(row["telegram_file_id"]), caption=str(row["caption"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
    )
