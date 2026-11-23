"""Host-mediated persistence for the private Toothless web application.

The Ubuntu worker never receives this connection. The runtime DSN belongs in the
Mac login Keychain; database access is restricted to the private schema/RLS role.
"""

from __future__ import annotations

import json
import re
import ssl
import uuid
from datetime import date, datetime
from typing import Any, Awaitable, Callable, cast
from urllib.parse import parse_qs, urlsplit

MAX_PAGE = 100
_REQUIRED_WEB_TABLES = (
    "projects", "conversations", "messages", "agent_runs", "agent_events", "approval_requests",
    "project_context", "web_admin", "web_sessions", "web_login_limits", "web_auth_events",
)
_SAFE_HOST = re.compile(r"^(?:db\.[a-z0-9-]+\.supabase\.co|[a-z0-9.-]+\.pooler\.supabase\.com)$")


def owner_uuid(telegram_owner_id: int) -> uuid.UUID:
    if telegram_owner_id <= 0:
        raise ValueError("a configured Telegram owner is required")
    return uuid.uuid5(uuid.NAMESPACE_URL, f"toothless-owner:{telegram_owner_id}")


def json_record(record: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in dict(record).items():
        result[key] = str(value) if isinstance(value, uuid.UUID) else value.isoformat() if isinstance(value, (datetime, date)) else value
    return result


def validate_supabase_dsn(dsn: str) -> None:
    """Reject non-Supabase destinations and unverified TLS configuration."""
    if not 20 <= len(dsn) <= 4096:
        raise ValueError("connection string is missing or too large")
    parsed = urlsplit(dsn)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.username or not parsed.password:
        raise ValueError("enter a PostgreSQL connection string with a username and password")
    host = (parsed.hostname or "").lower()
    if not _SAFE_HOST.fullmatch(host):
        raise ValueError("connection host must be a Supabase database or pooler endpoint")
    if parsed.path not in {"/postgres", "/toothless"}:
        raise ValueError("database name must be postgres or toothless")
    query = parse_qs(parsed.query)
    if any(key.casefold() in {"sslmode", "sslrootcert", "sslcert", "sslkey"} for key in query):
        raise ValueError("remove TLS query overrides; Toothless enforces certificate and hostname verification")


class SupabaseWebStore:
    """Small asyncpg repository; every operation runs under the owner RLS setting."""

    def __init__(
        self, dsn: str, owner_id: uuid.UUID, *, pool_factory: Any = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        validate_supabase_dsn(dsn)
        self._dsn = dsn
        self.owner_id = owner_id
        self._pool_factory: Callable[..., Awaitable[Any]] | None = cast(Callable[..., Awaitable[Any]] | None, pool_factory)
        self._ssl_context = ssl_context
        self._pool: Any = None

    async def connect(self) -> None:
        if self._pool is not None:
            return
        factory = self._pool_factory
        if factory is None:
            try:
                import asyncpg  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError("The PostgreSQL driver is not installed on this controller") from exc
            factory = cast(Callable[..., Awaitable[Any]], asyncpg.create_pool)
        context = self._ssl_context or ssl.create_default_context()
        if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
            raise ValueError("Supabase TLS must verify the certificate and hostname")
        self._pool = await factory(
            dsn=self._dsn, ssl=context, min_size=1, max_size=3,
            command_timeout=8, timeout=8,
        )
        await self.health()

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            await pool.close()

    async def health(self) -> bool:
        async with self._owner_connection() as connection:
            await connection.fetchval("SELECT 1")
        return True

    async def verify_schema(self) -> None:
        async with self._owner_connection() as connection:
            tables = await connection.fetchval(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema='toothless_private' "
                "AND table_name = ANY($1::text[])",
                list(_REQUIRED_WEB_TABLES),
            )
            if tables != len(_REQUIRED_WEB_TABLES):
                raise LookupError("the reviewed Toothless web schema is not fully installed")

    class _OwnerConnection:
        def __init__(self, store: "SupabaseWebStore") -> None:
            self.store = store
            self.connection: Any = None

        async def __aenter__(self) -> Any:
            if self.store._pool is None:
                raise RuntimeError("Supabase is not connected")
            self.connection = await self.store._pool.acquire()
            try:
                self.transaction = self.connection.transaction()
                await self.transaction.start()
                await self.connection.execute("SELECT set_config('app.owner_id', $1, true)", str(self.store.owner_id))
            except Exception:
                try:
                    await self.transaction.rollback()
                finally:
                    await self.store._pool.release(self.connection)
                raise
            return self.connection

        async def __aexit__(self, _kind: Any, _value: Any, _traceback: Any) -> None:
            try:
                if _kind is None:
                    await self.transaction.commit()
                else:
                    await self.transaction.rollback()
            finally:
                await self.store._pool.release(self.connection)

    def _owner_connection(self) -> "SupabaseWebStore._OwnerConnection":
        return self._OwnerConnection(self)

    async def list_conversations(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(MAX_PAGE, int(limit)))
        async with self._owner_connection() as connection:
            rows = await connection.fetch(
                "SELECT id, project_id, title, created_at, updated_at FROM toothless_private.conversations "
                "WHERE owner_id=$1 AND archived_at IS NULL ORDER BY updated_at DESC LIMIT $2",
                self.owner_id, limit,
            )
            return [json_record(row) for row in rows]

    async def list_projects(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(MAX_PAGE, int(limit)))
        async with self._owner_connection() as connection:
            rows = await connection.fetch(
                "SELECT id,name,description,workspace_ref,created_at,updated_at "
                "FROM toothless_private.projects WHERE owner_id=$1 ORDER BY updated_at DESC LIMIT $2",
                self.owner_id, limit,
            )
            return [json_record(row) for row in rows]

    async def messages(self, conversation_id: uuid.UUID, before: str | None = None) -> list[dict[str, Any]]:
        async with self._owner_connection() as connection:
            if before:
                rows = await connection.fetch(
                    "SELECT id, role, content, content_type, created_at, associated_run_id, status "
                    "FROM toothless_private.messages WHERE owner_id=$1 AND conversation_id=$2 AND created_at<$3 "
                    "ORDER BY created_at DESC LIMIT 100", self.owner_id, conversation_id, before,
                )
            else:
                rows = await connection.fetch(
                    "SELECT id, role, content, content_type, created_at, associated_run_id, status "
                    "FROM toothless_private.messages WHERE owner_id=$1 AND conversation_id=$2 "
                    "ORDER BY created_at DESC LIMIT 100", self.owner_id, conversation_id,
                )
            return [json_record(row) for row in reversed(rows)]

    async def create_conversation(self, title: str, project_id: uuid.UUID | None = None) -> uuid.UUID:
        title = title.strip()[:200]
        if not title:
            raise ValueError("conversation title is required")
        conversation_id = uuid.uuid4()
        async with self._owner_connection() as connection:
            if project_id is None:
                await connection.execute(
                    "INSERT INTO toothless_private.conversations(id,owner_id,project_id,title) VALUES($1,$2,NULL,$3)",
                    conversation_id, self.owner_id, title,
                )
            else:
                result = await connection.execute(
                    "INSERT INTO toothless_private.conversations(id,owner_id,project_id,title) "
                    "SELECT $1,$2,p.id,$4 FROM toothless_private.projects p WHERE p.id=$3 AND p.owner_id=$2",
                    conversation_id, self.owner_id, project_id, title,
                )
                if result.endswith(" 0"):
                    raise LookupError("project was not found")
        return conversation_id

    async def create_project(self, name: str, description: str = "") -> uuid.UUID:
        name = name.strip()
        if not 1 <= len(name) <= 120 or len(description) > 4000:
            raise ValueError("project name or description is outside supported bounds")
        project_id = uuid.uuid4()
        async with self._owner_connection() as connection:
            row = await connection.fetchrow(
                "INSERT INTO toothless_private.projects(id,owner_id,name,description) VALUES($1,$2,$3,$4) "
                "ON CONFLICT(owner_id,name) DO UPDATE SET name=EXCLUDED.name RETURNING id",
                project_id, self.owner_id, name, description,
            )
            return row["id"]

    async def append_message(
        self, conversation_id: uuid.UUID, client_request_id: uuid.UUID, content: str,
    ) -> dict[str, Any]:
        if not content.strip() or len(content) > 8000:
            raise ValueError("message is empty or exceeds 8,000 characters")
        message_id = uuid.uuid4()
        async with self._owner_connection() as connection:
            existing = await connection.fetchrow(
                "SELECT id,conversation_id,role,content,created_at,status FROM toothless_private.messages "
                "WHERE owner_id=$1 AND client_request_id=$2", self.owner_id, client_request_id,
            )
            if existing:
                if existing["conversation_id"] != conversation_id or existing["content"] != content:
                    raise ValueError("idempotency key was already used for a different message")
                return json_record(existing)
            row = await connection.fetchrow(
                "INSERT INTO toothless_private.messages(id,owner_id,conversation_id,role,content,client_request_id,status) "
                "SELECT $1,$2,c.id,'user',$4,$5,'stored_no_execution' FROM toothless_private.conversations c "
                "WHERE c.id=$3 AND c.owner_id=$2 AND c.archived_at IS NULL "
                "ON CONFLICT(owner_id,client_request_id) DO NOTHING RETURNING id,role,content,created_at,status",
                message_id, self.owner_id, conversation_id, content, client_request_id,
            )
            if row is None:
                existing = await connection.fetchrow(
                    "SELECT id,conversation_id,role,content,created_at,status FROM toothless_private.messages "
                    "WHERE owner_id=$1 AND client_request_id=$2", self.owner_id, client_request_id,
                )
                if existing and existing["conversation_id"] == conversation_id and existing["content"] == content:
                    return json_record(existing)
                if existing:
                    raise ValueError("idempotency key was already used for a different message")
                raise LookupError("conversation was not found")
            await connection.execute(
                "UPDATE toothless_private.conversations SET updated_at=now() WHERE id=$1 AND owner_id=$2",
                conversation_id, self.owner_id,
            )
            await connection.execute(
                "INSERT INTO toothless_private.messages(id,owner_id,conversation_id,role,content,client_request_id,status) "
                "VALUES($1,$2,$3,'system-visible-event',$4,$5,'notice')",
                uuid.uuid4(), self.owner_id, conversation_id,
                "Message saved. Agent execution is not enabled; no work was started.", uuid.uuid4(),
            )
            return json_record(row)

    async def append_event(
        self, run_id: uuid.UUID, sequence: int, event_type: str, summary: str,
        *, tool_name: str | None = None, safe_metadata: dict[str, Any] | None = None,
    ) -> None:
        if (not 0 < sequence <= 1_000_000 or len(summary) > 2000
                or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", event_type)
                or tool_name is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", tool_name)):
            raise ValueError("event is outside supported bounds")
        allowed_metadata = {"step", "duration_ms", "retry_count", "status", "error_category"}
        if set(safe_metadata or {}) - allowed_metadata:
            raise ValueError("event metadata contains unsupported fields")
        metadata = safe_metadata or {}
        if any(
            not (type(value) is int and 0 <= value <= 1_000_000 or isinstance(value, str) and len(value) <= 80 and re.fullmatch(r"[A-Za-z0-9_.:-]*", value))
            for value in metadata.values()
        ):
            raise ValueError("event metadata is not safe")
        async with self._owner_connection() as connection:
            await connection.execute(
                "INSERT INTO toothless_private.agent_events(id,owner_id,run_id,sequence,event_type,safe_summary,tool_name,safe_metadata) "
                "VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb) ON CONFLICT(run_id,sequence) DO NOTHING",
                uuid.uuid4(), self.owner_id, run_id, sequence, event_type[:64], summary,
                tool_name, json.dumps(metadata)[:1000],
            )

    async def has_admin(self) -> bool:
        async with self._owner_connection() as connection:
            return bool(await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM toothless_private.web_admin WHERE owner_id=$1)", self.owner_id,
            ))

    async def bootstrap_admin(self, username: str, username_normalized: str, password_hash: str) -> bool:
        async with self._owner_connection() as connection:
            await connection.execute("SELECT pg_advisory_xact_lock(hashtext('toothless-web-single-owner'))")
            exists = await connection.fetchval("SELECT EXISTS(SELECT 1 FROM toothless_private.web_admin)")
            if exists:
                return False
            await connection.execute(
                "INSERT INTO toothless_private.web_admin(singleton,owner_id,username,username_normalized,password_hash) "
                "VALUES(true,$1,$2,$3,$4)", self.owner_id, username, username_normalized, password_hash,
            )
            await connection.execute(
                "INSERT INTO toothless_private.web_auth_events(id,owner_id,event_type) VALUES($1,$2,'bootstrap')",
                uuid.uuid4(), self.owner_id,
            )
            return True

    async def admin_password_hash(self, username_normalized: str) -> str | None:
        async with self._owner_connection() as connection:
            return await connection.fetchval(
                "SELECT password_hash FROM toothless_private.web_admin "
                "WHERE owner_id=$1 AND username_normalized=$2", self.owner_id, username_normalized,
            )

    async def admin_credentials(self) -> tuple[str, str] | None:
        async with self._owner_connection() as connection:
            row = await connection.fetchrow(
                "SELECT username_normalized,password_hash FROM toothless_private.web_admin WHERE owner_id=$1",
                self.owner_id,
            )
            return (row["username_normalized"], row["password_hash"]) if row else None

    async def login_is_limited(self, username_key: str) -> bool:
        async with self._owner_connection() as connection:
            return bool(await connection.fetchval(
                "SELECT blocked_until > now() FROM toothless_private.web_login_limits WHERE username_key=$1",
                username_key,
            ))

    async def record_login_failure(self, username_key: str) -> None:
        async with self._owner_connection() as connection:
            await connection.execute(
                "DELETE FROM toothless_private.web_login_limits WHERE window_started_at < now()-interval '1 day'"
            )
            await connection.execute(
                "DELETE FROM toothless_private.web_auth_events WHERE created_at < now()-interval '90 days'"
            )
            await connection.execute(
                "INSERT INTO toothless_private.web_login_limits(username_key,window_started_at,failures,blocked_until) "
                "VALUES($1,now(),1,NULL) ON CONFLICT(username_key) DO UPDATE SET "
                "window_started_at=CASE WHEN toothless_private.web_login_limits.window_started_at < now()-interval '15 minutes' THEN now() ELSE toothless_private.web_login_limits.window_started_at END, "
                "failures=CASE WHEN toothless_private.web_login_limits.window_started_at < now()-interval '15 minutes' THEN 1 ELSE least(toothless_private.web_login_limits.failures+1,100) END, "
                "blocked_until=CASE WHEN (CASE WHEN toothless_private.web_login_limits.window_started_at < now()-interval '15 minutes' THEN 1 ELSE toothless_private.web_login_limits.failures+1 END)>=5 THEN now()+interval '15 minutes' ELSE NULL END",
                username_key,
            )
            await connection.execute(
                "INSERT INTO toothless_private.web_auth_events(id,owner_id,event_type,username_key) "
                "VALUES($1,$2,'login_failed',$3)", uuid.uuid4(), self.owner_id, username_key,
            )

    async def clear_login_failures(self, username_key: str) -> None:
        async with self._owner_connection() as connection:
            await connection.execute("DELETE FROM toothless_private.web_login_limits WHERE username_key=$1", username_key)

    async def record_login_success(self, username_key: str) -> None:
        async with self._owner_connection() as connection:
            await connection.execute("DELETE FROM toothless_private.web_login_limits WHERE username_key=$1", username_key)
            await connection.execute(
                "INSERT INTO toothless_private.web_auth_events(id,owner_id,event_type,username_key) "
                "VALUES($1,$2,'login_succeeded',$3)", uuid.uuid4(), self.owner_id, username_key,
            )

    async def create_web_session(self, token_hash: str, csrf_hash: str, expires_at: datetime) -> None:
        async with self._owner_connection() as connection:
            await connection.execute(
                "DELETE FROM toothless_private.web_sessions WHERE owner_id=$1 "
                "AND (expires_at < now()-interval '30 days' OR revoked_at < now()-interval '30 days')",
                self.owner_id,
            )
            await connection.execute(
                "INSERT INTO toothless_private.web_sessions(token_hash,owner_id,csrf_hash,expires_at) VALUES($1,$2,$3,$4)",
                token_hash, self.owner_id, csrf_hash, expires_at,
            )

    async def valid_web_session(self, token_hash: str, csrf_hash: str) -> bool:
        async with self._owner_connection() as connection:
            return bool(await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM toothless_private.web_sessions WHERE token_hash=$1 AND owner_id=$2 "
                "AND csrf_hash=$3 AND revoked_at IS NULL AND expires_at>now())",
                token_hash, self.owner_id, csrf_hash,
            ))

    async def revoke_web_session(self, token_hash: str) -> None:
        async with self._owner_connection() as connection:
            await connection.execute(
                "UPDATE toothless_private.web_sessions SET revoked_at=now() WHERE token_hash=$1 AND owner_id=$2 AND revoked_at IS NULL",
                token_hash, self.owner_id,
            )
            await connection.execute(
                "INSERT INTO toothless_private.web_auth_events(id,owner_id,event_type) VALUES($1,$2,'logout')",
                uuid.uuid4(), self.owner_id,
            )

    async def revoke_all_web_sessions(self) -> None:
        async with self._owner_connection() as connection:
            await connection.execute(
                "UPDATE toothless_private.web_sessions SET revoked_at=now() WHERE owner_id=$1 AND revoked_at IS NULL",
                self.owner_id,
            )
            await connection.execute(
                "INSERT INTO toothless_private.web_auth_events(id,owner_id,event_type) VALUES($1,$2,'logout_all')",
                uuid.uuid4(), self.owner_id,
            )

    async def change_admin_password(self, password_hash: str) -> None:
        async with self._owner_connection() as connection:
            await connection.execute(
                "UPDATE toothless_private.web_admin SET password_hash=$2,updated_at=now() WHERE owner_id=$1",
                self.owner_id, password_hash,
            )
            await connection.execute(
                "UPDATE toothless_private.web_sessions SET revoked_at=now() WHERE owner_id=$1 AND revoked_at IS NULL",
                self.owner_id,
            )
            await connection.execute(
                "INSERT INTO toothless_private.web_auth_events(id,owner_id,event_type) VALUES($1,$2,'password_changed')",
                uuid.uuid4(), self.owner_id,
            )
