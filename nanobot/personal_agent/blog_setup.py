"""Host-owned, explicitly approved experimental blog provisioning."""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

import httpx

OWNER = "logolica99"
SOURCE_REPOSITORY = "tech-pulse"
TARGET_REPOSITORY = "tech-pulse-toothless"
TARGET_PROJECT = "tech-pulse-toothless"
GITHUB_API = "https://api.github.com"
VERCEL_API = "https://api.vercel.com"
MAX_SECRET_CHARS = 4096
_SAFE_LABEL = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class CredentialVault(Protocol):
    def set(self, name: str, value: str) -> None: ...
    def get(self, name: str) -> str | None: ...
    def delete(self, name: str) -> None: ...


class MacOSKeychainVault:
    """Use the user's login Keychain API directly; secrets never enter argv."""

    SERVICE = b"com.toothless.blog-provisioning"

    def __init__(self, service: bytes = b"com.toothless.blog-provisioning") -> None:
        if os.name != "posix" or not Path("/System/Library/Frameworks/Security.framework/Security").exists():
            raise RuntimeError("macOS Keychain is required for blog credentials")
        if not service or len(service) > 128 or b"\x00" in service:
            raise ValueError("invalid Keychain service name")
        self._service = service
        self._security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        self._configure()

    def _configure(self) -> None:
        security = self._security
        security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
        security.SecKeychainItemDelete.restype = ctypes.c_int32
        security.SecKeychainItemFreeContent.restype = ctypes.c_int32
        security.SecKeychainFindGenericPassword.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32,
            ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        security.SecKeychainAddGenericPassword.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32,
            ctypes.c_char_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p,
        ]
        security.SecKeychainItemModifyAttributesAndData.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
        ]
        security.SecKeychainItemDelete.argtypes = [ctypes.c_void_p]
        security.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

    @staticmethod
    def _name(name: str) -> bytes:
        if not _SAFE_LABEL.fullmatch(name):
            raise ValueError("invalid credential name")
        return name.encode("ascii")

    def _find(
        self, name: str
    ) -> tuple[int, tuple[ctypes.c_uint32, ctypes.c_void_p] | None, ctypes.c_void_p | None]:
        account = self._name(name)
        length = ctypes.c_uint32()
        data = ctypes.c_void_p()
        item = ctypes.c_void_p()
        status = self._security.SecKeychainFindGenericPassword(
            None, len(self._service), self._service, len(account), account,
            ctypes.byref(length), ctypes.byref(data), ctypes.byref(item),
        )
        return int(status), (length, data) if status == 0 else None, item if status == 0 else None

    def set(self, name: str, value: str) -> None:
        account = self._name(name)
        encoded = value.encode("utf-8")
        if not encoded or len(encoded) > MAX_SECRET_CHARS or b"\x00" in encoded:
            raise ValueError("credential is empty or exceeds the supported size")
        status, _, item = self._find(name)
        buffer = ctypes.create_string_buffer(encoded)
        if status == 0 and item is not None:
            result = self._security.SecKeychainItemModifyAttributesAndData(item, None, len(encoded), buffer)
        else:
            result = self._security.SecKeychainAddGenericPassword(
                None, len(self._service), self._service, len(account), account,
                len(encoded), buffer, None,
            )
        if result != 0:
            raise RuntimeError("macOS Keychain could not store the credential")

    def get(self, name: str) -> str | None:
        status, content, _ = self._find(name)
        if status != 0 or content is None:
            return None
        length, data = content
        try:
            return ctypes.string_at(data, length.value).decode("utf-8")
        finally:
            self._security.SecKeychainItemFreeContent(None, data)

    def delete(self, name: str) -> None:
        status, _, item = self._find(name)
        if status == 0 and item is not None:
            if self._security.SecKeychainItemDelete(item) != 0:
                raise RuntimeError("macOS Keychain could not remove the credential")


