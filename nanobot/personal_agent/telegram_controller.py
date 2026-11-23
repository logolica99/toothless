"""Owner-only Telegram polling for the limited Toothless Phase 03 commands."""

from __future__ import annotations

import argparse
import asyncio
import base64
import html
import ipaddress
import json
import logging
import logging.handlers
import os
import re
import secrets
import signal
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
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from nanobot.personal_agent.blog_setup import (
    OWNER as BLOG_OWNER,
)
from nanobot.personal_agent.blog_setup import (
    SOURCE_REPOSITORY,
    TARGET_PROJECT,
    TARGET_REPOSITORY,
    BlogProviderClient,
    BlogProvisioner,
    BlogSetupStore,
    MacOSKeychainVault,
    ProviderError,
)
from nanobot.personal_agent.controller_lock import (
    ControllerAlreadyRunningError,
    acquire_controller_lock,
)
from nanobot.personal_agent import desktop_viewer as desktop_viewer_module
from nanobot.personal_agent.desktop_viewer import REMOTE_ORIGIN, DesktopViewer
from nanobot.personal_agent.memory_store import MemoryStore
from nanobot.personal_agent.opencode_task import (
    OpenCodeTaskRunner,
    needs_browser_profile,
    needs_visible_browser,
    parse_task_result,
)
from nanobot.personal_agent.remote_sessions import RemoteSessionStore
from nanobot.personal_agent.schedule_parser import parse_schedule_request
from nanobot.personal_agent.schedule_store import ScheduleRecord, ScheduleStore
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
from nanobot.personal_agent.worker_readiness import worker_readiness
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
_TASK_REFERENCE = re.compile(r"\btask-[0-9a-f]{32}\b")
_AFFIRMATIVE = re.compile(r"^(?:yes|yes please|approved|approve|go ahead|do it)[.!\s]*$", re.IGNORECASE)
_NEGATIVE = re.compile(r"^(?:no|no thanks|don't|do not|not now|reject)[.!\s]*$", re.IGNORECASE)
_FOLLOW_UP = re.compile(
    r"\b(actually|change|modify|make (?:it|the|that)|use the same|same csv|same file|"
    r"using (?:the )?(?:previous|same|last) (?:data|file|result)|"
    r"explain (?:the )?(?:second|last|previous)|try again|redo|update the|revise)\b",
    re.IGNORECASE,
)
_STATUS_QUERY = re.compile(r"\b(what(?:['’]s| is) running|is (?:it|that) (?:finished|done|complete)|"
                           r"what happened to my last task|show me the last result|"
                           r"what(?:['’]s| is) (?:the )?status|status update)\b", re.IGNORECASE)
_CANCEL_QUERY = re.compile(r"\b(cancel|stop) (?:that|this|(?:the )?current|my current) task\b", re.IGNORECASE)
_MEMORY_QUESTION = re.compile(
    r"^(?:what do you remember about|what did we decide about|find my last)\s+(.+?)[?.!]*$",
    re.IGNORECASE,
)
_SAVE_RESULT = re.compile(r"^save this result under my (.+?) project[.\s]*$", re.IGNORECASE)
_GRAPH_PROJECT_FILES = re.compile(r"^(?:which|what) files belong to my (.+?) project[?.!]*$", re.IGNORECASE)
_GRAPH_FILE_TASKS = re.compile(r"^what tasks used (.+?)[?.!]*$", re.IGNORECASE)
_GRAPH_LAST_RESULTS = re.compile(r"^find the results produced by my last (.+?) analysis[?.!]*$", re.IGNORECASE)
_GRAPH_SUPERSESSION = re.compile(r"^which memory replaced the earlier project fact[?.!]*$", re.IGNORECASE)
_SHOW_SCHEDULES = re.compile(r"^(?:show|list) my scheduled tasks[?.!]*$", re.IGNORECASE)
_PAUSE_SCHEDULE = re.compile(r"^pause my (.+?)[.!]*$", re.IGNORECASE)
_RESUME_SCHEDULE = re.compile(r"^resume my (.+?)[.!]*$", re.IGNORECASE)
_CANCEL_SCHEDULE = re.compile(r"^cancel (?:that|my) (.+?)(?: schedule| reminder)?[.!]*$", re.IGNORECASE)
_REMEMBER_PROJECT_FACT = re.compile(r"^remember that project\s+(.+?)\s+uses\s+(.+?)[.!]*$", re.IGNORECASE)
_REMEMBER_PREFERENCE = re.compile(r"^remember that i prefer\s+(.+?)[.!]*$", re.IGNORECASE)
_REMEMBER_DECISION = re.compile(r"^remember (?:that )?we decided\s+(.+?)[.!]*$", re.IGNORECASE)
_UPDATE_UNSPECIFIED = re.compile(r"^update (?:the )?(.+?) we previously used[.!]*$", re.IGNORECASE)


