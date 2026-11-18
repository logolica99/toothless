from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from nanobot.personal_agent import telegram_files
from nanobot.personal_agent.telegram_files import AttachmentStorage, prepare_upload, safe_filename


def _zip(entries: list[tuple[str, bytes]], *, compression: int = zipfile.ZIP_STORED) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return output.getvalue()


def test_safe_names_and_plain_supported_upload() -> None:
    assert safe_filename("../../report.csv") == "report.csv"
    with pytest.raises(ValueError, match="not supported"):
        safe_filename("program.sh")
    prepared = prepare_upload("data.csv", b"n\n1\n", "text/csv")
    assert [(item.filename, item.content) for item in prepared] == [("data.csv", b"n\n1\n")]


def test_zip_members_are_returned_without_filesystem_extraction() -> None:
    prepared = prepare_upload("inputs.zip", _zip([("folder/data.csv", b"x\n1\n")]), "application/zip")
    assert [item.filename for item in prepared] == ["inputs.zip", "data.csv"]
    assert prepared[1].content == b"x\n1\n"


@pytest.mark.parametrize("name", ["../escape.csv", "/absolute.csv", "folder/../../escape.csv"])
def test_zip_path_traversal_is_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="unsafe path"):
        prepare_upload("inputs.zip", _zip([(name, b"x")]), "application/zip")


def test_zip_archive_bomb_and_nested_archives_are_rejected() -> None:
    bomb = _zip([("large.txt", b"a" * 100_000)], compression=zipfile.ZIP_DEFLATED)
    with pytest.raises(ValueError, match="ratio"):
        prepare_upload("bomb.zip", bomb, "application/zip")
    with pytest.raises(ValueError, match="nested ZIP"):
        prepare_upload("outer.zip", _zip([("inner.zip", b"PK\x03\x04")]), "application/zip")


def test_upload_byte_limit_is_enforced() -> None:
    with pytest.raises(ValueError, match="upload must be between"):
        prepare_upload("large.csv", b"x" * (512 * 1024 + 1), "text/csv")


def test_host_attachment_storage_has_a_hard_quota(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(telegram_files, "MAX_STORAGE_BYTES", 2)
    storage = AttachmentStorage(tmp_path / "attachments")
    storage.save(b"ab")
    with pytest.raises(ValueError, match="storage limit"):
        storage.save(b"c")
