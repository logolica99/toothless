from __future__ import annotations

import asyncio
import json
import logging
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Callable

import httpx
import pytest

from nanobot.personal_agent import telegram_controller as controller
from nanobot.personal_agent.task_store import TaskStatus
from nanobot.worker.execution_store import SqliteExecutionStore
from nanobot.worker.fake import FakeCommandPlan, FakeWorkerBackend
from nanobot.worker.interface import (
    ExecuteRequest,
    ExecutionStatus,
    WorkerErrorCode,
    WorkerInterface,
    WorkerOperationError,
)


def _update(update_id: int, *, user_id: int = 42, chat_id: int = 42, chat_type: str = "private", text: str = "/ping") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "from": {"id": user_id},
            "chat": {"id": chat_id, "type": chat_type},
            "text": text,
        },
    }


def _app(
    tmp_path: Path,
    *,
    execution_store: SqliteExecutionStore | None = None,
    worker_provider: Callable[[], AbstractContextManager[WorkerInterface]] = controller._remote_worker,
) -> tuple[controller.TelegramController, list[dict], httpx.AsyncClient]:
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sendMessage"):
            sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = controller.TelegramConfig("test-token", 42, tmp_path / "updates.sqlite3")
    app = controller.TelegramController(
        config,
        client=client,
        execution_store=execution_store or SqliteExecutionStore(tmp_path / "executions.sqlite3"),
        worker_provider=worker_provider,
    )
    return app, sent, client


def test_commands_are_limited_and_unknown_text_does_not_dispatch() -> None:
    assert controller.command_reply("/ping", worker_state="ignored") == "Toothless is online."
    assert "/worker" in controller.command_reply("/help", worker_state="ignored")
    assert "stopped" in controller.command_reply("/worker", worker_state="stopped")
    assert "/run <task_id>" in controller.command_reply("hello", worker_state="ignored")
    assert "Unknown command" in controller.command_reply("/shell", worker_state="ignored")