def _validate_remote_origin(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        valid = (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.hostname.lower().endswith(".ts.net")
            and parsed.username is None
            and parsed.password is None
            and parsed.port is None
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise RuntimeError(
            "Set TOOTHLESS_REMOTE_ORIGIN in the protected Telegram env file to the HTTPS Tailscale Serve origin only"
        )
    return f"https://{parsed.netloc.lower()}"


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    token: str
    owner_id: int
    database: Path
    time_zone: str | None = None
    remote_origin: str = "https://toothless.invalid"

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
                if not separator or key not in {
                    "TOOTHLESS_TELEGRAM_BOT_TOKEN", "TOOTHLESS_TELEGRAM_OWNER_ID",
                    "TOOTHLESS_TIME_ZONE", "TOOTHLESS_REMOTE_ORIGIN",
                }:
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
        time_zone = os.environ.get("TOOTHLESS_TIME_ZONE") or _system_time_zone()
        if time_zone:
            try:
                ZoneInfo(time_zone)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise RuntimeError("TOOTHLESS_TIME_ZONE must be a valid IANA time zone") from exc
        remote_origin = _validate_remote_origin(os.environ.get("TOOTHLESS_REMOTE_ORIGIN", ""))
        return cls(
            token=token, owner_id=int(owner_raw), database=database,
            time_zone=time_zone, remote_origin=remote_origin,
        )


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
        self._db.row_factory = sqlite3.Row
        os.chmod(path, 0o600)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS telegram_updates ("
            "update_id INTEGER PRIMARY KEY, processed_at REAL NOT NULL)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS telegram_callbacks ("
            "callback_id TEXT PRIMARY KEY, action TEXT NOT NULL, target_id TEXT NOT NULL, data TEXT NOT NULL DEFAULT '', "
            "owner_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, message_id INTEGER, expires_at REAL NOT NULL, consumed_at REAL)"
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

    def create_callback(
        self, action: str, target_id: str, owner_id: int, chat_id: int, *, data: str = "", ttl_seconds: int = 3600
    ) -> str:
        if not re.fullmatch(r"[a-z_]{1,32}", action) or not target_id or len(target_id) > 180 or len(data) > 120:
            raise ValueError("invalid callback binding")
        if not 30 <= ttl_seconds <= 86_400:
            raise ValueError("callback lifetime is outside the supported range")
        callback_id = secrets.token_hex(12)
        self._db.execute(
            "DELETE FROM telegram_callbacks WHERE expires_at < ? OR consumed_at < ?",
            (time.time(), time.time() - 7 * 86_400),
        )
        self._db.execute(
            "INSERT INTO telegram_callbacks(callback_id,action,target_id,data,owner_id,chat_id,expires_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (callback_id, action, target_id, data, owner_id, chat_id, time.time() + ttl_seconds),
        )
        return f"c:{callback_id}"

    def bind_callbacks(self, callbacks: tuple[str, ...], message_id: int) -> None:
        for payload in callbacks:
            match = re.fullmatch(r"c:([0-9a-f]{24})", payload)
            if match:
                self._db.execute(
                    "UPDATE telegram_callbacks SET message_id=? WHERE callback_id=? AND consumed_at IS NULL",
                    (message_id, match.group(1)),
                )

    def invalidate_message_callbacks(self, chat_id: int, message_id: int) -> None:
        self._db.execute(
            "UPDATE telegram_callbacks SET consumed_at=? WHERE chat_id=? AND message_id=? AND consumed_at IS NULL",
            (time.time(), chat_id, message_id),
        )

    def consume_callback(
        self, payload: str, *, owner_id: int, chat_id: int, message_id: int
    ) -> dict[str, object] | None:
        match = re.fullmatch(r"c:([0-9a-f]{24})", payload)
        if match is None:
            return None
        self._db.execute("BEGIN IMMEDIATE")
        row = self._db.execute(
            "SELECT action,target_id,data,owner_id,chat_id,message_id,expires_at,consumed_at "
            "FROM telegram_callbacks WHERE callback_id=?", (match.group(1),),
        ).fetchone()
        if (
            row is None or row["owner_id"] != owner_id or row["chat_id"] != chat_id
            or row["message_id"] != message_id or row["expires_at"] <= time.time()
            or row["consumed_at"] is not None
        ):
            self._db.rollback()
            return None
        self._db.execute(
            "UPDATE telegram_callbacks SET consumed_at=? WHERE callback_id=? AND consumed_at IS NULL",
            (time.time(), match.group(1)),
        )
        self._db.commit()
        return {"action": str(row["action"]), "target_id": str(row["target_id"]), "data": str(row["data"])}

    def close(self) -> None:
        self._db.close()


@contextmanager
def _remote_worker(
    accepted_callback: Callable[[str], None] | None = None,
) -> Generator[WorkerInterface, None, None]:
    """Create the existing strict SSH interface only for a running worker."""
    if _worker_state() != "running":
        raise WorkerUnavailableError("Ubuntu worker is not running")
    readiness = worker_readiness()
    if readiness.get("softnet_policy") != "verified" or readiness.get("dns_relay") != "ready":
        raise WorkerUnavailableError("Protected Softnet/DNS launcher is not verified; start only with ops/worker/start.sh")
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


def _safe_status_error(error: str | None) -> str:
    text = (error or "").lower()
    if any(part in text for part in ("auth", "credential", "reauth")):
        return "guest authentication required"
    if "dns" in text or "relay" in text:
        return "DNS relay unavailable"
    if "worker_unavailable" in text or "worker unavailable" in text:
        return "worker unavailable"
    if "timeout" in text or "timed out" in text:
        return "execution timed out"
    if "rate" in text or "quota" in text:
        return "provider limit"
    return "see task details"


def _timestamp_iso(value: str) -> str:
    return value[:19].replace("T", " ") + " UTC"


def _system_time_zone() -> str | None:
    for candidate in (Path("/etc/localtime"), Path("/var/db/timezone/localtime")):
        try:
            resolved = candidate.resolve(strict=True).as_posix()
        except OSError:
            continue
        marker = "/zoneinfo/"
        if marker in resolved:
            name = resolved.rsplit(marker, 1)[1]
            try:
                ZoneInfo(name)
            except ZoneInfoNotFoundError:
                continue
            return name
    return None


def _schedule_time_label(record: ScheduleRecord) -> str:
    if record.next_run_at is None:
        if record.state == "pending":
            local = datetime.fromisoformat(record.local_start)
            return f"pending approval at {local.strftime('%Y-%m-%d %I:%M %p')} {record.timezone}"
        return "none"
    instant = datetime.fromisoformat(record.next_run_at).astimezone(ZoneInfo(record.timezone))
    return instant.strftime("%Y-%m-%d %I:%M %p %Z (UTC%z)")


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
            "/executions, /execution <execution_id>, /project <name> | <description>, /remember [project |] <fact>, "
            "/memory <query>, /memory-update <memory_id> | <correction>, /forget <memory_id>, "
            "/schedules, /schedule approve|pause|resume|cancel|show|latest <schedule_id>, "
            "/start, /ping, /help, /worker, /status, /desktop, /remote [revoke], /web, /blog setup|status|cancel. "
            "Attach a supported file with a task caption to create a queued file task. Upload limit: 512 KiB. "
            "Reply to a task message with a follow-up to queue a continuation. Reply Yes directly to its approval message "
            "to approve that exact proposal, or use /run. Memory writes require explicit owner wording and are independent of run approval. "
            "Workspace tasks require approval; browser tasks use a separate profile."
        )
    if command == "/worker":
        return f"Ubuntu worker: {worker_state}. This query does not start or change the VM."
    if command == "/status":
        return (
            "Controller: connected (Telegram polling active)\n"
            f"Ubuntu worker: {worker_state}\n"
            "Phase 06: owner-approved schedules are enabled; reminders use no model and research stays in the browser-only profile."
        )
    if command.startswith("/"):
        return "Unknown command. Use /help to see the commands currently available."
    return AUTONOMY_NOTICE


def _recorded_task_tools(events: tuple[Any, ...]) -> tuple[str, ...]:
    """Return only bounded tool names from host-persisted sanitized diagnostics."""
    found: list[str] = []
    for event in events:
        if getattr(event, "event_type", None) != "execution_diagnostics":
            continue
        detail = getattr(event, "detail", None)
        if not isinstance(detail, str) or len(detail) > 8192:
            continue
        try:
            value: object = json.loads(detail)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        tools = cast(dict[str, object], value).get("tools")
        if not isinstance(tools, list):
            continue
        for item in cast(list[object], tools)[:16]:
            if isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", item) and item not in found:
                found.append(item)
    return tuple(found[:16])


def _recorded_oversized_event(events: tuple[Any, ...]) -> str | None:
    """Format only the runner's allowlisted event-size metadata for owner task detail."""
    for event in events:
        if getattr(event, "event_type", None) != "execution_diagnostics":
            continue
        detail = getattr(event, "detail", None)
        if not isinstance(detail, str) or len(detail) > 8192:
            continue
        try:
            value: object = json.loads(detail)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        diagnostics = cast(dict[str, object], value)
        oversized = diagnostics.get("oversized_event")
        if diagnostics.get("failure_category") != "event_size_limit" or not isinstance(oversized, dict):
            continue
        item = cast(dict[str, object], oversized)
        if item.get("failure_category") != "event_size_limit":
            continue
        sequence = item.get("sequence")
        observed = item.get("observed_bytes")
        observed_at_least = item.get("observed_bytes_at_least")
        limit = item.get("limit_bytes")
        if type(sequence) is not int or not 0 < sequence <= 1000:
            continue
        is_lower_bound = type(observed_at_least) is int and 0 <= observed_at_least <= 1_048_577
        if (type(observed) is not int or not 0 <= observed <= 1_048_577) and not is_lower_bound:
            continue
        if type(limit) is not int or not 0 < limit <= 65_536:
            continue
        event_type = item.get("event_type")
        event_label = event_type if isinstance(event_type, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", event_type) else "unknown"
        tool = item.get("tool_name")
        if isinstance(tool, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", tool):
            event_label = f"{event_label}/{tool}"
        execution_id = item.get("execution_id", diagnostics.get("execution_id"))
        suffix = f"; execution {execution_id}" if isinstance(execution_id, str) and re.fullmatch(r"opencode-[0-9a-f]{32}", execution_id) else ""
        size_text = (
            f"at least {observed_at_least:,} bytes" if is_lower_bound else f"{observed:,} bytes"
        )
        return f"OpenCode event-size diagnostic: event {sequence} ({event_label}) was {size_text}; limit {limit:,} bytes{suffix}."
    return None


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
        self.memory_store = MemoryStore(self.task_store.path.parent / "memory.sqlite3")
        self.schedule_store = ScheduleStore(self.task_store.path.parent / "schedules.sqlite3")
        self.remote_sessions = RemoteSessionStore(self.task_store.path.parent / "remote-sessions.sqlite3")
        self.blog_setup_store = BlogSetupStore(self.task_store.path.parent / "blog-setup.sqlite3")
        self.remote_sessions.advance_epoch()
        self._memory_explorer_process: asyncio.subprocess.Process | None = None
        for task in self.task_store.list_latest(100):
            self._sync_task_graph(task.task_id)
        self._attachment_storage = AttachmentStorage(self.task_store.path.parent / "attachments")
        self._worker_provider = worker_provider
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(40, connect=10))
        self._owns_client = client is None
        self._base_url = f"https://api.telegram.org/bot{config.token}"
        self._last_poll_ok = 0.0
        self._active_task_run: asyncio.Task[None] | None = None
        self._schedule_loop_task: asyncio.Task[None] | None = None
        self.desktop_viewer = DesktopViewer(self._capture_desktop_screenshot)
        self.desktop_viewer.invalidate_stale_token()

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
            duplicate_callback = update.get("callback_query")
            if isinstance(duplicate_callback, dict):
                duplicate_callback = cast(dict[str, Any], duplicate_callback)
                duplicate_id = duplicate_callback.get("id")
                if isinstance(duplicate_id, str):
                    await self._answer_callback(duplicate_id, "This action was already received.")
            return
        callback_raw = update.get("callback_query")
        if isinstance(callback_raw, dict):
            await self._handle_callback(update_id, cast(dict[str, Any], callback_raw))
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
        reply_to_raw = message_data.get("reply_to_message")
        reply_message_id: int | None = None
        if isinstance(reply_to_raw, dict):
            reply_to = cast(dict[str, Any], reply_to_raw)
            candidate_message_id: object = reply_to.get("message_id")
            if isinstance(candidate_message_id, int) and not isinstance(candidate_message_id, bool):
                reply_message_id = candidate_message_id
        replied_task_id = (
            self.task_store.task_for_message(chat_id, reply_message_id)
            if reply_message_id is not None else None
        )
        memory_action = self._handle_memory_message(
            update_id, chat_id, message, command, argument, replied_task_id, reply_message_id
        )
        if memory_action is not None:
            reply, memory_ids = memory_action
            if not self.store.claim(update_id):
                return
            await self._send_text(chat_id, reply, memory_ids=memory_ids)
            if command.startswith("/"):
                LOGGER.info("Handled authorized memory command %s", command)
            else:
                LOGGER.info("Handled authorized explicit memory message")
            return
        schedule_reply = self._handle_schedule_message(update_id, user_id, chat_id, message, command, argument)
        if schedule_reply is not None:
            if not self.store.claim(update_id):
                return
            schedule_match = re.search(r"schedule-(?:[0-9a-f]{24}|[0-9a-f]{32})", schedule_reply)
            schedule_id = schedule_match.group(0) if schedule_match else None
            schedule_ids = tuple(
                record.schedule_id for record in self.schedule_store.list_for_owner(user_id, chat_id, limit=8)
            ) if command == "/schedules" or _SHOW_SCHEDULES.fullmatch(message.strip()) else ()
            await self._send_text(chat_id, schedule_reply, schedule_id=schedule_id, schedule_ids=schedule_ids)
            LOGGER.info("Handled authorized schedule request")
            return
        if command == "/blog":
            if not self.store.claim(update_id):
                return
            if argument.casefold() in {"setup", "status", ""}:
                reply = self._blog_setup_reply()
                await self._send_text(chat_id, reply, blog_setup_panel=True)
            elif argument.casefold() == "cancel":
                self.blog_setup_store.cancel_proposals()
                await self._send_text(chat_id, "Pending blog setup approvals were cancelled. Existing credentials and resources were not changed.", blog_setup_panel=True)
            else:
                await self._send_text(chat_id, "Use `/blog setup`, `/blog status`, or `/blog cancel`.")
            LOGGER.info("Handled authorized blog setup command")
            return
        bound_task_id: str | None = replied_task_id
        pending_task_id: str | None = None
        is_plain_text = bool(message.strip()) and not command.startswith("/")
        if is_plain_text and _AFFIRMATIVE.fullmatch(message.strip()):
            if reply_message_id is None:
                reply = "To approve a proposed task, reply directly to its approval message with Yes. Nothing was started."
            else:
                approved_task_id, approval_state = self.task_store.consume_reply_approval(chat_id, reply_message_id)
                if approval_state == "approved" and approved_task_id is not None:
                    bound_task_id = approved_task_id
                    reply = self._start_task_reply(approved_task_id, chat_id)
                else:
                    reply = {
                        "missing": "That message is not a pending approval. Nothing was started.",
                        "used": "That approval was already used. No new execution was started.",
                        "expired": "That approval expired. Nothing was started; review the task and approve it again with /run.",
                        "changed": "The proposed task changed or is no longer queued. Nothing was started; review it with /task.",
                    }.get(approval_state, "That approval could not be verified. Nothing was started.")
            if not self.store.claim(update_id):
                return
        elif is_plain_text and _NEGATIVE.fullmatch(message.strip()):
            if reply_message_id is None:
                reply = "No pending approval was identified. Nothing changed."
            else:
                declined_task_id, decline_state = self.task_store.decline_reply_approval(chat_id, reply_message_id)
                if decline_state == "declined" and declined_task_id is not None:
                    bound_task_id = declined_task_id
                    reply = f"Task {declined_task_id} was declined and cancelled. It was not started."
                else:
                    reply = "No active approval matched that reply. Nothing was started."
            if not self.store.claim(update_id):
                return
        elif is_plain_text and _STATUS_QUERY.search(message):
            reply, bound_task_id = self._natural_status_reply(message, chat_id, replied_task_id)
            if not self.store.claim(update_id):
                return
        elif is_plain_text and _CANCEL_QUERY.search(message):
            task_id, status = self._resolve_natural_task(message, chat_id, replied_task_id)
            if task_id is None:
                reply = status
            else:
                bound_task_id = task_id
                reply = await self._cancel_reply(task_id)
            if not self.store.claim(update_id):
                return
        elif is_plain_text and _FOLLOW_UP.search(message):
            parent_id, resolution = self._resolve_natural_task(message, chat_id, replied_task_id)
            if parent_id is None:
                reply = resolution
            else:
                task, created = self._create_continuation(update_id, chat_id, parent_id, message)
                if task is None:
                    reply = created
                else:
                    bound_task_id = task.task_id
                    pending_task_id = task.task_id
                    reply = (
                        f"Proposed follow-up action (the only work this run will do):\n{task.request}\n\n"
                        + ("Mode: Visible Browser · protected Ubuntu · public web research only.\n\n" if needs_visible_browser(task.request + "\n" + task.instructions) else "")
                        + f"Parent task {parent_id} and its saved files/results are context only; earlier actions "
                        + "will not be repeated unless the proposed action asks for them.\n\n"
                        f"Task {task.task_id} is queued. Reply directly to this message with Yes to approve this action "
                        f"(expires in 10 minutes), or use `/run {task.task_id}`. Nothing has started."
                    )
            if not self.store.claim(update_id):
                return
        else:
            is_new_task = command == "/new" or is_plain_text
            if is_new_task:
                request = argument if command == "/new" else message
                request, memory_context, memory_ids, context_error = self._memory_context_for_task(
                    request, chat_id, reply_message_id
                )
                reply = context_error or self._create_task_reply(
                    update_id, request, chat_id=chat_id, memory_context=memory_context, memory_ids=memory_ids
                )
                task_match = _TASK_REFERENCE.search(reply)
                bound_task_id = task_match.group(0) if task_match else None
                bound_task = self.task_store.get(bound_task_id) if bound_task_id else None
                pending_task_id = (
                    bound_task_id if bound_task is not None and bound_task.source_update_id == update_id
                    and bound_task.status is TaskStatus.QUEUED else None
                )
                if not self.store.claim(update_id):
                    return
            else:
                if not self.store.claim(update_id):
                    return
                if command == "/tasks":
                    reply = self._tasks_reply()
                elif command == "/remote":
                    reply = await self._remote_reply(chat_id, revoke=argument.casefold() == "revoke")
                elif command == "/web":
                    reply = await self._web_reply(chat_id)
                elif command == "/blog":
                    reply = self._blog_setup_reply()
                elif command == "/task":
                    reply = self._task_reply(argument)
                    bound_task_id = argument if self.task_store.get(argument) is not None else None
                elif command == "/run":
                    reply = self._start_task_reply(argument, chat_id)
                    bound_task_id = argument if self.task_store.get(argument) is not None else None
                elif command == "/cancel":
                    reply = await self._cancel_reply(argument)
                    bound_task_id = argument if argument.startswith("task-") else None
                elif command == "/pause":
                    reply = self._transition_task_reply(argument, TaskStatus.PAUSED)
                elif command == "/resume":
                    reply = self._transition_task_reply(argument, TaskStatus.QUEUED)
                elif command == "/update":
                    task_id, separator, instructions = argument.partition(" ")
                    reply = self._update_task_reply(task_id, instructions.strip() if separator else "")
                    bound_task_id = task_id if self.task_store.get(task_id) is not None else None
                elif command == "/executions":
                    reply = self._executions_reply()
                elif command == "/execution":
                    reply = await self._execution_reply(argument)
                elif command == "/desktop":
                    reply = await self._desktop_reply()
                else:
                    worker_state = _worker_state() if command == "/worker" else "unknown"
                    reply = command_reply(message, worker_state=worker_state)
        if command == "/status":
            reply = await self._status_reply()
        elif command == "/start":
            reply = await self._status_reply()
        await self._send_text(chat_id, reply, task_id=bound_task_id, pending_approval_task_id=pending_task_id,
                              status_panel=command in {"/status", "/start"}, desktop_panel=command == "/desktop",
                              remote_panel=command == "/remote" and argument.casefold() != "revoke")
        if command in {
            "/start", "/ping", "/help", "/worker", "/status", "/tasks", "/task", "/cancel",
            "/new", "/run", "/pause", "/resume", "/update", "/executions", "/execution", "/schedule", "/schedules", "/desktop", "/remote", "/web", "/blog",
        }:
            LOGGER.info("Handled authorized Telegram command %s", command)
        elif command.startswith("/"):
            LOGGER.info("Handled authorized unknown Telegram command")
        else:
            LOGGER.info("Handled authorized text message; autonomous dispatch is disabled")

    async def _ensure_memory_explorer(self) -> bool:
        process = self._memory_explorer_process
        if process is not None and process.returncode is None:
            return True
        self._memory_explorer_process = None
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", 8768), timeout=0.25)
        except (OSError, asyncio.TimeoutError):
            pass
        else:
            writer.close()
            await writer.wait_closed()
            # An unmanaged listener cannot be trusted with the one-use grant database.
            return False
        repository = Path(__file__).resolve().parents[2]
        child_env = {
            "HOME": str(Path.home()),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "TOOTHLESS_TASK_DB": str(self.task_store.path),
            "TOOTHLESS_TELEGRAM_DB": str(self.config.database),
            "TOOTHLESS_REMOTE_ORIGIN": REMOTE_ORIGIN,
            "TOOTHLESS_OWNER_ID": str(self.config.owner_id),
        }
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "nanobot.personal_agent.memory_explorer", "--managed",
                cwd=repository, env=child_env, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except (OSError, ValueError):
            return False
        self._memory_explorer_process = process
        for _ in range(30):
            if process.returncode is not None:
                self._memory_explorer_process = None
                return False
            try:
                _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", 8768), timeout=0.2)
            except (OSError, asyncio.TimeoutError):
                await asyncio.sleep(0.1)
                continue
            writer.close()
            await writer.wait_closed()
            return True
        process.terminate()
        await process.wait()
        self._memory_explorer_process = None
        return False

    async def _remote_reply(self, chat_id: int, *, revoke: bool = False) -> str:
        if revoke:
            self.remote_sessions.revoke()
            await self.desktop_viewer.close()
            return "Remote application sessions and unused login codes were revoked. The Ubuntu worker was not changed."
        if not await self._ensure_memory_explorer():
            return "Memory Explorer is unavailable or an unmanaged process owns its localhost port. No remote code was issued."
        code = self.remote_sessions.issue("memory", self.config.owner_id, chat_id)
        return (
            "Private tailnet access · HTTPS certificates enabled\n"
            f"Memory Explorer: {REMOTE_ORIGIN}/memory/\n"
            f"One-time sign-in code: `{code}` (expires in 10 minutes; enter it in the page, never in a URL).\n"
            "Open Desktop creates its own separate one-use code after protected worker readiness. "
            "All tailnet members can reach the routes; Toothless data remains protected by these separate app sessions."
        )

    async def _web_reply(self, chat_id: int) -> str:
        if not await self._ensure_memory_explorer():
            return "The private Toothless web console is unavailable. No login code was issued."
        code = self.remote_sessions.issue("web_console", self.config.owner_id, chat_id)
        return (
            "Private Toothless console · HTTPS on your tailnet\n"
            f"Open {REMOTE_ORIGIN}/memory/app/ and enter this one-use code: `{code}`. "
            "Agent execution from the web console is not enabled yet; this code grants only the authenticated application session."
        )

    def _blog_setup_reply(self) -> str:
        state = self.blog_setup_store.snapshot()
        repo = f"created · {state.repository_id}" if state.repository_id else "not created"
        project = f"created · {state.project_id}" if state.project_id else "not created"
        url = f"\nExperimental URL: {state.project_url}" if state.project_url else ""
        deployment = f"\nDeployment: {state.deployment_id or 'not verified'}" if state.project_id else ""
        return (
            "Toothless blog setup · host-only credentials\n"
            f"GitHub setup credential: {'connected' if state.github_setup else 'not connected'}\n"
            f"GitHub target-only publishing credential: {'connected' if state.github_publish else 'not connected'}\n"
            f"Vercel: {'connected' if state.vercel else 'not connected'}\n"
            f"Experimental repository {BLOG_OWNER}/{TARGET_REPOSITORY}: {repo}\n"
            f"Experimental Vercel project {TARGET_PROJECT}: {project}\n"
            f"State: {state.state}{url}{deployment}\n\n"
            "Use the buttons to connect credentials or prepare the next exact setup approval. The original production repository/project are excluded."
        )

    async def _handle_file_update(
        self, update_id: int, chat_id: int, message: dict[str, Any]
    ) -> None:
        task_id_for_reply: str | None = None
        created = False
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
            task_id_for_reply = task.task_id
            if created:
                self.task_store.create_pending_approval(task.task_id, chat_id)
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
            self._sync_task_graph(task.task_id)
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
        await self._send_text(chat_id, reply, task_id=task_id_for_reply,
                              pending_approval_task_id=task_id_for_reply if created else None)

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

    async def _send_text(
        self, chat_id: int, text: str, *, task_id: str | None = None,
        pending_approval_task_id: str | None = None, memory_ids: tuple[str, ...] = (),
        schedule_id: str | None = None, schedule_ids: tuple[str, ...] = (), status_panel: bool = False,
        desktop_panel: bool = False, remote_panel: bool = False, blog_setup_panel: bool = False,
        blog_enrollment: bool = False, blog_proposal_id: str | None = None,
    ) -> int | None:
        keyboard, callbacks = self._keyboard_for_message(
            chat_id, task_id=task_id, pending_task_id=pending_approval_task_id, schedule_id=schedule_id,
            schedule_ids=schedule_ids, memory_ids=memory_ids, status_panel=status_panel,
            desktop_panel=desktop_panel, remote_panel=remote_panel, blog_setup_panel=blog_setup_panel,
            blog_enrollment=blog_enrollment, blog_proposal_id=blog_proposal_id,
        )
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": _telegram_html(text, limit=MAX_TELEGRAM_MESSAGE_CHARS),
            "parse_mode": "HTML",
        }
        if keyboard is not None:
            payload["reply_markup"] = keyboard
        result = await self._api("sendMessage", payload)
        message_id: object = cast(dict[str, Any], result).get("message_id") if isinstance(result, dict) else None
        if isinstance(message_id, int) and not isinstance(message_id, bool):
            if task_id is not None:
                self.task_store.bind_message_context(chat_id, message_id, task_id)
            if pending_approval_task_id is not None:
                self.task_store.bind_approval_prompt(pending_approval_task_id, chat_id, message_id)
            if callbacks:
                self.store.bind_callbacks(callbacks, message_id)
            if memory_ids:
                self.memory_store.bind_message(chat_id, message_id, memory_ids)
            if blog_proposal_id:
                self.blog_setup_store.bind_proposal(blog_proposal_id, message_id)
            return message_id
        return None

    def _new_callback(self, action: str, target_id: str, chat_id: int, *, data: str = "") -> str:
        return self.store.create_callback(action, target_id, self.config.owner_id, chat_id, data=data)

    def _keyboard_for_message(
        self, chat_id: int, *, task_id: str | None = None, pending_task_id: str | None = None,
        schedule_id: str | None = None, schedule_ids: tuple[str, ...] = (), memory_ids: tuple[str, ...] = (),
        status_panel: bool = False,
        desktop_panel: bool = False,
        remote_panel: bool = False,
        blog_setup_panel: bool = False, blog_enrollment: bool = False,
        blog_proposal_id: str | None = None,
    ) -> tuple[dict[str, object] | None, tuple[str, ...]]:
        rows: list[list[dict[str, str]]] = []
        callbacks: list[str] = []
        if status_panel:
            for action, label in (("status_refresh", "↻ Refresh"), ("status_tasks", "Tasks"),
                                  ("status_schedules", "Schedules"), ("status_diagnostics", "Diagnostics")):
                callback = self._new_callback(action, "status", chat_id)
                callbacks.append(callback)
                rows.append([{"text": label, "callback_data": callback}])
        elif desktop_panel:
            for action, label in (
                ("desktop_screenshot", "📸 Screenshot"), ("desktop_refresh", "🔄 Refresh"),
                ("desktop_open", "🖥 Open Desktop"), ("desktop_details", "ℹ️ Details"),
                ("desktop_close", "⏹ Close Viewer"),
            ):
                callback = self._new_callback(action, "desktop", chat_id)
                callbacks.append(callback)
                rows.append([{"text": label, "callback_data": callback}])
        elif remote_panel:
            memory = self._new_callback("remote_memory", "memory", chat_id)
            desktop = self._new_callback("desktop_open", "desktop", chat_id)
            revoke = self._new_callback("remote_revoke", "remote", chat_id)
            callbacks.extend((memory, desktop, revoke))
            rows.extend([
                [{"text": "🧠 Open Memory Explorer", "url": f"{REMOTE_ORIGIN}/memory/"}],
                [{"text": "🔑 New Memory sign-in code", "callback_data": memory}],
                [{"text": "🖥 Open Ubuntu Desktop", "callback_data": desktop}],
                [{"text": "🔐 Revoke remote sessions", "callback_data": revoke}],
            ])
        elif blog_setup_panel:
            state = self.blog_setup_store.snapshot()
            for kind, label in (
                ("github_setup", "GitHub · create repo"),
                ("github_publish", "GitHub · target-only publish"),
                ("vercel", "Vercel"),
            ):
                callback = self._new_callback("blog_connect", "blog_setup", chat_id, data=kind)
                callbacks.append(callback)
                suffix = " · rotate" if getattr(state, kind) else " · connect"
                rows.append([{"text": label + suffix, "callback_data": callback}])
            if state.github_setup and state.repository_id is None:
                callback = self._new_callback("blog_prepare_repository", "blog_setup", chat_id)
                callbacks.append(callback)
                rows.append([{"text": "Review private repository setup", "callback_data": callback}])
            elif state.repository_id is not None and state.project_id is None:
                callback = self._new_callback("blog_prepare_deploy", "blog_setup", chat_id)
                callbacks.append(callback)
                rows.append([{"text": "Review copy and Vercel setup", "callback_data": callback}])
            refresh = self._new_callback("blog_status", "blog_setup", chat_id)
            cancel = self._new_callback("blog_cancel", "blog_setup", chat_id)
            callbacks.extend((refresh, cancel))
            rows.append([{"text": "↻ Refresh", "callback_data": refresh}, {"text": "Cancel pending setup", "callback_data": cancel}])
        elif blog_enrollment:
            rows.append([{"text": "Open secure credential setup", "url": f"{REMOTE_ORIGIN}/memory/blog-setup/"}])
        elif blog_proposal_id:
            approve = self._new_callback("blog_resource_approve", blog_proposal_id, chat_id)
            reject = self._new_callback("blog_resource_reject", blog_proposal_id, chat_id)
            callbacks.extend((approve, reject))
            rows.append([{"text": "Approve this exact operation", "callback_data": approve}, {"text": "Cancel", "callback_data": reject}])
        elif pending_task_id:
            approve = self._new_callback("task_approve", pending_task_id, chat_id)
            reject = self._new_callback("task_reject", pending_task_id, chat_id)
            callbacks.extend((approve, reject))
            rows.append([{"text": "✓ Approve", "callback_data": approve}, {"text": "Reject", "callback_data": reject}])
        elif schedule_id:
            record = self.schedule_store.get(schedule_id, owner_id=self.config.owner_id, chat_id=chat_id)
            if record is not None:
                if record.state == "pending":
                    approve = self._new_callback("schedule_approve", schedule_id, chat_id)
                    reject = self._new_callback("schedule_reject", schedule_id, chat_id)
                    callbacks.extend((approve, reject))
                    rows.append([{"text": "✓ Approve", "callback_data": approve}, {"text": "Reject", "callback_data": reject}])
                elif record.state in {"active", "paused"}:
                    action = "pause" if record.state == "active" else "resume"
                    control = self._new_callback(f"schedule_{action}", schedule_id, chat_id)
                    cancel = self._new_callback("schedule_cancel", schedule_id, chat_id)
                    callbacks.extend((control, cancel))
                    rows.append([{"text": "Ⅱ Pause" if action == "pause" else "▶ Resume", "callback_data": control}, {"text": "Cancel", "callback_data": cancel}])
        elif task_id:
            task = self.task_store.get(task_id)
            if task is not None:
                row: list[dict[str, str]] = []
                details = self._new_callback("task_details", task_id, chat_id)
                callbacks.append(details)
                row.append({"text": "Details", "callback_data": details})
                if task.status in {TaskStatus.QUEUED, TaskStatus.DISPATCHING, TaskStatus.RUNNING}:
                    cancel = self._new_callback("task_cancel", task_id, chat_id)
                    callbacks.append(cancel)
                    row.append({"text": "Cancel", "callback_data": cancel})
                if (task.status in {TaskStatus.DISPATCHING, TaskStatus.RUNNING}
                        and needs_visible_browser(task.request + "\n" + task.instructions)):
                    watch = self._new_callback("desktop_open", "desktop", chat_id)
                    callbacks.append(watch)
                    row.append({"text": "👁 Watch Live", "callback_data": watch})
                if self.task_store.sources(task_id):
                    sources = self._new_callback("sources_page", task_id, chat_id, data="0")
                    detail = self._new_callback("research_details", task_id, chat_id)
                    callbacks.extend((sources, detail))
                    rows.append([{"text": "📚 View Sources", "callback_data": sources}, {"text": "🔎 Research Details", "callback_data": detail}])
                if task.status is TaskStatus.SUCCEEDED:
                    save = self._new_callback("save_memory", task_id, chat_id)
                    callbacks.append(save)
                    rows.append([{"text": "💾 Save to Memory", "callback_data": save}])
                if row:
                    rows.insert(0, row)
                if schedule_id:
                    schedule = self.schedule_store.get(schedule_id, owner_id=self.config.owner_id, chat_id=chat_id)
                    if schedule and schedule.state in {"active", "paused"}:
                        action = "pause" if schedule.state == "active" else "resume"
                        control = self._new_callback(f"schedule_{action}", schedule_id, chat_id)
                        cancel = self._new_callback("schedule_cancel", schedule_id, chat_id)
                        callbacks.extend((control, cancel))
                        rows.append([
                            {"text": "Ⅱ Pause" if action == "pause" else "▶ Resume", "callback_data": control},
                            {"text": "Cancel schedule", "callback_data": cancel},
                        ])
        elif schedule_ids:
            for schedule_id_item in schedule_ids[:8]:
                record = self.schedule_store.get(
                    schedule_id_item, owner_id=self.config.owner_id, chat_id=chat_id
                )
                if record is None or record.state not in {"active", "paused"}:
                    continue
                action = "pause" if record.state == "active" else "resume"
                control = self._new_callback(f"schedule_{action}", schedule_id_item, chat_id)
                cancel = self._new_callback("schedule_cancel", schedule_id_item, chat_id)
                callbacks.extend((control, cancel))
                rows.append([
                    {"text": f"{record.description[:24]} · {action.title()}", "callback_data": control},
                    {"text": "Cancel", "callback_data": cancel},
                ])
        elif memory_ids:
            for memory_id in memory_ids[:8]:
                choice = self._new_callback("memory_select", memory_id, chat_id)
                callbacks.append(choice)
                rows.append([{"text": f"Open {memory_id[:16]}", "callback_data": choice}])
        return ({"inline_keyboard": rows} if rows else None), tuple(callbacks)

    async def _edit_message(
        self, chat_id: int, message_id: int, text: str,
        *, keyboard: dict[str, object] | None = None, callbacks: tuple[str, ...] = (),
        photo_caption: bool = False,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id, "message_id": message_id,
            ("caption" if photo_caption else "text"): _telegram_html(text, limit=MAX_TELEGRAM_MESSAGE_CHARS),
            "parse_mode": "HTML",
        }
        if keyboard is not None:
            payload["reply_markup"] = keyboard
        await self._api("editMessageCaption" if photo_caption else "editMessageText", payload)
        self.store.invalidate_message_callbacks(chat_id, message_id)
        self.store.bind_callbacks(callbacks, message_id)

    async def _answer_callback(self, callback_id: str, text: str = "", *, alert: bool = False) -> None:
        if not callback_id or len(callback_id) > 256:
            return
        try:
            await self._api("answerCallbackQuery", {
                "callback_query_id": callback_id, "text": _clean_text(text, limit=180), "show_alert": alert,
            })
        except (httpx.HTTPError, RuntimeError, ValueError):
            LOGGER.warning("Telegram callback acknowledgement failed")

    async def _status_reply(self, *, diagnostics: bool = False) -> str:
        readiness = await asyncio.to_thread(worker_readiness)
        tasks = self.task_store.list_latest(100)
        schedules = self.schedule_store.list_for_owner(self.config.owner_id, self.config.owner_id, limit=50)
        active = [task for task in tasks if task.status in {
            TaskStatus.DISPATCHING, TaskStatus.RUNNING, TaskStatus.CANCELLING,
        }]
        failed = next((task for task in tasks if task.status in {TaskStatus.INCOMPLETE, TaskStatus.FAILED, TaskStatus.UNKNOWN}), None)
        succeeded = next((task for task in tasks if task.status is TaskStatus.SUCCEEDED and task.execution_ids), None)
        worker_label = "ready" if readiness["worker_ready"] else "unavailable"
        if diagnostics:
            components = ("vm", "softnet_policy", "dns_relay", "guest_dns", "opencode_auth",
                          "execution_agent", "workspace_profile", "browser_profile")
            detail = "\n".join(f"{name.replace('_', ' ').title()}: {readiness[name]}" for name in components)
            return f"Toothless diagnostics\nWorker: {worker_label}\n{detail}"
        schedule_count = sum(schedule.state == "active" for schedule in schedules)
        failed_text = "none"
        if failed is not None:
            category = _safe_status_error(failed.error)
            failed_text = f"{failed.task_id} · {failed.status.value} · {category}"
        last_success = "none"
        if succeeded is not None:
            last_success = f"{succeeded.task_id} · {succeeded.execution_ids[-1]} · {_timestamp(succeeded.updated_at)}"
        return (
            "Toothless status\n"
            "Controller: connected\n"
            f"Ubuntu worker: {worker_label}\n"
            f"Scheduled tasks: {schedule_count}\n"
            f"Running tasks: {len(active)}\n"
            f"Most recent failure: {failed_text}\n"
            f"Last successful worker operation: {last_success}"
        )

    async def _capture_desktop_screenshot(self) -> bytes:
        readiness = await asyncio.to_thread(worker_readiness)
        if readiness.get("worker_ready") is not True:
            raise WorkerUnavailableError("Protected worker readiness is not verified")
        with self._worker_provider() as worker:
            if not await worker.desktop_available():
                raise WorkerUnavailableError("The dedicated Ubuntu desktop session is not running")
            return await worker.capture_desktop_screenshot()

    async def _desktop_reply(self, *, details: bool = False) -> str:
        readiness = await asyncio.to_thread(worker_readiness)
        worker = "ready" if readiness.get("worker_ready") is True else "unavailable"
        desktop_ready = False
        if worker == "ready":
            try:
                with self._worker_provider() as worker_interface:
                    desktop_ready = await worker_interface.desktop_available()
            except WorkerOperationError:
                desktop_ready = False
        desktop = "available" if desktop_ready else "not installed or unavailable"
        if details:
            return (
                "Ubuntu desktop diagnostics\n"
                f"Protected worker: {worker}\n"
                f"Graphical session: {desktop}\n"
                "View mode: read only; screenshots only\n"
                "Network access: local Mac only; no private tunnel is configured"
            )
        return (
            "🐉 Ubuntu Workspace\n"
            f"🖥 Desktop: {desktop}\n"
            f"🔒 Worker: {worker}\n"
            "👁 Access: view only; no mouse or keyboard control\n"
            "🌐 Viewer: local Mac only. iPhone access requires a separately approved private transport."
        )

    async def _send_desktop_photo(self, chat_id: int, message_id: int) -> None:
        try:
            content = await self._capture_desktop_screenshot()
        except WorkerOperationError as exc:
            await self._edit_message(
                chat_id, message_id,
                f"Desktop screenshot unavailable: {_clean_text(str(exc), limit=180)}. No VM was started.",
            )
            return
        keyboard, callbacks = self._keyboard_for_message(chat_id, desktop_panel=True)
        response = await self._client.post(
            f"{self._base_url}/sendPhoto",
            data={
                "chat_id": str(chat_id),
                "caption": "Ubuntu desktop · view only · freshly captured",
                "reply_markup": json.dumps(keyboard, separators=(",", ":")) if keyboard else "{}",
            },
            files={"photo": ("ubuntu-desktop.png", content, "image/png")},
            timeout=httpx.Timeout(40, connect=10),
        )
        response.raise_for_status()
        raw_body: object = response.json()
        body = cast(dict[str, Any], raw_body) if isinstance(raw_body, dict) else {}
        result = body.get("result") if body.get("ok") is True else None
        result = cast(dict[str, Any], result) if isinstance(result, dict) else {}
        new_message_id = result.get("message_id")
        if not isinstance(new_message_id, int) or isinstance(new_message_id, bool):
            raise RuntimeError("Telegram did not confirm the desktop screenshot")
        self.store.invalidate_message_callbacks(chat_id, message_id)
        self.store.bind_callbacks(callbacks, new_message_id)

    async def _handle_callback(self, update_id: int, callback: dict[str, Any]) -> None:
        callback_id = callback.get("id")
        sender = callback.get("from")
        message = callback.get("message")
        if not isinstance(callback_id, str) or not isinstance(sender, dict) or not isinstance(message, dict):
            self.store.claim(update_id)
            return
        sender = cast(dict[str, Any], sender)
        message = cast(dict[str, Any], message)
        chat = message.get("chat")
        if not isinstance(chat, dict):
            self.store.claim(update_id)
            await self._answer_callback(callback_id, "This action is unavailable.", alert=True)
            return
        chat = cast(dict[str, Any], chat)
        photo_caption = bool(message.get("photo"))
        owner_id, chat_id = sender.get("id"), chat.get("id")
        message_id, payload = message.get("message_id"), callback.get("data")
        if (not isinstance(owner_id, int) or isinstance(owner_id, bool) or owner_id != self.config.owner_id
                or chat.get("type") != "private"
                or not isinstance(chat_id, int) or isinstance(chat_id, bool)
                or not isinstance(message_id, int) or isinstance(message_id, bool)
                or not isinstance(payload, str) or len(payload) > 64):
            self.store.claim(update_id)
            await self._answer_callback(callback_id, "This action is not authorized.", alert=True)
            return
        if not self.store.claim(update_id):
            await self._answer_callback(callback_id, "This action was already received.")
            return
        binding = self.store.consume_callback(payload, owner_id=owner_id, chat_id=chat_id, message_id=message_id)
        if binding is None:
            await self._answer_callback(callback_id, "This control expired or was already used.", alert=True)
            return
        action = str(binding["action"])
        target = str(binding["target_id"])
        data = str(binding["data"])
        response = ""
        keyboard: dict[str, object] | None = None
        callbacks: tuple[str, ...] = ()
        blog_proposal_id: str | None = None
        blog_setup_panel = False
        blog_enrollment = False
        try:
            if action == "blog_status":
                response = self._blog_setup_reply()
                blog_setup_panel = True
            elif action == "blog_cancel":
                self.blog_setup_store.cancel_proposals()
                self.blog_setup_store.audit("proposals_cancelled", "Owner cancelled pending setup proposals")
                response = "Pending blog setup approvals were cancelled. Existing credentials and resources were not changed.\n\n" + self._blog_setup_reply()
                blog_setup_panel = True
            elif action == "blog_connect":
                if data not in {"github_setup", "github_publish", "vercel"}:
                    response = "Credential type is invalid. Nothing changed."
                else:
                    if not await self._ensure_memory_explorer():
                        raise RuntimeError("The local credential setup service is unavailable; no setup code was issued.")
                    code = self.remote_sessions.issue("blog_setup", owner_id, chat_id)
                    response = (
                        f"Secure credential setup: {REMOTE_ORIGIN}/memory/blog-setup/\n"
                        f"One-use code: `{code}` (10 minutes). Enter credentials only on that page.\n"
                        f"Selected connection: {'GitHub setup' if data == 'github_setup' else 'GitHub target-only publish' if data == 'github_publish' else 'Vercel'}. Credentials stay in the Mac login Keychain and are never sent to OpenCode, Ubuntu, or Telegram."
                    )
                    blog_enrollment = True
            elif action == "blog_prepare_repository":
                provider = BlogProviderClient()
                try:
                    provisioner = BlogProvisioner(self.blog_setup_store, MacOSKeychainVault(), provider)
                    proposal_id, baseline_sha, _branch = await provisioner.prepare_repository(owner_id, chat_id)
                finally:
                    await provider.close()
                blog_proposal_id = proposal_id
                response = (
                    "Review exact GitHub operation · no changes have been made\n"
                    f"Read source: {BLOG_OWNER}/{SOURCE_REPOSITORY} at commit {baseline_sha}\n"
                    f"Create private repository: {BLOG_OWNER}/{TARGET_REPOSITORY}\n"
                    "No writes to the source repository. Vercel project and deployment are not included in this approval.\n"
                    f"Proposal {proposal_id} expires in 10 minutes. Approving creates only this private repository."
                )
            elif action == "blog_prepare_deploy":
                provider = BlogProviderClient()
                try:
                    provisioner = BlogProvisioner(self.blog_setup_store, MacOSKeychainVault(), provider)
                    proposal_id = await provisioner.prepare_copy_and_deploy(owner_id, chat_id)
                finally:
                    await provider.close()
                blog_proposal_id = proposal_id
                state = self.blog_setup_store.snapshot()
                response = (
                    "Review exact copy and deployment operation · no changes have been made\n"
                    f"Copy history from {BLOG_OWNER}/{SOURCE_REPOSITORY} baseline {state.baseline_sha} to existing private repository {BLOG_OWNER}/{TARGET_REPOSITORY} (repository ID {state.repository_id}).\n"
                    f"Create Vercel project {TARGET_PROJECT}, linked only to {BLOG_OWNER}/{TARGET_REPOSITORY}; use its default experimental `vercel.app` domain.\n"
                    "No custom domain, original project, or source repository changes. No plan upgrade or purchase is permitted. Existing-plan usage may be billable; review your Vercel billing plan before approving this deployment.\n"
                    f"Proposal {proposal_id} expires in 10 minutes."
                )
            elif action in {"blog_resource_approve", "blog_resource_reject"}:
                spec = self.blog_setup_store.consume_proposal(target, owner_id, chat_id, message_id)
                if spec is None:
                    response = "That setup approval expired, was already used, or is bound to another message. Nothing changed."
                    blog_setup_panel = True
                elif action == "blog_resource_reject":
                    self.blog_setup_store.audit("proposal_rejected", f"Owner rejected {spec['stage']} proposal")
                    response = "Setup operation cancelled. No external resource was changed.\n\n" + self._blog_setup_reply()
                    blog_setup_panel = True
                elif spec["stage"] == "create_repository":
                    provider = BlogProviderClient()
                    snapshot = None
                    try:
                        provisioner = BlogProvisioner(self.blog_setup_store, MacOSKeychainVault(), provider)
                        snapshot = await provisioner.create_repository_approved(spec["baseline_sha"])
                    except ProviderError as exc:
                        self.blog_setup_store.update(state="setup_error", last_error=str(exc)[:240])
                        response = f"Repository setup stopped safely: {_clean_text(str(exc), limit=240)}"
                    finally:
                        await provider.close()
                    if snapshot is not None and snapshot.repository_id is not None:
                        response = (
                            f"Private experimental repository created: {BLOG_OWNER}/{TARGET_REPOSITORY} (ID {snapshot.repository_id}).\n"
                            f"Source baseline recorded: {snapshot.baseline_sha}. The source repository was not written.\n\n"
                            "Next: use Connect GitHub · target-only publish to enroll a new GitHub token restricted to the experimental repository with Contents read/write; also connect Vercel. Then review the separate copy/deployment approval."
                        )
                    blog_setup_panel = True
                else:
                    spec_id = self.blog_setup_store.snapshot()
                    if str(spec.get("repository_id")) != str(spec_id.repository_id) or spec.get("baseline_sha") != spec_id.baseline_sha:
                        response = "Recorded repository or source baseline changed after proposal. Nothing was deployed; review a fresh proposal."
                        blog_setup_panel = True
                    else:
                        provider = BlogProviderClient()
                        try:
                            provisioner = BlogProvisioner(self.blog_setup_store, MacOSKeychainVault(), provider)
                            snapshot = await provisioner.copy_and_deploy_approved()
                            response = (
                                f"Experimental repository copied at {snapshot.baseline_sha}. Vercel project {TARGET_PROJECT} is connected.\n"
                                f"Project: {snapshot.project_id}\nURL: {snapshot.project_url or 'deployment verification pending'}\n"
                                "The existing production repository/project were not modified."
                            )
                        except ProviderError as exc:
                            self.blog_setup_store.update(state="setup_error", last_error=str(exc)[:240])
                            response = f"Setup stopped safely: {_clean_text(str(exc), limit=240)}. Check `/blog status`; no operation was automatically retried."
                        finally:
                            await provider.close()
                        blog_setup_panel = True
            elif action == "remote_memory":
                if not await self._ensure_memory_explorer():
                    response = "Memory Explorer is unavailable. No remote login code was issued."
                else:
                    code = self.remote_sessions.issue("memory", owner_id, chat_id)
                    response = (
                        f"Memory Explorer: {REMOTE_ORIGIN}/memory/\n"
                        f"One-time sign-in code: `{code}` (10 minutes). Enter it in the page, not in the URL."
                    )
            elif action == "remote_revoke":
                self.remote_sessions.revoke()
                await self.desktop_viewer.close()
                response = "Remote application sessions and unused login codes were revoked. The Ubuntu worker was not changed."
            elif action.startswith("status_"):
                if action == "status_refresh":
                    response = await self._status_reply()
                    keyboard, callbacks = self._keyboard_for_message(chat_id, status_panel=True)
                elif action == "status_tasks":
                    records = self.task_store.list_latest(8)
                    response = "Recent tasks\n" + ("\n".join(
                        f"{item.task_id} · {item.status.value}" for item in records
                    ) if records else "No tasks recorded.")
                    keyboard, callbacks = self._keyboard_for_message(chat_id, status_panel=True)
                elif action == "status_schedules":
                    records = self.schedule_store.list_for_owner(owner_id, chat_id, limit=8)
                    response = "Schedules\n" + ("\n".join(
                        f"{item.schedule_id} · {item.state} · {item.recurrence} · {item.description[:80]}"
                        for item in records
                    ) if records else "No schedules recorded.")
                    status_rows: list[list[dict[str, str]]] = []
                    next_callbacks: list[str] = []
                    for record in records:
                        if record.state not in {"active", "paused"}:
                            continue
                        action_name = "pause" if record.state == "active" else "resume"
                        control = self._new_callback(f"schedule_{action_name}", record.schedule_id, chat_id)
                        cancel = self._new_callback("schedule_cancel", record.schedule_id, chat_id)
                        next_callbacks.extend((control, cancel))
                        status_rows.append([{"text": "Ⅱ Pause" if action_name == "pause" else "▶ Resume", "callback_data": control},
                                            {"text": "Cancel", "callback_data": cancel}])
                    back = self._new_callback("status_refresh", "status", chat_id)
                    next_callbacks.append(back)
                    status_rows.append([{"text": "← Status", "callback_data": back}])
                    keyboard = {"inline_keyboard": status_rows} if status_rows else None
                    callbacks = tuple(next_callbacks)
                else:
                    response = await self._status_reply(diagnostics=True)
                    keyboard, callbacks = self._keyboard_for_message(chat_id, status_panel=True)
                await self._edit_message(
                    chat_id, message_id, response, keyboard=keyboard, callbacks=callbacks,
                    photo_caption=photo_caption,
                )
                await self._answer_callback(callback_id, "Updated")
                return
            if action.startswith("blog_"):
                pass
            elif action in {"desktop_screenshot", "desktop_refresh"}:
                await self._send_desktop_photo(chat_id, message_id)
                await self._answer_callback(callback_id, "Fresh screenshot requested")
                return
            if action == "desktop_details":
                response = await self._desktop_reply(details=True)
            elif action == "desktop_open":
                readiness = await asyncio.to_thread(worker_readiness)
                if readiness.get("worker_ready") is not True:
                    response = "Protected worker readiness is unavailable. No VM was started."
                else:
                    with self._worker_provider() as worker:
                        try:
                            await worker.start_desktop()
                        except WorkerOperationError:
                            response = "The graphical desktop is not installed or could not start. No VM was started."
                        else:
                            desktop_ready = False
                            for _ in range(20):
                                if await worker.desktop_available():
                                    desktop_ready = True
                                    break
                                await asyncio.sleep(0.5)
                            if not desktop_ready:
                                response = "The graphical desktop did not become ready. No viewer session was created."
                                await worker.stop_desktop()
                            elif await self.desktop_viewer.open():
                                response = (
                                    f"View-only desktop session opened for 10 minutes.\n"
                                    f"Open {REMOTE_ORIGIN}/desktop/ in a browser connected to the tailnet and enter this one-use code: "
                                    f"`{self.desktop_viewer.access_code or ''}`"
                                )
                            else:
                                response = "A local desktop viewer session is already active."
            elif action == "desktop_close":
                await self.desktop_viewer.close()
                self.remote_sessions.revoke("desktop")
                try:
                    with self._worker_provider() as worker:
                        await worker.stop_desktop()
                    response = "The viewer session is closed and the isolated desktop session was stopped."
                except WorkerOperationError:
                    response = "The local viewer session is closed and its code was removed. The desktop may remain running because the worker is unavailable."
            elif action in {"task_approve", "task_reject"}:
                approval_id, status = (
                    self.task_store.consume_reply_approval(chat_id, message_id)
                    if action == "task_approve" else self.task_store.decline_reply_approval(chat_id, message_id)
                )
                if approval_id != target or status not in {"approved", "declined"}:
                    response = "Approval is no longer available. Nothing changed."
                elif action == "task_approve":
                    response = self._start_task_reply(target, chat_id)
                else:
                    response = f"Task {target} was declined and cancelled. It was not started."
            elif action == "task_cancel":
                record = self.task_store.get(target)
                response = await self._cancel_reply(target) if record and record.status in {TaskStatus.QUEUED, TaskStatus.DISPATCHING, TaskStatus.RUNNING, TaskStatus.CANCELLING} else "That task is already terminal; nothing changed."
            elif action.startswith("schedule_"):
                operation = action.removeprefix("schedule_")
                record = self.schedule_store.get(target, owner_id=owner_id, chat_id=chat_id)
                if operation == "approve":
                    approved = self.schedule_store.approve(target, owner_id=owner_id, chat_id=chat_id)
                    response = self._schedule_activated_reply(approved) if approved else "Schedule approval is no longer available. Nothing changed."
                elif operation == "reject":
                    current = self.schedule_store.transition(
                        target, owner_id=owner_id, chat_id=chat_id, target="cancel"
                    ) if record is not None and record.state == "pending" else None
                    response = f"Schedule {target} was rejected and cancelled." if current else "Schedule proposal is no longer pending."
                elif record and record.state in {"active", "paused"}:
                    changed = self.schedule_store.transition(target, owner_id=owner_id, chat_id=chat_id, target=operation)
                    response = f"Schedule {target} is {changed.state}." if changed else f"Schedule {target} is already {record.state}; nothing changed."
                else:
                    response = "Schedule control is no longer available. Nothing changed."
                if operation in {"pause", "resume"} and record:
                    keyboard, callbacks = self._keyboard_for_message(chat_id, schedule_id=target)
            elif action == "sources_page":
                sources = self.task_store.sources(target)
                page = int(data) if data.isdecimal() else 0
                page_size = 4
                start = page * page_size
                selected = sources[start:start + page_size]
                if not selected and page != 0:
                    response = "There are no more sources on that page."
                else:
                    browser_evidence_count = sum(source.verification_status in {"observed", "linked_unvisited"} for source in sources)
                    if browser_evidence_count == len(sources) and sources and all(source.verification_status == "observed" for source in sources):
                        heading = "Visited browser sources"
                    elif browser_evidence_count:
                        heading = "Browser evidence · visited pages and unvisited links"
                    else:
                        heading = "URLs mentioned in answer · not browser-verified"
                    response = f"{heading} · {page + 1}/{max(1, (len(sources) + page_size - 1) // page_size)}"
                    rows: list[list[dict[str, str]]] = []
                    for source in selected:
                        title = _clean_text(source.title, limit=120)
                        if source.verification_status == "linked_unvisited":
                            title = "Not visited · " + (source.label or title)
                        elif source.verification_status != "observed":
                            title = "Unverified · " + title
                        elif source.label:
                            title = _clean_text(source.label, limit=60) + " · " + title
                        rows.append([{"text": title[:60] or "Open source", "url": source.url}])
                    page_callbacks: list[str] = []
                    controls: list[dict[str, str]] = []
                    back = self._new_callback("research_details", target, chat_id)
                    page_callbacks.append(back)
                    controls.append({"text": "‹ Back to task", "callback_data": back})
                    if page > 0:
                        previous = self._new_callback("sources_page", target, chat_id, data=str(page - 1))
                        controls.append({"text": "‹ Previous", "callback_data": previous})
                        page_callbacks.append(previous)
                    if start + page_size < len(sources):
                        following = self._new_callback("sources_page", target, chat_id, data=str(page + 1))
                        controls.append({"text": "Next ›", "callback_data": following})
                        page_callbacks.append(following)
                    if controls:
                        rows.append(controls)
                    keyboard = {"inline_keyboard": rows} if rows else None
                    callbacks = tuple(page_callbacks)
                    await self._edit_message(
                        chat_id, message_id, response, keyboard=keyboard, callbacks=callbacks,
                        photo_caption=photo_caption,
                    )
                    await self._answer_callback(callback_id, "Sources opened")
                    return
            elif action == "research_details":
                response = self._task_reply(target)
            elif action == "memory_select":
                memory = self.memory_store.get(target)
                if memory is None or memory.deleted_at is not None:
                    response = "That memory is no longer available."
                else:
                    response = (
                        f"Memory {memory.memory_id} · {memory.kind}\n"
                        f"Project: {memory.project_name or 'none'}\n"
                        f"State: {'superseded' if memory.superseded_by else 'current'}\n"
                        f"Source: {memory.source_type}:{memory.source_id}\n\n{_clean_text(memory.content, limit=1200)}"
                    )
            elif action == "save_memory":
                projects = self.memory_store.project_names()
                if not projects:
                    response = "Create a project first with /project <name> | <description>."
                else:
                    response = "Choose the project for this task reference. This saves a host memory reference only; it does not execute the task."
                    project_rows: list[list[dict[str, str]]] = []
                    callback_ids: list[str] = []
                    for name in projects[:8]:
                        project = self.memory_store.get_project_by_name(name)
                        if project is None:
                            continue
                        choice = self._new_callback("save_project", target, chat_id, data=project.project_id)
                        callback_ids.append(choice)
                        project_rows.append([{"text": name[:48], "callback_data": choice}])
                    keyboard = {"inline_keyboard": project_rows} if project_rows else None
                    callbacks = tuple(callback_ids)
            elif action == "save_project":
                project = self.memory_store.get_project(data)
                task = self.task_store.get(target)
                if project is None or task is None or task.status is not TaskStatus.SUCCEEDED:
                    response = "That saved task or project is no longer available. Nothing changed."
                else:
                    refs = tuple(
                        [("file", f"inputs/{target}/{item.filename}", item.filename) for item in task.attachments]
                        + [("artifact", item.guest_path, item.filename) for item in self.task_store.list_artifacts(target)]
                    )
                    _memory_id, response, _ = self.memory_store.remember_task(
                        int(time.time_ns() % (2**62)), project.name, task_id=target,
                        summary=f"Completed task {target} with status succeeded; original result remains in TaskStore.",
                        references=refs,
                    )
            elif not action.startswith("blog_"):
                response = "This control is not supported. Nothing changed."
            if action.startswith("desktop_"):
                keyboard, callbacks = self._keyboard_for_message(chat_id, desktop_panel=True)
            elif action.startswith("remote_"):
                keyboard, callbacks = self._keyboard_for_message(chat_id, remote_panel=True)
            elif blog_setup_panel:
                keyboard, callbacks = self._keyboard_for_message(chat_id, blog_setup_panel=True)
            elif blog_enrollment:
                keyboard, callbacks = self._keyboard_for_message(chat_id, blog_enrollment=True)
            elif blog_proposal_id:
                keyboard, callbacks = self._keyboard_for_message(chat_id, blog_proposal_id=blog_proposal_id)
            await self._edit_message(
                chat_id, message_id, response, keyboard=keyboard, callbacks=callbacks,
                photo_caption=photo_caption,
            )
            if blog_proposal_id:
                self.blog_setup_store.bind_proposal(blog_proposal_id, message_id)
            await self._answer_callback(callback_id, "Done")
        except (ValueError, KeyError, httpx.HTTPError, RuntimeError) as exc:
            LOGGER.warning("Telegram callback action failed (%s)", type(exc).__name__)
            await self._answer_callback(callback_id, "This action could not be completed safely.", alert=True)

    def _handle_memory_message(
        self, update_id: int, chat_id: int, message: str, command: str, argument: str,
        replied_task_id: str | None, reply_message_id: int | None,
    ) -> tuple[str, tuple[str, ...]] | None:
        if command == "/project":
            name, separator, description = argument.partition("|")
            if not separator:
                return "Usage: /project <name> | <short description>", ()
            try:
                _, response, _ = self.memory_store.create_project(update_id, name, description)
                return response, ()
            except ValueError as exc:
                return _clean_text(str(exc), limit=200), ()

        if command == "/remember":
            project_name: str | None = None
            content = argument
            kind = "fact"
            project_part, separator, fact_part = argument.partition("|")
            if separator:
                project_name = project_part.strip()
                content = fact_part.strip()
            if content.casefold().startswith("preference:"):
                kind, content = "preference", content.partition(":")[2].strip()
            elif content.casefold().startswith("decision:"):
                kind, content = "decision", content.partition(":")[2].strip()
            try:
                memory_id, response, _ = self.memory_store.remember(
                    update_id, content, project_name=project_name, kind=kind
                )
                return response, (memory_id,) if memory_id else ()
            except ValueError as exc:
                return _clean_text(str(exc), limit=200), ()

        if command == "/memory-update":
            memory_id, separator, replacement = argument.partition("|")
            if not separator:
                return "Usage: /memory-update <memory_id> | <corrected fact or preference>", ()
            try:
                new_id, response, _ = self.memory_store.correct(update_id, memory_id.strip(), replacement.strip())
                return response, (new_id,) if new_id else ()
            except ValueError as exc:
                return _clean_text(str(exc), limit=200), ()

        if command == "/forget":
            response, _ = self.memory_store.forget(update_id, argument.strip())
            return response, ()

        if _GRAPH_PROJECT_FILES.fullmatch(message.strip()):
            return self._memory_graph_reply(message.strip()), ()
        if _GRAPH_FILE_TASKS.fullmatch(message.strip()):
            return self._memory_graph_reply(message.strip()), ()
        if _GRAPH_LAST_RESULTS.fullmatch(message.strip()):
            return self._memory_graph_reply(message.strip()), ()
        if _GRAPH_SUPERSESSION.fullmatch(message.strip()):
            return self._memory_graph_reply(message.strip()), ()

        query_match = _MEMORY_QUESTION.fullmatch(message.strip())
        if command == "/memory":
            query = argument
        elif query_match:
            query = query_match.group(1).strip()
        else:
            query = ""
        if query:
            return self._memory_search_reply(query)
        if command == "/memory":
            return "Usage: /memory <project, fact, decision, task, or file query>", ()

        fact_match = _REMEMBER_PROJECT_FACT.fullmatch(message.strip())
        if fact_match:
            project_name, fact = fact_match.groups()
            try:
                memory_id, response, _ = self.memory_store.remember(
                    update_id, f"Uses {(fact or '').rstrip('.! ')}", project_name=(project_name or '').strip(), kind="fact"
                )
                return response, (memory_id,) if memory_id else ()
            except ValueError as exc:
                return _clean_text(str(exc), limit=200), ()

        preference_match = _REMEMBER_PREFERENCE.fullmatch(message.strip())
        if preference_match:
            try:
                memory_id, response, _ = self.memory_store.remember(
                    update_id, preference_match.group(1).rstrip(".! "), kind="preference"
                )
                return response, (memory_id,) if memory_id else ()
            except ValueError as exc:
                return _clean_text(str(exc), limit=200), ()

        decision_match = _REMEMBER_DECISION.fullmatch(message.strip())
        if decision_match:
            decision = decision_match.group(1).rstrip(".! ")
            project_name = None
            project_suffix = re.search(r"\s+for project\s+(.+)$", decision, re.IGNORECASE)
            if project_suffix:
                project_name = project_suffix.group(1).strip()
                decision = decision[:project_suffix.start()].strip()
            try:
                memory_id, response, _ = self.memory_store.remember(
                    update_id, decision, project_name=project_name, kind="decision"
                )
                return response, (memory_id,) if memory_id else ()
            except ValueError as exc:
                return _clean_text(str(exc), limit=200), ()

        update_match = _UPDATE_UNSPECIFIED.fullmatch(message.strip())
        if update_match:
            return (
                f"I need the corrected {update_match.group(1)} values before changing memory. Reply to the saved fact or use /memory to find its ID, then send /memory-update <memory_id> | <corrected value>.",
                (),
            )

        forget_match = re.fullmatch(r"forget that (preference|fact|decision)[.!]*", message.strip(), re.IGNORECASE)
        if forget_match:
            records = self.memory_store.memory_for_message(chat_id, reply_message_id) if reply_message_id else ()
            records = tuple(record for record in records if record.kind == forget_match.group(1).lower())
            if len(records) != 1:
                return "Which saved memory should I forget? Reply to one memory result or use `/forget <memory_id>`.", ()
            response, _ = self.memory_store.forget(update_id, records[0].memory_id)
            return response, ()

        save_match = _SAVE_RESULT.fullmatch(message.strip())
        if save_match:
            task_id = replied_task_id or self.task_store.current_context_task(chat_id)
            task = self.task_store.get(task_id) if task_id else None
            if task is None or task.status is not TaskStatus.SUCCEEDED or not task.result:
                return "Reply to a completed task result first; I could not identify a successful result to save.", ()
            references: list[tuple[str, str, str]] = []
            for attachment in task.attachments:
                references.append(("file", f"inputs/{task.task_id}/{attachment.filename}", attachment.filename))
            for artifact in self.task_store.list_artifacts(task.task_id):
                references.append(("artifact", artifact.guest_path, artifact.filename))
            summary = (
                f"Completed task {task.task_id} with status {task.status.value}. "
                "Its final result remains in the original TaskStore record; model text and reasoning are not copied into memory."
            )
            try:
                memory_id, response, _ = self.memory_store.remember_task(
                    update_id, save_match.group(1).strip(), task_id=task.task_id,
                    summary=summary, references=tuple(references),
                )
                return response, (memory_id,) if memory_id else ()
            except ValueError as exc:
                return _clean_text(str(exc), limit=200), ()

        return None

    def _handle_schedule_message(
        self, update_id: int, owner_id: int, chat_id: int, message: str, command: str, argument: str
    ) -> str | None:
        if command == "/schedules" or _SHOW_SCHEDULES.fullmatch(message.strip()):
            return self._schedules_reply(owner_id, chat_id)
        if command == "/schedule":
            parts = argument.split(maxsplit=1)
            operation = parts[0].lower() if parts else ""
            value = parts[1].strip() if len(parts) == 2 else ""
            if operation == "list":
                return self._schedules_reply(owner_id, chat_id)
            if operation in {"approve", "pause", "resume", "cancel", "show", "latest"}:
                schedule_id = value
                if operation == "latest":
                    if not schedule_id:
                        return "Usage: /schedule latest <schedule_id>"
                    return self._schedule_latest_reply(schedule_id, owner_id, chat_id)
                if not schedule_id:
                    return f"Usage: /schedule {operation} <schedule_id>"
                if operation == "approve":
                    try:
                        approved = self.schedule_store.approve(
                            schedule_id, owner_id=owner_id, chat_id=chat_id
                        )
                    except ValueError as exc:
                        return _clean_text(str(exc), limit=240)
                    if approved is None:
                        return "That schedule is unavailable or no longer pending approval. Nothing was activated."
                    return self._schedule_activated_reply(approved)
                if operation == "show":
                    return self._schedule_detail_reply(schedule_id, owner_id, chat_id)
                record = self.schedule_store.transition(
                    schedule_id, owner_id=owner_id, chat_id=chat_id, target=operation
                )
                if record is None:
                    return f"Schedule {schedule_id} was not changed; check its current state with /schedule show {schedule_id}."
                return f"Schedule {record.schedule_id} is {record.state}. No in-flight occurrence was replayed or cancelled by this state change."
            return "Use /schedules, /schedule approve|pause|resume|cancel|show|latest <schedule_id>."

        management = (
            (_PAUSE_SCHEDULE.fullmatch(message.strip()), "pause"),
            (_RESUME_SCHEDULE.fullmatch(message.strip()), "resume"),
            (_CANCEL_SCHEDULE.fullmatch(message.strip()), "cancel"),
        )
        for match, operation in management:
            if match:
                phrase = match.group(1).strip()
                candidates = self._resolve_schedule_candidates(owner_id, chat_id, phrase)
                if not candidates:
                    return "I couldn't identify a pending or active schedule. Use /schedules to review the available schedules."
                if len(candidates) > 1:
                    return "I found multiple matching schedules: " + ", ".join(record.schedule_id for record in candidates[:8]) + ". Which one do you mean?"
                selected = next(iter(candidates), None)
                if selected is None:
                    return "I couldn't identify a pending or active schedule. Use /schedules to review the available schedules."
                record = self.schedule_store.transition(
                    selected.schedule_id, owner_id=owner_id, chat_id=chat_id, target=operation
                )
                if record is None:
                    return f"Schedule {selected.schedule_id} is {selected.state}; that change is not available."
                return f"Schedule {record.schedule_id} is {record.state}."

        lowered = message.strip().casefold()
        is_schedule_intent = lowered.startswith((
            "remind me ", "every ", "create a reminder", "set a reminder",
            "please create a reminder", "please set a reminder",
        ))
        if not is_schedule_intent:
            return None
        try:
            proposal = parse_schedule_request(message, timezone=self.config.time_zone)
            if proposal is None:
                return "I can schedule a one-time reminder or daily public research. Please specify a supported time and action."
            record = self.schedule_store.create_pending(
                source_update_id=update_id, owner_id=owner_id, chat_id=chat_id,
                description=proposal.description, action=proposal.action, timezone=proposal.timezone,
                local_start=proposal.local_start, recurrence=proposal.recurrence, profile=proposal.profile,
            )
        except ValueError as exc:
            return _clean_text(str(exc), limit=300)
        resolved = datetime.fromisoformat(proposal.resolved_local)
        recurrence = "once" if proposal.recurrence == "once" else "daily"
        profile = "reminder; no model inference" if proposal.profile == "reminder" else "browser-only public research; GET/HEAD, Telegram delivery"
        dst_note = " The nonexistent local time was shifted forward to the first valid minute." if proposal.shifted_for_dst else ""
        return (
            f"Schedule proposal `{record.schedule_id}` (pending, inactive).\n"
            f"Action: {proposal.action}\nTime: {resolved.strftime('%Y-%m-%d %I:%M %p %Z (UTC%z)')}\n"
            f"Recurrence: {recurrence}\nTime zone: {proposal.timezone}.{dst_note}\n"
            f"Scope: {profile}. Missed runs will not be replayed; concurrency is limited to one.\n\n"
            f"To activate this exact schedule, send `/schedule approve {record.schedule_id}`. Nothing is scheduled before approval."
        )

    def _resolve_schedule_candidates(self, owner_id: int, chat_id: int, phrase: str) -> tuple[ScheduleRecord, ...]:
        records = tuple(
            record for record in self.schedule_store.list_for_owner(owner_id, chat_id)
            if record.state in {"pending", "active", "paused"}
        )
        normalized = phrase.casefold().strip()
        if normalized in {"that reminder", "that schedule", "reminder", "schedule"}:
            records = tuple(record for record in records if record.profile == "reminder") if "reminder" in normalized else records
            return records[:2]
        if "morning research" in normalized:
            return tuple(record for record in records if record.profile == "browser" and record.recurrence == "daily")
        tokens = {token for token in re.findall(r"[\w-]{2,}", normalized) if token not in {"my", "the", "morning", "daily", "research", "schedule", "reminder", "that"}}
        if not tokens:
            return ()
        matches = [
            record for record in records
            if tokens <= {token for token in re.findall(r"[\w-]{2,}", (record.description + " " + record.action).casefold())}
        ]
        return tuple(matches[:8])

    def _schedules_reply(self, owner_id: int, chat_id: int) -> str:
        records = self.schedule_store.list_for_owner(owner_id, chat_id, limit=10)
        if not records:
            return "No scheduled tasks found."
        lines = ["Your scheduled tasks:"]
        for record in records:
            next_time = _schedule_time_label(record)
            lines.append(
                f"`{record.schedule_id}` — {record.state} — {record.recurrence} — {next_time}\n{record.description}"
            )
        return "\n".join(lines)

    def _schedule_detail_reply(self, schedule_id: str, owner_id: int, chat_id: int) -> str:
        record = self.schedule_store.get(schedule_id, owner_id=owner_id, chat_id=chat_id)
        if record is None:
            return "Schedule not found. Use /schedules to list owner schedules."
        lines = [
            f"Schedule: `{record.schedule_id}`", f"State: {record.state}",
            f"Action: {record.action}", f"Recurrence: {record.recurrence}",
            f"Time zone: {record.timezone}", f"Next: {_schedule_time_label(record)}",
            f"Profile: {record.profile}; authorization: {record.authorization_policy}",
        ]
        if record.approved_hash:
            lines.append(f"Approval snapshot: {record.approved_hash[:16]} (immutable scope)")
        if record.state_detail:
            lines.append(f"State detail: {record.state_detail}")
        return "\n".join(lines)

    def _schedule_activated_reply(self, record: ScheduleRecord) -> str:
        return (
            f"Activated schedule `{record.schedule_id}`.\n"
            f"Action: {record.action}\nRecurrence: {record.recurrence}; time zone: {record.timezone}.\n"
            f"Next occurrence: {_schedule_time_label(record)}\n"
            "Only this immutable scope is authorized. Missed occurrences are recorded, not replayed."
        )

    def _schedule_latest_reply(self, schedule_id: str, owner_id: int, chat_id: int) -> str:
        record = self.schedule_store.get(schedule_id, owner_id=owner_id, chat_id=chat_id)
        if record is None:
            return "Schedule not found. Use /schedules to list owner schedules."
        occurrences = self.schedule_store.occurrences(schedule_id, owner_id=owner_id, chat_id=chat_id, limit=1)
        if not occurrences:
            return f"Schedule `{schedule_id}` has no execution occurrences yet."
        occurrence = occurrences[0]
        lines = [
            f"Schedule `{schedule_id}` occurrence `{occurrence.occurrence_id}`",
            f"Scheduled for: {occurrence.scheduled_for}", f"Status: {occurrence.status}",
        ]
        if occurrence.task_id:
            task = self.task_store.get(occurrence.task_id)
            lines.append(f"Task: `{occurrence.task_id}` ({task.status.value if task else 'unknown'})")
            if task is not None:
                tools = _recorded_task_tools(self.task_store.events(task.task_id))
                if tools:
                    lines.append("Recorded OpenCode tools: " + ", ".join(tools))
        if occurrence.execution_id:
            lines.append(f"Execution: `{occurrence.execution_id}`")
        if occurrence.result:
            lines.append(f"Result: {occurrence.result[:1800]}")
        if occurrence.error:
            lines.append(f"Error: {_clean_text(occurrence.error, limit=500)}")
        lines.append(f"Telegram delivery: {occurrence.notification_status}")
        return "\n".join(lines)

    def _memory_graph_reply(self, message: str) -> str:
        project_match = _GRAPH_PROJECT_FILES.fullmatch(message)
        file_match = _GRAPH_FILE_TASKS.fullmatch(message)
        results_match = _GRAPH_LAST_RESULTS.fullmatch(message)
        supersession_match = _GRAPH_SUPERSESSION.fullmatch(message)
        if project_match:
            project_name = project_match.group(1).strip()
            result = self.memory_store.graph_search(project_name)
            entities = {entity.entity_id: entity for entity in (*result.matched, *result.connected)}
            project_entities = [entity for entity in result.matched if entity.entity_type.value == "project"]
            if not project_entities:
                return f"I couldn't find a project named {project_name}."
            names = tuple(dict.fromkeys(entity.label for entity in project_entities))
            if len(names) > 1:
                return "I found multiple matching projects: " + ", ".join(names[:8]) + ". Which one do you mean?"
            files: list[str] = []
            for relation in result.relationships:
                if relation.relationship_type.value not in {"TASK_USES_FILE", "TASK_PRODUCES_ARTIFACT"}:
                    continue
                target = entities.get(relation.target_entity_id)
                task = entities.get(relation.source_entity_id)
                if target and task:
                    kind = "uploaded file" if relation.relationship_type.value == "TASK_USES_FILE" else "generated artifact"
                    files.append(f"{target.label} ({kind}; task {task.external_id})")
            unique = tuple(dict.fromkeys(files))
            return (
                f"Files linked to project {project_name}:\n" + "\n".join(unique[:16])
                + "\nSource records: " + ", ".join(result.source_ids[:12])
                if unique else f"I found project {project_name}, but no linked task files or artifacts."
            )
        if file_match:
            filename = file_match.group(1).strip()
            result = self.memory_store.graph_search(filename)
            entities = {entity.entity_id: entity for entity in (*result.matched, *result.connected)}
            tasks: list[str] = []
            for relation in result.relationships:
                if relation.relationship_type.value != "TASK_USES_FILE":
                    continue
                target, task = entities.get(relation.target_entity_id), entities.get(relation.source_entity_id)
                if target and task and filename.casefold() in target.label.casefold():
                    tasks.append(f"{task.external_id} (source: {relation.source_type}:{relation.source_id})")
            unique = tuple(dict.fromkeys(tasks))
            return (
                f"Tasks that used {filename}:\n" + "\n".join(unique[:16])
                + "\nSource records: " + ", ".join(result.source_ids[:12])
                if unique else f"I couldn't find a task linked to {filename}."
            )
        if results_match:
            topic = results_match.group(1).strip()
            result = self.memory_store.graph_search(topic)
            entities = {entity.entity_id: entity for entity in (*result.matched, *result.connected)}
            outputs: dict[str, tuple[datetime, set[str]]] = {}
            for relation in result.relationships:
                if relation.relationship_type.value != "TASK_PRODUCES_ARTIFACT":
                    continue
                artifact, task_node = entities.get(relation.target_entity_id), entities.get(relation.source_entity_id)
                if artifact and task_node:
                    task = self.task_store.get(task_node.external_id)
                    if task is not None:
                        existing = outputs.get(task.task_id)
                        labels: set[str] = existing[1] if existing else set()
                        labels.add(artifact.label)
                        outputs[task.task_id] = (task.created_at, labels)
            if not outputs:
                return f"I couldn't find generated results linked to a {topic} analysis."
            task_id, (created_at, labels) = max(outputs.items(), key=lambda item: item[1][0])
            del created_at
            return (
                f"Latest matching task: {task_id}\nGenerated results: {', '.join(sorted(labels))}"
                f"\nSource records: {', '.join(result.source_ids[:12])}"
            )
        if supersession_match:
            result = self.memory_store.graph_search("fact memory")
            related = {entity.entity_id: entity for entity in (*result.matched, *result.connected)}
            replacements: list[str] = []
            for relation in result.relationships:
                if relation.relationship_type.value != "MEMORY_SUPERSEDES_MEMORY":
                    continue
                newer, older = related.get(relation.source_entity_id), related.get(relation.target_entity_id)
                if newer and older:
                    current = self.memory_store.get(newer.external_id)
                    project = current.project_name if current else None
                    replacements.append(
                        f"{older.external_id} was superseded by {newer.external_id}"
                        + (f" in project {project}" if project else "")
                        + f" (source {relation.source_type}:{relation.source_id})"
                    )
            unique = tuple(dict.fromkeys(replacements))
            if len(unique) > 1:
                return "I found multiple replaced facts. Specify the project or memory ID."
            return unique[0] + f"\nSource records: {', '.join(result.source_ids[:12])}" if unique else "I couldn't find a current fact that replaced an earlier project memory."
        return "I couldn't find a matching knowledge-graph record."

    def _sync_task_graph(self, task_id: str) -> None:
        task = self.task_store.get(task_id)
        if task is None:
            return
        files = tuple(
            (f"inputs/{task.task_id}/{attachment.filename}", attachment.filename)
            for attachment in task.attachments
        )
        artifacts = tuple(
            (artifact.guest_path, artifact.filename)
            for artifact in self.task_store.list_artifacts(task.task_id)
        )
        self.memory_store.sync_task(
            task.task_id, parent_task_id=task.parent_task_id, files=files, artifacts=artifacts,
            created_at=task.created_at.isoformat(),
        )

    def _memory_search_reply(self, query: str) -> tuple[str, tuple[str, ...]]:
        project_match = re.match(r"^project\s+(.+)$", query, re.IGNORECASE)
        project_name = project_match.group(1).strip() if project_match else None
        if project_name:
            context, project_records = self.memory_store.project_context(project_name)
            if not context:
                return f"I don't have saved memories for project {project_name}.", ()
            return "Saved project context (host records):\n" + _clean_text(context, limit=3000), tuple(
                record.memory_id for record in project_records
            )
        records = self.memory_store.search(query, project_name=project_name)
        if not records:
            task_reply = self._task_memory_search(query)
            return task_reply or "I don't have a matching saved memory or task reference.", ()
        projects = {record.project_name for record in records if record.project_name}
        if len(projects) > 1 and project_name is None:
            return "I found matching memories in several projects: " + ", ".join(sorted(projects)) + ". Which project should I search?", ()
        lines = ["Saved memory matches (host records):"]
        for record in records:
            lines.append(
                f"{record.memory_id} — {record.kind}" + (f" / {record.project_name}" if record.project_name else "")
                + f" — {_timestamp_iso(record.updated_at)}\n{_clean_text(record.content, limit=500)}\nSource: {record.source_type}:{record.source_id}"
            )
            if record.task_id:
                task = self.task_store.get(record.task_id)
                if task:
                    lines.append(f"Task reference: {task.task_id} ({task.status.value}); original result remains in /task {task.task_id}.")
            refs = self.memory_store.references(record.memory_id)
            file_labels = [label for kind, _, label in refs if kind in {"file", "artifact"}]
            if file_labels:
                lines.append("Referenced files: " + ", ".join(file_labels))
        ids = tuple(record.memory_id for record in records)
        return "\n\n".join(lines)[:3500], ids

    def _task_memory_search(self, query: str) -> str | None:
        tokens = {token.casefold() for token in re.findall(r"[\w-]{2,}", query) if token.casefold() not in {"find", "my", "last", "the", "about", "previous", "task"}}
        matches: list[tuple[int, Any]] = []
        for task in self.task_store.list_latest(100):
            files = [item.filename for item in task.attachments] + [item.filename for item in self.task_store.list_artifacts(task.task_id)]
            haystack = " ".join((task.request, " ".join(files))).casefold()
            score = sum(token in haystack for token in tokens)
            if score:
                matches.append((score, task))
        if not matches:
            return None
        matches.sort(key=lambda item: (item[0], item[1].created_at), reverse=True)
        best_score = matches[0][0]
        best = [task for score, task in matches if score == best_score]
        if len(best) > 1 and "last" not in query.casefold():
            return "I found multiple matching tasks. Include a task ID or ask for the last one."
        task = best[0]
        files = [item.filename for item in task.attachments] + [item.filename for item in self.task_store.list_artifacts(task.task_id)]
        return (
            f"Most relevant task record: {task.task_id} — {task.status.value}\n"
            f"Request: {_clean_text(task.request, limit=500)}\n"
            f"Files: {', '.join(files) if files else 'none'}\n"
            f"Source: TaskStore; inspect with /task {task.task_id}."
        )

    def _memory_context_for_task(
        self, request: str, chat_id: int, reply_message_id: int | None
    ) -> tuple[str, str, tuple[str, ...], str]:
        same = re.match(r"^(?:use|using) (?:the )?same project context(?: for this new task)?\s*[:,-]?\s*(.*)$", request.strip(), re.IGNORECASE)
        named = re.match(r"^(?:use|using) (?:my )?(.+?) project context(?: for this new task)?\s*[:,-]\s*(.+)$", request.strip(), re.IGNORECASE)
        if same is None and named is None:
            return request, "", (), ""
        reply_records = self.memory_store.memory_for_message(chat_id, reply_message_id) if reply_message_id else ()
        if same is not None:
            names = self.memory_store.project_names_for_message(chat_id, reply_message_id) if reply_message_id else ()
            if not names:
                names = self.memory_store.project_names_for_memories(tuple(record.memory_id for record in reply_records))
            if not names:
                all_projects = self.memory_store.project_names()
                names = all_projects if len(all_projects) == 1 else ()
            if len(set(names)) != 1:
                return request, "", (), "Which project should I use? Reply to one of its saved memory messages or name the project explicitly."
            project_name = next(iter(names))
            task_request = (same.group(1) or "").strip()
        else:
            assert named is not None
            project_name = (named.group(1) or "").strip()
            task_request = (named.group(2) or "").strip()
        if not task_request:
            return request, "", (), "Add the task you want done after the project-context phrase. Nothing was queued."
        context, records = self.memory_store.project_context(project_name)
        if not context:
            return request, "", (), f"I don't have saved context for project {project_name}. Nothing was queued."
        ids = tuple(record.memory_id for record in records)
        return task_request, context, ids, ""

    async def _send_running_progress(self, task_id: str, chat_id: int, elapsed_minutes: int) -> bool:
        task = self.task_store.get(task_id)
        if task is None or task.status is not TaskStatus.RUNNING or elapsed_minutes < 1:
            return False
        previous = re.search(r"\((\d+) minute\(s\) elapsed\)", task.progress)
        if previous and int(previous.group(1)) >= elapsed_minutes:
            return False
        progress = (
            f"Still running on the Ubuntu worker ({elapsed_minutes} minute(s) elapsed); "
            "no result is available yet."
        )
        if not self.task_store.record_progress(task_id, progress):
            return False
        try:
            await self._send_text(
                chat_id,
                f"Task `{task_id}` is still running on Ubuntu ({elapsed_minutes} minute(s)). "
                "No result is available yet.",
                task_id=task_id,
            )
        except (httpx.HTTPError, RuntimeError, ValueError):
            LOGGER.warning("Telegram progress delivery failed for task %s", task_id)
        return True

    def _resolve_natural_task(
        self, text: str, chat_id: int, replied_task_id: str | None
    ) -> tuple[str | None, str]:
        explicit = _TASK_REFERENCE.search(text)
        task_id = explicit.group(0) if explicit else replied_task_id
        if task_id is None and re.search(r"\b(last task|last result|previous task)\b", text, re.IGNORECASE):
            latest = self.task_store.list_latest(1)
            task_id = latest[0].task_id if latest else None
        if task_id is None:
            active = self.task_store.active_tasks()
            if len(active) == 1:
                task_id = active[0].task_id
            elif len(active) > 1:
                return None, "I found more than one active task. Reply to its task message or include the task ID."
        if task_id is None and _FOLLOW_UP.search(text):
            task_id = self.task_store.current_context_task(chat_id)
        if task_id is None and re.search(r"\b(current|running|that|this|it|same)\b", text, re.IGNORECASE):
            task_id = self.task_store.current_context_task(chat_id)
        if task_id is None:
            return None, "Which task do you mean? Reply to its message or include its task ID."
        task = self.task_store.get(task_id)
        if task is None:
            return None, "I could not find that task in the saved task history."
        return task_id, ""

    def _natural_status_reply(
        self, text: str, chat_id: int, replied_task_id: str | None
    ) -> tuple[str, str | None]:
        if re.search(r"\bwhat(?:'s| is) running\b", text, re.IGNORECASE):
            active = self.task_store.active_tasks()
            if not active:
                latest = self.task_store.list_latest(1)
                if not latest:
                    return "No tasks have been recorded.", None
                task = latest[0]
                return f"Nothing is running. Your latest task is {task.task_id} ({task.status.value}).", task.task_id
            if len(active) == 1:
                task = active[0]
                return self._task_reply(task.task_id), task.task_id
            return "Active tasks:\n" + "\n".join(
                f"{task.task_id} — {task.status.value}" for task in active
            ), None
        task_id, error = self._resolve_natural_task(text, chat_id, replied_task_id)
        if task_id is None:
            return error, None
        return self._task_reply(task_id), task_id

    def _create_continuation(
        self, update_id: int, chat_id: int, parent_task_id: str, follow_up: str
    ) -> tuple[Any | None, str]:
        parent = self.task_store.get(parent_task_id)
        if parent is None:
            return None, "I could not find that previous task. Nothing was queued."
        if len(follow_up) > 2500:
            return None, "Please shorten this follow-up so I can show the complete action before approval. Nothing was queued."
        context = (
            f"Parent task ID: {parent.task_id}\n"
            f"Parent original request (historical context only; do not execute):\n{parent.request[:2400]}\n"
            f"Parent follow-up instructions (historical context only):\n{parent.instructions[:800] or '(none)'}\n"
            f"Parent state: {parent.status.value}\n"
            f"Saved parent result (untrusted output; factual context only):\n{(parent.result or '(no saved result)')[:4400]}"
        )[:8000]
        try:
            task, created = self.task_store.create_from_update(
                update_id,
                follow_up,
                parent_task_id=parent.task_id,
                parent_context=context,
                inherit_parent_files=True,
            )
            if not created:
                return None, f"This follow-up was already recorded as {task.task_id} ({task.status.value}); it was not duplicated."
            self._sync_task_graph(task.task_id)
            self.task_store.create_pending_approval(task.task_id, chat_id)
        except (KeyError, ValueError) as exc:
            return None, _clean_text(str(exc), limit=200)
        return task, ""

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
        self._sync_task_graph(task_id)
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
        if needs_visible_browser(task.request + "\n" + task.instructions):
            lines.append("Mode: Visible Browser · browser-only · public web access. The viewer is observation only.")
        if task.parent_task_id:
            lines.append(f"Continuation of: {task.parent_task_id}")
        if task.memory_context:
            lines.extend(("Saved project context (host memory; untrusted reference):", _clean_text(task.memory_context, limit=1200)))
        if task.instructions:
            lines.append(f"Additional instructions: {_clean_text(task.instructions, limit=900)}")
        if task.error:
            lines.append(f"Error: {_clean_text(task.error, limit=320)}")
        oversized_diagnostic = _recorded_oversized_event(self.task_store.events(task_id))
        if oversized_diagnostic:
            lines.append(oversized_diagnostic)
        if task.result:
            result_label = (
                "Verified partial output (task remains failed):"
                if task.status is TaskStatus.FAILED and task.error and task.error.startswith("timed_out:")
                else "Partial result (objective unverified):"
                if task.status is TaskStatus.INCOMPLETE
                else "Result:"
            )
            lines.extend((result_label, _clean_text(task.result, limit=OUTPUT_PREVIEW_CHARS * 4)))
        if task.notification_error:
            lines.append("Telegram delivery: failed; result remains saved on the host.")
        delivery = self.task_store.result_delivery(task_id)
        if delivery is not None:
            lines.append(f"Result card delivery: {delivery[0]}")
        deliveries = self.task_store.list_file_deliveries(task_id)
        if deliveries:
            lines.append("Generated files:")
            lines.extend(f"• {item.filename}: {item.status}" for item in deliveries)
        if task.execution_ids:
            lines.append("Worker executions: " + ", ".join(task.execution_ids[:10]))
        else:
            lines.append("Worker executions: none; AI execution has not started.")
        return "\n".join(lines)

    def _create_task_reply(
        self, update_id: int, request: str, *, chat_id: int,
        memory_context: str = "", memory_ids: tuple[str, ...] = ()
    ) -> str:
        if not request.strip():
            return "Usage: /new <task description>"
        try:
            task, created = self.task_store.create_from_update(
                update_id, request, memory_context=memory_context
            )
            self._sync_task_graph(task.task_id)
        except ValueError as exc:
            return f"Could not create task: {_clean_text(str(exc), limit=240)}"
        if created:
            self.task_store.create_pending_approval(task.task_id, chat_id)
            if memory_ids:
                self.memory_store.link_memories_to_task(update_id, task.task_id, memory_ids)
            if needs_visible_browser(task.request):
                return (
                    "🐉 **TOOTHLESS — RESEARCH TASK**\n\n"
                    f"Research: {_clean_text(task.request, limit=600)}\n"
                    "Mode: Visible Browser\n"
                    "Status: Awaiting approval\n"
                    "Environment: Protected Ubuntu\n"
                    "Network: Public web research only\n\n"
                    f"Task: `{task.task_id}`\n"
                    f"Approve with the button or `/run {task.task_id}`. Nothing has started."
                )
            return (
                f"Task {task.task_id} is queued."
                + ("\nMode: Visible Browser · protected Ubuntu · public web research only." if needs_visible_browser(task.request) else "")
                + (" Relevant saved project context is attached with source IDs; review it with /task before approval.\n\n" if memory_context else "\n\n")
                + f"To start it, send:\n`/run {task.task_id}`"
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
        mode_note = (
            " The approved visible browser starts only after worker acknowledgement; use Watch Live to observe it."
            if candidate is not None and needs_visible_browser(candidate.request + "\n" + candidate.instructions)
            else ""
        )
        return f"Task {task.task_id} is dispatching to the Ubuntu worker. I will report running only after worker acknowledgement.{mode_note}"

    async def _execute_task(
        self, task: Any, execution_id: str, chat_id: int, *, schedule_occurrence_id: str | None = None
    ) -> None:
        last_progress_minute = 0

        async def report_progress(elapsed_seconds: float) -> None:
            nonlocal last_progress_minute
            elapsed_minutes = int(elapsed_seconds // 60)
            if elapsed_minutes < 1 or elapsed_minutes <= last_progress_minute:
                return
            last_progress_minute = elapsed_minutes
            await self._send_running_progress(task.task_id, chat_id, elapsed_minutes)

        def accepted(worker_execution_id: str) -> None:
            self.task_store.mark_worker_accepted(worker_execution_id)
            if schedule_occurrence_id is not None:
                self.schedule_store.mark_running(worker_execution_id)

        runner = OpenCodeTaskRunner(
            lambda: _remote_worker(accepted_callback=accepted),
            attachment_storage=self._attachment_storage,
            cancel_requested=lambda: (
                (current := self.task_store.get(task.task_id)) is None
                or current.status is TaskStatus.CANCELLING
            ),
            progress_callback=report_progress,
        )
        outcome = await runner.execute(task, execution_id)
        saved = self.task_store.finish_execution(
            execution_id, outcome.status, result=outcome.result, error=outcome.error,
            diagnostics=outcome.diagnostics,
        )
        if saved is None:
            return
        if outcome.sources:
            self.task_store.record_observed_sources(saved.task_id, outcome.sources)
        if schedule_occurrence_id is not None:
            await self._complete_scheduled_task(
                saved, execution_id, outcome.status, outcome.result, outcome.error
            )
            if outcome.status is TaskStatus.SUCCEEDED and _expects_task_files(saved.request + "\n" + saved.instructions):
                await self._send_task_files(saved.task_id, chat_id)
            return
        if outcome.status is TaskStatus.SUCCEEDED and outcome.result:
            message = f"**Task completed ·** {saved.task_id}\n\n{outcome.result}"
        elif outcome.status is TaskStatus.INCOMPLETE and outcome.result:
            message = (
                f"**Task incomplete ·** {saved.task_id}\n\n"
                f"{_clean_text(outcome.error or 'The requested objective could not be verified.', limit=500)}\n\n"
                f"Partial response:\n{outcome.result}"
            )
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
            message_id = await self._send_text(chat_id, message, task_id=saved.task_id)
            if outcome.result:
                self.task_store.record_result_delivery(saved.task_id, status="sent", telegram_message_id=message_id)
        except (httpx.HTTPError, RuntimeError):
            if outcome.result:
                self.task_store.record_result_delivery(saved.task_id, status="uncertain")
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
                        storage_key = self._attachment_storage.save(content)
                        self.task_store.record_artifact(
                            task_id, guest_path=guest_path, filename=filename,
                            mime_type=mime_type, size_bytes=len(content), storage_key=storage_key,
                        )
                    except (OSError, RuntimeError, ValueError):
                        LOGGER.warning("Task artifact retention unavailable for task %s", task_id)
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

    async def _schedule_loop(self) -> None:
        while True:
            try:
                for occurrence in self.schedule_store.pending_notifications():
                    schedule = self.schedule_store.schedule_for_occurrence(occurrence.occurrence_id)
                    if schedule is not None:
                        await self._send_schedule_notice(
                            occurrence, schedule, status=occurrence.status,
                            result=occurrence.result, error=occurrence.error,
                            task_id=occurrence.task_id, execution_id=occurrence.execution_id,
                        )
                due = self.schedule_store.claim_due()
                for occurrence in due:
                    await self._dispatch_schedule_occurrence(occurrence)
            except asyncio.CancelledError:
                raise
            except (OSError, sqlite3.Error, ValueError, RuntimeError):
                LOGGER.warning("Scheduled task tick failed; no retry was issued for an existing occurrence")
            await asyncio.sleep(1)

    async def _dispatch_schedule_occurrence(self, occurrence: Any) -> None:
        schedule = self.schedule_store.schedule_for_occurrence(occurrence.occurrence_id)
        if schedule is None:
            self.schedule_store.finish_occurrence(
                occurrence.occurrence_id, status="unknown", error="Schedule record is missing; no work was dispatched."
            )
            return
        if occurrence.status == "missed":
            await self._send_schedule_notice(
                occurrence, schedule, status="missed", error="The controller was unavailable at the scheduled time. This occurrence was not replayed."
            )
            return
        if not self.schedule_store.is_approved_spec_valid(schedule.schedule_id):
            if schedule.recurrence == "daily":
                self.schedule_store.transition(schedule.schedule_id, owner_id=schedule.owner_id, chat_id=schedule.chat_id, target="pause")
            await self._send_schedule_notice(
                occurrence, schedule, status="failed", error="The approved schedule specification did not validate; nothing was run."
            )
            return
        if schedule.profile == "reminder":
            await self._send_schedule_notice(
                occurrence, schedule, status="succeeded", result=f"Reminder: {schedule.action}"
            )
            return
        if (
            schedule.profile != "browser"
            or schedule.authorization_policy != "public_https_get_head_browser_only_telegram_delivery"
            or not needs_browser_profile(schedule.action)
        ):
            if schedule.recurrence == "daily":
                self.schedule_store.transition(schedule.schedule_id, owner_id=schedule.owner_id, chat_id=schedule.chat_id, target="pause")
            await self._send_schedule_notice(
                occurrence, schedule, status="failed", error="The schedule scope is not an approved browser-only research action."
            )
            return
        active = self._active_task_run is not None and not self._active_task_run.done()
        if active or self.task_store.active_tasks():
            await self._send_schedule_notice(
                occurrence, schedule, status="failed", error="The single worker execution slot was busy; this occurrence was not queued for later replay."
            )
            if schedule.recurrence == "daily":
                self.schedule_store.transition(schedule.schedule_id, owner_id=schedule.owner_id, chat_id=schedule.chat_id, target="pause")
            return
        try:
            task, _ = self.task_store.create_scheduled_task(occurrence.occurrence_id, schedule.action)
            execution_id = OpenCodeTaskRunner.new_execution_id()
            if not self.schedule_store.set_occurrence_intent(
                occurrence.occurrence_id, task_id=task.task_id, execution_id=execution_id
            ):
                return
            claimed_task, claimed = self.task_store.claim_for_dispatch(
                task.task_id,
                execution_id,
                authorization_reason=f"Owner-approved schedule {schedule.schedule_id} occurrence {occurrence.occurrence_id}",
            )
            if claimed_task is None or not claimed:
                await self._send_schedule_notice(
                    occurrence, schedule, status="failed", task_id=task.task_id, execution_id=execution_id,
                    error="The worker slot or task claim was unavailable; this occurrence will not be retried.",
                )
                if schedule.recurrence == "daily":
                    self.schedule_store.transition(schedule.schedule_id, owner_id=schedule.owner_id, chat_id=schedule.chat_id, target="pause")
                return
            self._active_task_run = asyncio.create_task(
                self._execute_task(
                    claimed_task, execution_id, schedule.chat_id, schedule_occurrence_id=occurrence.occurrence_id
                ),
                name=f"toothless-scheduled-{occurrence.occurrence_id}",
            )
        except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
            await self._send_schedule_notice(
                occurrence, schedule, status="failed", error=_clean_text(str(exc), limit=400)
            )
            if schedule.recurrence == "daily":
                self.schedule_store.transition(schedule.schedule_id, owner_id=schedule.owner_id, chat_id=schedule.chat_id, target="pause")

    async def _send_schedule_notice(
        self,
        occurrence: Any,
        schedule: ScheduleRecord,
        *,
        status: str,
        result: str | None = None,
        error: str | None = None,
        task_id: str | None = None,
        execution_id: str | None = None,
    ) -> None:
        current = self.schedule_store.get_occurrence(occurrence.occurrence_id)
        if current is not None and current.notification_status in {"sent", "uncertain"}:
            self.schedule_store.finish_occurrence(
                occurrence.occurrence_id, status=status, result=result, error=error
            )
            return
        self.schedule_store.finish_occurrence(
            occurrence.occurrence_id, status=status, result=result, error=error,
            notification_status="pending",
        )
        if not self.schedule_store.mark_notification_sending(occurrence.occurrence_id):
            return
        lines = [
            f"Schedule `{schedule.schedule_id}` occurrence `{occurrence.occurrence_id}`: {status}.",
            f"Scheduled for {occurrence.scheduled_for} ({schedule.timezone}).",
        ]
        if task_id:
            lines.append(f"Task: `{task_id}`")
        if execution_id:
            lines.append(f"Execution: `{execution_id}`")
        if result:
            lines.append(result)
        if error:
            lower_error = error.lower()
            if any(item in lower_error for item in ("auth-required", "authentication", "credential")):
                lines.append(
                    "Guest OpenCode Go authentication is unavailable. Reauthenticate inside Ubuntu with "
                    "ops/worker/opencode-auth-login.sh; this occurrence will not be replayed."
                )
            elif "dns" in lower_error or "relay" in lower_error:
                lines.append(
                    "The trusted guest DNS relay is unavailable. Start the VM only with "
                    "ops/worker/start.sh; this occurrence will not be replayed."
                )
            else:
                lines.append(_clean_text(error, limit=800))
        try:
            message_id = await self._send_text(
                schedule.chat_id, "\n\n".join(lines), task_id=task_id, schedule_id=schedule.schedule_id
            )
        except (httpx.HTTPError, RuntimeError):
            if task_id is not None:
                self.task_store.record_result_delivery(task_id, status="uncertain")
            self.schedule_store.finish_occurrence(
                occurrence.occurrence_id, status=status,
                error=error or "Telegram delivery failed; delivery outcome is uncertain.",
                notification_status="uncertain",
            )
            return
        self.schedule_store.finish_occurrence(
            occurrence.occurrence_id, status=status, result=result, error=error,
            notification_status="sent", telegram_message_id=message_id,
        )
        if task_id is not None:
            self.task_store.record_result_delivery(task_id, status="sent", telegram_message_id=message_id)

    async def _complete_scheduled_task(
        self, task: Any, execution_id: str, status: TaskStatus, result: str | None, error: str | None
    ) -> None:
        occurrence = self.schedule_store.occurrence_for_execution(execution_id)
        if occurrence is None:
            return
        schedule = self.schedule_store.schedule_for_occurrence(occurrence.occurrence_id)
        if schedule is None:
            self.schedule_store.finish_occurrence(occurrence.occurrence_id, status="unknown", error="Schedule record is missing.")
            return
        occurrence_status = {
            TaskStatus.SUCCEEDED: "succeeded",
            TaskStatus.CANCELLED: "cancelled",
            TaskStatus.UNKNOWN: "unknown",
        }.get(status, "failed")
        if occurrence_status in {"failed", "unknown", "cancelled"} and schedule.recurrence == "daily":
            self.schedule_store.transition(schedule.schedule_id, owner_id=schedule.owner_id, chat_id=schedule.chat_id, target="pause")
        current = self.schedule_store.get_occurrence(occurrence.occurrence_id)
        notify_state = current.notification_status if current else "not_applicable"
        self.schedule_store.finish_occurrence(
            occurrence.occurrence_id, status=occurrence_status, result=result, error=error,
            notification_status="pending" if notify_state not in {"sent", "uncertain"} else None,
        )
        if task.cancellation_requested_at is not None:
            return
        await self._send_schedule_notice(
            occurrence, schedule, status=occurrence_status, result=result, error=error,
            task_id=task.task_id, execution_id=execution_id,
        )

    async def _recover_scheduled_occurrences(self) -> None:
        for occurrence in self.schedule_store.unresolved_occurrences():
            schedule = self.schedule_store.schedule_for_occurrence(occurrence.occurrence_id)
            task = self.task_store.get(occurrence.task_id) if occurrence.task_id else None
            if task is not None and occurrence.execution_id in task.execution_ids:
                if task.status in {TaskStatus.DISPATCHING, TaskStatus.RUNNING, TaskStatus.CANCELLING}:
                    continue
                if task.status in {TaskStatus.SUCCEEDED, TaskStatus.INCOMPLETE, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.UNKNOWN}:
                    await self._complete_scheduled_task(
                        task, occurrence.execution_id or "", task.status, task.result, task.error
                    )
                    continue
            self.schedule_store.finish_occurrence(
                occurrence.occurrence_id, status="unknown",
                error="Controller restarted before the schedule occurrence reached a confirmed worker execution; it was not resubmitted.",
            )
            if schedule is not None:
                if schedule.recurrence == "daily":
                    self.schedule_store.transition(schedule.schedule_id, owner_id=schedule.owner_id, chat_id=schedule.chat_id, target="pause")
                current = self.schedule_store.get_occurrence(occurrence.occurrence_id)
                if current is not None:
                    await self._send_schedule_notice(current, schedule, status="unknown", error=current.error)
        for occurrence in self.schedule_store.pending_notifications():
            schedule = self.schedule_store.schedule_for_occurrence(occurrence.occurrence_id)
            if schedule is not None:
                await self._send_schedule_notice(
                    occurrence, schedule, status=occurrence.status, error=occurrence.error,
                    result=occurrence.result, task_id=occurrence.task_id, execution_id=occurrence.execution_id,
                )

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
        self.schedule_store.recover_interrupted()
        await self._recover_active_tasks()
        await self._recover_scheduled_occurrences()
        # A due occurrence discovered at startup may have been missed while the
        # controller was down; record and notify it instead of replaying it.
        for occurrence in self.schedule_store.claim_due(grace_seconds=0):
            await self._dispatch_schedule_occurrence(occurrence)
        self._schedule_loop_task = asyncio.create_task(self._schedule_loop(), name="toothless-scheduler")
        while True:
            try:
                updates = await self._api("getUpdates", {
                    "offset": self.store.next_offset(),
                    "timeout": MAX_POLL_WAIT_SECONDS,
                    "allowed_updates": ["message", "callback_query"],
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
        await self.desktop_viewer.close()
        self.remote_sessions.revoke()
        process, self._memory_explorer_process = self._memory_explorer_process, None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if self._schedule_loop_task is not None and not self._schedule_loop_task.done():
            self._schedule_loop_task.cancel()
            await asyncio.gather(self._schedule_loop_task, return_exceptions=True)
        if self._active_task_run is not None and not self._active_task_run.done():
            self._active_task_run.cancel()
            await asyncio.gather(self._active_task_run, return_exceptions=True)
        self.store.close()
        self.remote_sessions.close()
        self.blog_setup_store.close()
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
                stored = self.execution_store.get(execution_id)
                started_at = stored.started_at if stored is not None else None
                if started_at is None:
                    started_at = task.updated_at
                elapsed = max(0.0, (datetime.now(started_at.tzinfo) - started_at).total_seconds())
                await self._send_running_progress(task.task_id, self.config.owner_id, int(elapsed // 60))
                continue
            await self._save_reconciled_result(task, execution_id, snapshot)
            return

    async def _save_reconciled_result(self, task: Any, execution_id: str, snapshot: Any) -> None:
        outcome = parse_task_result(execution_id, snapshot.stdout, snapshot.status, snapshot.exit_code)
        saved = self.task_store.finish_execution(
            execution_id, outcome.status, result=outcome.result, error=outcome.error,
            diagnostics=outcome.diagnostics,
        )
        if saved is None:
            return
        if outcome.sources:
            self.task_store.record_observed_sources(saved.task_id, outcome.sources)
        if self.schedule_store.occurrence_for_execution(execution_id) is not None:
            await self._complete_scheduled_task(
                saved, execution_id, outcome.status, outcome.result, outcome.error
            )
            return
        if outcome.status is TaskStatus.SUCCEEDED and outcome.result:
            message = f"Task {saved.task_id} completed after controller recovery.\n\n{outcome.result}"
        elif outcome.status is TaskStatus.INCOMPLETE and outcome.result:
            message = (
                f"Task {saved.task_id} is incomplete after controller recovery.\n\n"
                f"{_clean_text(outcome.error or 'The requested objective could not be verified.', limit=500)}\n\n"
                f"Partial response:\n{outcome.result}"
            )
        else:
            message = f"Task {saved.task_id} reconciled as {outcome.status.value}: {_clean_text(outcome.error or '', limit=500)}"
        try:
            message_id = await self._send_text(self.config.owner_id, message, task_id=saved.task_id)
            if outcome.result:
                self.task_store.record_result_delivery(saved.task_id, status="sent", telegram_message_id=message_id)
        except (httpx.HTTPError, RuntimeError):
            if outcome.result:
                self.task_store.record_result_delivery(saved.task_id, status="uncertain")
            self.task_store.record_notification_error(saved.task_id, "Telegram recovery notification failed.")
        if outcome.status is TaskStatus.SUCCEEDED and _expects_task_files(saved.request + "\n" + saved.instructions):
            await self._send_task_files(saved.task_id, self.config.owner_id)


async def _run() -> None:
    global REMOTE_ORIGIN
    config = TelegramConfig.from_environment()
    configured_remote_origin = getattr(config, "remote_origin", REMOTE_ORIGIN)
    REMOTE_ORIGIN = configured_remote_origin
    desktop_viewer_module.REMOTE_ORIGIN = configured_remote_origin
    controller = TelegramController(config)
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    if main_task is None:
        raise RuntimeError("Telegram controller has no owning asyncio task")
    loop.add_signal_handler(signal.SIGTERM, main_task.cancel)
    try:
        await controller.run()
    except asyncio.CancelledError:
        LOGGER.info("Telegram controller received SIGTERM; closing polling and reconciling on restart")
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
        await controller.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Toothless owner-only Telegram controller")
    parser.parse_args()
    _configure_logging()
    try:
        lock_fd = acquire_controller_lock(
            Path(os.environ.get("TOOTHLESS_TELEGRAM_DB", "~/.local/share/toothless/telegram/updates.sqlite3")).expanduser()
        )
    except ControllerAlreadyRunningError:
        LOGGER.error("Another Toothless controller already owns Telegram polling")
        sys.exit(75)
    except Exception as exc:
        LOGGER.error("Could not establish the controller singleton lock (%s)", type(exc).__name__)
        sys.exit(1)
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        LOGGER.info("Telegram controller stopped")
    except Exception as exc:
        # Avoid formatting exception text because HTTP clients can include the token URL.
        LOGGER.error("Telegram controller stopped (%s)", type(exc).__name__)
        sys.exit(1)
    finally:
        os.close(lock_fd)


def _configure_logging() -> None:
    log_directory = os.environ.get("TOOTHLESS_LOG_DIR")
    if log_directory:
        directory = Path(log_directory).expanduser()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise RuntimeError("controller log directory must be owner-only")
        log_path = directory / "controller.log"
        if log_path.is_symlink():
            raise RuntimeError("controller log file must not be a symlink")
        handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8",
        )
        os.chmod(log_path, 0o600)
        logging.basicConfig(
            level=os.environ.get("TOOTHLESS_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s",
            handlers=[handler],
        )
    else:
        logging.basicConfig(level=os.environ.get("TOOTHLESS_LOG_LEVEL", "INFO"), format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


if __name__ == "__main__":
    main()
