"""Authenticated localhost-only API and static UI for the host memory graph."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import ipaddress
import logging
import os
import re
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from aiohttp import web
from argon2 import PasswordHasher
from argon2 import exceptions as argon2_errors

from nanobot.personal_agent.blog_setup import (
    MAX_SECRET_CHARS,
    BlogProviderClient,
    BlogSetupStore,
    CredentialVault,
    MacOSKeychainVault,
    ProviderError,
)
from nanobot.personal_agent.memory_store import (
    EntityType,
    GraphEntity,
    GraphRelationship,
    GraphSearchResult,
    MemoryStore,
)
from nanobot.personal_agent.remote_sessions import RemoteSessionStore
from nanobot.personal_agent.supabase_store import SupabaseWebStore, owner_uuid
from nanobot.personal_agent.task_store import TaskStore
from nanobot.personal_agent.web_console_config import WebConsoleConfig

LOGGER = logging.getLogger("toothless.memory_explorer")
STATIC_ROOT = Path(__file__).with_name("memory_explorer_static")
MAX_QUERY = 200
MAX_GRAPH_NODES = 64
_ALLOWED_TYPES = {item.value for item in EntityType}
_TOKEN_KEY = web.AppKey("token", str)
_MEMORY_STORE_KEY = web.AppKey("memory_store", MemoryStore)
_TASK_STORE_KEY = web.AppKey("task_store", TaskStore)
_SESSION_KEY = web.AppKey("sessions", dict[str, tuple[float, int]])
_BLOG_CSRF_KEY = web.AppKey("blog_csrf", dict[str, str])
_BLOG_STORE_KEY = web.AppKey("blog_store", object)
_BLOG_VAULT_KEY = web.AppKey("blog_vault", object)
_APP_CSRF_KEY = web.AppKey("app_csrf", dict[str, str])
_WEB_STORE_KEY = web.AppKey("web_store", object)
_WEB_STORE_STATE_KEY = web.AppKey("web_store_state", str)
_WEB_VAULT_KEY = web.AppKey("web_vault", object)
_WEB_OWNER_KEY = web.AppKey("web_owner", object)
_WEB_CONFIG_KEY = web.AppKey("web_config", object)
_WEB_PERSISTENT_SESSION = web.RequestKey[bool]("web_persistent_session")
REMOTE_ORIGIN = os.environ.get(
    "TOOTHLESS_REMOTE_ORIGIN", "https://toothless.invalid"
).rstrip("/")
REMOTE_BASE_PATH = os.environ.get("TOOTHLESS_MEMORY_BASE_PATH", "/memory").rstrip("/")
REMOTE_SESSION_SECONDS = 1800
WEB_SESSION_SECONDS = 12 * 60 * 60
_PASSWORDS = PasswordHasher()
_DUMMY_PASSWORD_HASH = _PASSWORDS.hash(secrets.token_urlsafe(32))
_WEB_USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$")
_WEB_STORE_RETRY_SECONDS = 5


class _WebStoreLifecycle(Protocol):
    async def connect(self) -> None: ...

    async def verify_schema(self) -> None: ...

    async def close(self) -> None: ...


async def _maintain_web_store_connection(
    app: web.Application, store: _WebStoreLifecycle, *, retry_seconds: float = _WEB_STORE_RETRY_SECONDS,
) -> None:
    """Keep the configured store available after temporary startup/network failures."""
    delay = retry_seconds
    while True:
        await asyncio.sleep(delay)
        try:
            await store.connect()
            await store.verify_schema()
        except Exception as exc:
            app[_WEB_STORE_KEY] = None
            app[_WEB_STORE_STATE_KEY] = f"retrying:{type(exc).__name__}"
            LOGGER.warning("Supabase web store unavailable; retrying (error_type=%s)", type(exc).__name__)
            try:
                await store.close()
            except Exception as close_exc:
                LOGGER.warning("Supabase web store cleanup failed (error_type=%s)", type(close_exc).__name__)
            delay = min(delay * 2, 60)
            continue
        app[_WEB_STORE_KEY] = store
        app[_WEB_STORE_STATE_KEY] = "connected"
        return


async def _connect_web_store_once(app: web.Application, store: _WebStoreLifecycle) -> bool:
    try:
        await store.connect()
        await store.verify_schema()
    except Exception as exc:
        app[_WEB_STORE_KEY] = None
        app[_WEB_STORE_STATE_KEY] = f"retrying:{type(exc).__name__}"
        LOGGER.warning("Supabase web store unavailable; retrying (error_type=%s)", type(exc).__name__)
        try:
            await store.close()
        except Exception as close_exc:
            LOGGER.warning("Supabase web store cleanup failed (error_type=%s)", type(close_exc).__name__)
        return False
    app[_WEB_STORE_KEY] = store
    app[_WEB_STORE_STATE_KEY] = "connected"
    return True


def _task_store_path() -> Path:
    raw = os.environ.get("TOOTHLESS_TASK_DB")
    if raw:
        return Path(raw).expanduser()
    update_path = Path(os.environ.get(
        "TOOTHLESS_TELEGRAM_DB", "~/.local/share/toothless/telegram/updates.sqlite3"
    )).expanduser()
    return update_path.parent / "tasks.sqlite3"


def _load_web_console_runtime(
    web_config: WebConsoleConfig, owner_raw: str,
) -> tuple[CredentialVault | None, uuid.UUID | None, SupabaseWebStore | None, str]:
    """Load the DB runtime independently from optional legacy Keychain support."""
    try:
        web_vault: CredentialVault | None = MacOSKeychainVault(service=b"com.toothless.supabase")
    except (OSError, RuntimeError, ValueError) as exc:
        LOGGER.warning("Legacy Supabase Keychain migration unavailable (error_type=%s)", type(exc).__name__)
        web_vault = None

    web_owner = owner_uuid(int(owner_raw)) if owner_raw.isdigit() else None
    web_store: SupabaseWebStore | None = None
    state = "missing_dsn_or_owner"
    try:
        runtime_dsn = web_config.get_dsn()
        if runtime_dsn and web_owner is not None:
            web_store = SupabaseWebStore(runtime_dsn, web_owner, ssl_context=web_config.create_ssl_context())
            state = "configured"
    except (OSError, RuntimeError, ValueError, PermissionError) as exc:
        LOGGER.warning("Owner-only web console configuration is unavailable (error_type=%s)", type(exc).__name__)
        state = f"configuration_error:{type(exc).__name__}"
    return web_vault, web_owner, web_store, state


def create_app(
    token: str, memory_store: MemoryStore, task_store: TaskStore,
    *, remote_store: RemoteSessionStore | None = None, remote_origin: str = REMOTE_ORIGIN,
    base_path: str = REMOTE_BASE_PATH, blog_store: BlogSetupStore | None = None,
    blog_vault: CredentialVault | None = None, blog_provider: BlogProviderClient | None = None,
    web_store: SupabaseWebStore | None = None, web_vault: CredentialVault | None = None,
    web_owner: Any = None, web_config: WebConsoleConfig | None = None,
    web_store_state: str = "not_configured",
) -> web.Application:
    if len(token) < 32:
        raise ValueError("explorer token must contain at least 32 characters")
    app = web.Application(client_max_size=16 * 1024)
    app[_TOKEN_KEY] = token
    app[_MEMORY_STORE_KEY] = memory_store
    app[_TASK_STORE_KEY] = task_store
    app[_SESSION_KEY] = {}
    app[_BLOG_CSRF_KEY] = {}
    app[_BLOG_STORE_KEY] = blog_store
    app[_BLOG_VAULT_KEY] = blog_vault
    app[_APP_CSRF_KEY] = {}
    app[_WEB_STORE_KEY] = web_store
    app[_WEB_STORE_STATE_KEY] = "connecting" if web_store is not None else web_store_state
    app[_WEB_VAULT_KEY] = web_vault
    app[_WEB_OWNER_KEY] = web_owner
    app[_WEB_CONFIG_KEY] = web_config
    base_path = base_path.rstrip("/") if remote_store is not None else ""

    @web.middleware
    async def local_only(request: web.Request, handler: Any) -> web.StreamResponse:
        peer = request.transport.get_extra_info("peername") if request.transport else None
        try:
            if not peer or not ipaddress.ip_address(peer[0]).is_loopback:
                raise web.HTTPForbidden(text="localhost access only")
        except ValueError as exc:
            raise web.HTTPForbidden(text="localhost access only") from exc
        host_header = request.headers.get("Host", "")
        host = host_header.split(":", 1)[0].strip("[]").lower()
        remote_host = urlsplit(remote_origin).netloc.lower()
        if host not in {"127.0.0.1", "localhost", "::1"} and host_header.lower() != remote_host:
            raise web.HTTPMisdirectedRequest(text="invalid host")
        origin = request.headers.get("Origin")
        local_origins = {f"http://{host_header}", "http://localhost:8768", "http://127.0.0.1:8768"}
        if origin and origin not in local_origins | {remote_origin}:
            raise web.HTTPForbidden(text="cross-origin requests are not allowed")
        response = await handler(request)
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    app.middlewares.append(local_only)

    def require_same_origin(request: web.Request) -> None:
        origin = request.headers.get("Origin")
        allowed = {remote_origin, f"http://{request.headers.get('Host', '')}", "http://localhost:8768", "http://127.0.0.1:8768"}
        if origin not in allowed:
            raise web.HTTPForbidden(text="same-origin request required")

    @web.middleware
    async def authenticate(request: web.Request, handler: Any) -> web.StreamResponse:
        path = _local_path(request.path, base_path)
        if path is None:
            raise web.HTTPNotFound()
        if path.startswith("/app/api/"):
            if path in {"/app/api/auth/login", "/app/api/auth/bootstrap", "/app/api/auth/session"}:
                return await handler(request)
            supplied = request.cookies.get("toothless_web", "")
            csrf_cookie = request.cookies.get("toothless_web_csrf", "")
            token_hash = _hash(supplied) if supplied else ""
            store = cast(SupabaseWebStore | None, app[_WEB_STORE_KEY])
            persistent_session = bool(
                supplied and csrf_cookie and store is not None
                and await store.valid_web_session(token_hash, _hash(csrf_cookie))
            )
            session = app[_SESSION_KEY].get(token_hash) if supplied else None
            generation = remote_store.epoch() if remote_store is not None else 0
            legacy_session = bool(session and session[0] > time.time() and session[1] == generation)
            if not persistent_session and not legacy_session:
                raise web.HTTPUnauthorized()
            request[_WEB_PERSISTENT_SESSION] = persistent_session
        elif path.startswith("/api/"):
            supplied = request.headers.get("Authorization", "")
            cookie = request.cookies.get("toothless_memory", "")
            session = app[_SESSION_KEY].get(_hash(cookie)) if cookie else None
            generation = remote_store.epoch() if remote_store is not None else 0
            if session and (session[0] <= time.time() or session[1] != generation):
                app[_SESSION_KEY].pop(_hash(cookie), None)
                session = None
            bearer_ok = supplied.startswith("Bearer ") and hmac.compare_digest(supplied[7:], app[_TOKEN_KEY])
            remote_request = request.headers.get("Origin") == remote_origin
            if not session and not (bearer_ok and not remote_request):
                raise web.HTTPUnauthorized()
        if path.startswith("/blog-setup/api/"):
            supplied = request.cookies.get("toothless_blog_setup", "")
            session = app[_SESSION_KEY].get(_hash(supplied)) if supplied else None
            generation = remote_store.epoch() if remote_store is not None else 0
            if not session or session[0] <= time.time() or session[1] != generation:
                raise web.HTTPUnauthorized()
        return await handler(request)

    app.middlewares.append(authenticate)

    async def index(_request: web.Request) -> web.Response:
        content = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
        content = content.replace("__BASE_PATH__", base_path)
        return web.Response(text=content, content_type="text/html")

    async def web_console_page(_request: web.Request) -> web.Response:
        content = (STATIC_ROOT / "web_console.html").read_text(encoding="utf-8")
        content = content.replace("__BASE_PATH__", base_path)
        return web.Response(text=content, content_type="text/html")

    async def web_console_setup_page(_request: web.Request) -> web.Response:
        store = cast(SupabaseWebStore | None, app[_WEB_STORE_KEY])
        if store is None:
            raise web.HTTPServiceUnavailable(
                text="Supabase runtime connection is not ready",
                headers={"X-Toothless-Store-State": app[_WEB_STORE_STATE_KEY]},
            )
        if await store.has_admin():
            raise web.HTTPNotFound(text="Administrator setup is already complete")
        content = (STATIC_ROOT / "web_console_setup.html").read_text(encoding="utf-8")
        content = content.replace("__BASE_PATH__", base_path)
        return web.Response(text=content, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def static_asset(request: web.Request) -> web.FileResponse:
        name = request.match_info["name"]
        if name not in {"carbon.css", "app.js", "app.css", "app-extras.css", "cytoscape.umd.js", "CYTOSCAPE-LICENSE.txt", "blog_setup.js", "blog_setup.css", "web_console.js", "web_console.css", "web_console_setup.js"}:
            raise web.HTTPNotFound()
        return web.FileResponse(STATIC_ROOT / name)

    def entity_json(entity: GraphEntity) -> dict[str, str]:
        return {
            "id": entity.entity_id, "type": entity.entity_type.value, "label": entity.label,
            "state": entity.state.value, "source_type": entity.source_type, "source_id": entity.source_id,
            "created_at": entity.created_at, "updated_at": entity.updated_at,
        }

    def relationship_json(edge: GraphRelationship) -> dict[str, str]:
        return {
            "id": edge.relationship_id, "source": edge.source_entity_id,
            "target": edge.target_entity_id, "type": edge.relationship_type.value,
            "source_type": edge.source_type, "source_id": edge.source_id,
            "created_at": edge.created_at, "updated_at": edge.updated_at,
        }

    def graph_json(result: Any, *, type_filter: str = "") -> dict[str, object]:
        entities = (*result.matched, *result.connected)
        if type_filter:
            entities = tuple(entity for entity in entities if entity.entity_type.value == type_filter)
        ids = {entity.entity_id for entity in entities}
        edges = tuple(edge for edge in result.relationships if edge.source_entity_id in ids and edge.target_entity_id in ids)
        return {
            "nodes": [entity_json(entity) for entity in entities],
            "edges": [relationship_json(edge) for edge in edges],
            "source_ids": list(result.source_ids[:64]),
            "bounded": True,
        }

    async def projects(_request: web.Request) -> web.Response:
        rows = [item for item in memory_store.graph_entities(limit=500) if item.entity_type is EntityType.PROJECT]
        payload: list[dict[str, str]] = []
        for row in rows:
            project = memory_store.get_project(row.external_id)
            if project is not None:
                payload.append({"id": project.project_id, "name": project.name, "description": project.description})
        return web.json_response({"projects": payload[:100]})

    async def graph(request: web.Request) -> web.Response:
        query = request.query.get("q", "")[:MAX_QUERY]
        type_filter = request.query.get("type", "")
        project_id = request.query.get("project", "")
        depth = _bounded_int(request.query.get("depth", "2"), 1, 3, 2)
        limit = _bounded_int(request.query.get("limit", "32"), 1, MAX_GRAPH_NODES, 32)
        if type_filter and type_filter not in _ALLOWED_TYPES:
            raise web.HTTPBadRequest(text="unsupported entity filter")
        if project_id:
            project = memory_store.get_project(project_id)
            if project is None:
                raise web.HTTPBadRequest(text="unknown project filter")
            base = memory_store.graph_search(project.name, max_depth=3, max_nodes=limit)
            entities = (*base.matched, *base.connected)
            if query.strip():
                terms = set(re.findall(r"[\w-]+", query.casefold()))
                matches = tuple(
                    item for item in entities
                    if terms <= set(re.findall(r"[\w-]+", item.label.casefold()))
                )
                included = {item.entity_id: item for item in matches}
                frontier = set(included)
                relationships: dict[str, GraphRelationship] = {}
                available = {item.entity_id: item for item in entities}
                for _ in range(depth):
                    if not frontier or len(included) >= limit:
                        break
                    next_frontier: set[str] = set()
                    for edge in base.relationships:
                        if edge.source_entity_id not in frontier and edge.target_entity_id not in frontier:
                            continue
                        relationships[edge.relationship_id] = edge
                        for entity_id in (edge.source_entity_id, edge.target_entity_id):
                            if entity_id in included or entity_id not in available or len(included) >= limit:
                                continue
                            included[entity_id] = available[entity_id]
                            next_frontier.add(entity_id)
                    frontier = next_frontier
                result = GraphSearchResult(
                    matches, tuple(item for key, item in included.items() if key not in {m.entity_id for m in matches}),
                    tuple(relationships.values()), tuple(base.source_ids[:64]),
                )
            else:
                result = base
        elif query.strip():
            result = memory_store.graph_search(query, max_depth=depth, max_nodes=limit)
        else:
            result = memory_store.graph_search("mechanochemistry", max_depth=depth, max_nodes=limit)
        payload = graph_json(result, type_filter=type_filter)
        if project_id:
            # Keep only the selected project and nodes reached from its bounded neighborhood.
            payload["project_id"] = project_id
        return web.json_response(payload)

    async def expand(request: web.Request) -> web.Response:
        entity_id = request.query.get("id", "")
        result = memory_store.graph_expand(entity_id, max_nodes=_bounded_int(request.query.get("limit", "24"), 1, 64, 24))
        if not result.matched:
            raise web.HTTPNotFound(text="graph node was not found")
        return web.json_response(graph_json(result))

    async def node(request: web.Request) -> web.Response:
        entity_id = request.query.get("id", "")
        entity = memory_store.graph_entity(entity_id)
        if entity is None:
            raise web.HTTPNotFound(text="graph node was not found")
        detail: dict[str, object] = {"entity": entity_json(entity)}
        if entity.entity_type is EntityType.PROJECT:
            project = memory_store.get_project(entity.external_id)
            if project:
                detail["description"] = project.description
        elif entity.entity_type is EntityType.MEMORY:
            memory = memory_store.get(entity.external_id)
            if memory and memory.deleted_at is None:
                detail["content"] = memory.content
                detail["references"] = [
                    {"type": kind, "id": reference_id, "label": label}
                    for kind, reference_id, label in memory_store.references(memory.memory_id)[:20]
                ]
                detail["history"] = [
                    {"id": record.memory_id, "kind": record.kind, "content": record.content,
                     "state": "superseded" if record.superseded_by else "current", "source_id": record.source_id,
                     "updated_at": record.updated_at}
                    for record in memory_store.graph_history(memory.memory_id)[:16]
                ]
        elif entity.entity_type is EntityType.TASK:
            task = task_store.get(entity.external_id)
            if task:
                detail["task"] = {
                    "task_id": task.task_id, "status": task.status.value,
                    "request": task.request[:500], "created_at": task.created_at.isoformat(),
                    "updated_at": task.updated_at.isoformat(),
                    "files": [item.filename for item in task.attachments[:20]],
                    "artifacts": [item.filename for item in task_store.list_artifacts(task.task_id)[:20]],
                    "execution_ids": list(task.execution_ids[:8]),
                }
        neighborhood = memory_store.graph_expand(entity_id, max_nodes=24)
        detail["relationships"] = [relationship_json(edge) for edge in neighborhood.relationships[:48]]
        detail["related"] = [entity_json(item) for item in neighborhood.connected[:24]]
        detail["source_ids"] = list(neighborhood.source_ids[:64])
        return web.json_response(detail)

    async def authorize(request: web.Request) -> web.Response:
        origin = request.headers.get("Origin")
        if origin not in {remote_origin, f"http://{request.headers.get('Host', '')}", "http://localhost:8768", "http://127.0.0.1:8768"}:
            raise web.HTTPForbidden(text="same-origin login required")
        raw_data: object = await request.json()
        data = cast(dict[str, object], raw_data) if isinstance(raw_data, dict) else {}
        code = data.get("code", "")
        remote_ok = isinstance(code, str) and remote_store is not None and remote_store.consume("memory", code)
        legacy_ok = isinstance(code, str) and hmac.compare_digest(code, app[_TOKEN_KEY])
        if not remote_ok and not legacy_ok:
            raise web.HTTPUnauthorized(text="one-time access code is invalid or expired")
        session_token = secrets.token_urlsafe(32)
        generation = remote_store.epoch() if remote_store is not None else 0
        app[_SESSION_KEY][_hash(session_token)] = (time.time() + REMOTE_SESSION_SECONDS, generation)
        response = web.json_response({"ok": True})
        is_remote = origin == remote_origin
        response.set_cookie(
            "toothless_memory", session_token, httponly=True, secure=is_remote, samesite="Strict",
            path=base_path + "/" if is_remote else "/", max_age=REMOTE_SESSION_SECONDS,
        )
        return response

    async def web_console_authorize(request: web.Request) -> web.Response:
        origin = request.headers.get("Origin")
        if origin not in {remote_origin, f"http://{request.headers.get('Host', '')}", "http://localhost:8768", "http://127.0.0.1:8768"}:
            raise web.HTTPForbidden(text="same-origin login required")
        raw_data: object = await request.json()
        data = cast(dict[str, object], raw_data) if isinstance(raw_data, dict) else {}
        code = data.get("code", "")
        if not isinstance(code, str) or remote_store is None or not remote_store.consume("web_console", code):
            raise web.HTTPUnauthorized(text="one-time web console code is invalid or expired")
        old_token = request.cookies.get("toothless_web", "")
        if old_token:
            old_hash = _hash(old_token)
            store = cast(SupabaseWebStore | None, app[_WEB_STORE_KEY])
            if store is not None:
                await store.revoke_web_session(old_hash)
            app[_SESSION_KEY].pop(old_hash, None)
            app[_APP_CSRF_KEY].pop(old_hash, None)
        session_token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        generation = remote_store.epoch()
        session_hash = _hash(session_token)
        app[_SESSION_KEY][session_hash] = (time.time() + REMOTE_SESSION_SECONDS, generation)
        app[_APP_CSRF_KEY][session_hash] = csrf
        response = web.json_response({"ok": True})
        is_remote = origin == remote_origin
        response.set_cookie("toothless_web", session_token, httponly=True, secure=is_remote,
                            samesite="Strict", path=base_path + "/" if is_remote else "/",
                            max_age=REMOTE_SESSION_SECONDS)
        response.set_cookie("toothless_web_csrf", csrf, httponly=False, secure=is_remote,
                            samesite="Strict", path=base_path + "/" if is_remote else "/",
                            max_age=REMOTE_SESSION_SECONDS)
        return response

    async def web_auth_session(request: web.Request) -> web.Response:
        store = cast(SupabaseWebStore | None, app[_WEB_STORE_KEY])
        authenticated = False
        if store is not None:
            token = request.cookies.get("toothless_web", "")
            csrf = request.cookies.get("toothless_web_csrf", "")
            if token and csrf:
                authenticated = await store.valid_web_session(_hash(token), _hash(csrf))
        if not authenticated:
            token = request.cookies.get("toothless_web", "")
            legacy = app[_SESSION_KEY].get(_hash(token)) if token else None
            generation = remote_store.epoch() if remote_store is not None else 0
            authenticated = bool(legacy and legacy[0] > time.time() and legacy[1] == generation)
        return web.json_response({
            "authenticated": authenticated,
            "storage": "connected" if store is not None else "not_configured",
            "administrator_configured": await store.has_admin() if store is not None else False,
        })

    async def web_auth_login(request: web.Request) -> web.Response:
        require_same_origin(request)
        store = cast(SupabaseWebStore | None, app[_WEB_STORE_KEY])
        owner = cast(uuid.UUID | None, app[_WEB_OWNER_KEY])
        if store is None or owner is None:
            raise web.HTTPServiceUnavailable(text="Supabase authentication storage is unavailable")
        if request.content_length is None or request.content_length > 4096:
            raise web.HTTPRequestEntityTooLarge(max_size=4096, actual_size=request.content_length or 0)
        raw_data: object = await request.json()
        data = cast(dict[str, object], raw_data) if isinstance(raw_data, dict) else {}
        username = data.get("username", "")
        password = data.get("password", "")
        username = username.strip() if isinstance(username, str) else ""
        password = password if isinstance(password, str) else ""
        normalized = username.casefold()
        username_key = hashlib.sha256(normalized.encode("utf-8")[:256]).hexdigest()
        if await store.login_is_limited(username_key):
            raise web.HTTPUnauthorized(text="Username or password is incorrect")
        stored_hash = await store.admin_password_hash(normalized)
        checked_hash = stored_hash or _DUMMY_PASSWORD_HASH
        valid = False
        try:
            valid = len(password) <= 1024 and await asyncio.to_thread(_PASSWORDS.verify, checked_hash, password)
        except (argon2_errors.VerificationError, argon2_errors.InvalidHashError, ValueError):
            valid = False
        if not stored_hash or not valid:
            await store.record_login_failure(username_key)
            raise web.HTTPUnauthorized(text="Username or password is incorrect")
        await store.record_login_success(username_key)
        response = await _persistent_login_response(request, store)
        return response

    async def web_auth_bootstrap(request: web.Request) -> web.Response:
        require_same_origin(request)
        store = cast(SupabaseWebStore | None, app[_WEB_STORE_KEY])
        owner = cast(uuid.UUID | None, app[_WEB_OWNER_KEY])
        if store is None or owner is None or remote_store is None:
            raise web.HTTPServiceUnavailable(text="Supabase setup and owner authorization are required")
        if request.content_length is None or request.content_length > 4096:
            raise web.HTTPRequestEntityTooLarge(max_size=4096, actual_size=request.content_length or 0)
        raw_data: object = await request.json()
        data = cast(dict[str, object], raw_data) if isinstance(raw_data, dict) else {}
        code, username, password = data.get("code"), data.get("username"), data.get("password")
        if (not isinstance(code, str) or not remote_store.consume("web_console", code)
                or not isinstance(username, str) or not isinstance(password, str)):
            raise web.HTTPUnauthorized(text="Owner setup authorization is invalid or expired")
        username = username.strip()
        normalized = username.casefold()
        if not _WEB_USERNAME.fullmatch(username) or not 12 <= len(password) <= 1024:
            raise web.HTTPBadRequest(text="Choose a valid username and a password of at least 12 characters")
        password_hash = await asyncio.to_thread(_PASSWORDS.hash, password)
        created = await store.bootstrap_admin(username, normalized, password_hash)
        if not created:
            raise web.HTTPConflict(text="Administrator setup is already complete")
        return await _persistent_login_response(request, store)

    async def web_auth_logout(request: web.Request) -> web.Response:
        require_web_csrf(request)
        store = cast(SupabaseWebStore | None, app[_WEB_STORE_KEY])
        token = request.cookies.get("toothless_web", "")
        session_hash = _hash(token) if token else ""
        if store is not None and request.get(_WEB_PERSISTENT_SESSION, False):
            await store.revoke_web_session(session_hash)
        else:
            app[_SESSION_KEY].pop(session_hash, None)
            app[_APP_CSRF_KEY].pop(session_hash, None)
        return _clear_web_cookies(request, web.json_response({"ok": True}))

    async def web_auth_logout_all(request: web.Request) -> web.Response:
        require_web_csrf(request)
        if not request.get(_WEB_PERSISTENT_SESSION, False):
            raise web.HTTPForbidden(text="Password-authenticated session required")
        store = cast(SupabaseWebStore | None, app[_WEB_STORE_KEY])
        if store is None:
            raise web.HTTPServiceUnavailable(text="Supabase authentication storage is unavailable")
        await store.revoke_all_web_sessions()
        return _clear_web_cookies(request, web.json_response({"ok": True}))

    async def web_auth_password_change(request: web.Request) -> web.Response:
        require_web_csrf(request)
        if not request.get(_WEB_PERSISTENT_SESSION, False):
            raise web.HTTPForbidden(text="Password-authenticated session required")
        store = cast(SupabaseWebStore | None, app[_WEB_STORE_KEY])
        if store is None:
            raise web.HTTPServiceUnavailable(text="Supabase authentication storage is unavailable")
        if request.content_length is None or request.content_length > 4096:
            raise web.HTTPRequestEntityTooLarge(max_size=4096, actual_size=request.content_length or 0)
        raw_data: object = await request.json()
        data = cast(dict[str, object], raw_data) if isinstance(raw_data, dict) else {}
        current, replacement = data.get("current_password"), data.get("new_password")
        credentials = await store.admin_credentials()
        valid = False
        if credentials and isinstance(current, str) and len(current) <= 1024:
            try:
                valid = await asyncio.to_thread(_PASSWORDS.verify, credentials[1], current)
            except (argon2_errors.VerificationError, argon2_errors.InvalidHashError, ValueError):
                valid = False
        if not valid:
            raise web.HTTPUnauthorized(text="Current password is incorrect")
        if not isinstance(replacement, str) or not 12 <= len(replacement) <= 1024:
            raise web.HTTPBadRequest(text="New password must contain at least 12 characters")
        password_hash = await asyncio.to_thread(_PASSWORDS.hash, replacement)
        await store.change_admin_password(password_hash)
        return await _persistent_login_response(request, store, replace_existing=False)

    def _clear_web_cookies(request: web.Request, response: web.Response) -> web.Response:
        is_remote = request.headers.get("Origin") == remote_origin
        path = base_path + "/" if is_remote else "/"
        response.del_cookie("toothless_web", path=path, secure=is_remote, httponly=True, samesite="Strict")
        response.del_cookie("toothless_web_csrf", path=path, secure=is_remote, samesite="Strict")
        return response

    async def _persistent_login_response(
        request: web.Request, store: SupabaseWebStore, *, replace_existing: bool = True,
    ) -> web.Response:
        old_token = request.cookies.get("toothless_web", "")
        if old_token and replace_existing:
            old_hash = _hash(old_token)
            if request.get(_WEB_PERSISTENT_SESSION, False):
                await store.revoke_web_session(old_hash)
            app[_SESSION_KEY].pop(old_hash, None)
            app[_APP_CSRF_KEY].pop(old_hash, None)
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        await store.create_web_session(
            _hash(token), _hash(csrf), datetime.now(UTC) + timedelta(seconds=WEB_SESSION_SECONDS),
        )
        response = web.json_response({"ok": True, "expires_in": WEB_SESSION_SECONDS})
        is_remote = request.headers.get("Origin") == remote_origin
        path = base_path + "/" if is_remote else "/"
        response.set_cookie("toothless_web", token, httponly=True, secure=is_remote,
                            samesite="Strict", path=path, max_age=WEB_SESSION_SECONDS)
        response.set_cookie("toothless_web_csrf", csrf, httponly=False, secure=is_remote,
                            samesite="Strict", path=path, max_age=WEB_SESSION_SECONDS)
        return response

    def require_web_csrf(request: web.Request) -> None:
        origin = request.headers.get("Origin")
        cookie = request.cookies.get("toothless_web", "")
        csrf_cookie = request.cookies.get("toothless_web_csrf", "")
        session_hash = _hash(cookie) if cookie else ""
        session = app[_SESSION_KEY].get(session_hash)
        legacy_valid = bool(session and session[0] > time.time()
                            and hmac.compare_digest(app[_APP_CSRF_KEY].get(session_hash, ""), csrf_cookie))
        persistent_valid = request.get(_WEB_PERSISTENT_SESSION, False)
        if (origin != remote_origin or not (legacy_valid or persistent_valid)
                or not csrf_cookie
                or not hmac.compare_digest(csrf_cookie, request.headers.get("X-Toothless-CSRF", ""))):
            raise web.HTTPForbidden(text="web console session expired or request origin is invalid")

    async def web_console_status(request: web.Request) -> web.Response:
        store = cast(SupabaseWebStore | None, app[_WEB_STORE_KEY])
        return web.json_response({
            "database": "connected" if store is not None else "not_configured",
            "agent_execution": "disabled_pending_approval",
            "automatic_low_risk_start": False,
            "message": "No work is started from this screen until the database and agent authorization are configured.",
        })

    async def web_console_projects(request: web.Request) -> web.Response:
        store = cast(SupabaseWebStore | None, app[_WEB_STORE_KEY])
        if store is None:
            raise web.HTTPServiceUnavailable(text="Connect the reviewed Supabase runtime role first")
        if request.method == "GET":
            return web.json_response({"projects": await store.list_projects()})
        require_web_csrf(request)
        if request.content_length is None or request.content_length > 8 * 1024:
            raise web.HTTPRequestEntityTooLarge(max_size=8 * 1024, actual_size=request.content_length or 0)
        raw_data: object = await request.json()
        data = cast(dict[str, object], raw_data) if isinstance(raw_data, dict) else {}
        try:
            project_id = await store.create_project(str(data.get("name", "")), str(data.get("description", "")))
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from None
        return web.json_response({"id": str(project_id)}, status=201)

    async def web_console_conversations(request: web.Request) -> web.Response:
        store = cast(SupabaseWebStore | None, app[_WEB_STORE_KEY])
        if store is None:
            raise web.HTTPServiceUnavailable(text="Connect the reviewed Supabase runtime role first")
        if request.method == "GET":
            return web.json_response({"conversations": await store.list_conversations()})
        require_web_csrf(request)
        if request.content_length is None or request.content_length > 8192:
            raise web.HTTPRequestEntityTooLarge(max_size=8192, actual_size=request.content_length or 0)
        raw_data: object = await request.json()
        data = cast(dict[str, object], raw_data) if isinstance(raw_data, dict) else {}
        try:
            project_id = data.get("project_id")
            identifier = uuid.UUID(project_id) if isinstance(project_id, str) and project_id else None
            conversation_id = await store.create_conversation(str(data.get("title", "New conversation")), identifier)
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text="invalid conversation request") from None
        return web.json_response({"id": str(conversation_id)}, status=201)

    async def web_console_messages(request: web.Request) -> web.Response:
        store = cast(SupabaseWebStore | None, app[_WEB_STORE_KEY])
        if store is None:
            raise web.HTTPServiceUnavailable(text="Connect the reviewed Supabase runtime role first")
        try:
            conversation_id = uuid.UUID(request.match_info["conversation_id"])
        except ValueError:
            raise web.HTTPBadRequest(text="invalid conversation id") from None
        if request.method == "GET":
            return web.json_response({"messages": await store.messages(conversation_id)})
        require_web_csrf(request)
        if request.content_length is None or request.content_length > 16 * 1024:
            raise web.HTTPRequestEntityTooLarge(max_size=16 * 1024, actual_size=request.content_length or 0)
        raw_data: object = await request.json()
        data = cast(dict[str, object], raw_data) if isinstance(raw_data, dict) else {}
        try:
            request_id = uuid.UUID(str(data.get("client_request_id", "")))
            message = await store.append_message(conversation_id, request_id, str(data.get("content", "")))
        except ValueError:
            raise web.HTTPBadRequest(text="message or idempotency key is invalid") from None
        except LookupError:
            raise web.HTTPNotFound(text="conversation was not found") from None
        return web.json_response({"message": message, "execution": "disabled_pending_approval"}, status=202)

    async def web_console_connect(request: web.Request) -> web.Response:
        require_web_csrf(request)
        if request.content_length is None or request.content_length > 8192:
            raise web.HTTPRequestEntityTooLarge(max_size=8192, actual_size=request.content_length or 0)
        raw_data: object = await request.json()
        data = cast(dict[str, object], raw_data) if isinstance(raw_data, dict) else {}
        dsn = data.get("dsn", "")
        if not isinstance(dsn, str):
            raise web.HTTPBadRequest(text="database connection string is required")
        config = cast(WebConsoleConfig | None, app[_WEB_CONFIG_KEY])
        owner = cast(uuid.UUID | None, app[_WEB_OWNER_KEY])
        if config is None or owner is None:
            raise web.HTTPServiceUnavailable(text="Owner-only web console configuration or Telegram owner is unavailable")
        candidate = SupabaseWebStore(dsn, owner, ssl_context=config.create_ssl_context())
        try:
            await candidate.connect()
            await candidate.verify_schema()
            await candidate.create_project(
                "Toothless Blog",
                "Experimental project context for the independent Toothless blog. Phase 08.0 provisioning code exists locally; no repository, Vercel project, or deployment has been created.",
            )
            config.set_dsn(dsn)
        except Exception:
            await candidate.close()
            raise web.HTTPServiceUnavailable(text="Supabase connection or reviewed schema verification failed") from None
        previous = cast(SupabaseWebStore | None, app[_WEB_STORE_KEY])
        app[_WEB_STORE_KEY] = candidate
        if previous is not None:
            await previous.close()
        return web.json_response({"database": "connected", "secret_storage": "owner-only web-console.env"})

    async def web_console_migrate_keychain(request: web.Request) -> web.Response:
        require_web_csrf(request)
        vault = cast(CredentialVault | None, app[_WEB_VAULT_KEY])
        config = cast(WebConsoleConfig | None, app[_WEB_CONFIG_KEY])
        owner = cast(uuid.UUID | None, app[_WEB_OWNER_KEY])
        if vault is None or config is None or owner is None:
            raise web.HTTPServiceUnavailable(text="Keychain migration is unavailable")
        if config.path.exists():
            raise web.HTTPConflict(text="Existing web-console.env was preserved; remove or back it up before migration")
        dsn = vault.get("runtime_dsn")
        if not dsn:
            raise web.HTTPNotFound(text="No legacy Supabase credential was found in Keychain")
        candidate = SupabaseWebStore(dsn, owner, ssl_context=config.create_ssl_context())
        try:
            await candidate.connect()
            await candidate.verify_schema()
            config.migrate_keychain_dsn(dsn)
        except Exception:
            await candidate.close()
            raise web.HTTPServiceUnavailable(text="Legacy connection could not be verified; no configuration was changed") from None
        previous = cast(SupabaseWebStore | None, app[_WEB_STORE_KEY])
        app[_WEB_STORE_KEY] = candidate
        if previous is not None:
            await previous.close()
        return web.json_response({"database": "connected", "secret_storage": "owner-only web-console.env"})

    async def web_console_logout(request: web.Request) -> web.Response:
        require_web_csrf(request)
        cookie = request.cookies.get("toothless_web", "")
        session_hash = _hash(cookie) if cookie else ""
        if request.get(_WEB_PERSISTENT_SESSION, False):
            store = cast(SupabaseWebStore | None, app[_WEB_STORE_KEY])
            if store is not None:
                await store.revoke_web_session(session_hash)
        else:
            app[_SESSION_KEY].pop(session_hash, None)
            app[_APP_CSRF_KEY].pop(session_hash, None)
        response = web.json_response({"ok": True})
        response.del_cookie("toothless_web", path=base_path + "/" if request.headers.get("Origin") == remote_origin else "/")
        response.del_cookie("toothless_web_csrf", path=base_path + "/" if request.headers.get("Origin") == remote_origin else "/")
        return response

    async def blog_setup_page(_request: web.Request) -> web.Response:
        return web.Response(
            text=_BLOG_SETUP_HTML.replace("__BASE_PATH__", base_path), content_type="text/html",
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    async def blog_setup_authorize(request: web.Request) -> web.Response:
        origin = request.headers.get("Origin")
        if origin != remote_origin:
            raise web.HTTPForbidden(text="private same-origin setup required")
        raw_data: object = await request.json()
        data = cast(dict[str, object], raw_data) if isinstance(raw_data, dict) else {}
        code = data.get("code", "")
        if not isinstance(code, str) or remote_store is None or not remote_store.consume("blog_setup", code):
            raise web.HTTPUnauthorized(text="one-time setup code is invalid or expired")
        session_token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        session_hash = _hash(session_token)
        generation = remote_store.epoch()
        app[_SESSION_KEY][session_hash] = (time.time() + 600, generation)
        app[_BLOG_CSRF_KEY][session_hash] = csrf
        response = web.json_response({"ok": True})
        response.set_cookie("toothless_blog_setup", session_token, httponly=True, secure=True,
                            samesite="Strict", path=base_path + "/", max_age=600)
        response.set_cookie("toothless_blog_csrf", csrf, httponly=False, secure=True,
                            samesite="Strict", path=base_path + "/", max_age=600)
        return response

    def require_blog_csrf(request: web.Request, *, require_origin: bool = True) -> None:
        origin = request.headers.get("Origin")
        cookie = request.cookies.get("toothless_blog_setup", "")
        csrf_cookie = request.cookies.get("toothless_blog_csrf", "")
        csrf_header = request.headers.get("X-Toothless-CSRF", "")
        session_hash = _hash(cookie) if cookie else ""
        session = app[_SESSION_KEY].get(session_hash)
        generation = remote_store.epoch() if remote_store is not None else 0
        if (
            (require_origin and origin != remote_origin) or not session or session[0] <= time.time()
            or session[1] != generation or not csrf_cookie
            or (require_origin and not hmac.compare_digest(csrf_cookie, csrf_header))
            or not hmac.compare_digest(app[_BLOG_CSRF_KEY].get(session_hash, ""), csrf_cookie)
        ):
            raise web.HTTPForbidden(text="setup session expired or request origin is invalid")

    async def blog_setup_status(request: web.Request) -> web.Response:
        require_blog_csrf(request, require_origin=False)
        store = cast(BlogSetupStore | None, app[_BLOG_STORE_KEY])
        if store is None:
            raise web.HTTPServiceUnavailable(text="blog setup is unavailable")
        snapshot = store.snapshot()
        return web.json_response({
            "github_setup": snapshot.github_setup, "github_publish": snapshot.github_publish,
            "vercel": snapshot.vercel, "repository_id": snapshot.repository_id,
            "baseline_sha": snapshot.baseline_sha, "project_id": snapshot.project_id,
            "project_url": snapshot.project_url, "state": snapshot.state,
            "last_error": snapshot.last_error,
        })

    async def blog_setup_credential(request: web.Request) -> web.Response:
        require_blog_csrf(request)
        if request.content_length is None or request.content_length > 8 * 1024:
            raise web.HTTPRequestEntityTooLarge(max_size=8 * 1024, actual_size=request.content_length or 0)
        raw_data: object = await request.json()
        data = cast(dict[str, object], raw_data) if isinstance(raw_data, dict) else {}
        name = data.get("credential", "")
        secret = data.get("secret", "")
        if not isinstance(name, str) or name not in {"github_setup", "github_publish", "vercel"}:
            raise web.HTTPBadRequest(text="unsupported credential type")
        if not isinstance(secret, str) or not 20 <= len(secret) <= MAX_SECRET_CHARS or any(ch.isspace() for ch in secret):
            raise web.HTTPBadRequest(text="credential format or size is invalid")
        vault = cast(CredentialVault | None, app[_BLOG_VAULT_KEY])
        store = cast(BlogSetupStore | None, app[_BLOG_STORE_KEY])
        if vault is None or store is None:
            raise web.HTTPServiceUnavailable(text="secure credential storage is unavailable")
        provider = blog_provider or BlogProviderClient()
        try:
            if name.startswith("github_"):
                await provider.validate_github(secret)
            else:
                await provider.validate_vercel(secret)
            vault.set(name, secret)
        except ProviderError as exc:
            raise web.HTTPUnauthorized(text=str(exc)) from None
        except (OSError, RuntimeError, ValueError):
            raise web.HTTPServiceUnavailable(text="macOS Keychain could not store the credential") from None
        finally:
            await provider.close()
        store.update(state="credentials_connected", last_error=None, **{name: True})
        store.audit("credential_connected", f"{name} validated and stored in login Keychain")
        return web.json_response({"ok": True, "credential": name, "stored": "macOS login Keychain"})

    async def blog_setup_forget_credential(request: web.Request) -> web.Response:
        require_blog_csrf(request)
        name = request.match_info["name"]
        if name not in {"github_setup", "github_publish", "vercel"}:
            raise web.HTTPBadRequest(text="unsupported credential type")
        vault = cast(CredentialVault | None, app[_BLOG_VAULT_KEY])
        store = cast(BlogSetupStore | None, app[_BLOG_STORE_KEY])
        if vault is None or store is None:
            raise web.HTTPServiceUnavailable(text="secure credential storage is unavailable")
        vault.delete(name)
        store.update(**{name: False}, state="credentials_changed")
        store.audit("credential_removed", f"{name} removed from local login Keychain; revoke it at its provider too")
        return web.json_response({"ok": True, "provider_revocation_required": True})

    async def logout(request: web.Request) -> web.Response:
        origin = request.headers.get("Origin")
        if origin not in {remote_origin, f"http://{request.headers.get('Host', '')}", "http://localhost:8768", "http://127.0.0.1:8768"}:
            raise web.HTTPForbidden(text="same-origin logout required")
        cookie = request.cookies.get("toothless_memory", "")
        if cookie:
            app[_SESSION_KEY].pop(_hash(cookie), None)
        response = web.json_response({"ok": True})
        is_remote = origin == remote_origin
        response.del_cookie("toothless_memory", path=base_path + "/" if is_remote else "/", secure=is_remote, httponly=True, samesite="Strict")
        return response

    prefixes = ("", base_path) if remote_store is not None else ("",)
    for prefix in prefixes:
        app.router.add_get(prefix + "/", index)
        app.router.add_get(prefix + "/app/", web_console_page)
        app.router.add_get(prefix + "/app", web_console_page)
        app.router.add_get(prefix + "/app/login", web_console_page)
        app.router.add_get(prefix + "/app/setup", web_console_setup_page)
        app.router.add_post(prefix + "/app/authorize", web_console_authorize)
        app.router.add_get(prefix + "/app/api/status", web_console_status)
        app.router.add_post(prefix + "/app/api/connect", web_console_connect)
        app.router.add_post(prefix + "/app/api/migrate-keychain", web_console_migrate_keychain)
        app.router.add_get(prefix + "/app/api/auth/session", web_auth_session)
        app.router.add_post(prefix + "/app/api/auth/login", web_auth_login)
        app.router.add_post(prefix + "/app/api/auth/bootstrap", web_auth_bootstrap)
        app.router.add_post(prefix + "/app/api/auth/logout", web_auth_logout)
        app.router.add_post(prefix + "/app/api/auth/logout-all", web_auth_logout_all)
        app.router.add_post(prefix + "/app/api/auth/password", web_auth_password_change)
        app.router.add_post(prefix + "/app/api/logout", web_console_logout)
        app.router.add_route("GET", prefix + "/app/api/projects", web_console_projects)
        app.router.add_route("POST", prefix + "/app/api/projects", web_console_projects)
        app.router.add_route("GET", prefix + "/app/api/conversations", web_console_conversations)
        app.router.add_route("POST", prefix + "/app/api/conversations", web_console_conversations)
        app.router.add_get(prefix + "/app/api/conversations/{conversation_id}/messages", web_console_messages)
        app.router.add_post(prefix + "/app/api/conversations/{conversation_id}/messages", web_console_messages)
        if prefix:
            # Serve path mounts may normalize the mount root without its trailing slash.
            app.router.add_get(prefix, index)
        app.router.add_get(prefix + "/assets/{name}", static_asset)
        app.router.add_post(prefix + "/authorize", authorize)
        app.router.add_get(prefix + "/blog-setup/", blog_setup_page)
        app.router.add_get(prefix + "/blog-setup", blog_setup_page)
        app.router.add_post(prefix + "/blog-setup/authorize", blog_setup_authorize)
        app.router.add_get(prefix + "/blog-setup/api/status", blog_setup_status)
        app.router.add_post(prefix + "/blog-setup/api/credentials", blog_setup_credential)
        app.router.add_delete(prefix + "/blog-setup/api/credentials/{name}", blog_setup_forget_credential)
        app.router.add_post(prefix + "/logout", logout)
        app.router.add_get(prefix + "/api/projects", projects)
        app.router.add_get(prefix + "/api/graph", graph)
        app.router.add_get(prefix + "/api/expand", expand)
        app.router.add_get(prefix + "/api/node", node)
    return app


def _bounded_int(value: str, minimum: int, maximum: int, default: int) -> int:
    try:
        parsed = int(value)
    except ValueError:
        return default
    return min(maximum, max(minimum, parsed))


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _local_path(path: str, prefix: str) -> str | None:
    if not prefix:
        return path
    if path == "/":
        # Tailscale Serve strips the mount prefix before proxying to the local app.
        return "/"
    if path == prefix:
        return "/"
    if path.startswith(prefix + "/"):
        return path[len(prefix):]
    if path in {"/authorize", "/logout"}:
        return path
    return path if path.startswith(("/api/", "/assets/", "/blog-setup", "/app")) else None


_BLOG_SETUP_HTML = """<!doctype html>
<html lang="en" data-carbon-theme="g100"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark"><meta name="referrer" content="no-referrer"><link rel="stylesheet" href="__BASE_PATH__/assets/carbon.css"><link rel="stylesheet" href="__BASE_PATH__/assets/blog_setup.css"><title>Toothless · Blog setup</title></head>
<body><header class="cds--header"><a class="cds--header__name" href="__BASE_PATH__/app/" aria-label="Toothless"><span class="cds--brand-mark" aria-hidden="true">T</span><span>Toothless <span class="cds--header__name--prefix">/ Blog setup</span></span></a><div class="cds--header-actions"><span class="cds--status-tag"><span aria-hidden="true"></span>Owner authorized</span></div></header>
<main class="cds--page blog-layout"><div class="blog-intro"><p class="cds--eyebrow">EXPERIMENTAL PUBLISHING SETUP</p><h1>Connect publishing services.</h1><p>Credentials are validated and stored in this Mac’s login Keychain. They are never sent to Telegram, OpenCode, or Ubuntu.</p><div class="blog-security"><strong>Fixed destination only</strong><span>Toothless enforces the reviewed repository and project allowlist.</span></div></div>
<section id="login" class="cds--panel blog-panel"><p class="cds--eyebrow">OWNER VERIFICATION</p><h2>Open setup</h2><label class="cds--label" for="code">One-time code from your private Toothless chat</label><input class="cds--text-input" id="code" type="password" autocomplete="one-time-code" maxlength="128"><button id="sign-in" class="cds--button cds--button--primary">Continue</button></section>
<section id="enroll" class="cds--panel blog-panel"><p class="cds--eyebrow">CREDENTIALS</p><h2>Connect services</h2><p>GitHub setup credential: create the target repository only. Use GitHub fine-grained access, account <b>logolica99</b>, with Repository creation: write. Source copy uses a separate token restricted to <b>logolica99/tech-pulse-toothless</b> with Contents: read/write. Do not grant access to the original repository for publishing.</p>
<p><a href="https://github.com/settings/personal-access-tokens/new" target="_blank" rel="noreferrer">GitHub fine-grained token settings</a></p>
<p>Vercel credential is validated against the personal account <b>logolica99</b>; use the shortest practical expiration. The app enforces the exact project allowlist.</p>
<p><a href="https://vercel.com/account/tokens" target="_blank" rel="noreferrer">Vercel token settings</a></p>
<p>If Vercel reports that GitHub access is missing, connect or authorize the GitHub account in Vercel and grant access to only <b>tech-pulse-toothless</b> where repository selection is available. See the <a href="https://vercel.com/docs/git/vercel-for-github" target="_blank" rel="noreferrer">official Vercel GitHub integration guide</a>. Toothless will not switch repositories or deployment methods.</p>
<label class="cds--label" for="kind">Credential type</label><select class="cds--select" id="kind"><option value="github_setup">GitHub · create experimental repository</option><option value="github_publish">GitHub · publish to target repository only</option><option value="vercel">Vercel · experimental project</option></select>
<label class="cds--label blog-secret-label" for="secret">Credential</label><input class="cds--text-input" id="secret" type="password" autocomplete="new-password" maxlength="4096"><div class="blog-actions"><button id="save" class="cds--button cds--button--primary">Validate and store</button><button id="remove" class="cds--button cds--button--secondary">Remove local credential</button></div><p id="status" class="cds--status-line" role="status"></p><pre id="connections"></pre>
<p class="blog-footnote">Rotation: enroll a replacement credential. Removal clears only the local Keychain item; revoke the old credential at its provider separately.</p></section></main>
<script src="__BASE_PATH__/assets/blog_setup.js" defer></script></body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the private local Toothless Memory Explorer")
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--managed", action="store_true", help="run without printing a bearer token; Telegram one-use codes only")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    task_path = _task_store_path()
    memory = MemoryStore(task_path.parent / "memory.sqlite3", read_only=True)
    tasks = TaskStore(task_path, read_only=True)
    token = secrets.token_urlsafe(32)
    remote_store = RemoteSessionStore(task_path.parent / "remote-sessions.sqlite3") if args.managed else None
    blog_store = BlogSetupStore(task_path.parent / "blog-setup.sqlite3") if args.managed else None
    blog_vault = MacOSKeychainVault() if args.managed and Path("/System/Library/Frameworks/Security.framework/Security").exists() else None
    web_config = WebConsoleConfig()
    web_store_state = "not_managed"
    web_vault = None
    web_owner = None
    web_store = None
    if args.managed:
        web_vault, web_owner, web_store, web_store_state = _load_web_console_runtime(
            web_config, os.environ.get("TOOTHLESS_OWNER_ID", ""),
        )
    app = create_app(
        token, memory, tasks, remote_store=remote_store, blog_store=blog_store,
        blog_vault=blog_vault, web_store=web_store, web_vault=web_vault, web_owner=web_owner,
        web_config=web_config if args.managed else None, web_store_state=web_store_state,
    )

    async def announce_ready(_app: web.Application) -> None:
        print("Memory Explorer ready on 127.0.0.1:%d" % args.port)
        if not args.managed:
            print("Temporary access token (enter in the sign-in panel; not saved to disk): %s" % token)

    app.on_startup.append(announce_ready)
    if web_store is not None:
        retry_task: asyncio.Task[None] | None = None

        async def connect_web_store(_app: web.Application) -> None:
            nonlocal retry_task
            if not await _connect_web_store_once(_app, web_store):
                retry_task = asyncio.create_task(_maintain_web_store_connection(_app, web_store))

        app.on_startup.append(connect_web_store)
        async def close_web_store(_app: web.Application) -> None:
            if retry_task is not None:
                retry_task.cancel()
                await asyncio.gather(retry_task, return_exceptions=True)
            await web_store.close()

        app.on_cleanup.append(close_web_store)
    if remote_store is not None:
        async def close_remote_store(_app: web.Application) -> None:
            remote_store.close()

        app.on_cleanup.append(close_remote_store)
    if blog_store is not None:
        async def close_blog_store(_app: web.Application) -> None:
            blog_store.close()

        app.on_cleanup.append(close_blog_store)
    web.run_app(app, host="127.0.0.1", port=args.port, print=None)


if __name__ == "__main__":
    main()