@pytest.mark.asyncio
async def test_only_exact_owner_private_messages_receive_replies_and_updates_persist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(controller, "_worker_state", lambda: "stopped")
    app, sent, client = _app(tmp_path)
    await app._handle(_update(1))
    await app._handle(_update(1))
    await app._handle(_update(2, user_id=7))
    await app._handle(_update(3, chat_type="group"))
    await app._handle({"update_id": 4, "callback_query": {"from": {"id": 42}}})
    assert len(sent) == 1
    assert sent[0]["text"] == "Toothless is online."
    await app.close()

    reopened = controller.UpdateStore(tmp_path / "updates.sqlite3")
    assert reopened.next_offset() == 5
    assert reopened.claim(1) is False
    assert reopened.claim(4) is False
    reopened.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_rate_limit_uses_bounded_retry_after(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"parameters": {"retry_after": 2}})
        return httpx.Response(200, json={"ok": True, "result": True})

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", record_sleep)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = controller.TelegramController(
        controller.TelegramConfig("test-token", 42, tmp_path / "updates.sqlite3"),
        client=client,
        execution_store=SqliteExecutionStore(tmp_path / "executions.sqlite3"),
    )
    assert await app._api("sendMessage", {"chat_id": 42, "text": "ok"}) is True
    assert calls == 2
    assert delays == [2.0]
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_task_commands_show_records_without_arbitrary_execution(tmp_path: Path) -> None:
    execution_store = SqliteExecutionStore(tmp_path / "executions.sqlite3")
    backend = FakeWorkerBackend(command_plans={
        "harmless complete": FakeCommandPlan(stdout=b"done\x1b[31m" + b"x" * 5000),
        "sleep": FakeCommandPlan(stays_running=True),
    })
    worker = WorkerInterface(backend, execution_store=execution_store)
    await worker.execute(ExecuteRequest("harmless complete", execution_id="completed-123456"))
    running = await worker.execute(ExecuteRequest("sleep", execution_id="running-123456"))
    app, sent, client = _app(
        tmp_path,
        execution_store=execution_store,
        worker_provider=lambda: nullcontext(worker),
    )

    await app._handle(_update(10, text="/executions"))
    listing = sent[-1]["text"]
    assert "completed-12" in listing
    assert "running-123456"[:12] in listing
    assert "succeeded" in listing and "running" in listing
    assert "started" in listing

    await app._handle(_update(11, text="/execution completed-123456"))
    detail = sent[-1]["text"]
    assert "Execution: completed-123456" in detail
    assert "Stored status: succeeded" in detail
    assert "Fresh reconciled status: not queried" in detail
    assert "stdout:" in detail and "truncated" in detail
    assert "\x1b" not in detail
    assert len(detail) <= controller.MAX_TELEGRAM_MESSAGE_CHARS

    await app._handle(_update(12, text="/execution ../invalid"))
    assert "Invalid execution ID" in sent[-1]["text"]
    await app._handle(_update(13, text="/execution missing-123"))
    assert "not found" in sent[-1]["text"]
    await app._handle(_update(15, text="/cancel missing-123"))
    assert "nothing was cancelled" in sent[-1]["text"]
    await app._handle(_update(16, text="/cancel ../invalid"))
    assert "Invalid execution ID" in sent[-1]["text"]

    await app._handle(_update(14, text="/execution running-123456"))
    assert "Stored status: running" in sent[-1]["text"]
    assert "Fresh reconciled status: running" in sent[-1]["text"]
    assert running.status is ExecutionStatus.RUNNING
    assert len(backend.submitted_requests) == 2
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_task_and_execution_commands_survive_controller_restart(tmp_path: Path) -> None:
    database = tmp_path / "executions.sqlite3"
    execution_store = SqliteExecutionStore(database)
    first_app, first_sent, first_client = _app(tmp_path, execution_store=execution_store)
    await first_app._handle(_update(40, text="/new review the safety checklist"))
    task_id = first_sent[-1]["text"].split()[1]
    assert task_id.startswith("task-")
    assert "/run " in first_sent[-1]["text"]
    await first_app._handle(_update(41, text="/tasks"))
    assert task_id in first_sent[-1]["text"]
    await first_app.close()
    await first_client.aclose()

    backend = FakeWorkerBackend(command_plans={"done": FakeCommandPlan(stdout=b"restart result")})
    worker = WorkerInterface(backend, execution_store=SqliteExecutionStore(database))
    await worker.execute(ExecuteRequest("done", execution_id="restart-task-123"))
    restarted_store = SqliteExecutionStore(database)
    restarted_app, sent, client = _app(tmp_path, execution_store=restarted_store)
    await restarted_app._handle(_update(42, text="/tasks"))
    assert task_id in sent[-1]["text"]
    await restarted_app._handle(_update(43, text=f"/task {task_id}"))
    assert "Status: queued" in sent[-1]["text"]
    assert "review the safety checklist" in sent[-1]["text"]
    await restarted_app._handle(_update(44, text="/executions"))
    assert "restart-task" in sent[-1]["text"]
    await restarted_app._handle(_update(45, text="/execution restart-task-123"))
    assert "Stored status: succeeded" in sent[-1]["text"]
    assert "restart result" in sent[-1]["text"]
    await restarted_app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_task_commands_create_update_pause_resume_cancel_and_deduplicate(tmp_path: Path) -> None:
    execution_store = SqliteExecutionStore(tmp_path / "executions.sqlite3")
    backend = FakeWorkerBackend()
    worker = WorkerInterface(backend, execution_store=execution_store)
    app, sent, client = _app(
        tmp_path,
        execution_store=execution_store,
        worker_provider=lambda: nullcontext(worker),
    )

    await app._handle(_update(50, text="/new Research current open-source coding agents"))
    first_reply = sent[-1]["text"]
    task_id = app.task_store.list_latest()[0].task_id
    assert task_id.startswith("task-")
    assert first_reply == (
        f"Task <code>{task_id}</code> is queued.\n\n"
        f"To start it, send:\n<code>/run {task_id}</code>"
    )
    await app._handle(_update(50, text="/new duplicate payload"))
    assert len(sent) == 1
    assert len(app.task_store.list_latest()) == 1

    await app._handle(_update(51, text="Compare two public project licenses"))
    second_id = app.task_store.list_latest()[0].task_id
    assert second_id != task_id
    assert "queued" in sent[-1]["text"]
    assert len(backend.submitted_requests) == 0

    await app._handle(_update(52, text="/tasks"))
    assert task_id in sent[-1]["text"] and second_id in sent[-1]["text"]
    await app._handle(_update(53, text=f"/task {task_id}"))
    detail = sent[-1]["text"]
    assert "Status: queued" in detail
    assert "Research current open-source coding agents" in detail
    assert "AI execution has not started" in detail

    await app._handle(_update(54, text=f"/update {task_id} Include primary sources"))
    assert "Instructions added" in sent[-1]["text"]
    await app._handle(_update(55, text=f"/pause {task_id}"))
    assert "is paused" in sent[-1]["text"]
    await app._handle(_update(56, text=f"/resume {task_id}"))
    assert "queued again" in sent[-1]["text"]
    await app._handle(_update(57, text=f"/cancel {task_id}"))
    assert "cancelled before execution" in sent[-1]["text"]

    saved = app.task_store.get(task_id)
    assert saved is not None
    assert saved.instructions == "Include primary sources"
    assert saved.status.value == "cancelled"
    assert saved.execution_ids == ()
    assert len(backend.submitted_requests) == 0
    assert [event.event_type for event in app.task_store.events(task_id)] == [
        "created", "instruction_added", "paused", "queued", "cancelled"
    ]
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_task_list_distinguishes_terminal_unknown_and_unavailable_states(tmp_path: Path) -> None:
    store = SqliteExecutionStore(tmp_path / "executions.sqlite3")
    backend = FakeWorkerBackend(command_plans={
        "fail": FakeCommandPlan(exit_code=3),
        "sleep": FakeCommandPlan(stays_running=True),
    })
    worker = WorkerInterface(backend, execution_store=store)
    await worker.execute(ExecuteRequest("fail", execution_id="status-failed"))
    await worker.execute(ExecuteRequest("sleep", execution_id="status-cancelled"))
    await worker.terminate("status-cancelled")
    store.create_intent(ExecuteRequest("uncertain", execution_id="status-unknown"))
    store.record_error(
        "status-unknown",
        WorkerOperationError(WorkerErrorCode.EXECUTION_UNKNOWN, "dispatch uncertain"),
        execute_dispatch=True,
    )
    store.create_intent(ExecuteRequest("unavailable", execution_id="status-unavailable"))
    store.record_error(
        "status-unavailable",
        WorkerOperationError(WorkerErrorCode.WORKER_UNAVAILABLE, "worker unavailable"),
        execute_dispatch=True,
    )
    app, _, client = _app(tmp_path, execution_store=store)
    listing = app._executions_reply()
    assert "failed" in listing
    assert "cancelled" in listing
    assert "unknown" in listing
    assert "unavailable" in listing
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_cancel_persists_request_and_reports_success_terminal_and_unavailable(tmp_path: Path) -> None:
    execution_store = SqliteExecutionStore(tmp_path / "executions.sqlite3")
    backend = FakeWorkerBackend(command_plans={
        "sleep": FakeCommandPlan(stays_running=True),
        "done": FakeCommandPlan(),
    })
    worker = WorkerInterface(backend, execution_store=execution_store)
    await worker.execute(ExecuteRequest("sleep", execution_id="cancel-running"))
    await worker.execute(ExecuteRequest("done", execution_id="already-done"))
    app, sent, client = _app(
        tmp_path,
        execution_store=execution_store,
        worker_provider=lambda: nullcontext(worker),
    )
    await app._handle(_update(20, text="/cancel cancel-running"))
    assert "Cancellation succeeded" in sent[-1]["text"]
    assert execution_store.get("cancel-running").cancel_requested_at is not None
    await app._handle(_update(21, text="/cancel already-done"))
    assert "Already terminal: succeeded" in sent[-1]["text"]
    assert execution_store.get("already-done").cancel_requested_at is None
    await app.close()
    await client.aclose()

    unavailable_backend = FakeWorkerBackend(
        command_plans={"sleep": FakeCommandPlan(stays_running=True)},
        available=True,
    )
    unavailable_worker = WorkerInterface(unavailable_backend, execution_store=execution_store)
    await unavailable_worker.execute(ExecuteRequest("sleep", execution_id="cancel-offline"))
    unavailable_backend.available = False
    offline_app, offline_sent, offline_client = _app(
        tmp_path / "offline",
        execution_store=execution_store,
        worker_provider=lambda: nullcontext(unavailable_worker),
    )
    await offline_app._handle(_update(22, text="/cancel cancel-offline"))
    assert "Worker unavailable" in offline_sent[-1]["text"]
    assert execution_store.get("cancel-offline").cancel_requested_at is not None
    assert execution_store.get("cancel-offline").error_code == "worker_unavailable"
    await offline_app.close()
    await offline_client.aclose()


