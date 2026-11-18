"""Bounded host-side storage and validation for Telegram task files."""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MAX_UPLOAD_BYTES = 512 * 1024
MAX_ARCHIVE_ENTRIES = 12
MAX_ARCHIVE_TOTAL_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_RATIO = 100
MAX_TASK_FILES = 12
MAX_STORAGE_BYTES = 50 * 1024 * 1024
MAX_OUTBOUND_FILES = 10
MAX_OUTBOUND_TOTAL_BYTES = 4 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".txt", ".csv", ".json", ".png", ".jpg", ".jpeg", ".zip"}
_MIME_BY_EXTENSION = {
    ".pdf": "application/pdf", ".txt": "text/plain", ".csv": "text/csv",
    ".json": "application/json", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".zip": "application/zip",
}
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class PreparedFile:
    filename: str
    content: bytes
    mime_type: str


def safe_filename(value: str) -> str:
    """Return a short basename safe for a guest workspace path."""
    name = value.replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    name = _SAFE_NAME.sub("_", name).strip("._-")[:100]
    if not name or name in {".", ".."}:
        raise ValueError("file name is empty or unsafe")
    if Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise ValueError("file type is not supported")
    return name


def prepare_upload(filename: str, content: bytes, mime_type: str) -> tuple[PreparedFile, ...]:
    if not content or len(content) > MAX_UPLOAD_BYTES:
        raise ValueError(f"upload must be between 1 byte and {MAX_UPLOAD_BYTES} bytes")
    safe = safe_filename(filename)
    extension = Path(safe).suffix.lower()
    if extension != ".zip":
        _validate_content(extension, content)
        return (PreparedFile(safe, content, _MIME_BY_EXTENSION[extension]),)
    return (PreparedFile(safe, content, "application/zip"), *_extract_zip(content))


def _extract_zip(content: bytes) -> tuple[PreparedFile, ...]:
    import io

    output: list[PreparedFile] = []
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = [entry for entry in archive.infolist() if not entry.is_dir()]
            if len(entries) > MAX_ARCHIVE_ENTRIES:
                raise ValueError("ZIP contains too many files")
            names: set[str] = set()
            for entry in entries:
                path = PurePosixPath(entry.filename)
                mode = entry.external_attr >> 16
                if path.is_absolute() or ".." in path.parts or "\\" in entry.filename or stat.S_ISLNK(mode):
                    raise ValueError("ZIP contains an unsafe path or link")
                if len(path.parts) > 5 or not path.name:
                    raise ValueError("ZIP path is too deep")
                name = safe_filename(path.name)
                if Path(name).suffix.lower() == ".zip":
                    raise ValueError("nested ZIP files are not supported")
                if name in names:
                    raise ValueError("ZIP contains duplicate file names")
                names.add(name)
                if entry.flag_bits & 1:
                    raise ValueError("encrypted ZIP files are not supported")
                total += entry.file_size
                if total > MAX_ARCHIVE_TOTAL_BYTES:
                    raise ValueError("ZIP expands beyond the configured limit")
                if entry.file_size and entry.compress_size == 0:
                    raise ValueError("ZIP has an invalid compression ratio")
                if entry.compress_size and entry.file_size / entry.compress_size > MAX_ARCHIVE_RATIO:
                    raise ValueError("ZIP compression ratio exceeds the configured limit")
                with archive.open(entry) as stream:
                    data = stream.read(min(entry.file_size, MAX_UPLOAD_BYTES) + 1)
                if len(data) != entry.file_size or len(data) > MAX_UPLOAD_BYTES:
                    raise ValueError("ZIP member exceeds the per-file limit")
                extension = Path(name).suffix.lower()
                _validate_content(extension, data)
                output.append(PreparedFile(name, data, _MIME_BY_EXTENSION[extension]))
    except zipfile.BadZipFile as exc:
        raise ValueError("ZIP archive is invalid") from exc
    if not output:
        raise ValueError("ZIP contains no supported files")
    if len(output) + 1 > MAX_TASK_FILES:
        raise ValueError("ZIP exceeds the task file count limit")
    return tuple(output)


def _validate_content(extension: str, content: bytes) -> None:
    if extension == ".pdf" and b"%PDF-" not in content[:1024]:
        raise ValueError("PDF signature is missing")
    if extension == ".png" and not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("PNG signature is invalid")
    if extension in {".jpg", ".jpeg"} and not content.startswith(b"\xff\xd8\xff"):
        raise ValueError("JPEG signature is invalid")
    if extension in {".txt", ".csv", ".json"}:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("text files must use UTF-8 encoding") from exc
        if extension == ".json":
            try:
                json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError("JSON document is invalid") from exc


class AttachmentStorage:
    """Owner-only opaque-file storage; model-facing paths are never host paths."""

    def __init__(self, root: Path):
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise RuntimeError("attachment storage must be a real directory")
        if root.stat().st_uid != os.getuid() or stat.S_IMODE(root.stat().st_mode) & 0o077:
            raise RuntimeError("attachment storage must be owner-only")
        self.root = root

    def save(self, content: bytes) -> str:
        current = 0
        count = 0
        for item in self.root.iterdir():
            if item.is_symlink() or not item.is_file():
                raise RuntimeError("attachment storage contains an unexpected entry")
            current += item.stat().st_size
            count += 1
        if current + len(content) > MAX_STORAGE_BYTES or count >= 500:
            raise ValueError("host attachment storage limit reached")
        key = secrets.token_hex(24)
        path = self.root / key
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        return key

    def read(self, key: str) -> bytes:
        if not re.fullmatch(r"[0-9a-f]{48}", key):
            raise ValueError("invalid attachment storage key")
        path = self.root / key
        if path.is_symlink():
            raise RuntimeError("attachment storage file must not be a symlink")
        return path.read_bytes()
