from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from nanobot.personal_agent.memory_explorer import (
    _WEB_STORE_KEY,
    _WEB_STORE_STATE_KEY,
    _load_web_console_runtime,
    _maintain_web_store_connection,
    create_app,
)
from nanobot.personal_agent.memory_store import MemoryStore
from nanobot.personal_agent.remote_sessions import RemoteSessionStore
from nanobot.personal_agent.supabase_store import owner_uuid, validate_supabase_dsn
from nanobot.personal_agent.task_store import TaskStatus, TaskStore
from nanobot.personal_agent.telegram_controller import (
    TelegramConfig,
    TelegramController,
    UpdateStore,
)
from nanobot.personal_agent.web_console_config import WebConsoleConfig
from nanobot.worker.execution_store import SqliteExecutionStore


@pytest.mark.asyncio
async def test_web_store_recovers_after_transient_startup_failure() -> None:
    class Store:
        attempts = 0
        closes = 0

        async def connect(self) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise OSError("temporary connection failure")

        async def verify_schema(self) -> None:
            return None

        async def close(self) -> None:
            self.closes += 1

    app = web.Application()
    store = Store()

    await _maintain_web_store_connection(app, store, retry_seconds=0.001)

    assert store.attempts == 2
    assert store.closes == 1
    assert app[_WEB_STORE_KEY] is store
    assert app[_WEB_STORE_STATE_KEY] == "connected"


def test_unavailable_keychain_does_not_disable_configured_web_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nanobot.personal_agent.memory_explorer as memory_explorer

    def unavailable_vault(*, service: bytes) -> None:
        raise RuntimeError("Keychain integration unavailable")

    monkeypatch.setattr(memory_explorer, "MacOSKeychainVault", unavailable_vault)
    config = WebConsoleConfig(tmp_path / "toothless" / "web-console.env")
    config.set_dsn("postgresql://toothless_runtime:private@db.abc123.supabase.co:5432/postgres")

    vault, owner, store, state = _load_web_console_runtime(config, "42")

    assert vault is None
    assert owner == owner_uuid(42)
    assert store is not None
    assert state == "configured"


def test_supabase_connection_target_and_tls_options_are_restricted() -> None:
    validate_supabase_dsn("postgresql://toothless:secret@db.abc123.supabase.co:5432/postgres")
    validate_supabase_dsn("postgresql://toothless:secret@aws-0-us-west-1.pooler.supabase.com:5432/postgres")
    assert owner_uuid(42) == owner_uuid(42)
    for dsn in (
        "postgresql://u:p@localhost:5432/postgres",
        "postgresql://u:p@127.0.0.1:5432/postgres",
        "postgresql://u:p@example.com:5432/postgres",
        "postgresql://u:p@db.abc123.supabase.co:5432/postgres?sslmode=disable",
    ):
        with pytest.raises(ValueError):
            validate_supabase_dsn(dsn)


