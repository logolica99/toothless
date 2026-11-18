from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.personal_agent.task_store import TaskStatus, TaskStore, validate_task_id


def test_task_creation_is_durable_and_idempotent_by_telegram_update(tmp_path: Path) -> None:
    database = tmp_path / "tasks.sqlite3"
    first = TaskStore(database)
    task, created = first.create_from_update(71, "Research open-source coding agents")
    assert created is True
    assert task.status is TaskStatus.QUEUED
    assert task.request == "Research open-source coding agents"
    assert task.progress == "AI execution has not started."
    assert task.error is None
    assert task.execution_ids == ()

    duplicate, created_again = first.create_from_update(71, "different redelivered payload")
    assert created_again is False
    assert duplicate.task_id == task.task_id
    assert duplicate.request == task.request
    assert first.list_latest() == (task,)

    restarted = TaskStore(database)
    recovered = restarted.get(task.task_id)
    assert recovered == task
    assert [event.event_type for event in restarted.events(task.task_id)] == ["created"]


def test_task_lifecycle_updates_instructions_and_execution_links(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite3")
    task, _ = store.create_from_update(10, "Prepare a short summary")

    paused, changed = store.transition(task.task_id, TaskStatus.PAUSED)
    assert changed and paused is not None and paused.status is TaskStatus.PAUSED
    repeated_pause, changed = store.transition(task.task_id, TaskStatus.PAUSED)
    assert changed is False and repeated_pause == paused

    updated, changed = store.append_instruction(task.task_id, "Keep the answer under five bullets.")
    assert changed and updated is not None
    assert updated.instructions == "Keep the answer under five bullets."

    resumed, changed = store.transition(task.task_id, TaskStatus.QUEUED)
    assert changed and resumed is not None and resumed.status is TaskStatus.QUEUED
    store.associate_execution(task.task_id, "worker-run-123")
    store.associate_execution(task.task_id, "worker-run-123")
    linked = store.get(task.task_id)
    assert linked is not None and linked.execution_ids == ("worker-run-123",)

    cancelled, changed = store.transition(task.task_id, TaskStatus.CANCELLED)
    assert changed and cancelled is not None and cancelled.status is TaskStatus.CANCELLED
    repeated_cancel, changed = store.transition(task.task_id, TaskStatus.CANCELLED)
    assert changed is False and repeated_cancel == cancelled
    cannot_resume, changed = store.transition(task.task_id, TaskStatus.QUEUED)
    assert changed is False and cannot_resume is not None
    assert cannot_resume.status is TaskStatus.CANCELLED
    assert [event.event_type for event in store.events(task.task_id)] == [
        "created", "paused", "instruction_added", "queued", "cancelled"
    ]


def test_task_request_validation_and_owner_only_database(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite3")
    with pytest.raises(ValueError, match="1 to"):
        store.create_from_update(1, "  ")
    with pytest.raises(ValueError, match="1 to"):
        store.create_from_update(1, "x" * 8_001)
    with pytest.raises(ValueError, match="invalid task ID"):
        validate_task_id("../not-a-task")
    assert store.path.stat().st_mode & 0o777 == 0o600

    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    with pytest.raises(ValueError, match="owner-only"):
        TaskStore(shared / "tasks.sqlite3")
    assert shared.stat().st_mode & 0o777 == 0o755


def test_explicit_run_claim_is_single_active_durable_and_cancellable(tmp_path: Path) -> None:
    database = tmp_path / "tasks.sqlite3"
    store = TaskStore(database)
    first, _ = store.create_from_update(101, "Explain CPU and GPU")
    second, _ = store.create_from_update(102, "Another queued task")

    claimed, changed = store.claim_for_dispatch(first.task_id, "opencode-run-1")
    assert changed and claimed is not None
    assert claimed.status is TaskStatus.DISPATCHING
    assert claimed.execution_ids == ("opencode-run-1",)
    assert claimed.approved_at is not None
    blocked, changed = store.claim_for_dispatch(second.task_id, "opencode-run-2")
    assert not changed and blocked is not None and blocked.status is TaskStatus.QUEUED

    store.mark_worker_accepted("opencode-run-1")
    running = store.get(first.task_id)
    assert running is not None and running.status is TaskStatus.RUNNING
    cancelling, changed = store.request_task_cancellation(first.task_id)
    assert changed and cancelling is not None and cancelling.status is TaskStatus.CANCELLING
    assert cancelling.cancellation_requested_at is not None

    completed = store.finish_execution("opencode-run-1", TaskStatus.CANCELLED)
    assert completed is not None and completed.status is TaskStatus.CANCELLED
    repeated = store.finish_execution("opencode-run-1", TaskStatus.SUCCEEDED, result="stale result")
    assert repeated is not None and repeated.status is TaskStatus.CANCELLED
    assert repeated.result is None
    assert [event.event_type for event in store.events(first.task_id)] == [
        "created", "dispatching", "running", "cancelling", "cancelled"
    ]


def test_existing_phase04_task_database_migrates_without_losing_records(tmp_path: Path) -> None:
    import sqlite3

    database = tmp_path / "tasks.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY, request TEXT NOT NULL, instructions TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL, progress TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, source_update_id INTEGER NOT NULL UNIQUE
        );
        CREATE TABLE task_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
            event_type TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE task_executions (
            task_id TEXT NOT NULL, execution_id TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(task_id, execution_id)
        );"""
    )
    connection.execute(
        "INSERT INTO tasks VALUES (?, ?, '', 'queued', ?, NULL, ?, ?, ?)",
        ("task-" + "a" * 32, "old task", "AI execution has not started.", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", 5),
    )
    connection.commit()
    connection.close()

    migrated = TaskStore(database).get("task-" + "a" * 32)
    assert migrated is not None
    assert migrated.request == "old task"
    assert migrated.status is TaskStatus.QUEUED
    assert migrated.result is None and migrated.notification_error is None


def test_missing_execution_history_is_unknown_and_never_requeued(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite3")
    task, _ = store.create_from_update(103, "Reconcile a reset worker task")
    claimed, changed = store.claim_for_dispatch(task.task_id, "reset-run-1")
    assert changed and claimed is not None
    store.mark_worker_accepted("reset-run-1")
    store.mark_unresolved(task.task_id, "Guest execution history is missing after reset.")
    unknown = store.get(task.task_id)
    assert unknown is not None and unknown.status is TaskStatus.UNKNOWN
    assert unknown.execution_ids == ("reset-run-1",)
    assert unknown.progress == "Execution outcome is unknown; the operation was not resubmitted."
    latest, changed = store.claim_for_dispatch(task.task_id, "reset-run-2")
    assert not changed and latest is not None and latest.status is TaskStatus.UNKNOWN


def test_interrupted_file_delivery_becomes_uncertain_without_retry(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite3")
    task, _ = store.create_from_update(104, "Create an output CSV")
    path = f"opencode-task-workspace/exports/{task.task_id}/result.csv"
    assert store.begin_file_delivery(
        task.task_id, guest_path=path, filename="result.csv", mime_type="text/csv", size_bytes=12
    )
    reopened = TaskStore(tmp_path / "tasks.sqlite3")
    assert reopened.resolve_interrupted_file_deliveries() == 1
    delivery = reopened.list_file_deliveries(task.task_id)[0]
    assert delivery.status == "uncertain"
    assert not reopened.begin_file_delivery(
        task.task_id, guest_path=path, filename="result.csv", mime_type="text/csv", size_bytes=12
    )


def test_recovered_result_is_saved_without_rewriting_timeout_failure(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite3")
    task, _ = store.create_from_update(105, "Analyze the uploaded CSV")
    claimed, changed = store.claim_for_dispatch(task.task_id, "opencode-timeout-1")
    assert changed and claimed is not None
    store.mark_worker_accepted("opencode-timeout-1")
    failed = store.finish_execution(
        "opencode-timeout-1", TaskStatus.FAILED, error="timed_out: OpenCode timed out."
    )
    assert failed is not None and failed.status is TaskStatus.FAILED

    assert store.record_recovered_partial_result(
        task.task_id,
        "Independent check: count 5, mean 6.",
        progress="Artifacts recovered and independently checked after timeout.",
        detail="Partial artifact result recovered; task remains failed.",
    )
    recovered = store.get(task.task_id)
    assert recovered is not None and recovered.status is TaskStatus.FAILED
    assert recovered.error == "timed_out: OpenCode timed out."
    assert recovered.result == "Independent check: count 5, mean 6."
    assert [event.event_type for event in store.events(task.task_id)][-1] == "partial_result_recovered"
    assert not store.record_recovered_partial_result(
        task.task_id,
        "duplicate",
        progress="duplicate",
        detail="duplicate",
    )


def test_task_progress_is_persisted_only_while_running(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite3")
    task, _ = store.create_from_update(106, "A task with progress")
    assert not store.record_progress(task.task_id, "Still running")
    claimed, changed = store.claim_for_dispatch(task.task_id, "opencode-progress-1")
    assert changed and claimed is not None
    store.mark_worker_accepted("opencode-progress-1")
    assert store.record_progress(task.task_id, "Still running on Ubuntu (1 minute elapsed).")
    updated = store.get(task.task_id)
    assert updated is not None and updated.progress == "Still running on Ubuntu (1 minute elapsed)."
    store.finish_execution("opencode-progress-1", TaskStatus.FAILED, error="timed_out: stop")
    assert not store.record_progress(task.task_id, "late progress")
