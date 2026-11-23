from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from aiohttp import web
from aiohttp.streams import StreamReader
from aiohttp.test_utils import make_mocked_request

from nanobot.personal_agent.blog_setup import (
    BlogProviderClient,
    BlogProvisioner,
    BlogSetupStore,
    MemoryCredentialVault,
    ProviderError,
)
from nanobot.personal_agent.memory_explorer import create_app
from nanobot.personal_agent.memory_store import MemoryStore
from nanobot.personal_agent.remote_sessions import RemoteSessionStore
from nanobot.personal_agent.task_store import TaskStore
from nanobot.personal_agent.telegram_controller import TelegramConfig, TelegramController
from nanobot.worker.execution_store import SqliteExecutionStore


@pytest.mark.asyncio
async def test_approved_provisioning_stages_only_fixed_targets_and_verifies_deployment(
    tmp_path: Path,
) -> None:
    source_sha = "a" * 40
    calls: list[tuple[str, str]] = []
    target_created = False

    class FakeProvider:
        async def validate_github(self, _token: str) -> str:
            return "logolica99"

        async def validate_vercel(self, _token: str) -> str:
            return "logolica99"

        async def github_default_commit(self, _token: str, repository: str) -> tuple[str, str]:
            calls.append(("read", repository))
            return "main", source_sha

        async def github_repository(self, _token: str, repository: str):
            calls.append(("inspect", repository))
            if repository == "logolica99/tech-pulse-toothless" and target_created:
                return {"id": 512, "private": True, "name": "tech-pulse-toothless"}
            return None

        async def inspect_github_repository(self, _token: str, repository: str):
            calls.append(("inspect_full", repository))
            return {"id": 100, "name": "tech-pulse", "private": False}

        async def create_github_repository(self, _token: str):
            nonlocal target_created
            target_created = True
            calls.append(("create", "logolica99/tech-pulse-toothless"))
            return {"id": 512, "name": "tech-pulse-toothless", "private": True,
                    "owner": {"login": "logolica99"}}

        async def github_default_commit_or_none(self, _token: str, repository: str):
            calls.append(("default_commit", repository))
            return None if sum(call[0] == "default_commit" for call in calls) == 1 else ("main", source_sha)

        async def inspect_vercel_project(self, _token: str, project: str):
            calls.append(("inspect_project", project))
            return None

        async def create_vercel_project(self, _token: str):
            calls.append(("create_project", "tech-pulse-toothless"))
            return {"id": "prj_experimental", "name": "tech-pulse-toothless",
                    "domains": ["tech-pulse-toothless.vercel.app"],
                    "link": {"repo": "tech-pulse-toothless"}}

        async def latest_vercel_deployment(self, _token: str, project_id: str):
            calls.append(("deployment", project_id))
            return {"uid": "dpl_experimental", "readyState": "READY",
                    "url": "tech-pulse-toothless-a1b2.vercel.app"}

        async def get_deployment_homepage(self, url: str):
            calls.append(("homepage", url))
            return httpx.Response(200, request=httpx.Request("GET", url))

        async def close(self) -> None:
            return None

    store = BlogSetupStore(tmp_path / "blog.sqlite3")
    store.update(github_setup=True)
    vault = MemoryCredentialVault()
    vault.set("github_setup", "fake-github-setup-credential-123")
    vault.set("github_publish", "fake-github-publish-credential-123")
    vault.set("vercel", "fake-vercel-credential-123456")
    provider = FakeProvider()
    provisioner = BlogProvisioner(store, vault, provider)  # type: ignore[arg-type]
    provisioner._mirror_source = lambda _branch, _sha, _token: calls.append(("push", "logolica99/tech-pulse-toothless"))  # type: ignore[method-assign]
    try:
        repo_proposal, baseline, _branch = await provisioner.prepare_repository(42, 42)
        store.bind_proposal(repo_proposal, 700)
        approved_repo = store.consume_proposal(repo_proposal, 42, 42, 700)
        assert approved_repo is not None and approved_repo["baseline_sha"] == source_sha
        created = await provisioner.create_repository_approved(approved_repo["baseline_sha"])
        assert created.repository_id == 512 and created.baseline_sha == baseline
        store.update(github_publish=True, vercel=True)

        deploy_proposal = await provisioner.prepare_copy_and_deploy(42, 42)
        store.bind_proposal(deploy_proposal, 701)
        approved_deploy = store.consume_proposal(deploy_proposal, 42, 42, 701)
        assert approved_deploy is not None and approved_deploy["repository_id"] == "512"
        deployed = await provisioner.copy_and_deploy_approved()
        assert deployed.state == "deployed_ready"
        assert deployed.project_id == "prj_experimental"
        assert deployed.deployment_id == "dpl_experimental"
        assert ("create", "logolica99/tech-pulse-toothless") in calls
        assert ("push", "logolica99/tech-pulse-toothless") in calls
        assert ("create_project", "tech-pulse-toothless") in calls
        assert ("homepage", "https://tech-pulse-toothless-a1b2.vercel.app") in calls
        assert not any(repository == "logolica99/tech-pulse" and action in {"create", "push"}
                       for action, repository in calls)
    finally:
        store.close()