@pytest.mark.asyncio
async def test_web_console_has_separate_authenticated_one_use_login(tmp_path: Path) -> None:
    from aiohttp.test_utils import TestClient, TestServer

    origin = "https://toothless.test"
    sessions = RemoteSessionStore(tmp_path / "remote-sessions.sqlite3")
    code = sessions.issue("web_console", 42, 42)
    app = create_app(
        "test-token-that-is-long-enough-for-api-auth", MemoryStore(tmp_path / "memory.sqlite3"),
        TaskStore(tmp_path / "tasks.sqlite3"), remote_store=sessions,
        remote_origin=origin, base_path="/memory",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    headers = {"Host": "toothless.test", "Origin": origin}
    try:
        assert (await client.get("/memory/app/api/status", headers=headers)).status == 401
        page = await client.get("/memory/app/", headers={"Host": headers["Host"]})
        assert page.status == 200
        assert b"Connect your project database" in await page.read()
        login = await client.post("/memory/app/authorize", json={"code": code}, headers=headers)
        assert login.status == 200
        cookie = login.cookies["toothless_web"].value
        api_headers = {**headers, "Cookie": f"toothless_web={cookie}"}
        status = await client.get("/memory/app/api/status", headers=api_headers)
        assert status.status == 200
        assert (await status.json())["automatic_low_risk_start"] is False
        assert (await client.get("/memory/app/api/projects", headers=api_headers)).status == 503
        csrf = login.cookies["toothless_web_csrf"].value
        connect = await client.post(
            "/memory/app/api/connect",
            json={"dsn": "postgresql://u:p@db.abc123.supabase.co:5432/postgres"},
            headers={**headers, "Cookie": f"toothless_web={cookie}; toothless_web_csrf={csrf}", "X-Toothless-CSRF": csrf},
        )
        assert connect.status == 503
        assert "configuration" in (await connect.text())
        setup = await client.get("/memory/app/setup", headers={"Host": headers["Host"]})
        assert setup.status == 503
        assert setup.headers["X-Toothless-Store-State"] == "not_configured"
        assert (await client.post("/memory/app/authorize", json={"code": code}, headers=headers)).status == 401
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mounted_blog_setup_uses_absolute_asset_paths_without_trailing_slash(tmp_path: Path) -> None:
    from aiohttp.test_utils import TestClient, TestServer

    origin = "https://toothless.test"
    sessions = RemoteSessionStore(tmp_path / "remote-sessions.sqlite3")
    app = create_app(
        "test-token-that-is-long-enough-for-api-auth", MemoryStore(tmp_path / "memory.sqlite3"),
        TaskStore(tmp_path / "tasks.sqlite3"), remote_store=sessions,
        remote_origin=origin, base_path="/memory",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        page = await client.get("/memory/blog-setup", headers={"Host": "toothless.test"})
        assert page.status == 200
        body = await page.text()
        assert '/memory/assets/carbon.css' in body
        assert '/memory/assets/blog_setup.css' in body
        assert '/memory/assets/blog_setup.js' in body
        assert "../assets/" not in body
        for asset in ("carbon.css", "blog_setup.css", "blog_setup.js"):
            response = await client.get(
                f"/memory/assets/{asset}", headers={"Host": "toothless.test"}
            )
            assert response.status == 200
    finally:
        await client.close()


def test_web_console_config_is_private_atomic_and_refuses_overwrite(tmp_path: Path) -> None:
    parent = tmp_path / "toothless"
    path = parent / "web-console.env"
    config = WebConsoleConfig(path)
    dsn = "postgresql://toothless_web_login:private@db.abc123.supabase.co:5432/postgres"
    config.set_dsn(dsn)
    assert config.get_dsn() == dsn
    assert parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        config.migrate_keychain_dsn("postgresql://other:secret@db.abc123.supabase.co:5432/postgres")
    assert config.get_dsn() == dsn


def test_web_console_config_rejects_broad_permissions_and_unexpected_keys(tmp_path: Path) -> None:
    parent = tmp_path / "toothless"
    parent.mkdir(mode=0o700)
    path = parent / "web-console.env"
    path.write_text("TOOTHLESS_SUPABASE_DSN=private\nOTHER=value\n", encoding="utf-8")
    path.chmod(0o600)
    config = WebConsoleConfig(path)
    with pytest.raises(ValueError):
        config.get_dsn()
    path.write_text("TOOTHLESS_SUPABASE_DSN=private\n", encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(PermissionError):
        config.get_dsn()


@pytest.mark.asyncio
async def test_remote_memory_mount_uses_one_use_code_secure_cookie_and_restart_epoch(tmp_path: Path) -> None:
    from aiohttp.test_utils import TestClient, TestServer

    origin = "https://toothless.test"
    sessions = RemoteSessionStore(tmp_path / "remote-sessions.sqlite3")
    code = sessions.issue("memory", 42, 42)
    memory = MemoryStore(tmp_path / "memory.sqlite3")
    memory.create_project(1, "mechanochemistry", "Simulation project")
    memory.remember(2, "Uses FEM and QM/MD", project_name="mechanochemistry")
    tasks = TaskStore(tmp_path / "tasks.sqlite3")
    app = create_app(
        "test-token-that-is-long-enough-for-api-auth", memory, tasks,
        remote_store=sessions, remote_origin=origin, base_path="/memory",
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        headers = {"Host": "toothless.test"}
        page = await client.get("/memory/", headers=headers)
        assert page.status == 200
        assert (await client.get("/memory", headers=headers)).status == 200
        assert (await client.get("/", headers=headers)).status == 200
        assert b"/memory/assets/app.js" in await page.read()
        assert (await client.get("/memory/api/projects", headers={**headers, "Origin": origin})).status == 401
        assert (await client.get(
            "/memory/api/projects", headers={**headers, "Origin": origin,
            "Authorization": "Bearer test-token-that-is-long-enough-for-api-auth"},
        )).status == 401
        login = await client.post(
            "/memory/authorize", json={"code": code}, headers={**headers, "Origin": origin},
        )
        assert login.status == 200
        cookie_header = login.headers["Set-Cookie"]
        assert "Secure" in cookie_header and "HttpOnly" in cookie_header and "SameSite=Strict" in cookie_header
        assert "Path=/memory/" in cookie_header
        cookie = login.cookies["toothless_memory"].value
        api_headers = {**headers, "Origin": origin, "Cookie": f"toothless_memory={cookie}"}
        assert (await client.get("/memory/api/projects", headers=api_headers)).status == 200
        graph = await client.get(
            "/memory/api/graph?q=mechanochemistry", headers=api_headers,
        )
        graph_payload = await graph.json()
        assert graph.status == 200
        assert any(node["label"] == "mechanochemistry" for node in graph_payload["nodes"])
        assert any(node["type"] == "memory" for node in graph_payload["nodes"])
        assert (await client.post(
            "/memory/authorize", json={"code": code}, headers={**headers, "Origin": origin},
        )).status == 401
        stripped_code = sessions.issue("memory", 42, 42)
        stripped_login = await client.post(
            "/authorize", json={"code": stripped_code}, headers={**headers, "Origin": origin},
        )
        assert stripped_login.status == 200
        stripped_cookie = stripped_login.cookies["toothless_memory"].value
        stripped_api_headers = {
            **headers, "Origin": origin, "Cookie": f"toothless_memory={stripped_cookie}",
        }
        assert (await client.get("/api/projects", headers=stripped_api_headers)).status == 200
        stripped_graph = await client.get("/api/graph?q=mechanochemistry", headers=stripped_api_headers)
        assert stripped_graph.status == 200
        assert any(node["label"] == "mechanochemistry" for node in (await stripped_graph.json())["nodes"])
        assert (await client.post("/logout", headers=stripped_api_headers)).status == 200
        sessions.advance_epoch()
        assert (await client.get("/memory/api/projects", headers=api_headers)).status == 401
        assert (await client.post("/memory/logout", headers=api_headers)).status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_memory_explorer_is_authenticated_read_only_and_shows_graph_sources(tmp_path: Path) -> None:
    tasks = TaskStore(tmp_path / "tasks.sqlite3")
    memory = MemoryStore(tmp_path / "memory.sqlite3")
    project, _, _ = memory.create_project(1, "Mechanochemistry", "Simulation research")
    memory_id, _, _ = memory.remember(2, "Uses FEM and QM/MD", project_name="Mechanochemistry")
    task, _ = tasks.create_from_update(1, "Analyze number.csv")
    tasks.add_attachment(
        task.task_id, telegram_file_id="test-file", filename="number.csv", mime_type="text/csv",
        size_bytes=17, storage_key="safe-storage-key", caption="Analyze this file",
    )
    memory.sync_task(
        task.task_id, parent_task_id=None, files=((f"inputs/{task.task_id}/number.csv", "number.csv"),),
        artifacts=(), created_at=task.created_at.isoformat(),
    )
    memory.remember_task(
        3, "Mechanochemistry", task_id=task.task_id, summary=f"Completed task {task.task_id}.",
        references=(("file", "number.csv", "number.csv"),),
    )
    api_memory = MemoryStore(tmp_path / "memory.sqlite3", read_only=True)
    api_tasks = TaskStore(tmp_path / "tasks.sqlite3", read_only=True)
    app = create_app("test-token-that-is-long-enough-for-api-auth", api_memory, api_tasks)

    class LoopbackTransport:
        @staticmethod
        def get_extra_info(name: str) -> tuple[str, int] | None:
            return ("127.0.0.1", 12345) if name == "peername" else None

    async def invoke(path: str, *, authorized: bool = True):
        headers = {"Host": "127.0.0.1:8768"}
        if authorized:
            headers["Authorization"] = "Bearer test-token-that-is-long-enough-for-api-auth"
        request = make_mocked_request("GET", path, headers=headers, app=app, transport=LoopbackTransport())
        match = await app.router.resolve(request)
        request._match_info = match
        local_only, authenticate = app.middlewares
        try:
            return await local_only(request, lambda req: authenticate(req, match.handler))
        except web.HTTPException as exc:
            return exc

    denied = await invoke("/api/projects", authorized=False)
    assert denied.status == 401
    projects = await invoke("/api/projects")
    assert projects.status == 200
    assert any(item["id"] == project.project_id for item in json.loads(projects.body)["projects"])
    graph = await invoke(f"/api/graph?q=number.csv&project={project.project_id}&depth=2&limit=32")
    payload = json.loads(graph.body)
    assert graph.status == 200
    assert any(item["label"] == "number.csv" for item in payload["nodes"])
    node = await invoke(f"/api/node?id=task:{task.task_id}")
    detail = json.loads(node.body)
    assert detail["task"]["task_id"] == task.task_id
    assert detail["task"]["files"] == ["number.csv"]
    memory_detail = await invoke(f"/api/node?id=memory:{memory_id}")
    assert json.loads(memory_detail.body)["content"] == "Uses FEM and QM/MD"
    assert tasks.get(task.task_id) is not None
    assert memory.get(memory_id or "").content == "Uses FEM and QM/MD"
    with pytest.raises(sqlite3.OperationalError):
        api_memory.remember(4, "This API must not write")


def test_telegram_callbacks_are_owner_message_bound_and_single_use(tmp_path: Path) -> None:
    store = UpdateStore(tmp_path / "updates.sqlite3")
    payload = store.create_callback("task_approve", "task-" + "a" * 32, 42, 42)
    assert store.consume_callback(payload, owner_id=7, chat_id=42, message_id=100) is None
    assert store.consume_callback(payload, owner_id=42, chat_id=42, message_id=100) is None
    store.bind_callbacks((payload,), 100)
    expected = {"action": "task_approve", "target_id": "task-" + "a" * 32, "data": ""}
    assert store.consume_callback(payload, owner_id=42, chat_id=42, message_id=101) is None
    store.close()
    store = UpdateStore(tmp_path / "updates.sqlite3")
    assert store.consume_callback(payload, owner_id=42, chat_id=42, message_id=100) == expected
    assert store.consume_callback(payload, owner_id=42, chat_id=42, message_id=100) is None
    store.close()


@pytest.mark.asyncio
async def test_task_approval_button_is_single_use_and_message_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(200, json={"ok": True, "result": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tasks = TaskStore(tmp_path / "tasks.sqlite3")
    app = TelegramController(
        TelegramConfig("test-token", 42, tmp_path / "updates.sqlite3"), client=client,
        execution_store=SqliteExecutionStore(tmp_path / "executions.sqlite3"), task_store=tasks,
    )
    task, _ = tasks.create_from_update(1, "Reply with exactly READY")
    tasks.create_pending_approval(task.task_id, 42)
    tasks.bind_approval_prompt(task.task_id, 42, 100)
    payload = app.store.create_callback("task_approve", task.task_id, 42, 42)
    app.store.bind_callbacks((payload,), 100)
    started: list[str] = []
    monkeypatch.setattr(app, "_start_task_reply", lambda task_id, _chat_id: started.append(task_id) or "dispatch approved")
    query = {
        "id": "callback-1", "from": {"id": 42}, "data": payload,
        "message": {"message_id": 100, "chat": {"id": 42, "type": "private"}},
    }
    await app._handle({"update_id": 9, "callback_query": query})
    assert started == [task.task_id]
    assert requests.count("/bottest-token/editMessageText") == 1
    await app._handle({"update_id": 9, "callback_query": {**query, "id": "callback-duplicate"}})
    assert started == [task.task_id]
    assert requests.count("/bottest-token/answerCallbackQuery") == 2
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_schedule_inline_pause_and_resume_recheck_authoritative_state(tmp_path: Path) -> None:
    payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.content:
            payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = TelegramController(
        TelegramConfig("test-token", 42, tmp_path / "updates.sqlite3"), client=client,
        execution_store=SqliteExecutionStore(tmp_path / "executions.sqlite3"),
        task_store=TaskStore(tmp_path / "tasks.sqlite3"),
    )
    zone = ZoneInfo("America/Los_Angeles")
    local_start = (datetime.now(UTC) + timedelta(days=2)).astimezone(zone).replace(tzinfo=None).isoformat()
    schedule = app.schedule_store.create_pending(
        source_update_id=501, owner_id=42, chat_id=42, description="daily research",
        action="research only", timezone="America/Los_Angeles", local_start=local_start,
        recurrence="daily", profile="browser",
    )
    active = app.schedule_store.approve(schedule.schedule_id, owner_id=42, chat_id=42)
    assert active is not None and active.state == "active"
    pause_data = app._new_callback("schedule_pause", schedule.schedule_id, 42)
    app.store.bind_callbacks((pause_data,), 200)
    await app._handle({"update_id": 20, "callback_query": {
        "id": "pause-1", "from": {"id": 42}, "data": pause_data,
        "message": {"message_id": 200, "chat": {"id": 42, "type": "private"}},
    }})
    paused = app.schedule_store.get(schedule.schedule_id, owner_id=42, chat_id=42)
    assert paused is not None and paused.state == "paused"
    edit = next(item for item in reversed(payloads) if "reply_markup" in item)
    resume_data = edit["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    assert isinstance(resume_data, str)
    await app._handle({"update_id": 21, "callback_query": {
        "id": "resume-1", "from": {"id": 42}, "data": resume_data,
        "message": {"message_id": 200, "chat": {"id": 42, "type": "private"}},
    }})
    resumed = app.schedule_store.get(schedule.schedule_id, owner_id=42, chat_id=42)
    assert resumed is not None and resumed.state == "active"
    await app._handle({"update_id": 22, "callback_query": {
        "id": "pause-stale", "from": {"id": 42}, "data": pause_data,
        "message": {"message_id": 200, "chat": {"id": 42, "type": "private"}},
    }})
    still_active = app.schedule_store.get(schedule.schedule_id, owner_id=42, chat_id=42)
    assert still_active is not None and still_active.state == "active"
    await app.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_source_callback_shows_persisted_urls_and_stale_press_is_safe(tmp_path: Path) -> None:
    payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tasks = TaskStore(tmp_path / "tasks.sqlite3")
    app = TelegramController(
        TelegramConfig("test-token", 42, tmp_path / "updates.sqlite3"), client=client,
        execution_store=SqliteExecutionStore(tmp_path / "executions.sqlite3"), task_store=tasks,
    )
    task, _ = tasks.create_from_update(1, "Research public Python release notes")
    execution_id = "opencode-" + "b" * 32
    tasks.claim_for_dispatch(task.task_id, execution_id)
    tasks.mark_worker_accepted(execution_id)
    tasks.finish_execution(
        execution_id, TaskStatus.SUCCEEDED,
        result="Release details from https://www.python.org/downloads/",
    )
    tasks.record_sources(task.task_id, (("Python downloads", "https://www.python.org/downloads/"),))
    callback = app._new_callback("sources_page", task.task_id, 42, data="0")
    app.store.bind_callbacks((callback,), 300)
    update = {"callback_query": {
        "id": "source-1", "from": {"id": 42}, "data": callback,
        "message": {"message_id": 300, "chat": {"id": 42, "type": "private"}},
    }}
    await app._handle({"update_id": 30, **update})
    edit = next(item for item in payloads if item.get("message_id") == 300)
    keyboard = edit["reply_markup"]["inline_keyboard"]
    assert any(button.get("url") == "https://www.python.org/downloads/" for row in keyboard for button in row)
    assert tasks.get(task.task_id).execution_ids == (execution_id,)
    edits_before_stale = sum(item.get("message_id") == 300 for item in payloads)
    await app._handle({"update_id": 31, **update})
    assert sum(item.get("message_id") == 300 for item in payloads) == edits_before_stale
    await app.close()
    await client.aclose()
