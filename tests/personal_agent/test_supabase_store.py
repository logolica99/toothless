from __future__ import annotations

import ssl
import uuid
from typing import Any

import pytest

from nanobot.personal_agent.supabase_store import SupabaseWebStore


class _Transaction:
    def __init__(self) -> None:
        self.started = False
        self.committed = False
        self.rolled_back = False

    async def start(self) -> None:
        self.started = True

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class _Connection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.tx = _Transaction()
        self.schema_table_count = 11

    def transaction(self) -> _Transaction:
        return self.tx

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append((sql, args))
        return "INSERT 0 1"

    async def fetchval(self, sql: str, *args: Any) -> int:
        self.calls.append((sql, args))
        if "information_schema.tables" in sql:
            return self.schema_table_count
        return 1

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        return []


class _Pool:
    def __init__(self) -> None:
        self.connection = _Connection()
        self.released = 0
        self.closed = False

    async def acquire(self) -> _Connection:
        return self.connection

    async def release(self, _connection: _Connection) -> None:
        self.released += 1

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_pool_is_bounded_tls_verified_and_every_query_sets_owner_scope() -> None:
    pool = _Pool()
    options: dict[str, Any] = {}

    async def factory(**kwargs: Any) -> _Pool:
        options.update(kwargs)
        return pool

    owner = uuid.uuid4()
    store = SupabaseWebStore(
        "postgresql://toothless:secret@db.abc123.supabase.co:5432/postgres",
        owner,
        pool_factory=factory,
    )
    await store.connect()
    assert options["min_size"] == 1 and options["max_size"] == 3
    assert options["command_timeout"] == 8 and options["timeout"] == 8
    context = options["ssl"]
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    await store.list_conversations(10_000)
    owner_scopes = [args[0] for sql, args in pool.connection.calls if "set_config('app.owner_id'" in sql]
    assert owner_scopes == [str(owner), str(owner)]
    assert pool.connection.tx.committed
    assert pool.released == 2
    await store.close()
    assert pool.closed


@pytest.mark.asyncio
async def test_event_metadata_is_whitelisted_before_storage() -> None:
    pool = _Pool()

    async def factory(**_kwargs: Any) -> _Pool:
        return pool

    store = SupabaseWebStore(
        "postgresql://toothless:secret@db.abc123.supabase.co:5432/postgres",
        uuid.uuid4(), pool_factory=factory,
    )
    await store.connect()
    with pytest.raises(ValueError, match="unsupported fields"):
        await store.append_event(
            uuid.uuid4(), 1, "tool_started", "Inspecting source", safe_metadata={"prompt": "private"},
        )
    await store.append_event(
        uuid.uuid4(), 1, "tool_started", "Inspecting source", tool_name="read_file",
        safe_metadata={"step": 1, "status": "started"},
    )
    assert pool.connection.tx.committed
    await store.close()


@pytest.mark.asyncio
async def test_schema_verification_requires_all_eleven_tables() -> None:
    pool = _Pool()

    async def factory(**_kwargs: Any) -> _Pool:
        return pool

    store = SupabaseWebStore(
        "postgresql://toothless:secret@db.abc123.supabase.co:5432/postgres",
        uuid.uuid4(), pool_factory=factory,
    )
    await store.connect()
    await store.verify_schema()
    pool.connection.schema_table_count = 10
    with pytest.raises(LookupError, match="schema is not fully installed"):
        await store.verify_schema()
    await store.close()
