from __future__ import annotations

import hashlib
import os
import ssl
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from argon2 import PasswordHasher

from nanobot.personal_agent.memory_explorer import create_app
from nanobot.personal_agent.memory_store import MemoryStore
from nanobot.personal_agent.remote_sessions import RemoteSessionStore
from nanobot.personal_agent.task_store import TaskStore
from nanobot.personal_agent.web_console_config import WebConsoleConfig


class _AuthStore:
    def __init__(self) -> None:
        self.password_hash: str | None = None
        self.username = ""
        self.limits: dict[str, int] = {}
        self.sessions: dict[str, tuple[str, datetime, bool]] = {}

    async def has_admin(self) -> bool:
        return self.password_hash is not None

    async def bootstrap_admin(self, username: str, normalized: str, password_hash: str) -> bool:
        if self.password_hash is not None:
            return False
        self.username, self.password_hash = normalized, password_hash
        return True

    async def admin_password_hash(self, normalized: str) -> str | None:
        return self.password_hash if self.username == normalized else None

    async def admin_credentials(self) -> tuple[str, str] | None:
        return (self.username, self.password_hash) if self.password_hash else None

    async def login_is_limited(self, key: str) -> bool:
        return self.limits.get(key, 0) >= 5

    async def record_login_failure(self, key: str) -> None:
        self.limits[key] = self.limits.get(key, 0) + 1

    async def record_login_success(self, key: str) -> None:
        self.limits.pop(key, None)

    async def create_web_session(self, token_hash: str, csrf_hash: str, expiry: datetime) -> None:
        self.sessions[token_hash] = (csrf_hash, expiry, False)

    async def valid_web_session(self, token_hash: str, csrf_hash: str) -> bool:
        record = self.sessions.get(token_hash)
        return bool(record and record[0] == csrf_hash and record[1] > datetime.now(record[1].tzinfo) and not record[2])

    async def revoke_web_session(self, token_hash: str) -> None:
        record = self.sessions.get(token_hash)
        if record:
            self.sessions[token_hash] = (record[0], record[1], True)

    async def revoke_all_web_sessions(self) -> None:
        self.sessions = {key: (row[0], row[1], True) for key, row in self.sessions.items()}

    async def change_admin_password(self, password_hash: str) -> None:
        self.password_hash = password_hash
        await self.revoke_all_web_sessions()


