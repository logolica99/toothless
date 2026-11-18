"""Owner-only Telegram polling for the limited Toothless Phase 03 commands."""

from __future__ import annotations

import argparse
import asyncio
import base64
import html
import ipaddress
import logging
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, ContextManager, Generator, cast
from urllib.parse import quote

import httpx

from nanobot.personal_agent.opencode_task import (
    OpenCodeTaskRunner,
    needs_browser_profile,
    parse_task_result,
)
from nanobot.personal_agent.task_store import TaskStatus, TaskStore, validate_task_id
from nanobot.personal_agent.telegram_files import (
    MAX_OUTBOUND_FILES,
    MAX_OUTBOUND_TOTAL_BYTES,
    MAX_TASK_FILES,
    MAX_UPLOAD_BYTES,
    AttachmentStorage,
    prepare_upload,
    safe_filename,
)
from nanobot.worker.execution_store import ExecutionRecord, SqliteExecutionStore
from nanobot.worker.interface import (
    ExecutionStatus,
    WorkerErrorCode,
    WorkerInterface,
    WorkerOperationError,
    WorkerUnavailableError,
    validate_execution_id,
)
from nanobot.worker.ssh_backend import SSHWorkerConfig, create_persistent_worker

LOGGER = logging.getLogger("toothless.telegram")
WORKER_NAME = "toothless-worker"
TART_HOME = "/Volumes/toothless/Tools/Tart"
TART_PATH = "/usr/local/bin:/Volumes/toothless/Tools/bin:/usr/bin:/bin"
MAX_POLL_WAIT_SECONDS = 30
MAX_TELEGRAM_MESSAGE_CHARS = 3900
OUTPUT_PREVIEW_CHARS = 700
AUTONOMY_NOTICE = "Task saved as queued. It will not run until you approve it with /run <task_id>."


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    token: str
    owner_id: int
    database: Path

    @classmethod
    def from_environment(cls) -> TelegramConfig:
        env_file = Path(os.environ.get(
            "TOOTHLESS_TELEGRAM_ENV_FILE",
            "~/.config/toothless/telegram.env",
        )).expanduser()
        if env_file.exists():
            repository_root = Path(__file__).resolve().parents[2]
            if env_file.resolve().is_relative_to(repository_root):
                raise RuntimeError("Telegram configuration file must be outside the repository")
            mode = stat.S_IMODE(env_file.stat().st_mode)
            if mode & 0o077:
                raise RuntimeError("Telegram configuration file must have permissions 0600")
            for line in env_file.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                key, separator, value = stripped.partition("=")
                if not separator or key not in {"TOOTHLESS_TELEGRAM_BOT_TOKEN", "TOOTHLESS_TELEGRAM_OWNER_ID"}:
                    raise RuntimeError("Telegram configuration contains an unsupported setting")
                os.environ.setdefault(key, value.strip().strip("\"'"))

        token = os.environ.get("TOOTHLESS_TELEGRAM_BOT_TOKEN", "").strip()
        owner_raw = os.environ.get("TOOTHLESS_TELEGRAM_OWNER_ID", "").strip()
        if not token or not owner_raw.isdecimal() or int(owner_raw) <= 0:
            raise RuntimeError(
                "Configure a BotFather token and exact numeric owner ID in the protected Telegram env file"
            )
        database = Path(os.environ.get(
            "TOOTHLESS_TELEGRAM_DB",
            "~/.local/share/toothless/telegram/updates.sqlite3",
        )).expanduser()
        return cls(token=token, owner_id=int(owner_raw), database=database)


class UpdateStore:
    """Host-owned durable update deduplication, separate from worker records."""

    def __init__(self, path: Path):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.parent.is_symlink() or path.is_symlink():
            raise RuntimeError("Telegram update database path must not contain symlinks")
        parent_info = path.parent.stat()
        if parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) & 0o077:
            raise RuntimeError("Telegram update database directory must be owner-only and owned by this user")
        self._db = sqlite3.connect(path, timeout=10, isolation_level=None)
        os.chmod(path, 0o600)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS telegram_updates ("
            "update_id INTEGER PRIMARY KEY, processed_at REAL NOT NULL)"
        )

    def next_offset(self) -> int:
        row = self._db.execute("SELECT MAX(update_id) FROM telegram_updates").fetchone()
        return int(row[0]) + 1 if row and row[0] is not None else 0

    def claim(self, update_id: int) -> bool:
        cursor = self._db.execute(
            "INSERT OR IGNORE INTO telegram_updates(update_id, processed_at) VALUES (?, ?)",
            (update_id, time.time()),
        )
        return cursor.rowcount == 1

    def contains(self, update_id: int) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM telegram_updates WHERE update_id = ?", (update_id,)
        ).fetchone()
        return row is not None

    def close(self) -> None:
        self._db.close()


@contextmanager
def _remote_worker(
    accepted_callback: Callable[[str], None] | None = None,
) -> Generator[WorkerInterface, None, None]:
    """Create the existing strict SSH interface only for a running worker."""
    if _worker_state() != "running":
        raise WorkerUnavailableError("Ubuntu worker is not running")
    env = {"TART_HOME": TART_HOME, "PATH": TART_PATH, "HOME": str(Path.home()), "LANG": "C.UTF-8"}
    try:
        address = subprocess.run(
            ["tart", "ip", WORKER_NAME],
            check=True,
            capture_output=True,
            text=True,
            timeout=8,
            env=env,
        ).stdout.strip()
        address = str(ipaddress.IPv4Address(address))
        host_key_result = subprocess.run(
            ["tart", "exec", WORKER_NAME, "sudo", "-n", "cat", "/etc/ssh/ssh_host_ed25519_key.pub"],
            check=True,
            capture_output=True,
            text=True,
            timeout=8,
            env=env,
        )
        key_parts = host_key_result.stdout.strip().split()
        if len(key_parts) < 2 or key_parts[0] != "ssh-ed25519":
            raise ValueError("Tart returned an unexpected guest host key")
        base64.b64decode(key_parts[1], validate=True)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise WorkerUnavailableError("Could not obtain the running worker's trusted SSH identity") from exc

    known_hosts_fd, known_hosts_name = tempfile.mkstemp(prefix="toothless-worker-known-hosts-")
    known_hosts = Path(known_hosts_name)
    try:
        with os.fdopen(known_hosts_fd, "w", encoding="utf-8") as known_hosts_file:
            known_hosts_file.write(f"{address} {key_parts[0]} {key_parts[1]}\n")
        config = SSHWorkerConfig(
            host=address,
            username="admin",
            identity_file=Path.home() / ".ssh" / "id_ed25519",
            known_hosts_file=known_hosts,
        )
        yield create_persistent_worker(
            config, execution_store=_execution_store(), accepted_callback=accepted_callback
        )
    except (OSError, ValueError) as exc:
        raise WorkerUnavailableError("Pinned-key SSH worker configuration is unavailable") from exc
    finally:
        known_hosts.unlink(missing_ok=True)


