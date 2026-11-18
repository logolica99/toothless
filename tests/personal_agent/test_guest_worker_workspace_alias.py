from __future__ import annotations

from pathlib import Path

import pytest

from ops.worker import guest_worker_agent as guest


def test_opencode_workspace_alias_resolves_and_round_trips_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    general = tmp_path / "general"
    opencode = tmp_path / "opencode"
    general.mkdir()
    opencode.mkdir()
    (opencode / "result.csv").write_text("value\n1\n", encoding="utf-8")
    monkeypatch.setattr(guest, "WORKSPACE_ROOT", general)
    monkeypatch.setattr(guest, "OPENCODE_WORKSPACE_ROOT", opencode)

    resolved = guest._workspace_path("opencode-task-workspace/result.csv")

    assert resolved == opencode / "result.csv"
    assert guest._workspace_relative(resolved) == "opencode-task-workspace/result.csv"
    assert guest._workspace_path(".", allow_dot=True) == general


def test_opencode_workspace_alias_rejects_traversal_and_external_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    general = tmp_path / "general"
    opencode = tmp_path / "opencode"
    external = tmp_path / "external.txt"
    general.mkdir()
    opencode.mkdir()
    external.write_text("outside", encoding="utf-8")
    (opencode / "escape").symlink_to(external)
    monkeypatch.setattr(guest, "WORKSPACE_ROOT", general)
    monkeypatch.setattr(guest, "OPENCODE_WORKSPACE_ROOT", opencode)

    with pytest.raises(guest.AgentError, match="parent traversal"):
        guest._workspace_path("opencode-task-workspace/../general", allow_dot=True)
    with pytest.raises(guest.AgentError, match="outside"):
        guest._workspace_path("opencode-task-workspace/escape")
