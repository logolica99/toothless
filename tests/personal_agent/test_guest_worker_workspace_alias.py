from __future__ import annotations

import base64
import stat
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


def test_worker_file_transfer_makes_opencode_workspace_file_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    general = tmp_path / "general"
    opencode = tmp_path / "opencode"
    general.mkdir()
    opencode.mkdir()
    monkeypatch.setattr(guest, "WORKSPACE_ROOT", general)
    monkeypatch.setattr(guest, "OPENCODE_WORKSPACE_ROOT", opencode)
    result = guest._write_file({
        "path": "opencode-task-workspace/inputs/task-id/input.csv",
        "content_base64": base64.b64encode(b"x\n1\n").decode("ascii"),
    })

    assert result["size_bytes"] == 4
    assert (opencode / "inputs/task-id/input.csv").read_bytes() == b"x\n1\n"
    assert stat.S_IMODE((opencode / "inputs/task-id/input.csv").stat().st_mode) == 0o644