def test_setup_proposals_are_fixed_message_bound_and_one_use(tmp_path: Path) -> None:
    store = BlogSetupStore(tmp_path / "blog-setup.sqlite3")
    proposal = store.create_proposal(42, 42, {
        "owner": "logolica99", "source": "logolica99/tech-pulse",
        "target": "logolica99/tech-pulse-toothless", "project": "tech-pulse-toothless",
        "visibility": "private", "stage": "create_repository", "baseline_sha": "a" * 40,
    })
    assert store.consume_proposal(proposal, 42, 42, 100) is None
    store.bind_proposal(proposal, 100)
    assert store.consume_proposal(proposal, 42, 42, 101) is None
    approved = store.consume_proposal(proposal, 42, 42, 100)
    assert approved is not None and approved["target"] == "logolica99/tech-pulse-toothless"
    assert store.consume_proposal(proposal, 42, 42, 100) is None
    with pytest.raises(ValueError):
        store.create_proposal(42, 42, {
            "owner": "logolica99", "source": "logolica99/tech-pulse",
            "target": "logolica99/tech-pulse", "project": "tech-pulse-toothless",
            "visibility": "private", "stage": "create_repository", "baseline_sha": "a" * 40,
        })
    store.close()


@pytest.mark.asyncio
async def test_blog_setup_command_uses_owner_bound_persistent_connect_callback(tmp_path: Path) -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else {}
        requests.append(payload)
        if request.url.path.endswith("/sendMessage"):
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 900}})
        return httpx.Response(200, json={"ok": True, "result": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    controller = TelegramController(
        TelegramConfig("test-token", 42, tmp_path / "updates.sqlite3"), client=client,
        execution_store=SqliteExecutionStore(tmp_path / "executions.sqlite3"),
        task_store=TaskStore(tmp_path / "tasks.sqlite3"),
    )
    controller._ensure_memory_explorer = AsyncMock(return_value=True)  # type: ignore[method-assign]
    try:
        await controller._handle({"update_id": 10, "message": {
            "from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": "/blog setup",
        }})
        sent = requests[-1]
        assert "Toothless blog setup" in sent["text"]
        keyboard = sent["reply_markup"]["inline_keyboard"]
        github_button = next(button for row in keyboard for button in row if "create repo" in button["text"])
        callback_data = github_button["callback_data"]
        callback_id = callback_data.removeprefix("c:")
        stored = controller.store._db.execute(
            "SELECT action, target_id, data FROM telegram_callbacks WHERE callback_id=?", (callback_id,)
        ).fetchone()
        assert tuple(stored) == ("blog_connect", "blog_setup", "github_setup")
        await controller._handle({"update_id": 11, "callback_query": {
            "id": "connect-github", "from": {"id": 42}, "data": callback_data,
            "message": {"message_id": 900, "chat": {"id": 42, "type": "private"}},
        }})
        edit = next(item for item in reversed(requests) if "message_id" in item)
        assert "One-use code" in edit["text"]
        assert "GitHub" in edit["text"]
        assert "memory/blog-setup/" in edit["reply_markup"]["inline_keyboard"][0][0]["url"]
        code = __import__("re").search(r"<code>([A-Za-z0-9_-]+)</code>", edit["text"])
        assert code is not None
        assert controller.remote_sessions.consume("blog_setup", code.group(1))
        assert not controller.remote_sessions.consume("blog_setup", code.group(1))
    finally:
        await controller.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_provider_client_validates_fixed_accounts_and_sanitizes_errors() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "logolica99"})
        if request.url.path == "/v2/user":
            return httpx.Response(200, json={"user": {"username": "logolica99"}})
        return httpx.Response(403, text="PRIVATE RESPONSE BODY")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = BlogProviderClient(client=client)
    original_github, original_vercel = "https://api.github.com", "https://api.vercel.com"
    import nanobot.personal_agent.blog_setup as module

    module.GITHUB_API = "https://test.invalid"
    module.VERCEL_API = "https://test.invalid"
    try:
        assert await provider.validate_github("fake-github-credential-123456") == "logolica99"
        assert await provider.validate_vercel("fake-vercel-credential-123456") == "logolica99"
        with pytest.raises(ProviderError) as error:
            await provider.inspect_github_repository("fake-github-credential-123456", "logolica99/other")
        assert "PRIVATE RESPONSE BODY" not in str(error.value)
        assert seen[0].headers["Authorization"] == "Bearer fake-github-credential-123456"
        assert "fake-github-credential-123456" not in str(seen[0].url)
    finally:
        module.GITHUB_API = original_github
        module.VERCEL_API = original_vercel
        await provider.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_blog_enrollment_requires_private_one_use_code_and_csrf_then_stores_only_validated_secret(tmp_path: Path) -> None:
    origin = "https://toothless.test"
    remote = RemoteSessionStore(tmp_path / "remote.sqlite3")
    code = remote.issue("blog_setup", 42, 42)
    store = BlogSetupStore(tmp_path / "blog.sqlite3")
    vault = MemoryCredentialVault()

    def provider_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "logolica99"})
        return httpx.Response(403, text="secret provider diagnostics must not escape")

    provider_http = httpx.AsyncClient(transport=httpx.MockTransport(provider_handler))
    provider = BlogProviderClient(client=provider_http)
    app = create_app(
        "explorer-token-for-tests-that-is-long-enough", MemoryStore(tmp_path / "memory.sqlite3"),
        TaskStore(tmp_path / "tasks.sqlite3"), remote_store=remote, remote_origin=origin,
        base_path="/memory", blog_store=store, blog_vault=vault, blog_provider=provider,
    )
    headers = {"Host": "toothless.test", "Origin": origin}

    class LoopbackTransport:
        @staticmethod
        def get_extra_info(name: str):
            return ("127.0.0.1", 8768) if name == "peername" else None

    class PayloadProtocol:
        @staticmethod
        def resume_reading(**_kwargs: object) -> None:
            return None

        @staticmethod
        def pause_reading() -> None:
            return None

    async def invoke(method: str, path: str, *, data: dict[str, str] | None = None, extra: dict[str, str] | None = None):
        request_headers = {**headers, **(extra or {})}
        payload = StreamReader(protocol=PayloadProtocol(), limit=16 * 1024)
        if data is not None:
            raw = json.dumps(data).encode()
            request_headers["Content-Type"] = "application/json"
            request_headers["Content-Length"] = str(len(raw))
            payload.feed_data(raw)
            payload.feed_eof()
        request = make_mocked_request(method, path, headers=request_headers, app=app,
                                      transport=LoopbackTransport(), payload=payload)
        match = await app.router.resolve(request)
        request._match_info = match
        local_only, authenticate = app.middlewares
        try:
            return await local_only(request, lambda req: authenticate(req, match.handler))
        except web.HTTPException as exc:
            return exc

    try:
        page = await invoke("GET", "/memory/blog-setup/", extra={"Origin": ""})
        assert page.status == 200
        page_text = page.text
        assert "/memory/assets/blog_setup.js" in page_text
        assert "settings/personal-access-tokens/new" in page_text
        assert "PRIVATE RESPONSE BODY" not in page_text
        assert (await invoke("POST", "/memory/blog-setup/authorize", data={"code": code})).status == 200
        assert (await invoke("POST", "/memory/blog-setup/authorize", data={"code": code})).status == 401
        login = await invoke("POST", "/memory/blog-setup/authorize", data={"code": remote.issue("blog_setup", 42, 42)})
        assert login.status == 200
        parsed = login.cookies
        assert "toothless_blog_setup" in parsed and "toothless_blog_csrf" in parsed
        session_cookie = parsed["toothless_blog_setup"].value
        csrf = parsed["toothless_blog_csrf"].value
        cookie = f"toothless_blog_setup={session_cookie}; toothless_blog_csrf={csrf}"
        session_headers = {"Origin": origin, "Cookie": cookie}
        status = await invoke("GET", "/memory/blog-setup/api/status", extra=session_headers)
        assert status.status == 200
        assert "secret" not in json.dumps(json.loads(status.text)).lower()
        rejected = await invoke(
            "POST", "/memory/blog-setup/api/credentials", data={"credential": "github_setup", "secret": "fake-github-credential-123456"},
            extra=session_headers,
        )
        assert rejected.status == 403
        accepted = await invoke(
            "POST", "/memory/blog-setup/api/credentials", data={"credential": "github_setup", "secret": "fake-github-credential-123456"},
            extra={**session_headers, "X-Toothless-CSRF": csrf},
        )
        assert accepted.status == 200
        assert vault.get("github_setup") == "fake-github-credential-123456"
        assert "fake-github-credential-123456" not in accepted.text
        assert store.snapshot().github_setup
        cross_origin = await invoke(
            "POST", "/memory/blog-setup/api/credentials", data={"credential": "github_setup", "secret": "another-fake-credential-123456"},
            extra={**session_headers, "Origin": "https://evil.invalid", "X-Toothless-CSRF": csrf},
        )
        assert cross_origin.status == 403
        deleted = await invoke(
            "DELETE", "/memory/blog-setup/api/credentials/github_setup",
            extra={**session_headers, "X-Toothless-CSRF": csrf},
        )
        assert deleted.status == 200
        assert vault.get("github_setup") is None
        assert (await invoke("GET", "/memory/blog-setup/api/status", extra=session_headers)).status == 200
    finally:
        await provider.close()
        await provider_http.aclose()
        store.close()
        remote.close()