@pytest.mark.asyncio
async def test_task_commands_are_owner_private_and_existing_status_commands_remain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    execution_store = SqliteExecutionStore(tmp_path / "executions.sqlite3")
    backend = FakeWorkerBackend(command_plans={"sleep": FakeCommandPlan(stays_running=True)})
    worker = WorkerInterface(backend, execution_store=execution_store)
    await worker.execute(ExecuteRequest("sleep", execution_id="private-task"))
    monkeypatch.setattr(controller, "_worker_state", lambda: "stopped")
    app, sent, client = _app(
        tmp_path,
        execution_store=execution_store,
        worker_provider=lambda: nullcontext(worker),
    )
    await app._handle(_update(30, user_id=7, text="/task private-task"))
    await app._handle(_update(31, chat_type="group", text="/cancel private-task"))
    assert sent == []
    assert execution_store.get("private-task").cancel_requested_at is None

    await app._handle(_update(32, text="/new owner task for access-control test"))
    task_id = sent[-1]["text"].split()[1]
    assert task_id.startswith("task-")
    await app._handle(_update(34, text="/new forbidden", user_id=7))
    await app._handle(_update(35, text="ordinary unauthorized request", user_id=7))
    await app._handle(_update(36, text=f"/update {task_id} unauthorized change", user_id=7))
    await app._handle(_update(37, text=f"/pause {task_id}", chat_type="group"))
    task = app.task_store.get(task_id)
    assert task is not None and task.status.value == "queued" and task.instructions == ""
    assert len(app.task_store.list_latest()) == 1

    for update_id, command in enumerate(("/ping", "/worker", "/status", "/help"), start=38):
        await app._handle(_update(update_id, text=command))
    assert sent[1]["text"] == "Toothless is online."
    assert "stopped" in sent[2]["text"]
    assert "stopped" in sent[3]["text"]
    assert "/tasks" in sent[4]["text"] and "/executions" in sent[4]["text"]
    assert len(backend.submitted_requests) == 1
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_run_requires_explicit_owner_approval_and_delivers_saved_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from nanobot.personal_agent.opencode_task import TaskRunResult

    app, sent, client = _app(tmp_path)
    dispatched: list[str] = []

    class FakeTaskRunner:
        @staticmethod
        def new_execution_id() -> str:
            return "opencode-live-test"

        def __init__(self, _provider) -> None:
            pass

        async def execute(self, task, execution_id: str) -> TaskRunResult:
            dispatched.append(task.task_id)
            app.task_store.mark_worker_accepted(execution_id)
            return TaskRunResult(
                execution_id,
                TaskStatus.SUCCEEDED,
                result="• A CPU handles general-purpose processing.\n• A GPU processes many operations in parallel.\n• GPUs excel at graphics and AI workloads.",
            )

    monkeypatch.setattr(controller, "OpenCodeTaskRunner", FakeTaskRunner)
    await app._handle(_update(70, text="/new Explain the difference between CPU and GPU"))
    task_id = sent[-1]["text"].split()[1]
    assert app.task_store.get(task_id).status is TaskStatus.QUEUED
    assert dispatched == []

    await app._handle(_update(71, text=f"/run {task_id}"))
    assert "dispatching" in sent[-1]["text"]
    assert app.task_store.get(task_id).status is TaskStatus.DISPATCHING
    assert app._active_task_run is not None
    await app._active_task_run
    task = app.task_store.get(task_id)
    assert task is not None and task.status is TaskStatus.SUCCEEDED
    assert task.result is not None and "CPU handles" in task.result
    assert sent[-1]["text"].startswith(f"Task {task_id} completed.")

    await app._handle(_update(72, text=f"/run {task_id}"))
    assert "succeeded" in sent[-1]["text"]
    assert dispatched == [task_id]
    await app._handle(_update(73, text=f"/task {task_id}"))
    assert "Status: succeeded" in sent[-1]["text"]
    assert "CPU handles" in sent[-1]["text"]
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_active_task_cancel_uses_existing_worker_execution_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nanobot.personal_agent.opencode_task import TaskRunResult

    store = SqliteExecutionStore(tmp_path / "executions.sqlite3")
    backend = FakeWorkerBackend(command_plans={"sleep": FakeCommandPlan(stays_running=True)})
    worker = WorkerInterface(backend, execution_store=store)
    app, sent, client = _app(
        tmp_path, execution_store=store, worker_provider=lambda: nullcontext(worker)
    )
    task, _ = app.task_store.create_from_update(90, "A harmless cancellable task")
    task, claimed = app.task_store.claim_for_dispatch(task.task_id, "active-opencode-run")
    assert claimed and task is not None
    await worker.execute(ExecuteRequest("sleep", execution_id="active-opencode-run"))
    app.task_store.mark_worker_accepted("active-opencode-run")

    await app._handle(_update(91, text=f"/cancel {task.task_id}"))
    saved = app.task_store.get(task.task_id)
    assert saved is not None and saved.status is TaskStatus.CANCELLED
    assert store.get("active-opencode-run").cancel_requested_at is not None
    assert "cancelled by the worker" in sent[-1]["text"]
    sent_after_cancel = len(sent)

    class CancelledTaskRunner:
        @staticmethod
        def new_execution_id() -> str:
            return "unused-cancelled-run"

        def __init__(self, _provider) -> None:
            pass

        async def execute(self, _task, execution_id: str) -> TaskRunResult:
            return TaskRunResult(execution_id, TaskStatus.CANCELLED)

    monkeypatch.setattr(controller, "OpenCodeTaskRunner", CancelledTaskRunner)
    await app._execute_task(task, "active-opencode-run", 42)
    assert len(sent) == sent_after_cancel
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_controller_restart_reconciles_completed_task_without_resubmission(tmp_path: Path) -> None:
    store = SqliteExecutionStore(tmp_path / "executions.sqlite3")
    backend = FakeWorkerBackend(command_plans={
        "completed model response": FakeCommandPlan(
            stdout=json.dumps({"model": "opencode-go/glm-5.3", "status": "succeeded", "text": "Recovered answer."}).encode()
        )
    })
    worker = WorkerInterface(backend, execution_store=store)
    first, _, first_client = _app(tmp_path, execution_store=store)
    task, _ = first.task_store.create_from_update(92, "Recover this task")
    task, claimed = first.task_store.claim_for_dispatch(task.task_id, "recovery-run")
    assert claimed and task is not None
    await worker.execute(ExecuteRequest("completed model response", execution_id="recovery-run"))
    first.task_store.mark_worker_accepted("recovery-run")
    await first.close()
    await first_client.aclose()

    restarted, sent, client = _app(
        tmp_path,
        execution_store=SqliteExecutionStore(tmp_path / "executions.sqlite3"),
        worker_provider=lambda: nullcontext(worker),
    )
    await restarted._recover_active_tasks()
    assert restarted._active_task_run is not None
    await restarted._active_task_run
    recovered = restarted.task_store.get(task.task_id)
    assert recovered is not None and recovered.status is TaskStatus.SUCCEEDED
    assert recovered.result == "Recovered answer."
    assert "completed after controller recovery" in sent[-1]["text"]
    assert len(backend.submitted_requests) == 1
    await restarted.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_telegram_delivery_failure_keeps_completed_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from nanobot.personal_agent.opencode_task import TaskRunResult

    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if request.url.path.endswith("/sendMessage"):
            sent.append(payload)
            if "completed." in payload["text"]:
                return httpx.Response(500, json={"ok": False})
        return httpx.Response(200, json={"ok": True, "result": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = controller.TelegramController(
        controller.TelegramConfig("test-token", 42, tmp_path / "updates.sqlite3"),
        client=client,
        execution_store=SqliteExecutionStore(tmp_path / "executions.sqlite3"),
    )

    class FakeTaskRunner:
        @staticmethod
        def new_execution_id() -> str:
            return "delivery-run"

        def __init__(self, _provider) -> None:
            pass

        async def execute(self, task, execution_id: str) -> TaskRunResult:
            app.task_store.mark_worker_accepted(execution_id)
            return TaskRunResult(execution_id, TaskStatus.SUCCEEDED, result="Saved answer.")

    monkeypatch.setattr(controller, "OpenCodeTaskRunner", FakeTaskRunner)
    await app._handle(_update(93, text="/new Deliver this after completion"))
    task_id = sent[-1]["text"].split()[1]
    await app._handle(_update(94, text=f"/run {task_id}"))
    assert app._active_task_run is not None
    await app._active_task_run
    task = app.task_store.get(task_id)
    assert task is not None and task.status is TaskStatus.SUCCEEDED
    assert task.result == "Saved answer."
    assert task.notification_error is not None
    assert "failed" in task.notification_error
    await app.close()
    await client.aclose()


def test_protected_env_file_and_numeric_owner_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / "telegram.env"
    env_file.write_text("TOOTHLESS_TELEGRAM_BOT_TOKEN=secret\nTOOTHLESS_TELEGRAM_OWNER_ID=42\n")
    env_file.chmod(0o600)
    monkeypatch.setenv("TOOTHLESS_TELEGRAM_ENV_FILE", str(env_file))
    monkeypatch.setenv("TOOTHLESS_TELEGRAM_DB", str(tmp_path / "updates.sqlite3"))
    monkeypatch.delenv("TOOTHLESS_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TOOTHLESS_TELEGRAM_OWNER_ID", raising=False)
    config = controller.TelegramConfig.from_environment()
    assert config.owner_id == 42
    assert config.token == "secret"
    env_file.chmod(0o644)
    with pytest.raises(RuntimeError, match="permissions 0600"):
        controller.TelegramConfig.from_environment()


def test_update_store_refuses_shared_parent_without_changing_permissions(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    with pytest.raises(RuntimeError, match="owner-only"):
        controller.UpdateStore(shared / "updates.sqlite3")
    assert shared.stat().st_mode & 0o777 == 0o755


def test_update_store_rejects_database_symlinks(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite3"
    target.touch()
    link = tmp_path / "link.sqlite3"
    link.symlink_to(target)
    with pytest.raises(RuntimeError, match="must not contain symlinks"):
        controller.UpdateStore(link)


def test_http_client_logs_that_contain_bot_api_urls_are_suppressed() -> None:
    controller._configure_logging()
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