class MemoryCredentialVault:
    """Test-only vault; never selected by the deployed controller."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set(self, name: str, value: str) -> None:
        self.values[name] = value

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def delete(self, name: str) -> None:
        self.values.pop(name, None)


@dataclass(frozen=True, slots=True)
class BlogSetupSnapshot:
    github_setup: bool
    github_publish: bool
    vercel: bool
    repository_id: int | None
    baseline_sha: str | None
    project_id: str | None
    project_url: str | None
    deployment_id: str | None
    state: str
    last_error: str | None


class BlogSetupStore:
    """Durable, secret-free setup state and a bounded audit trail."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink() or path.parent.is_symlink():
            raise RuntimeError("blog setup database must not contain symlinks")
        parent = path.parent.stat()
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077:
            raise RuntimeError("blog setup database directory must be owner-only")
        self._db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        os.chmod(path, 0o600)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS blog_setup (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
            "github_setup INTEGER NOT NULL DEFAULT 0, github_publish INTEGER NOT NULL DEFAULT 0, "
            "vercel INTEGER NOT NULL DEFAULT 0, repository_id INTEGER, baseline_sha TEXT, project_id TEXT, "
            "project_url TEXT, deployment_id TEXT, state TEXT NOT NULL DEFAULT 'not_started', last_error TEXT, updated_at REAL NOT NULL)"
        )
        columns = {str(row[1]) for row in self._db.execute("PRAGMA table_info(blog_setup)")}
        if "deployment_id" not in columns:
            self._db.execute("ALTER TABLE blog_setup ADD COLUMN deployment_id TEXT")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS blog_setup_proposals (proposal_id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, "
            "chat_id INTEGER NOT NULL, message_id INTEGER, spec_json TEXT NOT NULL, created_at REAL NOT NULL, "
            "expires_at REAL NOT NULL, consumed_at REAL)"
        )
        self._db.execute("INSERT OR IGNORE INTO blog_setup(singleton,updated_at) VALUES(1,?)", (time.time(),))
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS blog_setup_audit (event_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "event TEXT NOT NULL, detail TEXT NOT NULL, created_at REAL NOT NULL)"
        )

    def snapshot(self) -> BlogSetupSnapshot:
        row = self._db.execute("SELECT * FROM blog_setup WHERE singleton=1").fetchone()
        assert row is not None
        return BlogSetupSnapshot(
            bool(row["github_setup"]), bool(row["github_publish"]), bool(row["vercel"]),
            row["repository_id"], row["baseline_sha"], row["project_id"], row["project_url"],
            row["deployment_id"], str(row["state"]), row["last_error"],
        )

    def update(self, **values: object) -> None:
        allowed = {"github_setup", "github_publish", "vercel", "repository_id", "baseline_sha", "project_id", "project_url", "deployment_id", "state", "last_error"}
        if not values or set(values) - allowed:
            raise ValueError("unsupported setup state update")
        assignments = ",".join(f"{key}=?" for key in values)
        self._db.execute(
            f"UPDATE blog_setup SET {assignments},updated_at=? WHERE singleton=1",
            (*values.values(), time.time()),
        )

    def audit(self, event: str, detail: str) -> None:
        if not _SAFE_LABEL.fullmatch(event) or len(detail) > 300 or re.search(r"(?:gh[pousr]_|vercel_|Bearer\s|token=)", detail, re.I):
            raise ValueError("audit detail is not safe")
        self._db.execute("INSERT INTO blog_setup_audit(event,detail,created_at) VALUES(?,?,?)", (event, detail, time.time()))
        self._db.execute("DELETE FROM blog_setup_audit WHERE event_id NOT IN (SELECT event_id FROM blog_setup_audit ORDER BY event_id DESC LIMIT 200)")

    def create_proposal(self, owner_id: int, chat_id: int, spec: dict[str, str], *, ttl: int = 600) -> str:
        required = {"owner", "source", "target", "project", "visibility", "stage"}
        fixed = {
            "owner": OWNER, "source": f"{OWNER}/{SOURCE_REPOSITORY}",
            "target": f"{OWNER}/{TARGET_REPOSITORY}", "project": TARGET_PROJECT, "visibility": "private",
        }
        extra = {"baseline_sha"} if spec.get("stage") == "create_repository" else {"baseline_sha", "repository_id"}
        if not required <= set(spec) or set(spec) - (required | extra) or any(spec.get(key) != value for key, value in fixed.items()) or spec.get("stage") not in {"create_repository", "copy_and_deploy"}:
            raise ValueError("proposal does not match the fixed experimental destination")
        if spec["stage"] == "create_repository" and not re.fullmatch(r"[0-9a-f]{40,64}", spec.get("baseline_sha", "")):
            raise ValueError("repository proposal must bind the exact source baseline")
        if spec["stage"] == "copy_and_deploy" and (not re.fullmatch(r"[0-9a-f]{40,64}", spec.get("baseline_sha", "")) or not spec.get("repository_id", "").isdecimal()):
            raise ValueError("deployment proposal must bind the recorded repository and source baseline")
        proposal_id = "blog-" + os.urandom(16).hex()
        self._db.execute(
            "INSERT INTO blog_setup_proposals(proposal_id,owner_id,chat_id,spec_json,created_at,expires_at) VALUES(?,?,?,?,?,?)",
            (proposal_id, owner_id, chat_id, json.dumps(spec, sort_keys=True), time.time(), time.time() + ttl),
        )
        return proposal_id

    def consume_proposal(self, proposal_id: str, owner_id: int, chat_id: int, message_id: int) -> dict[str, str] | None:
        now = time.time()
        self._db.execute("BEGIN IMMEDIATE")
        row = self._db.execute(
            "SELECT spec_json FROM blog_setup_proposals WHERE proposal_id=? AND owner_id=? AND chat_id=? AND message_id=? AND expires_at>? AND consumed_at IS NULL",
            (proposal_id, owner_id, chat_id, message_id, now),
        ).fetchone()
        if row is None:
            self._db.rollback()
            return None
        self._db.execute("UPDATE blog_setup_proposals SET consumed_at=? WHERE proposal_id=? AND consumed_at IS NULL", (now, proposal_id))
        self._db.commit()
        raw_value: object = json.loads(str(row[0]))
        if not isinstance(raw_value, dict):
            return None
        raw = cast(dict[str, object], raw_value)
        if any(raw.get(key) != value for key, value in {
            "owner": OWNER, "source": f"{OWNER}/{SOURCE_REPOSITORY}",
            "target": f"{OWNER}/{TARGET_REPOSITORY}", "project": TARGET_PROJECT, "visibility": "private",
        }.items()) or raw.get("stage") not in {"create_repository", "copy_and_deploy"}:
            return None
        if raw.get("stage") == "create_repository" and not re.fullmatch(r"[0-9a-f]{40,64}", str(raw.get("baseline_sha", ""))):
            return None
        if raw.get("stage") == "copy_and_deploy" and (not re.fullmatch(r"[0-9a-f]{40,64}", str(raw.get("baseline_sha", ""))) or not str(raw.get("repository_id", "")).isdecimal()):
            return None
        return {str(key): str(value) for key, value in raw.items()}

    def bind_proposal(self, proposal_id: str, message_id: int) -> None:
        self._db.execute("UPDATE blog_setup_proposals SET message_id=? WHERE proposal_id=? AND consumed_at IS NULL", (message_id, proposal_id))

    def cancel_proposals(self) -> None:
        self._db.execute("UPDATE blog_setup_proposals SET consumed_at=? WHERE consumed_at IS NULL", (time.time(),))

    def close(self) -> None:
        self._db.close()


