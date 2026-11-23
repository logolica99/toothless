"""Host-owned one-use login grants for private Tailscale-proxied apps."""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import stat
import time
from pathlib import Path


class RemoteSessionStore:
    """Issue and consume one-use codes without storing their plaintext values."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.parent.is_symlink() or path.is_symlink():
            raise RuntimeError("remote session database path must not contain symlinks")
        parent = path.parent.stat()
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077:
            raise RuntimeError("remote session database directory must be owner-only")
        self._db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        os.chmod(path, 0o600)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS remote_login_grants ("
            "code_hash TEXT PRIMARY KEY, scope TEXT NOT NULL, owner_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, "
            "created_at REAL NOT NULL, expires_at REAL NOT NULL, consumed_at REAL)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS remote_session_epoch (singleton INTEGER PRIMARY KEY CHECK(singleton=1), epoch INTEGER NOT NULL)"
        )
        self._db.execute("INSERT OR IGNORE INTO remote_session_epoch(singleton,epoch) VALUES(1,1)")

    def advance_epoch(self) -> int:
        self._db.execute("UPDATE remote_session_epoch SET epoch=epoch+1 WHERE singleton=1")
        return self.epoch()

    def epoch(self) -> int:
        row = self._db.execute("SELECT epoch FROM remote_session_epoch WHERE singleton=1").fetchone()
        return int(row[0])

    def issue(self, scope: str, owner_id: int, chat_id: int, *, ttl_seconds: int = 600) -> str:
        if scope not in {"desktop", "memory", "blog_setup", "web_console"} or not 60 <= ttl_seconds <= 600:
            raise ValueError("invalid remote login grant")
        now = time.time()
        self._db.execute("DELETE FROM remote_login_grants WHERE expires_at < ? OR consumed_at IS NOT NULL", (now,))
        code = secrets.token_urlsafe(24)
        self._db.execute(
            "INSERT INTO remote_login_grants(code_hash,scope,owner_id,chat_id,created_at,expires_at) VALUES(?,?,?,?,?,?)",
            (_digest(code), scope, owner_id, chat_id, now, now + ttl_seconds),
        )
        return code

    def consume(self, scope: str, code: str) -> bool:
        if scope not in {"desktop", "memory", "blog_setup", "web_console"} or not 20 <= len(code) <= 128:
            return False
        now = time.time()
        self._db.execute("BEGIN IMMEDIATE")
        cursor = self._db.execute(
            "UPDATE remote_login_grants SET consumed_at=? WHERE code_hash=? AND scope=? AND expires_at>? AND consumed_at IS NULL",
            (now, _digest(code), scope, now),
        )
        self._db.commit()
        return cursor.rowcount == 1

    def revoke(self, scope: str | None = None) -> None:
        if scope is None:
            self._db.execute("UPDATE remote_login_grants SET consumed_at=? WHERE consumed_at IS NULL", (time.time(),))
            self.advance_epoch()
        elif scope in {"desktop", "memory", "blog_setup", "web_console"}:
            self._db.execute(
                "UPDATE remote_login_grants SET consumed_at=? WHERE scope=? AND consumed_at IS NULL",
                (time.time(), scope),
            )

    def close(self) -> None:
        self._db.close()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
