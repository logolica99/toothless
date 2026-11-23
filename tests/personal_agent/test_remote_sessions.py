from __future__ import annotations

import sqlite3
from pathlib import Path

from nanobot.personal_agent.remote_sessions import RemoteSessionStore


def test_remote_login_codes_are_scoped_single_use_and_stored_as_hashes(tmp_path: Path) -> None:
    store_path = tmp_path / "remote-sessions.sqlite3"
    store = RemoteSessionStore(store_path)
    code = store.issue("memory", 42, 42)

    assert not store.consume("desktop", code)
    assert store.consume("memory", code)
    assert not store.consume("memory", code)

    with sqlite3.connect(store_path) as db:
        stored_code = db.execute("SELECT code_hash FROM remote_login_grants").fetchone()[0]
    assert stored_code != code
    store.close()


def test_remote_session_epoch_advances_and_revocation_consumes_unused_codes(tmp_path: Path) -> None:
    store = RemoteSessionStore(tmp_path / "remote-sessions.sqlite3")
    code = store.issue("memory", 42, 42)
    initial = store.epoch()

    assert store.advance_epoch() == initial + 1
    store.revoke()

    assert not store.consume("memory", code)
    assert store.epoch() == initial + 2
    store.close()


def test_web_console_grants_are_separate_and_single_use(tmp_path: Path) -> None:
    store = RemoteSessionStore(tmp_path / "remote-sessions.sqlite3")
    code = store.issue("web_console", 42, 42)
    assert not store.consume("memory", code)
    assert store.consume("web_console", code)
    assert not store.consume("web_console", code)
    store.revoke("web_console")
    store.close()