def _clean_text(value: str, *, limit: int) -> str:
    output: list[str] = []
    for char in value:
        if char in "\n\t" or not unicodedata.category(char).startswith("C"):
            output.append(char)
    cleaned = "".join(output)
    if len(cleaned) > limit:
        return cleaned[:limit] + "… [truncated]"
    return cleaned


def _telegram_html(value: str, *, limit: int) -> str:
    """Render common Markdown as safely escaped Telegram HTML."""
    text = _clean_text(value, limit=limit)
    text = re.sub(r"\\([^\w\s])", r"\1", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+(.+)$", r"**\1**", text)
    text = re.sub(r"(?m)^\s*[-*+]\s+", "• ", text)
    text = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r"\1 (\2)", text)

    def format_text(segment: str) -> str:
        rendered = html.escape(segment, quote=False)
        rendered = re.sub(
            r"\btask-[0-9a-f]{32}\b",
            lambda match: f"<code>{match.group(0)}</code>",
            rendered,
        )
        rendered = re.sub(
            r"\*\*(.+?)\*\*|__(.+?)__",
            lambda match: f"<b>{match.group(1) or match.group(2)}</b>",
            rendered,
        )
        rendered = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", rendered)
        rendered = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"<i>\1</i>", rendered)
        rendered = re.sub(r"~~(.+?)~~", r"<s>\1</s>", rendered)
        return rendered

    parts = re.split(r"(```.*?```|`[^`\n]+`)", text, flags=re.DOTALL)
    rendered_parts: list[str] = []
    for part in parts:
        if part.startswith("```") and part.endswith("```"):
            code = part[3:-3]
            _, separator, code = code.partition("\n")
            if not separator:
                code = part[3:-3]
            rendered_parts.append(f"<pre>{html.escape(code, quote=False)}</pre>")
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            rendered_parts.append(f"<code>{html.escape(part[1:-1], quote=False)}</code>")
        else:
            rendered_parts.append(format_text(part))
    return "".join(rendered_parts)


def _record_state(record: ExecutionRecord) -> str:
    if record.dispatch_state == "not_accepted":
        return "unavailable"
    if record.error_code == WorkerErrorCode.WORKER_UNAVAILABLE.value:
        return "unavailable"
    return record.status.value


def _failure_state(error: WorkerOperationError) -> str:
    if error.code is WorkerErrorCode.WORKER_UNAVAILABLE:
        return "unavailable"
    if error.code is WorkerErrorCode.EXECUTION_UNKNOWN:
        return "uncertain"
    return f"unknown ({error.code.value})"


def _timestamp(value: datetime | None) -> str:
    return value.isoformat(timespec="seconds") if value is not None else "—"


def _terminal(status: ExecutionStatus) -> bool:
    return status in {
        ExecutionStatus.SUCCEEDED,
        ExecutionStatus.FAILED,
        ExecutionStatus.TIMED_OUT,
        ExecutionStatus.CANCELLED,
    }


