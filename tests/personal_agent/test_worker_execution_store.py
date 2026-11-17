from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nanobot.worker.execution_store import SqliteExecutionStore
from nanobot.worker.fake import FakeCommandPlan, FakeWorkerBackend
from nanobot.worker.interface import (
    ExecuteRequest,
    ExecutionStatus,
    WorkerErrorCode,
    WorkerInterface,
    WorkerOperationError,
)


@pytest.mark.asyncio
async def test_intent_is_committed_before_dispatch_and_result_survives_reopen(
    tmp_path: Path,
) -> None:
    database = tmp_path / "worker-executions.sqlite3"
    store = SqliteExecutionStore(database)

    class IntentCheckingBackend(FakeWorkerBackend):
        async def execute(self, request: ExecuteRequest):
            assert request.execution_id is not None
            intent = store.get(request.execution_id)
            assert intent is not None
            assert intent.dispatch_state == "intent"
            assert intent.status is ExecutionStatus.UNKNOWN
            return await super().execute(request)

    backend = IntentCheckingBackend(
        command_plans={"printf secret": FakeCommandPlan(stdout=b"persisted result")}
    )
    worker = WorkerInterface(backend, execution_store=store)
    result = await worker.execute(
        ExecuteRequest("printf secret", execution_id="persisted-1")
    )

    assert result.status is ExecutionStatus.SUCCEEDED
    record = store.get("persisted-1")
    assert record is not None
    assert record.dispatch_state == "accepted"
    assert record.accepted_at is not None
    assert record.stdout == b"persisted result"
    assert record.error_code is None
    with sqlite3.connect(database) as connection:
        column_names = {row[1] for row in connection.execute("PRAGMA table_info(worker_executions)")}
    assert "command" not in column_names
    assert "command_sha256" not in column_names
    assert database.stat().st_mode & 0o777 == 0o600

    restarted = WorkerInterface(
        FakeWorkerBackend(available=False),
        execution_store=SqliteExecutionStore(database),
    )
    recovered = await restarted.reconcile("persisted-1")
    assert recovered.status is ExecutionStatus.SUCCEEDED
    assert recovered.stdout == b"persisted result"


@pytest.mark.asyncio
async def test_duplicate_id_is_rejected_after_interface_recreation(tmp_path: Path) -> None:
    store = SqliteExecutionStore(tmp_path / "worker-executions.sqlite3")
    first_backend = FakeWorkerBackend()
    await WorkerInterface(first_backend, execution_store=store).execute(
        ExecuteRequest("first", execution_id="stable-after-restart")
    )

    second_backend = FakeWorkerBackend()
    with pytest.raises(WorkerOperationError) as error:
        await WorkerInterface(
            second_backend,
            execution_store=SqliteExecutionStore(store.path),
        ).execute(ExecuteRequest("second", execution_id="stable-after-restart"))

    assert error.value.code is WorkerErrorCode.DUPLICATE_EXECUTION_ID
    assert second_backend.submitted_requests == []


@pytest.mark.asyncio
async def test_missing_guest_history_after_reset_is_unknown_not_success(
    tmp_path: Path,
) -> None:
    store = SqliteExecutionStore(tmp_path / "worker-executions.sqlite3")
    await WorkerInterface(
        FakeWorkerBackend(command_plans={"sleep": FakeCommandPlan(stays_running=True)}),
        execution_store=store,
    ).execute(ExecuteRequest("sleep", execution_id="lost-after-reset"))

    restarted = WorkerInterface(
        FakeWorkerBackend(), execution_store=SqliteExecutionStore(store.path)
    )
    result = await restarted.reconcile("lost-after-reset")

    assert result.status is ExecutionStatus.UNKNOWN
    record = restarted.get_execution_record("lost-after-reset")
    assert record is not None
    assert record.dispatch_state == "history_missing"
    assert record.error_code == WorkerErrorCode.EXECUTION_NOT_FOUND.value
    assert record.error_message == "Execution ID was not found"


@pytest.mark.asyncio
async def test_cancellation_request_and_final_status_are_persisted(tmp_path: Path) -> None:
    store = SqliteExecutionStore(tmp_path / "worker-executions.sqlite3")
    backend = FakeWorkerBackend(
        command_plans={"sleep": FakeCommandPlan(stays_running=True)}
    )
    worker = WorkerInterface(backend, execution_store=store)
    await worker.execute(ExecuteRequest("sleep", execution_id="cancel-me"))

    cancelled = await worker.terminate("cancel-me")

    assert cancelled.status is ExecutionStatus.CANCELLED
    record = store.get("cancel-me")
    assert record is not None
    assert record.cancel_requested_at is not None
    assert record.status is ExecutionStatus.CANCELLED
    assert record.finished_at is not None


@pytest.mark.asyncio
async def test_reconcile_retries_recorded_cancellation_not_the_command(
    tmp_path: Path,
) -> None:
    store = SqliteExecutionStore(tmp_path / "worker-executions.sqlite3")
    backend = FakeWorkerBackend(
        command_plans={"sleep": FakeCommandPlan(stays_running=True)}
    )
    worker = WorkerInterface(backend, execution_store=store)
    await worker.execute(ExecuteRequest("sleep", execution_id="cancel-reconnect"))
    backend.available = False
    with pytest.raises(WorkerOperationError) as error:
        await worker.terminate("cancel-reconnect")
    assert error.value.code is WorkerErrorCode.WORKER_UNAVAILABLE

    backend.available = True
    restarted = WorkerInterface(backend, execution_store=SqliteExecutionStore(store.path))
    result = await restarted.reconcile("cancel-reconnect")

    assert result.status is ExecutionStatus.CANCELLED
    assert len(backend.submitted_requests) == 1


@pytest.mark.asyncio
async def test_list_latest_is_bounded_and_returns_newest_first(tmp_path: Path) -> None:
    store = SqliteExecutionStore(tmp_path / "worker-executions.sqlite3")
    backend = FakeWorkerBackend()
    worker = WorkerInterface(backend, execution_store=store)

    for index in range(12):
        await worker.execute(ExecuteRequest(f"command-{index}", execution_id=f"list-{index}"))
    records = store.list_latest(10)
    assert len(records) == 10
    assert records[0].execution_id == "list-11"
    assert records[-1].execution_id == "list-2"
    with pytest.raises(ValueError, match="between 1 and 100"):
        store.list_latest(0)
