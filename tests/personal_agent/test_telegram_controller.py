from __future__ import annotations

import asyncio
import json
import logging
import socket
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Awaitable, Callable

import httpx
import pytest

from nanobot.personal_agent import telegram_controller as controller
from nanobot.personal_agent.task_store import TaskEvent, TaskStatus
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


def _task_id(reply: str) -> str:
    import re

    match = re.search(r"task-[0-9a-f]{32}", reply)
    assert match is not None
    return match.group(0)


def test_schedule_tool_evidence_uses_only_bounded_sanitized_tool_names() -> None:
    from datetime import UTC, datetime

    event = TaskEvent(
        "execution_diagnostics",
        json.dumps({"tools": ["navigate", "extract_text", "close_session", "bad tool", "x" * 81], "prompt": "private input"}),
        datetime.now(UTC),
    )
    assert controller._recorded_task_tools((event,)) == ("navigate", "extract_text", "close_session")


def test_task_diagnostic_formats_only_sanitized_oversized_event_metadata() -> None:
    from datetime import UTC, datetime

    event = TaskEvent(
        "execution_diagnostics",
        json.dumps({
            "failure_category": "event_size_limit",
            "oversized_event": {
                "sequence": 8,
                "observed_bytes": 75_123,
                "limit_bytes": 65_536,
                "event_type": "tool_use",
                "tool_name": "toothless_browser_extract_text",
                "failure_category": "event_size_limit",
                "execution_id": "opencode-" + "d" * 32,
                "raw": "PRIVATE PAGE CONTENT",
            },
        }),
        datetime.now(UTC),
    )

    summary = controller._recorded_oversized_event((event,))

    assert summary == (
        "OpenCode event-size diagnostic: event 8 "
        "(tool_use/toothless_browser_extract_text) was 75,123 bytes; "
        "limit 65,536 bytes; execution opencode-" + "d" * 32 + "."
    )
    assert "PRIVATE PAGE CONTENT" not in summary


def _document_update(update_id: int, *, user_id: int = 42, chat_type: str = "private", content: bytes = b"value\n1\n") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "from": {"id": user_id},
            "chat": {"id": user_id, "type": chat_type},
            "caption": "Calculate statistics from this CSV.",
            "document": {
                "file_id": f"file-{update_id}", "file_name": "numbers.csv",
                "mime_type": "text/csv", "file_size": len(content),
            },
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
    assert "/desktop" in controller.command_reply("/help", worker_state="ignored")