def _worker_state() -> str:
    env = {"TART_HOME": TART_HOME, "PATH": TART_PATH, "HOME": str(Path.home()), "LANG": "C.UTF-8"}
    try:
        result = subprocess.run(
            ["tart", "get", WORKER_NAME],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable (could not query Tart)"
    if result.returncode != 0:
        return "unavailable (VM not found or Tart query failed)"
    lines = result.stdout.splitlines()
    if len(lines) < 2:
        return "unknown (Tart returned an unrecognized status)"
    state = lines[1].split()[-1].lower()
    if state not in {"running", "stopped"}:
        return "unknown (Tart returned an unrecognized state)"
    return state


def _execution_store() -> SqliteExecutionStore:
    database = os.environ.get("TOOTHLESS_EXECUTION_DB")
    if database:
        return SqliteExecutionStore(Path(database).expanduser())
    return SqliteExecutionStore.default()


def _task_store(update_database: Path) -> TaskStore:
    database = os.environ.get("TOOTHLESS_TASK_DB")
    if database:
        return TaskStore(Path(database).expanduser())
    return TaskStore(update_database.parent / "tasks.sqlite3")


def _message_identity(message: dict[str, Any]) -> tuple[int, int, str] | None:
    sender = message.get("from")
    chat = message.get("chat")
    if not isinstance(sender, dict) or not isinstance(chat, dict):
        return None
    sender = cast(dict[str, Any], sender)
    chat = cast(dict[str, Any], chat)
    user_id = sender.get("id")
    chat_id = chat.get("id")
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        return None
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        return None
    return user_id, chat_id, str(chat.get("type", ""))


def _safe_telegram_file_path(value: str) -> bool:
    from pathlib import PurePosixPath

    path = PurePosixPath(value)
    return (
        len(value) <= 1024
        and not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in value
        and "\x00" not in value
    )


def _mime_type(filename: str) -> str:
    return {
        ".pdf": "application/pdf",
        ".txt": "text/plain",
        ".csv": "text/csv",
        ".json": "application/json",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
    }[Path(filename).suffix.lower()]


def _expects_task_files(request: str) -> bool:
    return bool(re.search(r"\b(file|save|csv|png|jpg|pdf|json|script|graph|export|spreadsheet|image)\b", request, re.IGNORECASE))


def command_reply(text: str, *, worker_state: str) -> str:
    command = text.strip().split(maxsplit=1)[0].split("@", maxsplit=1)[0].lower() if text.strip() else ""
    if command == "/start":
        return "Welcome to Toothless. Use /help to see the commands currently available."
    if command == "/ping":
        return "Toothless is online."
    if command == "/help":
        return (
            "Commands: /new <request>, /run <task_id>, /tasks, /task <task_id>, /update <task_id> <instructions>, "
            "/pause <task_id>, /resume <task_id>, /cancel <task_id|execution_id>, "
            "/executions, /execution <execution_id>, /start, /ping, /help, /worker, /status. "
            "Attach a supported file with a task caption to create a queued file task. Upload limit: 512 KiB. "
            "Workspace tasks require explicit /run approval; browser tasks use a separate profile."
        )
    if command == "/worker":
        return f"Ubuntu worker: {worker_state}. This query does not start or change the VM."
    if command == "/status":
        return (
            "Controller: connected (Telegram polling active)\n"
            f"Ubuntu worker: {worker_state}\n"
            "Phase 05: owner-approved OpenCode tasks run in Ubuntu; workspace and browser profiles stay separate."
        )
    if command.startswith("/"):
        return "Unknown command. Use /help to see the commands currently available."
    return AUTONOMY_NOTICE


class TelegramController:
    def __init__(
        self,
        config: TelegramConfig,
        *,
        client: httpx.AsyncClient | None = None,
        execution_store: SqliteExecutionStore | None = None,
        task_store: TaskStore | None = None,
        worker_provider: Callable[[], ContextManager[WorkerInterface]] = _remote_worker,
    ):
        self.config = config
        self.store = UpdateStore(config.database)
        self.execution_store = (
            execution_store if execution_store is not None else _execution_store()
        )
        self.task_store = task_store if task_store is not None else _task_store(config.database)
        self._attachment_storage = AttachmentStorage(self.task_store.path.parent / "attachments")
        self._worker_provider = worker_provider
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(40, connect=10))
        self._owns_client = client is None
        self._base_url = f"https://api.telegram.org/bot{config.token}"
        self._last_poll_ok = 0.0
        self._active_task_run: asyncio.Task[None] | None = None

    async def _api(self, method: str, payload: dict[str, Any]) -> Any:
        response: httpx.Response | None = None
        for attempt in range(4):
            response = await self._client.post(f"{self._base_url}/{method}", json=payload)
            if response.status_code != 429:
                break
            try:
                retry_after = response.json().get("parameters", {}).get("retry_after", 5)
                delay = min(max(float(retry_after), 1), 60)
            except (ValueError, TypeError, AttributeError):
                delay = 5
            if attempt == 3:
                raise RuntimeError("Telegram rate limit persisted after bounded retries")
            await asyncio.sleep(delay)
        if response is None:
            raise RuntimeError("Telegram request did not produce a response")
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise RuntimeError("Telegram API returned an unsuccessful result")
        body = cast(dict[str, Any], body)
        if body.get("ok") is not True:
            raise RuntimeError("Telegram API returned an unsuccessful result")
        return body.get("result")

    async def _handle(self, update: dict[str, Any]) -> None:
        update_id = update.get("update_id")
        if not isinstance(update_id, int) or isinstance(update_id, bool):
            return
        if self.store.contains(update_id):
            return
        message_raw = update.get("message")
        if not isinstance(message_raw, dict):
            self.store.claim(update_id)
            return
        message_data = cast(dict[str, Any], message_raw)
        identity = _message_identity(message_data)
        if identity is None:
            self.store.claim(update_id)
            return
        user_id, chat_id, chat_type = identity
        if user_id != self.config.owner_id or chat_type != "private":
            self.store.claim(update_id)
            return
        if "document" in message_data or "photo" in message_data:
            await self._handle_file_update(update_id, chat_id, message_data)
            return
        message = message_data.get("text")
        if not isinstance(message, str):
            self.store.claim(update_id)
            return
        words = message.strip().split(maxsplit=1)
        command = words[0].split("@", maxsplit=1)[0].lower() if words else ""
        argument = words[1].strip() if len(words) == 2 else ""
        is_new_task = command == "/new" or (bool(message.strip()) and not command.startswith("/"))
        if is_new_task:
            reply = self._create_task_reply(update_id, argument if command == "/new" else message)
            # A redelivery can recover this record using the unique source update ID.
            if not self.store.claim(update_id):
                return
        else:
            if not self.store.claim(update_id):
                return
            if command == "/tasks":
                reply = self._tasks_reply()
            elif command == "/task":
                reply = self._task_reply(argument)
            elif command == "/run":
                reply = self._start_task_reply(argument, chat_id)
            elif command == "/cancel":
                reply = await self._cancel_reply(argument)
            elif command == "/pause":
                reply = self._transition_task_reply(argument, TaskStatus.PAUSED)
            elif command == "/resume":
                reply = self._transition_task_reply(argument, TaskStatus.QUEUED)
            elif command == "/update":
                task_id, separator, instructions = argument.partition(" ")
                reply = self._update_task_reply(task_id, instructions.strip() if separator else "")
            elif command == "/executions":
                reply = self._executions_reply()
            elif command == "/execution":
                reply = await self._execution_reply(argument)
            else:
                worker_state = _worker_state() if command in {"/worker", "/status"} else "unknown"
                reply = command_reply(message, worker_state=worker_state)
        await self._send_text(chat_id, reply)
        if command in {
            "/start", "/ping", "/help", "/worker", "/status", "/tasks", "/task", "/cancel",
            "/new", "/run", "/pause", "/resume", "/update", "/executions", "/execution",
        }:
            LOGGER.info("Handled authorized Telegram command %s", command)
        elif command.startswith("/"):
            LOGGER.info("Handled authorized unknown Telegram command")
        else:
            LOGGER.info("Handled authorized text message; autonomous dispatch is disabled")

    async def _handle_file_update(
        self, update_id: int, chat_id: int, message: dict[str, Any]
    ) -> None:
        file_id: str
        filename: str
        mime_type: str
        declared_size: int | None
        document = message.get("document")
        if isinstance(document, dict):
            document = cast(dict[str, Any], document)
            file_id = str(document.get("file_id", ""))
            raw_name = document.get("file_name")
            if not isinstance(raw_name, str):
                await self._reject_upload(update_id, chat_id, "Please send a named PDF, TXT, CSV, JSON, PNG, JPG, or small ZIP file.")
                return
            try:
                filename = safe_filename(raw_name)
            except ValueError as exc:
                await self._reject_upload(update_id, chat_id, str(exc))
                return
            mime_type = str(document.get("mime_type", "application/octet-stream"))
            declared_size = document.get("file_size") if isinstance(document.get("file_size"), int) else None
        else:
            photos = message.get("photo")
            if not isinstance(photos, list) or not photos or not isinstance(photos[-1], dict):
                await self._reject_upload(update_id, chat_id, "The Telegram photo attachment was invalid.")
                return
            photo = cast(dict[str, Any], photos[-1])
            file_id = str(photo.get("file_id", ""))
            filename, mime_type = "telegram-photo.jpg", "image/jpeg"
            declared_size = photo.get("file_size") if isinstance(photo.get("file_size"), int) else None
        if not file_id or len(file_id) > 512:
            await self._reject_upload(update_id, chat_id, "The Telegram file reference was invalid.")
            return
        if declared_size is not None and not 0 < declared_size <= MAX_UPLOAD_BYTES:
            await self._reject_upload(update_id, chat_id, f"Files must be no larger than {MAX_UPLOAD_BYTES // 1024} KiB.")
            return
        caption = message.get("caption", "")
        caption = caption.strip() if isinstance(caption, str) else ""
        request = caption.removeprefix("/new ").strip() if caption else f"Review and summarize the attached file {filename}."
        try:
            file_info = await self._api("getFile", {"file_id": file_id})
            if not isinstance(file_info, dict):
                raise ValueError("Telegram file metadata was invalid")
            file_info = cast(dict[str, Any], file_info)
            file_path = file_info.get("file_path")
            file_size = file_info.get("file_size", declared_size)
            if not isinstance(file_path, str) or not _safe_telegram_file_path(file_path):
                raise ValueError("Telegram returned an invalid file path")
            if not isinstance(file_size, int) or not 0 < file_size <= MAX_UPLOAD_BYTES:
                raise ValueError(f"Files must be no larger than {MAX_UPLOAD_BYTES // 1024} KiB.")
            content = await self._download_telegram_file(file_path, file_size)
            prepared = prepare_upload(filename, content, mime_type)
            if len(prepared) > MAX_TASK_FILES:
                raise ValueError("The upload contains too many files")
            if not request:
                raise ValueError("Add a short task request in the file caption")
            staged = [(item, self._attachment_storage.save(item.content)) for item in prepared]
            task, created = self.task_store.create_from_update(update_id, request)
            for item, storage_key in staged:
                self.task_store.add_attachment(
                    task.task_id,
                    telegram_file_id=file_id,
                    filename=item.filename,
                    mime_type=item.mime_type,
                    size_bytes=len(item.content),
                    storage_key=storage_key,
                    caption=caption,
                )
            if not self.store.claim(update_id):
                return
            if created:
                reply = (
                    f"**File received.** Task {task.task_id} is queued.\n"
                    f"Files: {', '.join(item.filename for item in prepared)}\n\n"
                    f"To approve execution, send:\n`/run {task.task_id}`"
                )
            else:
                reply = f"Upload update was already recorded as task {task.task_id}; it was not duplicated."
        except (httpx.HTTPError, RuntimeError, ValueError, OSError) as exc:
            # Do not include request URLs, Telegram file paths, or local paths in logs/replies.
            self.store.claim(update_id)
            reason = str(exc) if isinstance(exc, ValueError) else "Telegram file transfer failed"
            reply = f"File was not accepted: {_clean_text(reason, limit=180)}. No task was started."
            LOGGER.warning("Authorized Telegram upload failed (%s)", type(exc).__name__)
        await self._send_text(chat_id, reply)

    async def _reject_upload(self, update_id: int, chat_id: int, message: str) -> None:
        self.store.claim(update_id)
        await self._send_text(chat_id, f"File was not accepted: {_clean_text(message, limit=200)}.")

    async def _download_telegram_file(self, file_path: str, expected_size: int) -> bytes:
        url = f"https://api.telegram.org/file/bot{self.config.token}/{quote(file_path, safe='/')}"
        output = bytearray()
        async with self._client.stream("GET", url, timeout=httpx.Timeout(30, connect=10)) as response:
            response.raise_for_status()
            header = response.headers.get("content-length")
            if header and (not header.isdecimal() or int(header) > MAX_UPLOAD_BYTES):
                raise ValueError("Telegram file exceeds the configured size limit")
            async for chunk in response.aiter_bytes():
                output.extend(chunk)
                if len(output) > MAX_UPLOAD_BYTES:
                    raise ValueError("Telegram file exceeds the configured size limit")
        if len(output) != expected_size:
            raise ValueError("Telegram file size did not match its metadata")
        return bytes(output)

    async def _send_text(self, chat_id: int, text: str) -> None:
        await self._api("sendMessage", {
            "chat_id": chat_id,
            "text": _telegram_html(text, limit=MAX_TELEGRAM_MESSAGE_CHARS),
            "parse_mode": "HTML",
        })

    def _tasks_reply(self) -> str:
        tasks = self.task_store.list_latest(10)
        if not tasks:
            return "No tasks found. Send /new <request> or describe a task in a message."
        rows = ["Recent tasks (queued tasks have not started):"]
        for task in tasks:
            rows.append(f"{task.task_id} — {task.status.value} — {_timestamp(task.created_at)}")
        return "\n".join(rows)

    def _executions_reply(self) -> str:
        records = self.execution_store.list_latest(10)
        if not records:
            return "No worker executions found."
        rows = ["Latest worker executions (host records):"]
        for record in records:
            rows.append(
                f"{record.execution_id[:12]} — {_record_state(record)} — "
                f"started {_timestamp(record.started_at or record.created_at)}"
            )
        return "\n".join(rows)

    def _task_reply(self, task_id: str) -> str:
        if not task_id:
            return "Usage: /task <task_id>"
        try:
            validate_task_id(task_id)
        except ValueError:
            return "Invalid task ID. Use the ID shown by /tasks."
        task = self.task_store.get(task_id)
        if task is None:
            return "Task not found in the host task store."
        lines = [
            f"Task: {task.task_id}",
            f"Status: {task.status.value}",
            f"Created: {_timestamp(task.created_at)}",
            f"Updated: {_timestamp(task.updated_at)}",
            f"Progress: {_clean_text(task.progress, limit=320)}",
            f"Request: {_clean_text(task.request, limit=900)}",
        ]
        if task.instructions:
            lines.append(f"Additional instructions: {_clean_text(task.instructions, limit=900)}")
        if task.error:
            lines.append(f"Error: {_clean_text(task.error, limit=320)}")
        if task.result:
            lines.extend(("Result:", _clean_text(task.result, limit=OUTPUT_PREVIEW_CHARS * 4)))
        if task.notification_error:
            lines.append("Telegram delivery: failed; result remains saved on the host.")
        deliveries = self.task_store.list_file_deliveries(task_id)
        if deliveries:
            lines.append("Generated files:")
            lines.extend(f"• {item.filename}: {item.status}" for item in deliveries)
        if task.execution_ids:
            lines.append("Worker executions: " + ", ".join(task.execution_ids[:10]))
        else:
            lines.append("Worker executions: none; AI execution has not started.")
        return "\n".join(lines)

    def _create_task_reply(self, update_id: int, request: str) -> str:
        if not request.strip():
            return "Usage: /new <task description>"
        try:
            task, created = self.task_store.create_from_update(update_id, request)
        except ValueError as exc:
            return f"Could not create task: {_clean_text(str(exc), limit=240)}"
        if created:
            return (
                f"Task {task.task_id} is queued.\n\n"
                f"To start it, send:\n`/run {task.task_id}`"
            )
        return f"Task {task.task_id} was already recorded as {task.status.value}; it was not duplicated."

    def _start_task_reply(self, task_id: str, chat_id: int) -> str:
        if not task_id:
            return "Usage: /run <task_id>"
        try:
            validate_task_id(task_id)
        except ValueError:
            return "Invalid task ID. Use the ID shown by /tasks."
        candidate = self.task_store.get(task_id)
        if candidate is not None and candidate.attachments and needs_browser_profile(candidate.request + "\n" + candidate.instructions):
            return "This task has files and cannot use the isolated browser-only profile. Remove the attachment or create a workspace-only task."
        if self._active_task_run is not None and not self._active_task_run.done():
            active = next(iter(self.task_store.active_tasks()), None)
            if active is not None:
                return f"Task {active.task_id} already owns the single task runner ({active.status.value})."
        execution_id = OpenCodeTaskRunner.new_execution_id()
        task, claimed = self.task_store.claim_for_dispatch(task_id, execution_id)
        if task is None:
            return "Task not found in the host task store."
        if not claimed:
            return f"Task {task.task_id} is {task.status.value}; it was not submitted again."
        self._active_task_run = asyncio.create_task(
            self._execute_task(task, execution_id, chat_id),
            name=f"toothless-task-{task.task_id}",
        )
        return f"Task {task.task_id} is dispatching to the Ubuntu worker. I will report running only after worker acknowledgement."

    async def _execute_task(self, task: Any, execution_id: str, chat_id: int) -> None:
        last_progress_minute = 0

        async def report_progress(elapsed_seconds: float) -> None:
            nonlocal last_progress_minute
            elapsed_minutes = int(elapsed_seconds // 60)
            if elapsed_minutes < 1 or elapsed_minutes <= last_progress_minute:
                return
            current = self.task_store.get(task.task_id)
            if current is None or current.status is not TaskStatus.RUNNING:
                return
            progress = (
                f"Still running on the Ubuntu worker ({elapsed_minutes} minute(s) elapsed); "
                "no result is available yet."
            )
            if not self.task_store.record_progress(task.task_id, progress):
                return
            last_progress_minute = elapsed_minutes
            try:
                await self._send_text(
                    chat_id,
                    f"Task `{task.task_id}` is still running on Ubuntu ({elapsed_minutes} minute(s)). "
                    "No result is available yet.",
                )
            except (httpx.HTTPError, RuntimeError, ValueError):
                LOGGER.warning("Telegram progress delivery failed for task %s", task.task_id)

        runner = OpenCodeTaskRunner(
            lambda: _remote_worker(accepted_callback=self.task_store.mark_worker_accepted),
            attachment_storage=self._attachment_storage,
            cancel_requested=lambda: (
                (current := self.task_store.get(task.task_id)) is None
                or current.status is TaskStatus.CANCELLING
            ),
            progress_callback=report_progress,
        )
        outcome = await runner.execute(task, execution_id)
        saved = self.task_store.finish_execution(
            execution_id, outcome.status, result=outcome.result, error=outcome.error
        )
        if saved is None:
            return
        if outcome.status is TaskStatus.SUCCEEDED and outcome.result:
            message = f"Task {saved.task_id} completed.\n\n{outcome.result}"
        elif outcome.status is TaskStatus.CANCELLED:
            if saved.cancellation_requested_at is not None:
                # /cancel already replies after worker termination; do not send a second notice here.
                return
            message = f"Task {saved.task_id} was cancelled by the worker."
        else:
            message = (
                f"Task {saved.task_id} ended as {outcome.status.value}: "
                f"{_clean_text(outcome.error or 'No further detail.', limit=500)}"
            )
        try:
            await self._send_text(chat_id, message)
        except (httpx.HTTPError, RuntimeError):
            self.task_store.record_notification_error(saved.task_id, "Telegram send failed after task completion.")
            LOGGER.warning("Telegram result delivery failed for task %s", saved.task_id)
        if outcome.status is TaskStatus.SUCCEEDED and _expects_task_files(saved.request + "\n" + saved.instructions):
            await self._send_task_files(saved.task_id, chat_id)

    async def _send_task_files(self, task_id: str, chat_id: int) -> None:
        if self._worker_provider is None:
            return
        root = f"opencode-task-workspace/exports/{task_id}"
        delivered_bytes = 0
        considered_files = 0
        try:
            with self._worker_provider() as worker:
                entries = await worker.list_files(root)
                for entry in entries:
                    if considered_files >= MAX_OUTBOUND_FILES:
                        break
                    if entry.kind.value != "file" or entry.size_bytes > 1024 * 1024:
                        continue
                    try:
                        filename = safe_filename(entry.name)
                    except ValueError:
                        continue
                    extension = Path(filename).suffix.lower()
                    if extension not in {".pdf", ".txt", ".csv", ".json", ".png", ".jpg", ".jpeg"}:
                        continue
                    if delivered_bytes + entry.size_bytes > MAX_OUTBOUND_TOTAL_BYTES:
                        continue
                    considered_files += 1
                    delivered_bytes += entry.size_bytes
                    guest_path = f"{root}/{filename}"
                    mime_type = _mime_type(filename)
                    if not self.task_store.begin_file_delivery(
                        task_id, guest_path=guest_path, filename=filename,
                        mime_type=mime_type, size_bytes=entry.size_bytes,
                    ):
                        continue
                    try:
                        content = await worker.read_file(guest_path)
                    except WorkerOperationError:
                        self.task_store.finish_file_delivery(
                            task_id, guest_path, status="failed", error="Guest file could not be read."
                        )
                        continue
                    if len(content) != entry.size_bytes or not content:
                        self.task_store.finish_file_delivery(
                            task_id, guest_path, status="failed", error="Guest file size changed before delivery."
                        )
                        continue
                    try:
                        result = await self._send_file_once(chat_id, filename, mime_type, content)
                    except httpx.TransportError:
                        self.task_store.finish_file_delivery(
                            task_id, guest_path, status="uncertain", error="Telegram transport ended before delivery was confirmed."
                        )
                        continue
                    except httpx.HTTPStatusError as exc:
                        delivery_status = "uncertain" if exc.response.status_code >= 500 else "failed"
                        self.task_store.finish_file_delivery(
                            task_id, guest_path, status=delivery_status,
                            error="Telegram did not confirm the file delivery."
                        )
                        continue
                    except (RuntimeError, ValueError):
                        self.task_store.finish_file_delivery(
                            task_id, guest_path, status="uncertain", error="Telegram response could not confirm delivery."
                        )
                        continue
                    if not isinstance(result, dict):
                        self.task_store.finish_file_delivery(
                            task_id, guest_path, status="failed", error="Telegram did not confirm the file message."
                        )
                        continue
                    result = cast(dict[str, Any], result)
                    message_id = result.get("message_id")
                    if not isinstance(message_id, int):
                        self.task_store.finish_file_delivery(
                            task_id, guest_path, status="failed", error="Telegram did not confirm the file message."
                        )
                        continue
                    self.task_store.finish_file_delivery(
                        task_id, guest_path, status="delivered", telegram_message_id=message_id
                    )
        except WorkerOperationError as exc:
            if exc.code.value == "file_not_found":
                return
            LOGGER.warning("Generated file retrieval failed for task %s", task_id)
        except (httpx.HTTPError, RuntimeError, ValueError):
            LOGGER.warning("Generated file retrieval failed for task %s", task_id)

    async def _send_file_once(self, chat_id: int, filename: str, mime_type: str, content: bytes) -> Any:
        method = "sendPhoto" if Path(filename).suffix.lower() in {".png", ".jpg", ".jpeg"} else "sendDocument"
        field = "photo" if method == "sendPhoto" else "document"
        response = await self._client.post(
            f"{self._base_url}/{method}",
            data={"chat_id": str(chat_id)},
            files={field: (filename, content, mime_type)},
            timeout=httpx.Timeout(40, connect=10),
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise RuntimeError("Telegram did not confirm the file message")
        body = cast(dict[str, Any], body)
        if body.get("ok") is not True:
            raise RuntimeError("Telegram did not confirm the file message")
        return body.get("result")

    def _transition_task_reply(self, argument: str, target: TaskStatus) -> str:
        if not argument:
            command = "/pause" if target is TaskStatus.PAUSED else "/resume"
            return f"Usage: {command} <task_id>"
        try:
            validate_task_id(argument)
        except ValueError:
            return "Invalid task ID. Use the ID shown by /tasks."
        task, changed = self.task_store.transition(argument, target)
        if task is None:
            return "Task not found in the host task store."
        if not changed:
            return f"Task {task.task_id} is {task.status.value}; that state change is not available."
        if target is TaskStatus.PAUSED:
            return f"Task {task.task_id} is paused. No AI execution has started."
        return f"Task {task.task_id} is queued again. AI execution remains disabled."

    def _update_task_reply(self, task_id: str, instructions: str) -> str:
        if not task_id or not instructions:
            return "Usage: /update <task_id> <instructions>"
        try:
            validate_task_id(task_id)
            task, changed = self.task_store.append_instruction(task_id, instructions)
        except ValueError as exc:
            return _clean_text(str(exc), limit=240)
        if task is None:
            return "Task not found in the host task store."
        if not changed:
            return f"Task {task.task_id} is {task.status.value}; instructions cannot be changed in this state."
        return f"Instructions added to task {task.task_id}. The task remains {task.status.value}; no work has started."

    async def _execution_reply(self, execution_id: str) -> str:
        if not execution_id:
            return "Usage: /execution <execution_id>"
        try:
            validate_execution_id(execution_id)
        except WorkerOperationError:
            return "Invalid execution ID. Use the ID shown by /executions."
        stored = self.execution_store.get(execution_id)
        if stored is None:
            return "Execution not found in the host record store."

        fresh_status = "not queried (stored status is terminal)" if _terminal(stored.status) else "not queried"
        current = stored
        if not _terminal(stored.status) and stored.dispatch_state != "not_accepted":
            try:
                with self._worker_provider() as worker:
                    snapshot = await worker.reconcile(execution_id)
                fresh_status = snapshot.status.value
                current = self.execution_store.get(execution_id) or stored
            except WorkerOperationError as exc:
                fresh_status = _failure_state(exc)
                current = self.execution_store.get(execution_id) or stored

        lines = [
            f"Execution: {current.execution_id}",
            f"Stored status: {stored.status.value}",
            f"Fresh reconciled status: {fresh_status}",
            f"Dispatch: {current.dispatch_state}",
            f"Created: {_timestamp(current.created_at)}",
            f"Accepted: {_timestamp(current.accepted_at)}",
            f"Started: {_timestamp(current.started_at)}",
            f"Finished: {_timestamp(current.finished_at)}",
            f"Exit code: {current.exit_code if current.exit_code is not None else '—'}",
        ]
        if current.error_code:
            message = _clean_text(current.error_message or "", limit=320)
            lines.append(f"Error: {current.error_code}" + (f" — {message}" if message else ""))
        lines.extend((
            "stdout:",
            _clean_text(current.stdout.decode("utf-8", errors="replace"), limit=OUTPUT_PREVIEW_CHARS) or "(empty)",
            "stderr:",
            _clean_text(current.stderr.decode("utf-8", errors="replace"), limit=OUTPUT_PREVIEW_CHARS) or "(empty)",
        ))
        return "\n".join(lines)

    async def _cancel_reply(self, execution_id: str) -> str:
        if not execution_id:
            return "Usage: /cancel <task_id|execution_id>"
        if execution_id.startswith("task-"):
            try:
                validate_task_id(execution_id)
            except ValueError:
                pass
            else:
                task = self.task_store.get(execution_id)
                if task is not None:
                    task, changed = self.task_store.request_task_cancellation(execution_id)
                    assert task is not None
                    if task.status is TaskStatus.CANCELLED and changed:
                        return f"Task {task.task_id} was cancelled before execution."
                    if task.status is TaskStatus.CANCELLED:
                        return f"Task {task.task_id} is already cancelled."
                    if not changed:
                        return f"Task {task.task_id} is {task.status.value}; cancellation was not applied."
                    if task.status is not TaskStatus.CANCELLING or not task.execution_ids:
                        return f"Task {task.task_id} cancellation intent is recorded; execution state is uncertain."
                    worker_execution_id = task.execution_ids[-1]
                    if self.execution_store.get(worker_execution_id) is None:
                        return f"Task {task.task_id} cancellation is recorded while preparing the upload; dispatch will stop before model execution."
                    self.execution_store.mark_cancel_requested(worker_execution_id)
                    try:
                        with self._worker_provider() as worker:
                            snapshot = await worker.terminate(worker_execution_id)
                    except WorkerOperationError as exc:
                        if exc.code is WorkerErrorCode.WORKER_UNAVAILABLE:
                            return f"Task {task.task_id} cancellation is recorded; the worker is unavailable, so termination is unconfirmed."
                        self.task_store.finish_execution(
                            worker_execution_id, TaskStatus.UNKNOWN,
                            error=f"Cancellation outcome is uncertain ({exc.code.value}); no resubmission occurred.",
                        )
                        return f"Task {task.task_id} cancellation is uncertain; check /task {task.task_id}."
                    if snapshot.status is ExecutionStatus.CANCELLED:
                        self.task_store.finish_execution(worker_execution_id, TaskStatus.CANCELLED)
                        return f"Task {task.task_id} was cancelled by the worker."
                    if snapshot.status is ExecutionStatus.SUCCEEDED:
                        outcome = parse_task_result(worker_execution_id, snapshot.stdout)
                        self.task_store.finish_execution(
                            worker_execution_id, outcome.status, result=outcome.result, error=outcome.error
                        )
                        return f"Task {task.task_id} was already complete; worker status is succeeded."
                    if snapshot.status in {ExecutionStatus.FAILED, ExecutionStatus.TIMED_OUT}:
                        self.task_store.finish_execution(
                            worker_execution_id, TaskStatus.FAILED,
                            error=f"Worker ended before cancellation: {snapshot.status.value}.",
                        )
                        return f"Task {task.task_id} was already terminal: {snapshot.status.value}."
                    return f"Task {task.task_id} cancellation is pending; the worker has not confirmed termination."
        try:
            validate_execution_id(execution_id)
        except WorkerOperationError:
            return "Invalid execution ID. Use the ID shown by /executions."
        record = self.execution_store.get(execution_id)
        if record is None:
            return "Execution not found in the host record store; nothing was cancelled."
        if _terminal(record.status):
            return f"Already terminal: {record.status.value}. No cancellation was sent."
        if record.dispatch_state == "not_accepted":
            return "Worker unavailable: this execution was never accepted by the worker."
        if record.dispatch_state == "history_missing":
            return "Uncertain: guest execution history is missing; no cancellation was sent."

        # Persist the operator's request before checking connectivity or sending SSH.
        self.execution_store.mark_cancel_requested(execution_id)
        try:
            with self._worker_provider() as worker:
                result = await worker.terminate(execution_id)
        except WorkerOperationError as exc:
            self.execution_store.record_error(execution_id, exc)
            if exc.code is WorkerErrorCode.WORKER_UNAVAILABLE:
                return "Worker unavailable. Cancellation is recorded and can be retried when the worker is available."
            if exc.code is WorkerErrorCode.EXECUTION_UNKNOWN:
                return "Uncertain: the worker accepted no confirmed cancellation result. The request is recorded; check /task."
            return f"Uncertain: {exc.code.value}. The cancellation request is recorded; check /task."
        if result.status is ExecutionStatus.CANCELLED:
            return "Cancellation succeeded; the worker reports the execution cancelled."
        if _terminal(result.status):
            return f"Already terminal: the worker reports {result.status.value}."
        if result.status is ExecutionStatus.UNKNOWN:
            return "Uncertain: the worker could not confirm the execution state. The cancellation request is recorded."
        return "Uncertain: the worker still reports the execution running after the cancellation request."

    async def run(self) -> None:
        # Validate the token before polling, while retrying startup network/rate failures.
        while True:
            try:
                await self._api("getMe", {})
                break
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {401, 403}:
                    raise RuntimeError("Telegram rejected the configured bot token") from None
                LOGGER.warning("Telegram startup check failed; retrying shortly")
                await asyncio.sleep(5)
            except (httpx.TransportError, RuntimeError):
                LOGGER.warning("Telegram startup check failed; retrying shortly")
                await asyncio.sleep(5)
        LOGGER.info("Telegram polling is connected; only the configured owner in private chat is accepted")
        await self._recover_active_tasks()
        while True:
            try:
                updates = await self._api("getUpdates", {
                    "offset": self.store.next_offset(),
                    "timeout": MAX_POLL_WAIT_SECONDS,
                    "allowed_updates": ["message"],
                })
                self._last_poll_ok = time.monotonic()
                if not isinstance(updates, list):
                    raise RuntimeError("Telegram returned malformed updates")
                updates = cast(list[Any], updates)
                for update in updates:
                    if isinstance(update, dict):
                        await self._handle(cast(dict[str, Any], update))
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, RuntimeError, ValueError, TypeError):
                LOGGER.warning("Telegram polling request failed; retrying shortly")
                await asyncio.sleep(5)

    async def close(self) -> None:
        if self._active_task_run is not None and not self._active_task_run.done():
            self._active_task_run.cancel()
            await asyncio.gather(self._active_task_run, return_exceptions=True)
        self.store.close()
        if self._owns_client:
            await self._client.aclose()

    async def _recover_active_tasks(self) -> None:
        """Reconcile host-owned execution IDs without resubmitting a command."""
        self.task_store.resolve_interrupted_file_deliveries()
        for task in self.task_store.active_tasks():
            if not task.execution_ids:
                self.task_store.mark_unresolved(task.task_id, "Dispatch intent has no worker execution ID.")
                continue
            execution_id = task.execution_ids[-1]
            record = self.execution_store.get(execution_id)
            if record is not None and record.dispatch_state == "not_accepted":
                self.task_store.finish_execution(
                    execution_id, TaskStatus.FAILED,
                    error="Worker confirmed the command was not accepted; no command ran.",
                )
                continue
            if record is None:
                self.task_store.mark_unresolved(
                    task.task_id,
                    "Controller restarted with unresolved dispatch history; command was not resubmitted.",
                )
                continue
            self._active_task_run = asyncio.create_task(
                self._reconcile_task(task, execution_id),
                name=f"toothless-reconcile-{task.task_id}",
            )
        for task in self.task_store.list_latest(100):
            if task.status is TaskStatus.SUCCEEDED and _expects_task_files(task.request + "\n" + task.instructions):
                await self._send_task_files(task.task_id, self.config.owner_id)

    async def _reconcile_task(self, task: Any, execution_id: str) -> None:
        try:
            with self._worker_provider() as worker:
                snapshot = await worker.reconcile(execution_id)
        except WorkerOperationError as exc:
            self.task_store.mark_unresolved(task.task_id, f"Restart reconciliation uncertain ({exc.code.value}); no resubmission occurred.")
            return
        if snapshot.status is ExecutionStatus.RUNNING:
            self.task_store.mark_worker_accepted(execution_id)
            self._active_task_run = asyncio.create_task(
                self._continue_reconciliation(task, execution_id),
                name=f"toothless-poll-{task.task_id}",
            )
            return
        await self._save_reconciled_result(task, execution_id, snapshot)

    async def _continue_reconciliation(self, task: Any, execution_id: str) -> None:
        while True:
            await asyncio.sleep(2)
            try:
                with self._worker_provider() as worker:
                    snapshot = await worker.reconcile(execution_id)
            except WorkerOperationError as exc:
                self.task_store.mark_unresolved(task.task_id, f"Worker reconciliation uncertain ({exc.code.value}); no resubmission occurred.")
                return
            if snapshot.status is ExecutionStatus.RUNNING:
                continue
            await self._save_reconciled_result(task, execution_id, snapshot)
            return

    async def _save_reconciled_result(self, task: Any, execution_id: str, snapshot: Any) -> None:
        outcome = parse_task_result(execution_id, snapshot.stdout, snapshot.status, snapshot.exit_code)
        saved = self.task_store.finish_execution(
            execution_id, outcome.status, result=outcome.result, error=outcome.error
        )
        if saved is None:
            return
        if outcome.status is TaskStatus.SUCCEEDED and outcome.result:
            message = f"Task {saved.task_id} completed after controller recovery.\n\n{outcome.result}"
        else:
            message = f"Task {saved.task_id} reconciled as {outcome.status.value}: {_clean_text(outcome.error or '', limit=500)}"
        try:
            await self._send_text(self.config.owner_id, message)
        except (httpx.HTTPError, RuntimeError):
            self.task_store.record_notification_error(saved.task_id, "Telegram recovery notification failed.")
        if outcome.status is TaskStatus.SUCCEEDED and _expects_task_files(saved.request + "\n" + saved.instructions):
            await self._send_task_files(saved.task_id, self.config.owner_id)


async def _run() -> None:
    config = TelegramConfig.from_environment()
    controller = TelegramController(config)
    try:
        await controller.run()
    finally:
        await controller.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Toothless owner-only Telegram controller")
    parser.parse_args()
    _configure_logging()
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        LOGGER.info("Telegram controller stopped")
    except Exception as exc:
        # Avoid formatting exception text because HTTP clients can include the token URL.
        LOGGER.error("Telegram controller stopped (%s)", type(exc).__name__)
        sys.exit(1)


def _configure_logging() -> None:
    logging.basicConfig(level=os.environ.get("TOOTHLESS_LOG_LEVEL", "INFO"), format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


if __name__ == "__main__":
    main()