async def _client(tmp_path: Path, store: _AuthStore, sessions: RemoteSessionStore) -> TestClient:
    app = create_app(
        "test-token-that-is-long-enough-for-api-auth", MemoryStore(tmp_path / "memory.sqlite3"),
        TaskStore(tmp_path / "tasks.sqlite3"), remote_store=sessions,
        remote_origin="https://toothless.test", base_path="/memory",
        web_store=store, web_owner=uuid.uuid4(),
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_bootstrap_is_owner_code_bound_one_time_and_creates_argon2id_admin(tmp_path: Path) -> None:
    store = _AuthStore()
    sessions = RemoteSessionStore(tmp_path / "remote.sqlite3")
    code = sessions.issue("web_console", 42, 42)
    client = await _client(tmp_path, store, sessions)
    headers = {"Host": "toothless.test", "Origin": "https://toothless.test"}
    try:
        response = await client.post(
            "/memory/app/api/auth/bootstrap",
            json={"code": code, "username": "root", "password": "a-unique-long-owner-password"}, headers=headers,
        )
        assert response.status == 200
        assert store.password_hash and store.password_hash.startswith("$argon2id$")
        assert "a-unique-long-owner-password" not in store.password_hash
        assert response.cookies["toothless_web"]["httponly"]
        assert response.cookies["toothless_web"]["secure"]
        replay = await client.post(
            "/memory/app/api/auth/bootstrap",
            json={"code": code, "username": "another", "password": "another-long-owner-password"}, headers=headers,
        )
        assert replay.status == 401
        assert store.username == "root"
        cookies = response.cookies
        cookie_header = f"toothless_web={cookies['toothless_web'].value}; toothless_web_csrf={cookies['toothless_web_csrf'].value}"
        assert (await client.get("/memory/app/api/status", headers={**headers, "Cookie": cookie_header})).status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_login_rate_limit_returns_generic_error(tmp_path: Path) -> None:
    store = _AuthStore()
    store.username = "root"
    store.password_hash = PasswordHasher().hash("a-unique-long-owner-password")
    sessions = RemoteSessionStore(tmp_path / "remote.sqlite3")
    client = await _client(tmp_path, store, sessions)
    headers = {"Host": "toothless.test", "Origin": "https://toothless.test"}
    try:
        for _ in range(5):
            bad = await client.post("/memory/app/api/auth/login", json={"username": "root", "password": "wrong"}, headers=headers)
            assert bad.status == 401 and await bad.text() == "Username or password is incorrect"
        limited = await client.post("/memory/app/api/auth/login", json={"username": "root", "password": "a-unique-long-owner-password"}, headers=headers)
        assert limited.status == 401
        assert await limited.text() == "Username or password is incorrect"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_login_session_survives_app_restart_and_logout_revokes_it(tmp_path: Path) -> None:
    store = _AuthStore()
    store.username = "root"
    store.password_hash = PasswordHasher().hash("a-unique-long-owner-password")
    sessions = RemoteSessionStore(tmp_path / "remote.sqlite3")
    client = await _client(tmp_path, store, sessions)
    headers = {"Host": "toothless.test", "Origin": "https://toothless.test"}
    try:
        login = await client.post("/memory/app/api/auth/login", json={"username": "root", "password": "a-unique-long-owner-password"}, headers=headers)
        assert login.status == 200
        cookies = login.cookies
        session_cookie = f"toothless_web={cookies['toothless_web'].value}; toothless_web_csrf={cookies['toothless_web_csrf'].value}"
        csrf = cookies["toothless_web_csrf"].value
        assert (await client.get("/memory/app/api/status", headers={**headers, "Cookie": session_cookie})).status == 200
        await client.close()
        client = await _client(tmp_path, store, sessions)
        persisted = await client.get("/memory/app/api/status", headers={**headers, "Cookie": session_cookie})
        assert persisted.status == 200
        logout = await client.post(
            "/memory/app/api/auth/logout", headers={**headers, "Cookie": session_cookie, "X-Toothless-CSRF": csrf},
        )
        assert logout.status == 200
        assert (await client.get("/memory/app/api/status", headers={**headers, "Cookie": session_cookie})).status == 401
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_expired_persistent_session_is_rejected(tmp_path: Path) -> None:
    store = _AuthStore()
    store.username = "root"
    store.password_hash = PasswordHasher().hash("a-unique-long-owner-password")
    sessions = RemoteSessionStore(tmp_path / "remote.sqlite3")
    client = await _client(tmp_path, store, sessions)
    headers = {"Host": "toothless.test", "Origin": "https://toothless.test"}
    try:
        login = await client.post("/memory/app/api/auth/login", json={"username": "root", "password": "a-unique-long-owner-password"}, headers=headers)
        cookies = login.cookies
        token = cookies["toothless_web"].value
        csrf = cookies["toothless_web_csrf"].value
        store.sessions[hashlib.sha256(token.encode()).hexdigest()] = (
            hashlib.sha256(csrf.encode()).hexdigest(), datetime.now(UTC) - timedelta(seconds=1), False,
        )
        cookie_header = f"toothless_web={token}; toothless_web_csrf={csrf}"
        assert (await client.get("/memory/app/api/status", headers={**headers, "Cookie": cookie_header})).status == 401
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_password_rotation_reauthenticates_and_logout_all_revokes_sessions(tmp_path: Path) -> None:
    store = _AuthStore()
    store.username = "root"
    store.password_hash = PasswordHasher().hash("a-unique-long-owner-password")
    sessions = RemoteSessionStore(tmp_path / "remote.sqlite3")
    client = await _client(tmp_path, store, sessions)
    headers = {"Host": "toothless.test", "Origin": "https://toothless.test"}
    try:
        login = await client.post("/memory/app/api/auth/login", json={"username": "root", "password": "a-unique-long-owner-password"}, headers=headers)
        old = login.cookies
        old_cookie = f"toothless_web={old['toothless_web'].value}; toothless_web_csrf={old['toothless_web_csrf'].value}"
        csrf = old["toothless_web_csrf"].value
        wrong = await client.post(
            "/memory/app/api/auth/password",
            json={"current_password": "wrong-current-password", "new_password": "a-replacement-owner-password"},
            headers={**headers, "Cookie": old_cookie, "X-Toothless-CSRF": csrf},
        )
        assert wrong.status == 401
        changed = await client.post(
            "/memory/app/api/auth/password",
            json={"current_password": "a-unique-long-owner-password", "new_password": "a-replacement-owner-password"},
            headers={**headers, "Cookie": old_cookie, "X-Toothless-CSRF": csrf},
        )
        assert changed.status == 200
        assert (await client.get("/memory/app/api/status", headers={**headers, "Cookie": old_cookie})).status == 401
        rotated = changed.cookies
        new_cookie = f"toothless_web={rotated['toothless_web'].value}; toothless_web_csrf={rotated['toothless_web_csrf'].value}"
        new_csrf = rotated["toothless_web_csrf"].value
        assert (await client.get("/memory/app/api/status", headers={**headers, "Cookie": new_cookie})).status == 200
        logout_all = await client.post(
            "/memory/app/api/auth/logout-all", headers={**headers, "Cookie": new_cookie, "X-Toothless-CSRF": new_csrf},
        )
        assert logout_all.status == 200
        assert (await client.get("/memory/app/api/status", headers={**headers, "Cookie": new_cookie})).status == 401
        correct_old = await client.post(
            "/memory/app/api/auth/login", json={"username": "root", "password": "a-unique-long-owner-password"}, headers=headers,
        )
        assert correct_old.status == 401
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_login_rejects_cross_origin_and_unauthenticated_api(tmp_path: Path) -> None:
    store = _AuthStore()
    sessions = RemoteSessionStore(tmp_path / "remote.sqlite3")
    client = await _client(tmp_path, store, sessions)
    headers = {"Host": "toothless.test", "Origin": "https://attacker.invalid"}
    try:
        assert (await client.get("/memory/app/api/status", headers={"Host": headers["Host"]})).status == 401
        response = await client.post("/memory/app/api/auth/login", json={"username": "root", "password": "password"}, headers=headers)
        assert response.status == 403
    finally:
        await client.close()


def test_web_config_adds_private_ca_without_disabling_hostname_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "private" / "web-console.env"
    config_path.parent.mkdir(mode=0o700)
    os.chmod(config_path.parent, 0o700)
    ca_path = config_path.parent / "supabase-root.crt"
    ca_path.write_text("test certificate fixture", encoding="ascii")
    ca_path.chmod(0o600)

    class _Context:
        check_hostname = True
        verify_mode = ssl.CERT_REQUIRED
        cafile: str | None = None

        def load_verify_locations(self, *, cafile: str) -> None:
            self.cafile = cafile

    context = _Context()
    monkeypatch.setattr("nanobot.personal_agent.web_console_config.ssl.create_default_context", lambda: context)
    result = WebConsoleConfig(config_path).create_ssl_context()
    assert result is context
    assert context.cafile == str(ca_path)


def test_web_config_rejects_group_readable_ca_certificate(tmp_path: Path) -> None:
    config_path = tmp_path / "private" / "web-console.env"
    config_path.parent.mkdir(mode=0o700)
    os.chmod(config_path.parent, 0o700)
    ca_path = config_path.parent / "supabase-root.crt"
    ca_path.write_text("test certificate fixture", encoding="ascii")
    ca_path.chmod(0o644)
    with pytest.raises(PermissionError, match="owner-only regular file"):
        WebConsoleConfig(config_path).create_ssl_context()