@pytest.mark.asyncio
async def test_remote_command_issues_owner_chat_memory_code_after_managed_service_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import re

    app, sent, client = _app(tmp_path)
    monkeypatch.setattr(controller, "REMOTE_ORIGIN", "https://toothless.test")
    async def ready() -> bool:
        return True

    monkeypatch.setattr(app, "_ensure_memory_explorer", ready)
    await app._handle(_update(41, text="/remote"))

    assert len(sent) == 1
    response = sent[0]["text"]
    assert "/memory/" in response and "https://toothless.test/memory/" in response
    code_match = re.search(r"<code>([A-Za-z0-9_-]+)</code>", response)
    assert code_match is not None
    code = code_match.group(1)
    assert app.remote_sessions.consume("memory", code)
    assert not app.remote_sessions.consume("memory", code)
    assert sent[0]["reply_markup"]["inline_keyboard"]
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_web_command_issues_separate_single_use_console_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import re

    app, sent, client = _app(tmp_path)

    async def ready() -> bool:
        return True

    monkeypatch.setattr(app, "_ensure_memory_explorer", ready)
    await app._handle(_update(42, text="/web"))

    assert len(sent) == 1
    assert "/memory/app/" in sent[0]["text"]
    code_match = re.search(r"<code>([A-Za-z0-9_-]{24,})</code>", sent[0]["text"])
    assert code_match is not None
    code = code_match.group(1)
    assert app.remote_sessions.consume("web_console", code)
    assert not app.remote_sessions.consume("memory", code)
    assert not app.remote_sessions.consume("web_console", code)
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_desktop_card_screenshot_callback_uses_worker_only_and_is_owner_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64

    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/n5sAAAAASUVORK5CYII="
    )
    photo_requests: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sendPhoto"):
            photo_requests.append(request.content)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 500}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 501}})

    monkeypatch.setattr(controller, "worker_readiness", lambda: {"worker_ready": True})
    backend = FakeWorkerBackend(desktop_screenshot=image)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = controller.TelegramConfig("test-token", 42, tmp_path / "updates.sqlite3")
    app = controller.TelegramController(
        config, client=client, execution_store=SqliteExecutionStore(tmp_path / "executions.sqlite3"),
        worker_provider=lambda: nullcontext(WorkerInterface(backend)),
    )
    payload = app._new_callback("desktop_screenshot", "desktop", 42)
    app.store.bind_callbacks((payload,), 77)
    callback = {
        "id": "desktop-screenshot", "from": {"id": 42}, "data": payload,
        "message": {"message_id": 77, "chat": {"id": 42, "type": "private"}},
    }
    await app._handle({"update_id": 700, "callback_query": callback})
    await app._handle({"update_id": 700, "callback_query": {**callback, "id": "replay"}})

    assert len(photo_requests) == 1
    assert image in photo_requests[0]
    assert backend.submitted_requests == []
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_desktop_screenshot_rejects_unready_worker_before_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64

    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/n5sAAAAASUVORK5CYII="
    )
    backend = FakeWorkerBackend(desktop_screenshot=image)
    app, _sent, client = _app(tmp_path, worker_provider=lambda: nullcontext(WorkerInterface(backend)))
    monkeypatch.setattr(controller, "worker_readiness", lambda: {"worker_ready": False})
    with pytest.raises(WorkerOperationError):
        await app._capture_desktop_screenshot()
    assert backend.submitted_requests == []
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_desktop_open_fails_closed_without_protected_worker_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(controller, "worker_readiness", lambda: {"worker_ready": False})

    def unexpected_provider() -> AbstractContextManager[WorkerInterface]:
        raise AssertionError("worker provider must not be reached")

    app, _sent, client = _app(
        tmp_path,
        worker_provider=unexpected_provider,
    )
    payload = app._new_callback("desktop_open", "desktop", 42)
    app.store.bind_callbacks((payload,), 88)
    callback = {
        "id": "desktop-open", "from": {"id": 42}, "data": payload,
        "message": {"message_id": 88, "chat": {"id": 42, "type": "private"}},
    }

    await app._handle({"update_id": 701, "callback_query": callback})

    assert not app.desktop_viewer.active
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_desktop_callback_payload_replay_with_new_update_is_consumed_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64

    from nanobot.personal_agent import desktop_viewer

    port = desktop_viewer.VIEWER_PORT
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])
    monkeypatch.setattr(desktop_viewer, "VIEWER_PORT", port)
    monkeypatch.setattr(controller, "worker_readiness", lambda: {"worker_ready": True})
    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/n5sAAAAASUVORK5CYII="
    )
    backend = FakeWorkerBackend(desktop_screenshot=image)
    provider_calls = 0

    def provider() -> AbstractContextManager[WorkerInterface]:
        nonlocal provider_calls
        provider_calls += 1
        return nullcontext(WorkerInterface(backend))

    app, _sent, client = _app(tmp_path, worker_provider=provider)
    app.desktop_viewer = desktop_viewer.DesktopViewer(
        app._capture_desktop_screenshot, token_file=tmp_path / "viewer-token",
    )
    payload = app._new_callback("desktop_open", "desktop", 42)
    app.store.bind_callbacks((payload,), 88)
    callback = {
        "id": "desktop-open-first", "from": {"id": 42}, "data": payload,
        "message": {"message_id": 88, "chat": {"id": 42, "type": "private"}},
    }

    await app._handle({"update_id": 701, "callback_query": callback})
    assert app.desktop_viewer.active
    token_before_replay = app.desktop_viewer._token_hash
    await app._handle({
        "update_id": 702,
        "callback_query": {**callback, "id": "desktop-open-replay"},
    })

    row = app.store._db.execute(
        "SELECT consumed_at FROM telegram_callbacks WHERE callback_id=?",
        (payload.removeprefix("c:"),),
    ).fetchone()
    assert row is not None and row["consumed_at"] is not None
    assert provider_calls == 1
    assert app.desktop_viewer.active and app.desktop_viewer._token_hash == token_before_replay
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_desktop_open_from_screenshot_edits_photo_caption_and_keeps_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(controller, "worker_readiness", lambda: {"worker_ready": False})
    app, _sent, client = _app(tmp_path)
    payload = app._new_callback("desktop_open", "desktop", 42)
    app.store.bind_callbacks((payload,), 88)
    api_calls: list[tuple[str, dict]] = []

    async def record_api(method: str, data: dict) -> dict[str, bool]:
        api_calls.append((method, data))
        return {"ok": True}

    app._api = record_api  # type: ignore[method-assign]
    callback = {
        "id": "desktop-open-photo", "from": {"id": 42}, "data": payload,
        "message": {
            "message_id": 88, "chat": {"id": 42, "type": "private"},
            "photo": [{"file_id": "opaque"}], "caption": "Ubuntu desktop · view only",
        },
    }

    await app._handle({"update_id": 702, "callback_query": callback})

    assert api_calls[0][0] == "editMessageCaption"
    assert "Protected worker readiness is unavailable" in api_calls[0][1]["caption"]
    assert len(api_calls[0][1]["reply_markup"]["inline_keyboard"]) == 5
    assert api_calls[1][0] == "answerCallbackQuery"
    assert not app.desktop_viewer.active
    await app.close()
    await client.aclose()


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
async def test_project_memory_natural_save_retrieval_correction_and_forget_survive_restart(tmp_path: Path) -> None:
    import re

    app, sent, client = _app(tmp_path)
    await app._handle(_update(910, text="Remember that project Mechanochemistry uses FEM and MD."))
    first_memory = re.search(r"memory-[0-9a-f]{24}", sent[-1]["text"])
    assert first_memory is not None and "Saved fact" in sent[-1]["text"]
    await app._handle(_update(911, text="What do you remember about project Mechanochemistry?"))
    assert first_memory.group(0) in sent[-1]["text"]
    assert "Uses FEM and MD" in sent[-1]["text"]
    await app.close()
    await client.aclose()

    reopened, sent, client = _app(tmp_path)
    message_id = 1000
    async def memory_api(method: str, payload: dict) -> dict:
        nonlocal message_id
        if method == "sendMessage":
            sent.append(payload)
            message_id += 1
            return {"message_id": message_id}
        return {}
    reopened._api = memory_api
    await reopened._handle(_update(912, text="What do you remember about project Mechanochemistry?"))
    assert "Uses FEM and MD" in sent[-1]["text"]
    await reopened._handle(_update(913, text="Remember that I prefer concise answers."))
    old_preference = re.search(r"memory-[0-9a-f]{24}", sent[-1]["text"])
    assert old_preference is not None
    await reopened._handle(_update(914, text=f"/memory-update {old_preference.group(0)} | Prefers short bullet summaries"))
    new_preference = re.search(r"memory-[0-9a-f]{24}", sent[-1]["text"])
    assert new_preference is not None and new_preference.group(0) != old_preference.group(0)
    await reopened._handle(_update(915, text="/memory short bullet summaries"))
    assert new_preference.group(0) in sent[-1]["text"]
    # Use the persisted memory-to-Telegram message mapping from the controller's database.
    with reopened.memory_store._connect() as connection:
        memory_result_message_id = int(connection.execute(
            "SELECT MAX(message_id) FROM memory_message_context WHERE memory_id=?", (new_preference.group(0),)
        ).fetchone()[0])
    forget = _update(916, text="Forget that preference.")
    forget["message"]["reply_to_message"] = {"message_id": memory_result_message_id}
    await reopened._handle(forget)
    assert "Forgot memory" in sent[-1]["text"]
    await reopened._handle(_update(917, text="/memory short bullet summaries"))
    assert "don't have a matching saved memory" in sent[-1]["text"].lower()
    await reopened.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_save_task_result_records_task_and_file_references_without_copying_model_text(tmp_path: Path) -> None:
    app, sent, client = _app(tmp_path)
    task, _ = app.task_store.create_from_update(920, "Analyze the uploaded CSV")
    app.task_store.add_attachment(
        task.task_id, telegram_file_id="file-safe", filename="values.csv", mime_type="text/csv",
        size_bytes=12, storage_key="opaque-values", caption="Analyze CSV",
    )
    app.task_store.claim_for_dispatch(task.task_id, "memory-save-result")
    app.task_store.mark_worker_accepted("memory-save-result")
    app.task_store.finish_execution(
        "memory-save-result", TaskStatus.SUCCEEDED,
        result="Internal attempt text; final answer: count 3, mean 4.",
    )
    app.task_store.record_artifact(
        task.task_id, guest_path=f"opencode-task-workspace/exports/{task.task_id}/chart.png",
        filename="chart.png", mime_type="image/png", size_bytes=100, storage_key="opaque-chart",
    )
    app.task_store.bind_message_context(42, 921, task.task_id)
    save = _update(922, text="Save this result under my mechanochemistry project.")
    save["message"]["reply_to_message"] = {"message_id": 921}
    await app._handle(save)
    memory_match = __import__("re").search(r"memory-[0-9a-f]{24}", sent[-1]["text"])
    assert memory_match is not None
    record = app.memory_store.get(memory_match.group(0))
    assert record is not None and record.task_id == task.task_id
    assert "Internal attempt text" not in record.content
    await app._handle(_update(923, text="Find my last CSV analysis."))
    assert task.task_id in sent[-1]["text"]
    assert "values.csv" in sent[-1]["text"]
    assert "chart.png" in sent[-1]["text"]
    assert "TaskStore" in sent[-1]["text"]
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_schedule_proposal_is_owner_only_deduplicated_and_inactive_until_approval(tmp_path: Path) -> None:
    from dataclasses import replace

    app, sent, client = _app(tmp_path)
    app.config = replace(app.config, time_zone="America/Los_Angeles")
    message = "Remind me in 2 minutes to review my results."
    await app._handle(_update(9200, text=message))
    await app._handle(_update(9200, text=message))
    assert len(sent) == 1
    assert "pending, inactive" in sent[0]["text"]
    assert "Nothing is scheduled before approval" in sent[0]["text"]
    schedules = app.schedule_store.list_for_owner(42, 42)
    assert len(schedules) == 1 and schedules[0].state == "pending"
    assert app.schedule_store.claim_due() == ()

    await app._handle(_update(9202, text="Every Monday at 8 AM remind me to review results."))
    assert "I can schedule a one-time reminder or daily public research" in sent[-1]["text"]
    assert len(app.task_store.list_latest()) == 0

    await app._handle(_update(9203, text="Create a reminder for me in 2 minutes to play games"))
    assert "pending, inactive" in sent[-1]["text"]
    assert len(app.schedule_store.list_for_owner(42, 42)) == 2
    assert len(app.task_store.list_latest()) == 0

    await app._handle(_update(9201, user_id=99, chat_id=99, text=message))
    assert len(app.schedule_store.list_for_owner(99, 99)) == 0
    assert len(sent) == 3
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_approved_reminder_delivers_once_without_worker_execution(tmp_path: Path) -> None:
    import re
    from dataclasses import replace
    from datetime import UTC, datetime

    app, sent, client = _app(tmp_path)
    app.config = replace(app.config, time_zone="America/Los_Angeles")
    await app._handle(_update(9300, text="Remind me in 2 minutes to review my results."))
    schedule_id = re.search(r"schedule-[0-9a-f]{24}", sent[-1]["text"]).group(0)
    await app._handle(_update(9301, text=f"/schedule approve {schedule_id}"))
    schedule = app.schedule_store.get(schedule_id, owner_id=42, chat_id=42)
    assert schedule is not None and schedule.state == "active" and schedule.approved_hash
    due = datetime.fromisoformat(schedule.next_run_at)
    occurrence = app.schedule_store.claim_due(now=due.replace(tzinfo=UTC))
    assert len(occurrence) == 1 and occurrence[0].status == "claimed"
    await app._dispatch_schedule_occurrence(occurrence[0])
    saved = app.schedule_store.get_occurrence(occurrence[0].occurrence_id)
    assert saved is not None and saved.status == "succeeded" and saved.notification_status == "sent"
    assert any("Reminder: review my results" in item["text"] for item in sent)
    assert app.task_store.active_tasks() == ()
    assert app.schedule_store.claim_due(now=due.replace(tzinfo=UTC)) == ()
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_scheduled_research_auth_failure_is_recorded_once_and_pauses_recurrence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta

    from nanobot.personal_agent.opencode_task import TaskRunResult

    app, sent, client = _app(tmp_path)

    class AuthFailureRunner:
        @staticmethod
        def new_execution_id() -> str:
            return "opencode-schedule-auth-fail"

        def __init__(self, _provider, **_kwargs) -> None:
            pass

        async def execute(self, task, execution_id: str) -> TaskRunResult:
            assert "browser" in task.request.lower()
            return TaskRunResult(
                execution_id, TaskStatus.FAILED,
                error="auth_required: guest OpenCode Go authentication is missing",
            )

    monkeypatch.setattr(controller, "OpenCodeTaskRunner", AuthFailureRunner)
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    start = (now + timedelta(minutes=2)).replace(tzinfo=None)
    schedule = app.schedule_store.create_pending(
        source_update_id=9400, owner_id=42, chat_id=42,
        description="Daily public research", action="Research public Python updates using the browser",
        timezone="UTC", local_start=start.isoformat(), recurrence="daily", profile="browser",
    )
    approved = app.schedule_store.approve(
        schedule.schedule_id, owner_id=42, chat_id=42, now=now
    )
    assert approved is not None and approved.next_run_at is not None
    due = datetime.fromisoformat(approved.next_run_at)
    occurrence = app.schedule_store.claim_due(now=due)
    assert len(occurrence) == 1
    await app._dispatch_schedule_occurrence(occurrence[0])
    assert app._active_task_run is not None
    await app._active_task_run
    saved = app.schedule_store.get_occurrence(occurrence[0].occurrence_id)
    current = app.schedule_store.get(schedule.schedule_id, owner_id=42, chat_id=42)
    assert saved is not None and saved.status == "failed"
    assert saved.execution_id == "opencode-schedule-auth-fail"
    assert "auth_required" in saved.error
    assert current is not None and current.state == "paused"
    assert len(app.task_store.list_latest()) == 1
    assert any("Reauthenticate inside Ubuntu" in item["text"] for item in sent)
    assert app.schedule_store.claim_due(now=due + timedelta(days=1)) == ()
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_memory_context_for_task_is_bounded_linked_and_does_not_start_execution(tmp_path: Path) -> None:
    app, sent, client = _app(tmp_path)
    await app._handle(_update(930, text="Remember that project Aster uses finite elements."))
    memory_match = __import__("re").search(r"memory-[0-9a-f]{24}", sent[-1]["text"])
    assert memory_match is not None
    await app._handle(_update(931, text="Use my Aster project context for this new task: Explain the simulation setup."))
    task_match = __import__("re").search(r"task-[0-9a-f]{32}", sent[-1]["text"])
    assert task_match is not None and "saved project context is attached" in sent[-1]["text"].lower()
    task = app.task_store.get(task_match.group(0))
    assert task is not None and task.status is TaskStatus.QUEUED
    assert "Uses finite elements" in task.memory_context
    assert task.execution_ids == ()
    with app.memory_store._connect() as connection:
        assert connection.execute("select count(*) from memory_task_context where task_id=?", (task.task_id,)).fetchone()[0] == 1
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_knowledge_graph_queries_project_files_task_sources_and_latest_outputs(tmp_path: Path) -> None:
    app, sent, client = _app(tmp_path)
    task, _ = app.task_store.create_from_update(960, "Analyze project CSV")
    app.task_store.add_attachment(
        task.task_id, telegram_file_id="graph-file", filename="number.csv", mime_type="text/csv",
        size_bytes=17, storage_key="opaque-graph-file", caption="",
    )
    app.task_store.record_artifact(
        task.task_id, guest_path=f"opencode-task-workspace/exports/{task.task_id}/summary.csv",
        filename="summary.csv", mime_type="text/csv", size_bytes=32, storage_key="opaque-graph-output",
    )
    app._sync_task_graph(task.task_id)
    app.memory_store.remember_task(
        961, "Mechanochemistry", task_id=task.task_id, summary="Saved TaskStore reference.",
        references=(("file", f"inputs/{task.task_id}/number.csv", "number.csv"),
                    ("artifact", f"opencode-task-workspace/exports/{task.task_id}/summary.csv", "summary.csv")),
    )

    await app._handle(_update(962, text="Which files belong to my Mechanochemistry project?"))
    assert "number.csv" in sent[-1]["text"] and "summary.csv" in sent[-1]["text"]
    assert task.task_id in sent[-1]["text"]
    await app._handle(_update(963, text="What tasks used number.csv?"))
    assert task.task_id in sent[-1]["text"] and "source records" in sent[-1]["text"].lower()
    await app._handle(_update(964, text="Find the results produced by my last CSV analysis."))
    assert task.task_id in sent[-1]["text"] and "summary.csv" in sent[-1]["text"]
    assert len(app.task_store.list_latest(10)) == 1

    await app._handle(_update(965, user_id=7, text="What tasks used number.csv?"))
    assert len(sent) == 3
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_graph_reports_supersession_without_exposing_old_fact_content(tmp_path: Path) -> None:
    app, sent, client = _app(tmp_path)
    old_id, _, _ = app.memory_store.remember(970, "Uses solver FEM-A", project_name="Aster")
    new_id, _, _ = app.memory_store.correct(971, old_id or "", "Uses solver FEM-B")
    await app._handle(_update(972, text="Which memory replaced the earlier project fact?"))
    assert new_id in sent[-1]["text"] and old_id in sent[-1]["text"]
    assert "FEM-A" not in sent[-1]["text"]
    assert len(app.task_store.list_latest(10)) == 0
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_memory_write_requires_owner_and_file_content_never_creates_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, sent, client = _app(tmp_path)
    await app._handle(_update(940, user_id=7, text="Remember that project Secret uses FEM."))
    await app._handle(_update(941, chat_type="group", text="/memory FEM"))
    assert sent == []
    with app.memory_store._connect() as connection:
        assert connection.execute("select count(*) from memories").fetchone()[0] == 0
    async def api(method: str, payload: dict) -> dict:
        if method == "getFile":
            return {"file_path": "safe/number.csv", "file_size": 6}
        return {"message_id": 1200}
    async def download(_path: str, _size: int) -> bytes:
        return b"Remember that project Injected uses MD"
    monkeypatch.setattr(app, "_api", api)
    monkeypatch.setattr(app, "_download_telegram_file", download)
    file_update = _document_update(942, content=b"x" * 6)
    file_update["message"]["caption"] = "Remember that project Caption uses FEM"
    await app._handle(file_update)
    with app.memory_store._connect() as connection:
        assert connection.execute("select count(*) from memories").fetchone()[0] == 0
    assert app.task_store.list_latest(1)[0].request.startswith("Remember that project Caption")
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_duplicate_memory_update_and_incomplete_correction_do_not_duplicate_or_guess(tmp_path: Path) -> None:
    app, sent, client = _app(tmp_path)
    message = _update(950, text="Remember that project X uses FEM.")
    await app._handle(message)
    reply = sent[-1]["text"]
    await app._handle(message)
    assert len(sent) == 1
    with app.memory_store._connect() as connection:
        assert connection.execute("select count(*) from memories").fetchone()[0] == 1
    await app._handle(_update(951, text="Update the material properties we previously used."))
    assert "corrected" in sent[-1]["text"].lower()
    with app.memory_store._connect() as connection:
        assert connection.execute("select count(*) from memories").fetchone()[0] == 1
    assert "memory-" in reply
    await app.close()
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
async def test_owner_upload_is_downloaded_once_and_persisted_for_a_queued_task(tmp_path: Path) -> None:
    content = b"value\n1\n2\n3\n"
    calls: list[str] = []
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/sendMessage"):
            sent.append(json.loads(request.content))
        if request.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "docs/input.csv", "file_size": len(content)}})
        if "/file/bot" in request.url.path:
            return httpx.Response(200, content=content)
        return httpx.Response(200, json={"ok": True, "result": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = controller.TelegramController(
        controller.TelegramConfig("test-token", 42, tmp_path / "updates.sqlite3"),
        client=client,
        execution_store=SqliteExecutionStore(tmp_path / "executions.sqlite3"),
    )
    await app._handle(_document_update(301, content=content))
    task = app.task_store.get(app.task_store.list_latest()[0].task_id)
    assert task is not None and task.status is TaskStatus.QUEUED
    assert task.request == "Calculate statistics from this CSV."
    assert len(task.attachments) == 1
    assert task.attachments[0].telegram_file_id == "file-301"
    assert task.attachments[0].filename == "numbers.csv"
    assert app._attachment_storage.read(task.attachments[0].storage_key) == content
    assert sum(path.endswith("/getFile") for path in calls) == 1
    task_id = task.task_id
    assert sent[0]["text"] == (
        f"<b>File received.</b> Task <code>{task_id}</code> is queued.\n"
        "Files: numbers.csv\n\nTo approve execution, send:\n"
        f"<code>/run {task_id}</code>"
    )
    await app.close()
    app = controller.TelegramController(
        controller.TelegramConfig("test-token", 42, tmp_path / "updates.sqlite3"),
        client=client,
        execution_store=SqliteExecutionStore(tmp_path / "executions.sqlite3"),
    )
    recovered = app.task_store.get(task_id)
    assert recovered is not None and recovered.attachments[0].storage_key == task.attachments[0].storage_key
    assert app._attachment_storage.read(recovered.attachments[0].storage_key) == content
    await app._handle(_document_update(301, content=content))
    assert sum(path.endswith("/getFile") for path in calls) == 1
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_declared_oversize_upload_is_rejected_before_get_file(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"ok": True, "result": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = controller.TelegramController(
        controller.TelegramConfig("test-token", 42, tmp_path / "updates.sqlite3"),
        client=client, execution_store=SqliteExecutionStore(tmp_path / "executions.sqlite3"),
    )
    update = _document_update(303)
    update["message"]["document"]["file_size"] = controller.MAX_UPLOAD_BYTES + 1
    await app._handle(update)
    assert not any(path.endswith("/getFile") for path in calls)
    assert app.task_store.list_latest() == ()
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_interrupted_telegram_file_download_does_not_create_a_task(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "docs/input.csv", "file_size": 10}})
        if "/file/bot" in request.url.path:
            raise httpx.ReadError("interrupted stream", request=request)
        return httpx.Response(200, json={"ok": True, "result": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = controller.TelegramController(
        controller.TelegramConfig("test-token", 42, tmp_path / "updates.sqlite3"),
        client=client, execution_store=SqliteExecutionStore(tmp_path / "executions.sqlite3"),
    )
    await app._handle(_document_update(305, content=b"value\n1\n"))
    assert app.task_store.list_latest() == ()
    assert sum(path.endswith("/getFile") for path in calls) == 1
    assert app.store.contains(305)
    await app._handle(_document_update(305, content=b"value\n1\n"))
    assert sum(path.endswith("/getFile") for path in calls) == 1
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_unauthorized_upload_is_rejected_before_file_download(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"ok": True, "result": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app, sent, _ = _app(tmp_path)
    await app._client.aclose()
    app._client = client
    app._owns_client = False
    await app._handle(_document_update(302, user_id=7))
    assert calls == []
    assert sent == []
    assert app.task_store.list_latest() == ()
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_file_task_cannot_be_dispatched_with_browser_profile(tmp_path: Path) -> None:
    app, _, client = _app(tmp_path)
    task, _ = app.task_store.create_from_update(304, "Use browser to read this attached file")
    key = app._attachment_storage.save(b"text\n")
    app.task_store.add_attachment(
        task.task_id, telegram_file_id="file", filename="notes.txt", mime_type="text/plain",
        size_bytes=5, storage_key=key, caption=task.request,
    )
    reply = app._start_task_reply(task.task_id, 42)
    assert "cannot use the isolated browser-only profile" in reply
    assert app.task_store.get(task.task_id).execution_ids == ()
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_generated_file_delivery_is_one_shot_and_persisted(tmp_path: Path) -> None:
    task_store = controller.TaskStore(tmp_path / "tasks.sqlite3")
    task, _ = task_store.create_from_update(401, "Create outputs")
    path = f"opencode-task-workspace/exports/{task.task_id}/summary.csv"
    backend = FakeWorkerBackend(files={path: b"name,value\nmean,2\n"})
    worker = WorkerInterface(backend)
    sent_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        sent_methods.append(method)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 812}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = controller.TelegramController(
        controller.TelegramConfig("test-token", 42, tmp_path / "updates.sqlite3"),
        client=client, task_store=task_store,
        execution_store=SqliteExecutionStore(tmp_path / "executions.sqlite3"),
        worker_provider=lambda: nullcontext(worker),
    )
    await app._send_task_files(task.task_id, 42)
    await app._send_task_files(task.task_id, 42)
    deliveries = task_store.list_file_deliveries(task.task_id)
    assert len(deliveries) == 1
    assert deliveries[0].status == "delivered"
    assert deliveries[0].telegram_message_id == 812
    assert sent_methods == ["sendDocument"]
    await app.close()
    recovered_store = controller.TaskStore(tmp_path / "tasks.sqlite3")
    recovered = recovered_store.list_file_deliveries(task.task_id)
    assert recovered[0].status == "delivered" and recovered[0].telegram_message_id == 812
    app = controller.TelegramController(
        controller.TelegramConfig("test-token", 42, tmp_path / "updates.sqlite3"),
        client=client, task_store=recovered_store,
        execution_store=SqliteExecutionStore(tmp_path / "executions.sqlite3"),
        worker_provider=lambda: nullcontext(worker),
    )
    await app._send_task_files(task.task_id, 42)
    assert sent_methods == ["sendDocument"]
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
    task_id = _task_id(first_sent[-1]["text"])
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
    monkeypatch.setattr(controller, "worker_readiness", lambda: {"worker_ready": False})
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
    task_id = _task_id(sent[-1]["text"])
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
    assert "Ubuntu worker: unavailable" in sent[3]["text"]
    assert "/tasks" in sent[4]["text"] and "/executions" in sent[4]["text"]
    assert len(backend.submitted_requests) == 1
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_status_card_uses_safe_readiness_and_owner_bound_controls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(controller, "worker_readiness", lambda: {
        "vm": "running", "softnet_policy": "verified", "dns_relay": "ready", "guest_dns": "ready",
        "opencode_auth": "unavailable", "execution_agent": "ready", "workspace_profile": "ready",
        "browser_profile": "ready", "worker_ready": False,
    })
    app, sent, client = _app(tmp_path)
    await app._handle(_update(404, text="/status"))
    text = sent[-1]["text"]
    assert "Controller: connected" in text
    assert "Ubuntu worker: unavailable" in text
    assert "Most recent failure:" in text and "Last successful worker operation:" in text
    assert "token" not in text.lower() and "credential" not in text.lower()
    buttons = sent[-1]["reply_markup"]["inline_keyboard"]
    assert len(buttons) == 4
    actions = [row[0]["text"] for row in buttons]
    assert actions == [
        "↻ Refresh", "Tasks", "Schedules", "Diagnostics",
    ]
    stored_actions = [row[0] for row in app.store._db.execute(
        "SELECT action FROM telegram_callbacks ORDER BY rowid"
    ).fetchall()]
    assert stored_actions == [
        "status_refresh", "status_tasks", "status_schedules", "status_diagnostics",
    ]
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_status_tasks_callback_edits_card_and_returns_safe_controls(tmp_path: Path) -> None:
    app, _sent, client = _app(tmp_path)
    payload = app.store.create_callback("status_tasks", "status", 42, 42)
    app.store.bind_callbacks((payload,), 77)
    calls: list[tuple[str, dict]] = []

    async def api(method: str, data: dict) -> dict:
        calls.append((method, data))
        return {"message_id": 77}

    app._api = api
    await app._handle_callback(405, {
        "id": "callback-405", "from": {"id": 42}, "data": payload,
        "message": {"message_id": 77, "chat": {"id": 42, "type": "private"}},
    })
    assert calls[0][0] == "editMessageText"
    assert "No tasks recorded" in calls[0][1]["text"]
    assert [row[0]["text"] for row in calls[0][1]["reply_markup"]["inline_keyboard"]] == [
        "↻ Refresh", "Tasks", "Schedules", "Diagnostics",
    ]
    assert calls[1][0] == "answerCallbackQuery"
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_unauthorized_status_and_desktop_callbacks_are_rejected_without_consumption(tmp_path: Path) -> None:
    app, _sent, client = _app(tmp_path)
    payloads = (
        app.store.create_callback("status_tasks", "status", 42, 42),
        app.store.create_callback("desktop_screenshot", "desktop", 42, 42),
    )
    app.store.bind_callbacks(payloads, 77)
    calls: list[tuple[str, dict]] = []

    async def api(method: str, data: dict) -> dict:
        calls.append((method, data))
        return {}

    app._api = api
    for update_id, payload in enumerate(payloads, start=406):
        await app._handle_callback(update_id, {
            "id": f"callback-{update_id}", "from": {"id": 99}, "data": payload,
            "message": {"message_id": 77, "chat": {"id": 42, "type": "private"}},
        })
    assert [method for method, _data in calls] == ["answerCallbackQuery", "answerCallbackQuery"]
    assert all(data["text"] == "This action is not authorized." for _method, data in calls)
    for payload in payloads:
        assert app.store._db.execute(
            "SELECT consumed_at FROM telegram_callbacks WHERE callback_id=?", (payload[2:],)
        ).fetchone()[0] is None
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_sigterm_cancels_polling_and_closes_controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import signal

    started = asyncio.Event()
    closed = asyncio.Event()

    class Config:
        @staticmethod
        def from_environment() -> object:
            return object()

    class Controller:
        def __init__(self, _config: object) -> None:
            pass

        async def run(self) -> None:
            started.set()
            await asyncio.Event().wait()

        async def close(self) -> None:
            closed.set()

    monkeypatch.setattr(controller, "TelegramConfig", Config)
    monkeypatch.setattr(controller, "TelegramController", Controller)
    task = asyncio.create_task(controller._run())
    await asyncio.wait_for(started.wait(), timeout=2)
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, timeout=2)
    assert closed.is_set()


def test_controller_polling_lock_is_exclusive_and_released(tmp_path: Path) -> None:
    from nanobot.personal_agent.controller_lock import (
        ControllerAlreadyRunningError,
        acquire_controller_lock,
    )

    database = tmp_path / "updates.sqlite3"
    first = acquire_controller_lock(database)
    try:
        with pytest.raises(ControllerAlreadyRunningError):
            acquire_controller_lock(database)
    finally:
        import os

        os.close(first)
    second = acquire_controller_lock(database)
    import os

    os.close(second)


@pytest.mark.asyncio
async def test_run_requires_explicit_owner_approval_and_delivers_saved_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from nanobot.personal_agent.opencode_task import TaskRunResult

    app, sent, client = _app(tmp_path)
    dispatched: list[str] = []

    class FakeTaskRunner:
        @staticmethod
        def new_execution_id() -> str:
            return "opencode-live-test"

        def __init__(self, _provider, **_kwargs) -> None:
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
    task_id = _task_id(sent[-1]["text"])
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
    assert f"<b>Task completed ·</b> <code>{task_id}</code>" in sent[-1]["text"]

    await app._handle(_update(72, text=f"/run {task_id}"))
    assert "succeeded" in sent[-1]["text"]
    assert dispatched == [task_id]
    await app._handle(_update(73, text=f"/task {task_id}"))
    assert "Status: succeeded" in sent[-1]["text"]
    assert "CPU handles" in sent[-1]["text"]
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_conversational_follow_up_is_reply_bound_and_yes_approves_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nanobot.personal_agent.opencode_task import TaskRunResult

    app, sent, client = _app(tmp_path)
    message_id = 700

    async def api(method: str, payload: dict) -> dict:
        nonlocal message_id
        if method == "sendMessage":
            sent.append(payload)
            message_id += 1
            return {"message_id": message_id}
        return {}

    monkeypatch.setattr(app, "_api", api)
    dispatched: list[str] = []

    class FakeTaskRunner:
        @staticmethod
        def new_execution_id() -> str:
            return "conversation-run"

        def __init__(self, _provider, **_kwargs) -> None:
            pass

        async def execute(self, task, execution_id: str) -> TaskRunResult:
            dispatched.append(task.task_id)
            app.task_store.mark_worker_accepted(execution_id)
            assert task.parent_task_id is not None
            assert task.parent_context and "untrusted" in task.parent_context
            assert task.request == "Using the previous data, calculate its mean again and give me a short explanation."
            assert task.instructions == ""
            assert "Parent original request (historical context only; do not execute):" in task.parent_context
            assert {item.filename for item in task.attachments} == {"numbers.csv", "previous-chart.png"}
            return TaskRunResult(execution_id, TaskStatus.SUCCEEDED, result="The mean is 6 because 30 divided by 5 is 6.")

    monkeypatch.setattr(controller, "OpenCodeTaskRunner", FakeTaskRunner)
    await app._handle(_update(201, text="/new Calculate statistics from the CSV and create a chart"))
    parent = app.task_store.list_latest(1)[0]
    original_message_id = 701
    app.task_store.bind_message_context(42, original_message_id, parent.task_id)
    csv_key = app._attachment_storage.save(b"value\n2\n4\n")
    image_key = app._attachment_storage.save(b"\x89PNG\r\n\x1a\n")
    app.task_store.add_attachment(
        parent.task_id, telegram_file_id="file-conversation", filename="numbers.csv",
        mime_type="text/csv", size_bytes=9, storage_key=csv_key, caption="CSV input",
    )
    app.task_store.record_artifact(
        parent.task_id, guest_path=f"opencode-task-workspace/exports/{parent.task_id}/chart.png",
        filename="chart.png", mime_type="image/png", size_bytes=8, storage_key=image_key,
    )

    follow_up = _update(202, text="Using the previous data, calculate its mean again and give me a short explanation.")
    follow_up["message"]["reply_to_message"] = {"message_id": original_message_id}
    await app._handle(follow_up)
    child = app.task_store.list_latest(1)[0]
    assert child.parent_task_id == parent.task_id
    assert child.status is TaskStatus.QUEUED
    assert "Reply directly" in sent[-1]["text"]
    assert "Proposed follow-up action (the only work this run will do)" in sent[-1]["text"]
    assert follow_up["message"]["text"] in sent[-1]["text"]
    assert "earlier actions will not be repeated" in sent[-1]["text"]
    assert child.request == follow_up["message"]["text"]
    assert child.instructions == ""
    approval_message_id = message_id
    assert app.task_store.task_for_message(42, approval_message_id) == child.task_id
    assert dispatched == []

    await app._handle(_update(203, text="Yes"))
    assert "reply directly" in sent[-1]["text"].lower()
    assert dispatched == []

    approve = _update(204, text="Yes")
    approve["message"]["reply_to_message"] = {"message_id": approval_message_id}
    await app._handle(approve)
    assert app._active_task_run is not None
    await app._active_task_run
    assert dispatched == [child.task_id]
    assert app.task_store.get(child.task_id).status is TaskStatus.SUCCEEDED

    replay = _update(205, text="Yes")
    replay["message"]["reply_to_message"] = {"message_id": approval_message_id}
    await app._handle(replay)
    assert "already used" in sent[-1]["text"]
    assert dispatched == [child.task_id]
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_ambiguous_follow_up_does_not_guess_or_create_task(tmp_path: Path) -> None:
    app, sent, client = _app(tmp_path)
    app.task_store.create_from_update(301, "First graph task")
    app.task_store.create_from_update(302, "Second graph task")
    await app._handle(_update(303, text="Actually, make it blue."))
    assert "Which task" in sent[-1]["text"]
    assert len(app.task_store.list_latest(10)) == 2
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_natural_cancel_targets_the_single_active_task(tmp_path: Path) -> None:
    execution_store = SqliteExecutionStore(tmp_path / "executions.sqlite3")
    backend = FakeWorkerBackend(command_plans={"sleep": FakeCommandPlan(stays_running=True)})
    worker = WorkerInterface(backend, execution_store=execution_store)
    app, sent, client = _app(
        tmp_path, execution_store=execution_store, worker_provider=lambda: nullcontext(worker)
    )
    task, _ = app.task_store.create_from_update(304, "A harmless current task")
    app.task_store.claim_for_dispatch(task.task_id, "natural-cancel-run")
    await worker.execute(ExecuteRequest("sleep", execution_id="natural-cancel-run"))
    app.task_store.mark_worker_accepted("natural-cancel-run")
    await app._handle(_update(305, text="What's running?"))
    assert task.task_id in sent[-1]["text"] and "Status: running" in sent[-1]["text"]
    await app._handle(_update(306, text="What’s the status of my last task?"))
    assert task.task_id in sent[-1]["text"] and "Status: running" in sent[-1]["text"]
    assert len(app.task_store.list_latest(10)) == 1
    await app._handle(_update(307, text="Cancel current task"))
    saved = app.task_store.get(task.task_id)
    assert saved is not None and saved.status is TaskStatus.CANCELLED
    assert "cancelled by the worker" in sent[-1]["text"]
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_progress_updates_are_status_only_and_stop_at_terminal_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nanobot.personal_agent.opencode_task import TaskRunResult

    app, sent, client = _app(tmp_path)
    callbacks: list[Callable[[float], Awaitable[None]]] = []

    class ProgressTaskRunner:
        @staticmethod
        def new_execution_id() -> str:
            return "progress-run"

        def __init__(self, _provider, **kwargs) -> None:
            callbacks.append(kwargs["progress_callback"])

        async def execute(self, task, execution_id: str) -> TaskRunResult:
            app.task_store.mark_worker_accepted(execution_id)
            await callbacks[-1](60)
            return TaskRunResult(execution_id, TaskStatus.SUCCEEDED, result="Verified result.")

    monkeypatch.setattr(controller, "OpenCodeTaskRunner", ProgressTaskRunner)
    await app._handle(_update(401, text="/new A bounded report"))
    task_id = _task_id(sent[-1]["text"])
    await app._handle(_update(402, text=f"/run {task_id}"))
    assert app._active_task_run is not None
    await app._active_task_run
    progress_messages = [payload["text"] for payload in sent if "No result is available yet." in payload["text"]]
    assert len(progress_messages) == 1
    assert task_id in progress_messages[0]
    sent_count = len(sent)
    await callbacks[-1](120)
    assert len(sent) == sent_count
    task = app.task_store.get(task_id)
    assert task is not None and task.status is TaskStatus.SUCCEEDED
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

        def __init__(self, _provider, **_kwargs) -> None:
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
            if "Task completed" in payload["text"]:
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

        def __init__(self, _provider, **_kwargs) -> None:
            pass

        async def execute(self, task, execution_id: str) -> TaskRunResult:
            app.task_store.mark_worker_accepted(execution_id)
            return TaskRunResult(execution_id, TaskStatus.SUCCEEDED, result="Saved answer.")

    monkeypatch.setattr(controller, "OpenCodeTaskRunner", FakeTaskRunner)
    await app._handle(_update(93, text="/new Deliver this after completion"))
    task_id = _task_id(sent[-1]["text"])
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
    env_file.write_text(
        "TOOTHLESS_TELEGRAM_BOT_TOKEN=secret\n"
        "TOOTHLESS_TELEGRAM_OWNER_ID=42\n"
        "TOOTHLESS_REMOTE_ORIGIN=https://example.ts.net\n"
    )
    env_file.chmod(0o600)
    monkeypatch.setenv("TOOTHLESS_TELEGRAM_ENV_FILE", str(env_file))
    monkeypatch.setenv("TOOTHLESS_TELEGRAM_DB", str(tmp_path / "updates.sqlite3"))
    monkeypatch.delenv("TOOTHLESS_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TOOTHLESS_TELEGRAM_OWNER_ID", raising=False)
    monkeypatch.delenv("TOOTHLESS_REMOTE_ORIGIN", raising=False)
    config = controller.TelegramConfig.from_environment()
    assert config.owner_id == 42
    assert config.token == "secret"
    assert config.remote_origin == "https://example.ts.net"
    monkeypatch.setenv("TOOTHLESS_REMOTE_ORIGIN", "https://example.com")
    with pytest.raises(RuntimeError, match="HTTPS Tailscale Serve origin"):
        controller.TelegramConfig.from_environment()
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