class ProviderError(RuntimeError):
    """Sanitized provider failure suitable for owner-facing status."""


class BlogProviderClient:
    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5), follow_redirects=False)
        self._owns_client = client is None

    async def _request(self, method: str, url: str, token: str, *, json_body: dict[str, object] | None = None, params: dict[str, str] | None = None) -> dict[str, object]:
        try:
            response = await self._client.request(
                method, url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "Toothless-Blog-Setup"},
                json=json_body, params=params,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"provider network error ({type(exc).__name__})") from None
        if response.status_code >= 400:
            category = {400: "provider rejected setup; verify the GitHub App authorization in Vercel and the project settings", 401: "credential rejected", 403: "permission or account policy denied the operation", 404: "resource not found", 409: "resource conflict", 429: "provider rate limit"}.get(response.status_code, "provider request failed")
            raise ProviderError(f"{category} (HTTP {response.status_code})")
        try:
            raw_value: object = response.json()
        except ValueError:
            raise ProviderError("provider returned an invalid response") from None
        if not isinstance(raw_value, dict):
            raise ProviderError("provider returned an invalid response")
        return cast(dict[str, object], raw_value)

    async def validate_github(self, token: str) -> str:
        data = await self._request("GET", f"{GITHUB_API}/user", token)
        login = data.get("login")
        if login != OWNER:
            raise ProviderError(f"GitHub account mismatch; expected {OWNER}")
        return str(login)

    async def validate_vercel(self, token: str) -> str:
        data = await self._request("GET", f"{VERCEL_API}/v2/user", token)
        user_value = data.get("user")
        user = cast(dict[str, object], user_value) if isinstance(user_value, dict) else data
        username = user.get("username")
        if username != OWNER:
            raise ProviderError(f"Vercel account mismatch; expected personal account {OWNER}")
        return str(username)

    async def github_repository(self, token: str, repository: str) -> dict[str, object] | None:
        try:
            return await self.inspect_github_repository(token, repository)
        except ProviderError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise

    async def github_default_commit(self, token: str, repository: str) -> tuple[str, str]:
        repo = await self.inspect_github_repository(token, repository)
        branch = repo.get("default_branch")
        if not isinstance(branch, str) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", branch):
            raise ProviderError("GitHub source repository has no valid default branch")
        owner, name = _repository(repository)
        commit = await self._request("GET", f"{GITHUB_API}/repos/{owner}/{name}/commits/{branch}", token)
        sha = commit.get("sha")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            raise ProviderError("GitHub source returned an invalid baseline commit")
        return branch, sha

    async def github_default_commit_or_none(self, token: str, repository: str) -> tuple[str, str] | None:
        repo = await self.inspect_github_repository(token, repository)
        branch = repo.get("default_branch")
        if not isinstance(branch, str) or not branch:
            return None
        owner, name = _repository(repository)
        try:
            commit = await self._request("GET", f"{GITHUB_API}/repos/{owner}/{name}/commits/{branch}", token)
        except ProviderError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        sha = commit.get("sha")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            raise ProviderError("GitHub target returned an invalid default commit")
        return branch, sha

    async def create_github_repository(self, token: str) -> dict[str, object]:
        result = await self._request("POST", f"{GITHUB_API}/user/repos", token, json_body={
            "name": TARGET_REPOSITORY, "private": True,
            "description": "Independent experimental Toothless copy of tech-pulse.",
            "auto_init": False,
        })
        owner_value = result.get("owner")
        owner = cast(dict[str, object], owner_value) if isinstance(owner_value, dict) else {}
        login = owner.get("login")
        if login != OWNER or result.get("name") != TARGET_REPOSITORY or result.get("private") is not True:
            raise ProviderError("GitHub created a repository outside the approved owner/name/visibility")
        return result

    async def create_vercel_project(self, token: str) -> dict[str, object]:
        result = await self._request("POST", f"{VERCEL_API}/v11/projects", token, json_body={
            "name": TARGET_PROJECT,
            "gitRepository": {"type": "github", "repo": f"{OWNER}/{TARGET_REPOSITORY}"},
        })
        if result.get("name") != TARGET_PROJECT or result.get("id") is None:
            raise ProviderError("Vercel response did not match the approved project name")
        link_value = result.get("link")
        link = cast(dict[str, object], link_value) if isinstance(link_value, dict) else None
        if link is not None and link.get("repo") not in {None, TARGET_REPOSITORY}:
            raise ProviderError("Vercel linked a repository other than the approved experimental repository")
        return result

    async def latest_vercel_deployment(self, token: str, project_id: str) -> dict[str, object] | None:
        result = await self._request("GET", f"{VERCEL_API}/v6/deployments", token,
                                    params={"projectId": project_id, "limit": "1"})
        items_value = result.get("deployments")
        if not isinstance(items_value, list) or not items_value or not isinstance(items_value[0], dict):
            return None
        return cast(dict[str, object], items_value[0])

    async def inspect_github_repository(self, token: str, repository: str) -> dict[str, object]:
        owner, name = _repository(repository)
        return await self._request("GET", f"{GITHUB_API}/repos/{owner}/{name}", token)

    async def inspect_vercel_project(self, token: str, name: str) -> dict[str, object] | None:
        try:
            return await self._request("GET", f"{VERCEL_API}/v9/projects/{name}", token)
        except ProviderError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise

    async def get_deployment_homepage(self, url: str) -> httpx.Response:
        try:
            return await self._client.get(url, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise ProviderError(f"experimental deployment homepage check failed ({type(exc).__name__})") from None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class BlogProvisioner:
    """Fixed-destination resource workflow; callers must consume owner approval first."""

    def __init__(self, store: BlogSetupStore, vault: CredentialVault, provider: BlogProviderClient | None = None) -> None:
        self.store = store
        self.vault = vault
        self.provider = provider or BlogProviderClient()

    def _credential(self, name: str) -> str:
        value = self.vault.get(name)
        if not value:
            raise ProviderError(f"Connect {name.replace('_', ' ')} before setup")
        return value

    async def create_repository_approved(self, baseline_sha: str) -> BlogSetupSnapshot:
        state = self.store.snapshot()
        if not state.github_setup:
            raise ProviderError("Connect the GitHub repository-creation credential first")
        setup_token = self._credential("github_setup")
        await self.provider.validate_github(setup_token)
        _branch, current_sha = await self.provider.github_default_commit(setup_token, f"{OWNER}/{SOURCE_REPOSITORY}")
        if current_sha != baseline_sha:
            raise ProviderError("Source baseline changed after approval; review a fresh setup proposal")
        source = await self.provider.inspect_github_repository(setup_token, f"{OWNER}/{SOURCE_REPOSITORY}")
        source_id = source.get("id")
        if type(source_id) is not int or source.get("name") != SOURCE_REPOSITORY:
            raise ProviderError("GitHub source repository identity could not be verified")
        existing = await self.provider.github_repository(setup_token, f"{OWNER}/{TARGET_REPOSITORY}")
        if state.repository_id is None:
            if existing is not None:
                raise ProviderError("The target repository already exists but is not recorded by Toothless; refusing to adopt or overwrite it")
            self.store.update(state="creating_repository", last_error=None)
            created = await self.provider.create_github_repository(setup_token)
            repository_id = created.get("id")
            if type(repository_id) is not int or repository_id <= 0 or repository_id == source_id:
                raise ProviderError("GitHub did not return a permanent repository identity")
            self.store.update(repository_id=repository_id, baseline_sha=baseline_sha, state="repository_created")
            self.store.audit("repository_created", f"Created private {OWNER}/{TARGET_REPOSITORY}; source baseline pending push")
        elif existing is None or existing.get("id") != state.repository_id or existing.get("id") == source_id:
            raise ProviderError("Recorded experimental repository identity no longer matches GitHub")
        return self.store.snapshot()

    async def prepare_repository(self, owner_id: int, chat_id: int) -> tuple[str, str, str]:
        token = self._credential("github_setup")
        await self.provider.validate_github(token)
        _branch, baseline = await self.provider.github_default_commit(token, f"{OWNER}/{SOURCE_REPOSITORY}")
        existing = await self.provider.github_repository(token, f"{OWNER}/{TARGET_REPOSITORY}")
        state = self.store.snapshot()
        if existing is not None and (state.repository_id is None or existing.get("id") != state.repository_id):
            raise ProviderError("The experimental repository already exists but is not recorded by Toothless")
        if existing is not None:
            raise ProviderError("The approved experimental repository already exists; use Continue Setup")
        proposal_id = self.store.create_proposal(owner_id, chat_id, {
            "owner": OWNER, "source": f"{OWNER}/{SOURCE_REPOSITORY}",
            "target": f"{OWNER}/{TARGET_REPOSITORY}", "project": TARGET_PROJECT,
            "visibility": "private", "stage": "create_repository", "baseline_sha": baseline,
        })
        return proposal_id, baseline, _branch

    async def prepare_copy_and_deploy(self, owner_id: int, chat_id: int) -> str:
        state = self.store.snapshot()
        if not state.repository_id or not state.baseline_sha:
            raise ProviderError("The experimental repository is not ready; create it first")
        publish = self._credential("github_publish")
        vercel = self._credential("vercel")
        await self.provider.validate_github(publish)
        await self.provider.validate_vercel(vercel)
        source = await self.provider.inspect_github_repository(publish, f"{OWNER}/{SOURCE_REPOSITORY}")
        if type(source.get("id")) is not int or source.get("id") == state.repository_id:
            raise ProviderError("GitHub source and experimental repository identities are not independent")
        project = await self.provider.inspect_vercel_project(vercel, TARGET_PROJECT)
        if project is not None and (state.project_id is None or project.get("id") != state.project_id):
            raise ProviderError("The Vercel project already exists but is not recorded by Toothless")
        proposal_id = self.store.create_proposal(owner_id, chat_id, {
            "owner": OWNER, "source": f"{OWNER}/{SOURCE_REPOSITORY}",
            "target": f"{OWNER}/{TARGET_REPOSITORY}", "project": TARGET_PROJECT,
            "visibility": "private", "stage": "copy_and_deploy",
            "baseline_sha": state.baseline_sha, "repository_id": str(state.repository_id),
        })
        return proposal_id

    async def copy_and_deploy_approved(self) -> BlogSetupSnapshot:
        state = self.store.snapshot()
        if not state.repository_id or not state.baseline_sha:
            raise ProviderError("The approved experimental repository has not been created")
        if not state.github_publish or not state.vercel:
            raise ProviderError("Connect the target-only GitHub publishing credential and Vercel credential first")
        setup_token = self._credential("github_setup")
        publish_token = self._credential("github_publish")
        vercel_token = self._credential("vercel")
        await self.provider.validate_github(publish_token)
        await self.provider.validate_vercel(vercel_token)
        target = await self.provider.github_repository(publish_token, f"{OWNER}/{TARGET_REPOSITORY}")
        if target is None or target.get("id") != state.repository_id or target.get("private") is not True:
            raise ProviderError("Recorded experimental repository identity/visibility no longer matches GitHub")
        source = await self.provider.inspect_github_repository(setup_token, f"{OWNER}/{SOURCE_REPOSITORY}")
        if type(source.get("id")) is not int or source.get("id") == state.repository_id:
            raise ProviderError("GitHub source and experimental repository identities are not independent")
        branch, current_sha = await self.provider.github_default_commit(setup_token, f"{OWNER}/{SOURCE_REPOSITORY}")
        if current_sha != state.baseline_sha:
            raise ProviderError("Source has moved since repository creation; preserve the recorded baseline and request a reviewed update")
        target_head = await self.provider.github_default_commit_or_none(publish_token, f"{OWNER}/{TARGET_REPOSITORY}")
        if target_head is None:
            self._mirror_source(branch, state.baseline_sha, publish_token)
        elif target_head[1] != state.baseline_sha:
            raise ProviderError("Experimental target has a different commit; refusing to overwrite it")
        target_head = await self.provider.github_default_commit_or_none(publish_token, f"{OWNER}/{TARGET_REPOSITORY}")
        if target_head is None or target_head[1] != state.baseline_sha:
            raise ProviderError("Experimental repository does not match the approved source baseline")
        self.store.update(state="repository_copied")
        project = await self.provider.inspect_vercel_project(vercel_token, TARGET_PROJECT)
        if project is None:
            self.store.update(state="creating_vercel_project")
            project = await self.provider.create_vercel_project(vercel_token)
        elif state.project_id is None or project.get("id") != state.project_id:
            raise ProviderError("The Vercel target project already exists but is not recorded by Toothless; refusing to adopt or modify it")
        project_id_value = project.get("id")
        if not isinstance(project_id_value, str) or not project_id_value:
            raise ProviderError("Vercel did not return a project identity")
        project_url = f"https://{TARGET_PROJECT}.vercel.app"
        domains = project.get("domains")
        if not isinstance(domains, list) or project_url.removeprefix("https://") not in domains:
            raise ProviderError("Vercel did not confirm the expected experimental vercel.app domain")
        self.store.update(project_id=project_id_value, project_url=project_url, state="project_created", last_error=None)
        deployment: dict[str, object] | None = None
        for attempt in range(24):
            deployment = await self.provider.latest_vercel_deployment(vercel_token, project_id_value)
            status = deployment.get("readyState") if deployment else None
            if status in {"READY", "ERROR", "CANCELED"}:
                break
            await asyncio.sleep(5 if attempt < 3 else 10)
        if deployment is None or deployment.get("readyState") != "READY":
            state_name = str(deployment.get("readyState", "not found")) if deployment else "not found"
            self.store.update(state="deployment_not_ready", last_error=f"Vercel deployment state: {state_name}")
            raise ProviderError(f"Initial Vercel deployment did not reach READY (state: {state_name}); inspect the Vercel project before retrying")
        deployment_id = deployment.get("uid") or deployment.get("id")
        deployment_url = deployment.get("url")
        if not isinstance(deployment_id, str) or not isinstance(deployment_url, str) or not re.fullmatch(r"[A-Za-z0-9.-]+\.vercel\.app", deployment_url):
            raise ProviderError("Vercel READY response did not contain a verified deployment identity/URL")
        homepage = await self.provider.get_deployment_homepage(f"https://{deployment_url}")
        if homepage.status_code != 200 or homepage.url.host != deployment_url:
            raise ProviderError("experimental deployment homepage did not return HTTP 200 on the verified Vercel deployment host")
        self.store.update(deployment_id=deployment_id, project_id=project_id_value, project_url=project_url,
                          state="deployed_ready", last_error=None)
        self.store.audit("deployment_ready", f"Vercel deployment {deployment_id} reached READY and returned HTTP 200")
        return self.store.snapshot()

    def _mirror_source(self, baseline_branch: str, baseline_sha: str, publish_token: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", baseline_branch):
            raise ProviderError("GitHub source branch is invalid")
        with tempfile.TemporaryDirectory(prefix="toothless-blog-") as raw_temp:
            directory = Path(raw_temp)
            os.chmod(directory, 0o700)
            askpass = directory / "askpass.py"
            askpass.write_text(
                f"#!{sys.executable}\nimport os,sys\nfrom nanobot.personal_agent.blog_setup import MacOSKeychainVault\n"
                "p=sys.argv[1].lower() if len(sys.argv)>1 else ''\n"
                "print('x-access-token' if 'username' in p else (MacOSKeychainVault().get(os.environ['TOOTHLESS_CREDENTIAL_NAME']) or ''))\n",
                encoding="utf-8",
            )
            os.chmod(askpass, 0o700)
            clone = subprocess.run(
                ["git", "clone", "--mirror", f"https://github.com/{OWNER}/{SOURCE_REPOSITORY}.git", str(directory / "mirror.git")],
                cwd=directory, env={**os.environ, "HOME": str(directory), "GIT_CONFIG_GLOBAL": os.devnull,
                                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"},
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180, check=False,
            )
            if clone.returncode != 0:
                raise ProviderError("Could not clone the public source repository; target was not modified")
            verify = subprocess.run(
                ["git", "--git-dir", str(directory / "mirror.git"), "rev-parse", f"refs/heads/{baseline_branch}"],
                cwd=directory, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, timeout=15, check=False,
            )
            if verify.returncode != 0 or verify.stdout.strip() != baseline_sha:
                raise ProviderError("Cloned source changed from the recorded baseline; no target push was attempted")
            env = {
                **os.environ, "HOME": str(directory), "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": str(askpass), "TOOTHLESS_CREDENTIAL_NAME": "github_publish",
            }
            push = subprocess.run(
                ["git", "--git-dir", str(directory / "mirror.git"), "push", "--all",
                 f"https://github.com/{OWNER}/{TARGET_REPOSITORY}.git"],
                cwd=directory, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=180, check=False,
            )
            if push.returncode != 0:
                raise ProviderError("Could not push source history to the experimental repository; source was never written")
            tags = subprocess.run(
                ["git", "--git-dir", str(directory / "mirror.git"), "push", "--tags",
                 f"https://github.com/{OWNER}/{TARGET_REPOSITORY}.git"],
                cwd=directory, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=180, check=False,
            )
            if tags.returncode != 0:
                raise ProviderError("Source branches copied but tag push failed; inspect the experimental repository before retrying")


def _repository(value: str) -> tuple[str, str]:
    match = re.fullmatch(r"([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+)", value)
    if match is None:
        raise ValueError("invalid repository identity")
    return match.group(1), match.group(2)
