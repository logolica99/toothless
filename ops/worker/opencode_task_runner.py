#!/usr/bin/env python3
"""Fixed, bounded OpenCode entry point installed in the Ubuntu guest."""

from __future__ import annotations

import base64
import grp
import hashlib
import ipaddress
import json
import os
import pwd
import re
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

MODEL = "opencode-go/glm-5.3"
VERSION = "2.3.0"
CONFIG = Path("/etc/toothless/opencode.json")
CONFIG_SHA256 = "0b31a03d896e07b62f68f9b2499715bce125423d75b3393b991f47086678ff23"
CONFIG_BROWSER = Path("/etc/toothless/browser.json")
CONFIG_BROWSER_SHA256 = "95d5e9020f1e069de48a9a0fbeb2de57966044760ff628e309afe5f6f3a65c53"
NETWORK_POLICY = Path("/etc/toothless/browser-network-policy.json")
NETWORK_POLICY_SHA256 = "0f626085c1bc9121f2bccd32845bd215d1efc55618b885fda5dc5bca3804c873"
BROWSER_CONTROL_POLICY = Path("/etc/toothless/browser-control-network-policy.json")
BROWSER_CONTROL_POLICY_SHA256 = "c0f06e09c52a49b12e1f69c9bbd46712708a9d19feb9b168aa020a2b7f3efe94"
VISIBLE_BROWSER_AUTH_ROOT = Path("/run/toothless-browser-authorizations")
VISIBLE_BROWSER_SOCKET = Path("/run/toothless-desktop/visible-browser.sock")
AGENT = "toothless-worker"
AGENT_BROWSER = "toothless-browser"
AGENT_USER = "toothless-agent"
AGENT_HOME = "/home/toothless-agent"
AUTH_DATABASE = Path("/home/toothless-agent/.local/share/opencode/opencode.db")
CONFIG_HOME = "/home/toothless-agent/.config"
WORKSPACE = "/home/toothless-agent/workspace"
OPENCODE = "/usr/local/bin/opencode"
BROWSER_MCP = "/usr/local/libexec/toothless/browser_mcp.py"
BROWSER_MCP_PYTHON = "/home/toothless-agent/.local/share/toothless/browser-smoke/venv/bin/python"
MAX_REQUEST_BYTES = 16_384
MAX_OUTPUT_BYTES = 1_048_576
MAX_EVENT_BYTES = 65_536
MAX_RESULT_BYTES = 48_000
MAX_EVENT_HEADER_BYTES = 4096
BROWSER_TEXT_LIMIT_BYTES = 8192
AGENT_STEP_LIMIT = 12
VISIBLE_BROWSER_FAILURE_CATEGORIES = {
    "browser_target_crashed", "browser_context_closed", "browser_startup_failed",
    "browser_runtime_conflict", "navigation_timeout", "browser_session_invalid",
    "browser_request_invalid", "browser_transport_disconnected", "authorization_revoked",
    "navigation_http_error", "browser_operation_failed",
}
VISIBLE_BROWSER_EXCEPTION_CLASSES = {
    "Error", "TargetClosedError", "TimeoutError", "RuntimeError", "ValueError",
    "OSError", "BrowserError", "TargetCrashedError", "PlaywrightError", "NavigationHTTPError",
    "PermissionError", "FileNotFoundError", "Other",
}
VISIBLE_BROWSER_FATAL_CATEGORIES = {
    "authorization_revoked", "browser_context_closed", "browser_runtime_conflict",
    "browser_session_invalid", "browser_startup_failed", "browser_target_crashed",
    "browser_transport_disconnected",
}
VISIBLE_BROWSER_LIFECYCLE_EVENTS = {
    "browser_starting", "browser_process_created", "browser_context_created",
    "browser_page_created", "before_navigation", "after_navigation",
    "before_extraction", "after_extraction", "browser_disconnected",
    "browser_context_closed", "browser_page_closed", "browser_startup_failed",
    "tool_failure", "cleanup_start", "cleanup_complete", "cleanup_failed",
}
TIMEOUT_SECONDS = 120
FILE_TASK_TIMEOUT_SECONDS = 300
BROWSER_TIMEOUT_SECONDS = 300
_child: subprocess.Popen[bytes] | None = None
_scope_unit: str | None = None
_browser_helper_unit: str | None = None
_browser_helper_process: subprocess.Popen[bytes] | None = None
_browser_helper_directory: Path | None = None
_browser_fatal_signal_path: Path | None = None
_browser_source_records: list[dict[str, object]] = []
_visible_auth_file: Path | None = None
_visible_task_id: str | None = None
_visible_execution_id: str | None = None
_config_diagnostics: list[str] = []


def _kill_child(_signum: int, _frame: object) -> None:
    global _scope_unit
    child = _child
    if child is not None and child.poll() is None and _scope_unit is not None:
        try:
            subprocess.run(["/usr/bin/systemctl", "stop", _scope_unit], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if child is not None and child.poll() is None:
        try:
            os.killpg(child.pid, signal.SIGTERM)
            child.wait(timeout=1)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
    _stop_browser_helper()


def _authorize_visible_browser(task_id: str, execution_id: str) -> Path:
    global _visible_auth_file, _visible_task_id, _visible_execution_id
    target: Path | None = None
    if not re.fullmatch(r"task-[0-9a-f]{32}", task_id) or not re.fullmatch(r"opencode-[0-9a-f]{32}", execution_id):
        raise ValueError("Visible browser task or execution ID is invalid")
    try:
        account = pwd.getpwnam("toothless-desktop")
        group = grp.getgrnam("toothless-browser")
        subprocess.run(
            ["/usr/bin/systemctl", "start", "toothless-browser-auth-dir.service"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=True,
        )
        info = VISIBLE_BROWSER_AUTH_ROOT.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_gid != group.gr_gid
                or stat.S_IMODE(info.st_mode) != 0o750):
            raise ValueError("Visible browser authorization storage is unavailable or already occupied")
        for item in VISIBLE_BROWSER_AUTH_ROOT.glob("opencode-*.json"):
            item_info = item.lstat()
            if not stat.S_ISREG(item_info.st_mode) or item_info.st_uid != 0:
                raise ValueError("Visible browser authorization state is unsafe")
            existing = json.loads(item.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                raise ValueError("Visible browser authorization state is invalid")
            expires_at = cast(dict[str, object], existing).get("expires_at")
            if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
                raise ValueError("Visible browser authorization state is invalid")
            if expires_at > time.time():
                raise ValueError("Another visible browser execution is still authorized")
            item.unlink()
        subprocess.run(["/usr/bin/systemctl", "start", "toothless-desktop.service"],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=10, check=True)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if VISIBLE_BROWSER_SOCKET.is_socket():
                socket_info = VISIBLE_BROWSER_SOCKET.stat()
                if socket_info.st_uid == account.pw_uid and socket_info.st_gid == group.gr_gid and stat.S_IMODE(socket_info.st_mode) == 0o660:
                    break
            time.sleep(0.1)
        else:
            raise RuntimeError("Visible browser broker did not become ready")
        record = {
            "task_id": task_id,
            "execution_id": execution_id,
            "profile": "browser",
            "mode": "visible",
            "expires_at": time.time() + BROWSER_TIMEOUT_SECONDS + 30,
        }
        target = VISIBLE_BROWSER_AUTH_ROOT / f"{execution_id}.json"
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
        try:
            os.write(fd, json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chown(target, 0, group.gr_gid)
        os.chmod(target, 0o640)
        _visible_auth_file = target
        _visible_task_id = task_id
        _visible_execution_id = execution_id
        return target
    except (OSError, KeyError, subprocess.SubprocessError) as exc:
        if target is not None:
            target.unlink(missing_ok=True)
        raise RuntimeError("Approved visible browser session could not be authorized") from exc


def _close_visible_browser() -> None:
    global _visible_auth_file, _visible_task_id, _visible_execution_id
    target = _visible_auth_file
    task_id = _visible_task_id
    execution_id = _visible_execution_id
    if target is not None and task_id is not None and execution_id is not None and VISIBLE_BROWSER_SOCKET.is_socket():
        request = json.dumps({"task_id": task_id, "execution_id": execution_id, "name": "close_session", "arguments": {}}).encode("utf-8") + b"\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(2)
                connection.connect(str(VISIBLE_BROWSER_SOCKET))
                connection.sendall(request)
                with connection.makefile("rb") as reader:
                    reader.readline(MAX_EVENT_BYTES)
        except (OSError, TimeoutError):
            pass
    if target is not None:
        target.unlink(missing_ok=True)
    _visible_auth_file = None
    _visible_task_id = None
    _visible_execution_id = None


def _clean_error(value: str) -> str:
    value = re.sub(r"(?i)(api[_-]?key|token|authorization)(\s*[:=]\s*)\S+", r"\1\2[redacted]", value)
    return " ".join(value.split())[:600]


def _safe_error_category(*values: object) -> str:
    text = " ".join(value for value in values if isinstance(value, str)).lower()
    if any(token in text for token in ("rate limit", "too many requests", "429", "quota", "free-limit")):
        return "rate_limit"
    if any(token in text for token in ("unauthorized", "authentication", "auth-required", "invalid api key")):
        return "authentication"
    if any(token in text for token in ("tls", "ssl", "certificate")):
        return "tls"
    if any(token in text for token in ("timeout", "timed out", "etimedout")):
        return "timeout"
    if any(token in text for token in ("network", "connection", "connect", "dns", "transport", "fetch failed", "econn")):
        return "network"
    if any(token in text for token in ("parse", "malformed", "invalid response", "json")):
        return "response_parse"
    if any(token in text for token in ("unavailable", "overloaded", "503", "502")):
        return "provider_unavailable"
    if text:
        return "provider_error"
    return "unknown"


def _visible_browser_diagnostic(state: dict[str, object]) -> dict[str, object]:
    """Extract only the broker's fixed diagnostic marker; discard all tool text."""
    pending: list[tuple[object, int]] = [(state.get(key), 0) for key in ("error", "output", "result")]
    while pending:
        value, depth = pending.pop()
        if depth > 3:
            continue
        if isinstance(value, str) and len(value) <= 16_384:
            marker = "TOOTHLESS_BROWSER_DIAGNOSTIC:"
            start = value.find(marker)
            if start >= 0:
                encoded = value[start + len(marker):].encode("utf-8")[:16_384]
                end = _bounded_json_value_end(encoded, 0, len(encoded))
                if end is not None:
                    try:
                        parsed: object = json.loads(encoded[:end].decode("utf-8"))
                    except json.JSONDecodeError:
                        continue
                    if isinstance(parsed, dict):
                        source = cast(dict[str, object], parsed)
                        safe: dict[str, object] = {}
                        category = source.get("failure_category")
                        if isinstance(category, str) and category in VISIBLE_BROWSER_FAILURE_CATEGORIES:
                            safe["failure_category"] = category
                        exception = source.get("exception_class")
                        if isinstance(exception, str) and exception in VISIBLE_BROWSER_EXCEPTION_CLASSES:
                            safe["exception_class"] = exception
                        for key in ("invocation_sequence", "elapsed_ms"):
                            number = source.get(key)
                            if type(number) is int and 0 <= number <= 120_000:
                                safe[key] = number
                        for key, allowed in (
                            ("tool_name", re.compile(r"[a-z][a-z0-9_]{0,63}")),
                            ("session_state", re.compile(r"(?:invalidated|unchanged|active|idle)")),
                            ("execution_id", re.compile(r"opencode-[0-9a-f]{32}")),
                        ):
                            item = source.get(key)
                            if isinstance(item, str) and allowed.fullmatch(item):
                                safe[key] = item
                        connected = source.get("browser_connected")
                        if isinstance(connected, bool):
                            safe["browser_connected"] = connected
                        process_alive = source.get("browser_process_alive")
                        if isinstance(process_alive, bool):
                            safe["browser_process_alive"] = process_alive
                        fatal_signal = source.get("fatal_signal")
                        if isinstance(fatal_signal, bool):
                            safe["fatal_signal"] = fatal_signal
                        _copy_browser_lifecycle(source, safe)
                        return safe
        elif isinstance(value, dict):
            mapping = cast(dict[str, object], value)
            pending.extend((mapping.get(key), depth + 1) for key in ("text", "message", "error", "output"))
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value[:8])
    return {}


def _copy_browser_lifecycle(source: dict[str, object], target: dict[str, object]) -> None:
    task_id = source.get("task_id")
    if isinstance(task_id, str) and re.fullmatch(r"task-[0-9a-f]{32}", task_id):
        target["task_id"] = task_id
    category = source.get("failure_category")
    if isinstance(category, str) and category in VISIBLE_BROWSER_FAILURE_CATEGORIES:
        target["failure_category"] = category
    entries_raw = source.get("lifecycle")
    if not isinstance(entries_raw, list):
        return
    entries: list[dict[str, object]] = []
    allowed_states = {"idle", "launching", "active", "disconnected", "closed", "unknown"}
    allowed_termination = {
        "application_close", "target_crash", "transport_disconnect", "process_disappeared",
        "normal_exit", "signal_exit", "unknown_exit", "unknown",
    }
    allowed_cleanup = {"explicit_close", "fatal_failure", "authorization_revoked", "startup_failure", "service_shutdown", "mcp_cleanup", "cleanup_failed", ""}
    for entry_raw in cast(list[object], entries_raw)[-8:]:
        if not isinstance(entry_raw, dict):
            continue
        entry = cast(dict[str, object], entry_raw)
        event = entry.get("event")
        if not isinstance(event, str) or event not in VISIBLE_BROWSER_LIFECYCLE_EVENTS:
            continue
        item: dict[str, object] = {"event": event}
        timestamp = entry.get("timestamp")
        if isinstance(timestamp, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT[0-9:.+-]+Z", timestamp[:40]):
            item["timestamp"] = timestamp[:40]
        for key in (
            "elapsed_ms", "tool_invocation_sequence", "process_count", "thread_count", "fd_count", "fd_limit",
            "browser_fd_count", "browser_fd_limit", "browser_thread_count", "cgroup_pids_current",
            "cgroup_pids_max", "cgroup_memory_current_bytes", "cgroup_memory_max_bytes",
            "browser_pid", "playwright_driver_pid", "browser_exit_code", "browser_exit_signal",
            "playwright_driver_exit_code",
        ):
            number = entry.get(key)
            if type(number) is int and 0 <= number <= 2**63 - 1:
                item[key] = number
        roles = entry.get("process_roles")
        if isinstance(roles, dict):
            safe_roles: dict[str, dict[str, int]] = {}
            for role, counts_raw in cast(dict[str, object], roles).items():
                if role not in {"xvfb", "openbox", "xterm", "broker", "playwright_driver", "chrome_browser", "chrome_renderer_or_utility", "other"} or not isinstance(counts_raw, dict):
                    continue
                counts = cast(dict[str, object], counts_raw)
                safe_counts = {
                    key: count for key, count in counts.items()
                    if key in {"processes", "threads"} and type(count) is int and 0 <= count <= 4096
                }
                if safe_counts:
                    safe_roles[role] = safe_counts
            if safe_roles:
                item["process_roles"] = safe_roles
        ancestors = entry.get("cgroup_pid_ancestors")
        if isinstance(ancestors, list):
            safe_ancestors: list[dict[str, int | str]] = []
            for ancestor_raw in ancestors[:4]:
                if not isinstance(ancestor_raw, dict):
                    continue
                ancestor = cast(dict[str, object], ancestor_raw)
                level = ancestor.get("level")
                if not isinstance(level, str) or level not in {"service", "parent_1", "parent_2", "parent_3"}:
                    continue
                safe_ancestor: dict[str, int | str] = {"level": level}
                for key in ("pids_current", "pids_max"):
                    count = ancestor.get(key)
                    if type(count) is int and 0 <= count <= 2**31 - 1:
                        safe_ancestor[key] = count
                safe_ancestors.append(safe_ancestor)
            if safe_ancestors:
                item["cgroup_pid_ancestors"] = safe_ancestors
        for key in ("browser_connected", "context_open", "page_open", "browser_process_alive", "playwright_driver_alive"):
            flag = entry.get(key)
            if isinstance(flag, bool):
                item[key] = flag
        for key, allowed in (
            ("session_state", allowed_states),
            ("browser_termination", allowed_termination),
            ("cleanup_reason", allowed_cleanup),
        ):
            value = entry.get(key)
            if isinstance(value, str) and value in allowed:
                item[key] = value
        for key in ("memory_events_delta", "pids_events_delta"):
            nested = entry.get(key)
            if isinstance(nested, dict):
                counts = {
                    name: number for name, number in cast(dict[str, object], nested).items()
                    if name in {"low", "high", "max", "oom", "oom_kill", "oom_group_kill", "sock_throttled"}
                    and type(number) is int and 0 <= number <= 2**31 - 1
                }
                if counts:
                    item[key] = counts
        raw_cpu = entry.get("cpu_stats")
        if isinstance(raw_cpu, dict):
            counters = {
                name: number for name, number in cast(dict[str, object], raw_cpu).items()
                if name in {"usage_usec", "nr_periods", "nr_throttled", "throttled_usec"}
                and type(number) is int and 0 <= number <= 2**63 - 1
            }
            if counters:
                item["cpu_stats"] = counters
        entries.append(item)
    if entries:
        target["lifecycle"] = entries


def _has_active_opencode_go_credential() -> bool:
    """Check only provider metadata in OpenCode's SQLite auth store; never select secret values."""
    try:
        if AUTH_DATABASE.is_symlink() or not AUTH_DATABASE.is_file():
            return False
        if AUTH_DATABASE.stat().st_uid != pwd.getpwnam(AGENT_USER).pw_uid:
            return False
        database_uri = AUTH_DATABASE.as_uri() + "?mode=ro"
        with sqlite3.connect(database_uri, uri=True, timeout=2) as connection:
            return connection.execute(
                "SELECT 1 FROM credential WHERE integration_id = ? AND active = 1 LIMIT 1",
                ("opencode-go",),
            ).fetchone() is not None
    except (KeyError, OSError, sqlite3.Error, ValueError):
        return False


def _safe_timestamp(value: object) -> str | int | float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < 10**16:
        return value
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d\d-\d\d[Tt ][0-9:.+-]+Z?", value[:40]):
        return value[:40]
    return None


def _diagnostic_entry(event: dict[str, object], sequence: int) -> dict[str, object]:
    kind = event.get("type")
    kind = kind if isinstance(kind, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", kind) else "other"
    entry: dict[str, object] = {"sequence": sequence, "type": kind}
    timestamp = _safe_timestamp(event.get("timestamp", event.get("time")))
    if timestamp is None and isinstance(event.get("time"), dict):
        timestamp = _safe_timestamp(cast(dict[str, object], event["time"]).get("created"))
    if timestamp is not None:
        entry["timestamp"] = timestamp
    status_raw = event.get("status")
    status = cast(dict[str, object], status_raw) if isinstance(status_raw, dict) else {}
    retry = status if status.get("type") == "retry" else event if kind == "retry" else {}
    if retry:
        entry["activity"] = "retry"
        attempt = retry.get("attempt", retry.get("retry_count", retry.get("retryCount")))
        if isinstance(attempt, int) and not isinstance(attempt, bool) and 0 <= attempt <= 1000:
            entry["retry_attempt"] = attempt
        entry["error_category"] = _safe_error_category(retry.get("message"), retry.get("reason"))
        next_at = retry.get("next")
        if isinstance(next_at, (int, float)) and not isinstance(next_at, bool) and 0 <= next_at < 10**16:
            entry["retry_at"] = next_at
            if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool) and next_at >= timestamp:
                entry["backoff_ms"] = min(300_000, int(next_at - timestamp))
    part_raw = event.get("part")
    part = cast(dict[str, object], part_raw) if isinstance(part_raw, dict) else {}
    tool = part.get("tool")
    if kind == "tool_use" and isinstance(tool, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", tool):
        entry["activity"] = "tool"
        entry["tool"] = tool
        state_raw = part.get("state")
        if isinstance(state_raw, dict):
            state_data = cast(dict[str, object], state_raw)
            state = state_data.get("status")
            if isinstance(state, str) and state in {"pending", "running", "completed", "error"}:
                entry["tool_status"] = state
            if tool.startswith("toothless_browser_") and state == "error":
                entry["activity"] = "browser_failure"
                entry.update(_visible_browser_diagnostic(state_data))
                if "failure_category" not in entry:
                    entry["failure_category"] = "browser_session_invalid"
    if kind == "error":
        error_raw = event.get("error")
        error = cast(dict[str, object], error_raw) if isinstance(error_raw, dict) else {}
        entry["activity"] = "error"
        entry["error_category"] = _safe_error_category(error.get("type"), error.get("name"), error.get("message"))
        for key in ("status", "statusCode", "status_code", "httpStatus", "http_status"):
            code = error.get(key, event.get(key))
            if isinstance(code, int) and not isinstance(code, bool) and 100 <= code <= 599:
                entry["http_status_class"] = f"{code // 100}xx"
                break
    return entry


def _event_diagnostics(raw: bytes, *, execution_id: object = None) -> dict[str, object]:
    timeline: list[dict[str, object]] = []
    event_types: list[str] = []
    tools: list[str] = []
    event_count = retry_count = browser_tool_failure_count = step_start_count = 0
    truncated = False
    for line in raw.splitlines()[:1000]:
        if not line.strip():
            continue
        if len(line) > MAX_EVENT_BYTES:
            truncated = True
            continue
        try:
            parsed: object = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            truncated = True
            continue
        if not isinstance(parsed, dict):
            truncated = True
            continue
        event = cast(dict[str, object], parsed)
        kind = event.get("type")
        if not isinstance(kind, str):
            truncated = True
            continue
        event_count += 1
        kind = kind if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", kind) else "other"
        if kind == "step_start":
            step_start_count += 1
        if kind not in event_types and len(event_types) < 16:
            event_types.append(kind)
        entry = _diagnostic_entry(event, event_count)
        if entry.get("activity") == "retry":
            retry_count += 1
        if entry.get("activity") == "browser_failure":
            browser_tool_failure_count += 1
        tool = entry.get("tool")
        if isinstance(tool, str) and tool not in tools and len(tools) < 16:
            tools.append(tool)
        if len(timeline) < 32:
            timeline.append(entry)
        else:
            truncated = True
    if len(raw.splitlines()) > 1000:
        truncated = True
    result: dict[str, object] = {
        "event_count": event_count,
        "event_types": event_types,
        "retry_count": retry_count,
        "browser_tool_failure_count": browser_tool_failure_count,
        "step_start_count": step_start_count,
        "step_limit": AGENT_STEP_LIMIT,
        "tools": tools,
        "timeline": timeline,
        "metadata_truncated": truncated,
    }
    if isinstance(execution_id, str) and re.fullmatch(r"opencode-[0-9a-f]{32}", execution_id):
        result["execution_id"] = execution_id
    return result


def _skip_json_space(data: bytes, position: int, limit: int) -> int:
    while position < limit and data[position] in b" \t\r\n":
        position += 1
    return position


def _bounded_json_string(data: bytes, position: int, limit: int) -> tuple[str, int] | None:
    position = _skip_json_space(data, position, limit)
    if position >= limit or data[position] != 0x22:
        return None
    start = position
    position += 1
    escaped = False
    while position < limit:
        byte = data[position]
        position += 1
        if escaped:
            escaped = False
        elif byte == 0x5C:
            escaped = True
        elif byte == 0x22:
            token = data[start:position]
            if len(token) > 256:
                return None
            try:
                value = json.loads(token.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            return (value, position) if isinstance(value, str) else None
    return None


def _bounded_json_value_end(data: bytes, position: int, limit: int) -> int | None:
    position = _skip_json_space(data, position, limit)
    if position >= limit:
        return None
    if data[position] == 0x22:
        value = _bounded_json_string(data, position, limit)
        return value[1] if value is not None else None
    if data[position] not in {0x7B, 0x5B}:
        while position < limit and data[position] not in b",}] \t\r\n":
            position += 1
        return position if position < limit else None
    depth = 0
    in_string = False
    escaped = False
    for index in range(position, limit):
        byte = data[index]
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
        elif byte == 0x22:
            in_string = True
        elif byte in {0x7B, 0x5B}:
            depth += 1
        elif byte in {0x7D, 0x5D}:
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _bounded_object_string(data: bytes, position: int, target: str, limit: int) -> str | None:
    position = _skip_json_space(data, position, limit)
    if position >= limit or data[position] != 0x7B:
        return None
    position += 1
    for _ in range(32):
        position = _skip_json_space(data, position, limit)
        if position >= limit or data[position] == 0x7D:
            return None
        key = _bounded_json_string(data, position, limit)
        if key is None:
            return None
        name, position = key
        position = _skip_json_space(data, position, limit)
        if position >= limit or data[position] != 0x3A:
            return None
        position = _skip_json_space(data, position + 1, limit)
        if name == target:
            value = _bounded_json_string(data, position, limit)
            return value[0] if value is not None else None
        end = _bounded_json_value_end(data, position, limit)
        if end is None:
            return None
        position = _skip_json_space(data, end, limit)
        if position < limit and data[position] == 0x2C:
            position += 1
        elif position < limit and data[position] == 0x7D:
            return None
        else:
            return None
    return None


def _bounded_event_header(line: bytes) -> tuple[str | None, str | None]:
    limit = min(len(line), MAX_EVENT_HEADER_BYTES)
    position = _skip_json_space(line, 0, limit)
    if position >= limit or line[position] != 0x7B:
        return None, None
    position += 1
    event_type: str | None = None
    tool_name: str | None = None
    for _ in range(32):
        position = _skip_json_space(line, position, limit)
        if position >= limit or line[position] == 0x7D:
            break
        key = _bounded_json_string(line, position, limit)
        if key is None:
            break
        name, position = key
        position = _skip_json_space(line, position, limit)
        if position >= limit or line[position] != 0x3A:
            break
        position = _skip_json_space(line, position + 1, limit)
        if name == "type":
            value = _bounded_json_string(line, position, limit)
            if value is not None and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value[0]):
                event_type = value[0]
                position = value[1]
            else:
                break
        elif name == "part" and event_type == "tool_use":
            tool_name = _bounded_object_string(line, position, "tool", limit)
            end = _bounded_json_value_end(line, position, limit)
            if end is None:
                break
            position = end
        else:
            end = _bounded_json_value_end(line, position, limit)
            if end is None:
                break
            position = end
        position = _skip_json_space(line, position, limit)
        if position < limit and line[position] == 0x2C:
            position += 1
        elif position < limit and line[position] == 0x7D:
            break
        else:
            break
    if tool_name is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", tool_name):
        tool_name = None
    return event_type, tool_name


def _oversized_event_metadata(line: bytes, sequence: int, execution_id: object) -> dict[str, object]:
    event_type, tool_name = _bounded_event_header(line)
    metadata: dict[str, object] = {
        "sequence": sequence,
        "observed_bytes": min(len(line), MAX_OUTPUT_BYTES + 1),
        "limit_bytes": MAX_EVENT_BYTES,
        "event_type": event_type or "unknown",
        "failure_category": "event_size_limit",
    }
    if isinstance(execution_id, str) and re.fullmatch(r"opencode-[0-9a-f]{32}", execution_id):
        metadata["execution_id"] = execution_id
    if event_type == "tool_use" and tool_name is not None:
        metadata["tool_name"] = tool_name
    return metadata


def _events(raw: bytes, returncode: int, stderr: str, *, execution_id: object = None) -> dict[str, Any]:
    if len(raw) > MAX_OUTPUT_BYTES:
        return {"status": "output_limited", "error": "OpenCode output exceeded the configured limit."}
    text_parts: list[str] = []
    errors: list[str] = []
    tools: list[str] = []
    event_types: list[str] = []
    error_name = ""
    event_count = 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        if len(line) > MAX_EVENT_BYTES:
            diagnostics = _event_diagnostics(raw, execution_id=execution_id)
            oversized = _oversized_event_metadata(line, event_count + 1, execution_id)
            diagnostics["failure_category"] = "event_size_limit"
            diagnostics["oversized_event"] = oversized
            if isinstance(execution_id, str) and re.fullmatch(r"opencode-[0-9a-f]{32}", execution_id):
                diagnostics["execution_id"] = execution_id
            type_name = oversized["event_type"]
            error = f"OpenCode event {event_count + 1} exceeded the configured {MAX_EVENT_BYTES}-byte limit."
            if type_name != "unknown":
                error = f"OpenCode {type_name} event {event_count + 1} exceeded the configured {MAX_EVENT_BYTES}-byte limit."
            return {"status": "output_limited", "error": error, "diagnostics": diagnostics}
        try:
            event_raw: object = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"status": "malformed_output", "error": "OpenCode returned malformed event data."}
        if not isinstance(event_raw, dict):
            return {"status": "malformed_output", "error": "OpenCode returned an invalid event."}
        event = cast(dict[str, object], event_raw)
        event_type = event.get("type")
        if not isinstance(event_type, str):
            return {"status": "malformed_output", "error": "OpenCode returned an invalid event."}
        event_count += 1
        event_types.append(event_type if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", event_type) else "other")
        if event_count > 1000:
            return {"status": "output_limited", "error": "OpenCode returned too many events."}
        if event_type == "text":
            part = event.get("part")
            if not isinstance(part, dict):
                return {"status": "malformed_output", "error": "OpenCode returned an invalid text event."}
            part_data = cast(dict[str, object], part)
            part_text = part_data.get("text")
            if not isinstance(part_text, str):
                return {"status": "malformed_output", "error": "OpenCode returned an invalid text event."}
            text_parts.append(part_text)
        elif event_type == "tool_use":
            part = event.get("part")
            if not isinstance(part, dict):
                return {"status": "malformed_output", "error": "OpenCode returned an invalid tool event."}
            tool = cast(dict[str, object], part).get("tool")
            if not isinstance(tool, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", tool):
                return {"status": "malformed_output", "error": "OpenCode returned an invalid tool event."}
            if tool not in tools:
                tools.append(tool)
        elif event_type == "error":
            error = event.get("error")
            if isinstance(error, dict):
                error_data = cast(dict[str, object], error)
                name = error_data.get("type", error_data.get("name"))
                message = error_data.get("message")
                if isinstance(name, str):
                    error_name = name
                if isinstance(message, str):
                    errors.append(message)
    error_text = "\n".join(errors) or stderr
    normalized = f"{error_name} {error_text}".lower()
    if error_name == "provider.free-limit" or "rate limit" in normalized or "usage limit" in normalized:
        status = "rate_limited"
    elif error_name in {"provider.auth", "provider.auth-required"} or "unauthorized" in normalized:
        status = "auth_required"
    elif error_name in {"provider.model-not-found", "provider.model-unavailable"}:
        status = "model_unavailable"
    elif any(term in normalized for term in ("network", "connection", "connect", "dns", "fetch failed", "transport", "etimedout", "timed out")):
        status = "network_error"
    elif returncode != 0 or errors:
        status = "failed"
    elif event_count == 0:
        status = "malformed_output"
    else:
        status = "succeeded"
    result = "".join(text_parts)
    if len(result.encode("utf-8")) > MAX_RESULT_BYTES:
        return {"status": "output_limited", "error": "OpenCode answer exceeded the configured limit."}
    return {
        "status": status,
        "text": result,
        "error": _clean_error(error_text),
        "event_count": event_count,
        "event_types": event_types,
        "tools": tools,
        "diagnostics": _event_diagnostics(raw, execution_id=execution_id),
    }


def _partial_event_metadata(raw: bytes, execution_id: object = None) -> dict[str, object]:
    """Keep bounded structural diagnostics and identify a capped oversized line without parsing its body."""
    diagnostics = _event_diagnostics(raw)
    if len(raw) > MAX_OUTPUT_BYTES:
        event_count = diagnostics.get("event_count", 0)
        for line in raw.splitlines()[:1000]:
            if len(line) <= MAX_EVENT_BYTES:
                continue
            sequence = event_count + 1 if type(event_count) is int else 1
            oversized = _oversized_event_metadata(line, sequence, execution_id)
            oversized.pop("observed_bytes", None)
            oversized["observed_bytes_at_least"] = min(len(line), MAX_OUTPUT_BYTES + 1)
            diagnostics["failure_category"] = "event_size_limit"
            diagnostics["oversized_event"] = oversized
            if isinstance(execution_id, str) and re.fullmatch(r"opencode-[0-9a-f]{32}", execution_id):
                diagnostics["execution_id"] = execution_id
            break
    return diagnostics


def _agent_command(arguments: list[str], env: dict[str, str]) -> list[str]:
    return [
        "/usr/sbin/runuser", "-u", AGENT_USER, "--", "/usr/bin/env",
        *(f"{key}={value}" for key, value in env.items()), OPENCODE, *arguments,
    ]


def _run_probe(command: list[str], timeout: float) -> subprocess.CompletedProcess[bytes]:
    process = subprocess.Popen(
        command,
        cwd=WORKSPACE,
        env={"PATH": "/usr/sbin:/usr/bin:/bin", "LANG": "C.UTF-8"},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
        raise
    if len(stdout) > 262_144 or len(stderr) > 262_144:
        raise ValueError("OpenCode config probe exceeded its output limit")
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _verify_effective_config(env: dict[str, str], *, browser: bool = False) -> bool:
    config_path = CONFIG_BROWSER if browser else CONFIG
    agent_name = AGENT_BROWSER if browser else AGENT
    _config_diagnostics.clear()
    try:
        version_result = _run_probe(_agent_command(["--version"], env), 8)
        version = (version_result.stdout + version_result.stderr).decode("utf-8", errors="strict")
        if not re.search(r"(^|[^0-9])2[.]3[.]0([^0-9]|$)", version):
            detail = re.sub(r"(?i)(api[_-]?key|token|authorization)(\s*[:=]\s*)\S+", r"\1\2[redacted]", " ".join(version.split()))
            _config_diagnostics.append("version_probe:" + (detail[:120] or "empty"))
            return False
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else ""
        _config_diagnostics.append(f"version_command_exit_{exc.returncode}:" + _clean_error(detail)[:180])
        return False
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        _config_diagnostics.append("version_command_failed")
        return False
    try:
        result = _run_probe(_agent_command(["debug", "config", "--standalone"], env), 30)
        if result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)
        if len(result.stdout) > 262_144:
            _config_diagnostics.append("debug_output_over_limit")
            return False
        config_raw: object = json.loads(result.stdout.decode("utf-8"))
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else ""
        _config_diagnostics.append(f"debug_config_exit_{exc.returncode}:" + _clean_error(detail)[:240])
        return False
    except (OSError, subprocess.SubprocessError):
        _config_diagnostics.append("debug_config_command_failed")
        return False
    except (UnicodeDecodeError, json.JSONDecodeError):
        _config_diagnostics.append("debug_output_not_json")
        return False
    if isinstance(config_raw, dict):
        config = cast(dict[str, object], config_raw)
    elif isinstance(config_raw, list):
        config_sources = cast(list[object], config_raw)
        sources: list[dict[str, object]] = []
        for item in config_sources:
            if not isinstance(item, dict):
                _config_diagnostics.append("invalid_debug_source_entry")
                return False
            sources.append(cast(dict[str, object], item))
        if len(sources) != len(config_sources):
            _config_diagnostics.append("invalid_debug_source_entry")
            return False
        loaded: list[dict[str, object]] = []
        for source in sources:
            path = source.get("path")
            info = source.get("info")
            if path == str(config_path) and isinstance(info, dict):
                loaded.append(source)
            elif path == f"{CONFIG_HOME}/opencode" and info is None:
                # The XDG directory may be initialized by OpenCode but must contain no config.
                continue
            elif (
                path is None
                and isinstance(info, dict)
                and set(cast(dict[str, object], info)) == {"providers"}
            ):
                # This is OpenCode's provider catalog, not another user configuration source.
                continue
            else:
                _config_diagnostics.append("unexpected_config_sources")
                return False
        if len(loaded) != 1:
            _config_diagnostics.append("unexpected_config_sources")
            return False
        config = cast(dict[str, object], loaded[0]["info"])
    else:
        _config_diagnostics.append(f"debug_root_type:{type(config_raw).__name__}")
        return False

    permissions_raw = config.get("permissions", config.get("permission"))
    agents_raw = config.get("agents", config.get("agent"))
    plugins = config.get("plugins", config.get("plugin"))
    if not isinstance(permissions_raw, (list, dict)) or not isinstance(agents_raw, dict):
        _config_diagnostics.append("missing_policy_fields:" + ",".join(sorted(config.keys())[:20]))
        return False
    permissions = cast(object, permissions_raw)
    if browser and permissions != [{"action": "*", "resource": "*", "effect": "deny"}]:
        _config_diagnostics.append("browser_global_tool_policy_not_deny_all")
        return False
    if not browser and not _tool_policy_is_safe(permissions):
        shape = _permission_summary(permissions)
        _config_diagnostics.append("global_tool_policy_not_effective:" + shape)
        return False
    model_config = config.get("model")
    mcp_config = config.get("mcp")
    small_model = config.get("small_model", config.get("smallModel"))
    expected_mcp: object = {
        "servers": {
            "toothless_browser": {
                "type": "local",
                "command": ["/home/toothless-agent/.local/share/toothless/browser-smoke/venv/bin/python", "/usr/local/libexec/toothless/browser_mcp_bridge.py"],
                "cwd": WORKSPACE,
                "disabled": False,
                "codemode": False,
                "timeout": {"startup": 20_000, "catalog": 20_000, "execution": 20_000},
            }
        }
    } if browser else {}
    if (
        not _approved_model(model_config)
        or (small_model is not None and not _approved_model(small_model))
        or config.get("update") != "disable"
        or plugins != []
        or mcp_config != expected_mcp
    ):
        if isinstance(model_config, dict):
            model_data = cast(dict[str, object], model_config)
            model_summary = f"{model_data.get('providerID')}/{model_data.get('model')}[{','.join(sorted(model_data))}]"
        else:
            model_summary = model_config if isinstance(model_config, str) else type(model_config).__name__
        if isinstance(plugins, list):
            plugin_count = len(cast(list[object], plugins))
            plugin_type = "list"
        elif isinstance(plugins, dict):
            plugin_count = len(cast(dict[str, object], plugins))
            plugin_type = "dict"
        else:
            plugin_count = "n/a"
            plugin_type = type(plugins).__name__
        plugin_summary = f"{plugin_type}:{plugin_count}"
        if mcp_config == {}:
            mcp_summary = "empty"
        elif isinstance(mcp_config, dict):
            mcp_summary = ",".join(sorted(cast(dict[str, object], mcp_config)))
        else:
            mcp_summary = type(mcp_config).__name__
        if isinstance(mcp_config, dict):
            mcp_servers = cast(dict[str, object], mcp_config).get("servers")
            if isinstance(mcp_servers, dict):
                summaries: list[str] = []
                for server_name, raw_server in cast(dict[str, object], mcp_servers).items():
                    if isinstance(raw_server, dict):
                        server = cast(dict[str, object], raw_server)
                        command = server.get("command")
                        command_items = cast(list[object], command) if isinstance(command, list) else []
                        command_value = "".join(cast(str, item) for item in command_items)[:160] if command_items and all(isinstance(item, str) for item in command_items) else type(cast(object, command)).__name__
                        safe_values = ",".join(f"{key}={server[key]}" for key in ("type", "cwd", "disabled", "codemode", "timeout") if key in server)
                        summaries.append(f"{server_name}:{','.join(sorted(server))}:cmd={command_value}:values={safe_values}")
                if summaries:
                    mcp_summary += ":" + ";".join(summaries)
        _config_diagnostics.append(f"model_or_extensions_mismatch:model={model_summary};plugins={plugin_summary};mcp={mcp_summary}")
        return False
    agents = cast(dict[str, object], agents_raw)
    agent_raw = agents.get(agent_name)
    if not isinstance(agent_raw, dict):
        _config_diagnostics.append("worker_agent_missing")
        return False
    agent = cast(dict[str, object], agent_raw)
    if not _approved_model(agent.get("model")):
        _config_diagnostics.append("agent_model_mismatch")
        return False
    agent_permissions = agent.get("permissions", agent.get("permission"))
    if browser:
        expected_browser_permissions = [
            {"action": "*", "resource": "*", "effect": "deny"},
            {"action": "external_directory", "resource": "*", "effect": "deny"},
            *(
                {"action": f"toothless_browser_{tool}", "resource": "*", "effect": "allow"}
                for tool in ("navigate", "extract_text", "click_link", "click_button", "screenshot", "download", "close_session")
            ),
        ]
        if agent_permissions != expected_browser_permissions:
            _config_diagnostics.append("browser_agent_tool_policy_mismatch:" + _permission_summary(agent_permissions))
            return False
        if permissions != [{"action": "*", "resource": "*", "effect": "deny"}]:
            _config_diagnostics.append("browser_global_tool_policy_not_deny_all")
            return False
    elif not _tool_policy_is_safe(agent_permissions):
        _config_diagnostics.append("agent_tool_policy_not_effective:" + _permission_summary(agent_permissions))
        return False
    expected_policies = [
        {"action": "provider.use", "resource": "*", "effect": "deny"},
        {"action": "provider.use", "resource": "opencode-go", "effect": "allow"},
        {"action": "mcp.use", "resource": "*", "effect": "deny"},
        *([{"action": "mcp.use", "resource": "toothless_browser", "effect": "allow"}] if browser else []),
        {"action": "plugin.use", "resource": "*", "effect": "deny"},
        {"action": "skill.use", "resource": "*", "effect": "deny"},
        {"action": "tool.use", "resource": "subagent:*", "effect": "deny"},
    ]
    if config.get("policies") != expected_policies:
        _config_diagnostics.append("provider_or_extension_policy_mismatch")
        return False
    return True


def _tool_policy_is_safe(value: object) -> bool:
    if not isinstance(value, list):
        return False
    rules: list[dict[str, object]] = []
    for item in cast(list[object], value):
        if not isinstance(item, dict):
            return False
        rules.append(cast(dict[str, object], item))
    if not rules or rules[0] != {"action": "*", "resource": "*", "effect": "deny"}:
        return False
    allowed_actions = {"read", "edit", "glob", "grep", "shell"}
    required = {
        ("external_directory", "*", "deny"),
        *((action, "*", "allow") for action in allowed_actions),
    }
    denied_shell = {
        "sudo *", "su *", "doas *", "apt *", "apt-get *", "dpkg *",
        "systemctl *", "reboot *", "shutdown *", "mount *", "umount *",
    }
    seen: set[tuple[str, str, str]] = set()
    for rule in rules[1:]:
        action, resource, effect = (rule.get(key) for key in ("action", "resource", "effect"))
        if not all(isinstance(item, str) for item in (action, resource, effect)):
            return False
        key = (cast(str, action), cast(str, resource), cast(str, effect))
        if key not in required and not (key[0] == "shell" and key[1] in denied_shell and key[2] == "deny"):
            return False
        seen.add(key)
    return required <= seen and all(("shell", command, "deny") in seen for command in denied_shell)


def _browser_network_properties() -> list[str]:
    raw = NETWORK_POLICY.read_bytes()
    if hashlib.sha256(raw).hexdigest() != NETWORK_POLICY_SHA256:
        raise ValueError("browser destination policy hash mismatch")
    value: object = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("browser destination policy is invalid")
    policy = cast(dict[str, object], value)
    allow = policy.get("allow")
    deny = policy.get("deny")
    if (
        set(policy) != {"allow", "deny"}
        or not isinstance(allow, list)
        or not isinstance(deny, list)
        or not allow
        or not deny
        or not all(isinstance(item, str) for item in cast(list[object], allow) + cast(list[object], deny))
    ):
        raise ValueError("browser destination policy schema is invalid")
    allowed = cast(list[str], allow)
    denied = cast(list[str], deny)
    if allowed != ["127.0.0.53/32", "127.0.0.54/32"] or "::/0" not in denied or "169.254.0.0/16" not in denied:
        raise ValueError("browser destination policy does not match the reviewed boundaries")
    return [
        "--property=IPAddressAllow=" + " ".join(allowed),
        "--property=IPAddressDeny=" + " ".join(denied),
    ]


def _browser_control_network_properties() -> list[str]:
    strict_raw = NETWORK_POLICY.read_bytes()
    control_raw = BROWSER_CONTROL_POLICY.read_bytes()
    if hashlib.sha256(control_raw).hexdigest() != BROWSER_CONTROL_POLICY_SHA256:
        raise ValueError("browser control policy hash mismatch")
    strict: object = json.loads(strict_raw)
    control: object = json.loads(control_raw)
    if not isinstance(strict, dict) or not isinstance(control, dict):
        raise ValueError("browser control policy is invalid")
    strict_policy = cast(dict[str, object], strict)
    policy = cast(dict[str, object], control)
    strict_allow = strict_policy.get("allow")
    allow = policy.get("allow")
    strict_deny = strict_policy.get("deny")
    deny = policy.get("deny")
    if (
        set(policy) != {"allow", "deny"}
        or allow != ["127.0.0.1/32", "127.0.0.53/32", "127.0.0.54/32"]
        or strict_allow != ["127.0.0.53/32", "127.0.0.54/32"]
        or deny != strict_deny
        or not isinstance(deny, list)
        or not all(isinstance(item, str) for item in cast(list[object], deny))
    ):
        raise ValueError("browser control policy changed more than the private CLI loopback exception")
    return [
        "--property=IPAddressAllow=" + " ".join(cast(list[str], allow)),
        "--property=IPAddressDeny=" + " ".join(cast(list[str], deny)),
    ]


def _stop_browser_helper() -> None:
    global _browser_helper_unit, _browser_helper_process, _browser_helper_directory, _browser_fatal_signal_path
    global _browser_source_records
    _close_visible_browser()
    if _browser_helper_unit is not None:
        try:
            subprocess.run(
                ["/usr/bin/systemctl", "stop", _browser_helper_unit],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=4,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    helper = _browser_helper_process
    if helper is not None:
        try:
            helper.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(helper.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            helper.wait()
    directory = _browser_helper_directory
    if directory is not None and directory.parent == Path("/run") and directory.name.startswith("toothless-browser-"):
        try:
            _browser_source_records.extend(
                _read_browser_source_manifest(directory, owner_uid=pwd.getpwnam(AGENT_USER).pw_uid)
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            pass
        shutil.rmtree(directory, ignore_errors=True)
    _browser_helper_unit = None
    _browser_helper_process = None
    _browser_helper_directory = None
    _browser_fatal_signal_path = None


def _read_browser_source_manifest(directory: Path, *, owner_uid: int) -> list[dict[str, object]]:
    source_path = directory / "sources.json"
    directory_info = directory.lstat()
    source_info = source_path.lstat()
    if (not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_uid != owner_uid
            or stat.S_IMODE(directory_info.st_mode) != 0o700 or not stat.S_ISREG(source_info.st_mode)
            or source_info.st_uid != owner_uid or stat.S_IMODE(source_info.st_mode) != 0o600
            or source_info.st_size > 16_384):
        return []
    raw: object = json.loads(source_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("sources"), list):
        return []
    result: list[dict[str, object]] = []
    for item in cast(list[object], raw["sources"])[-12:]:
        if not isinstance(item, dict):
            continue
        record = cast(dict[str, object], item)
        title, url, label, status, evidence = (
            record.get("title"), record.get("url"), record.get("label"), record.get("http_status"),
            record.get("verification_status"),
        )
        if (isinstance(title, str) and 0 < len(title) <= 180 and isinstance(url, str)
                and 0 < len(url) <= 1000 and url.startswith(("https://", "http://"))
                and isinstance(label, str) and len(label) <= 120
                and (status is None or type(status) is int and 100 <= status <= 599)
                and isinstance(evidence, str) and evidence in {"observed", "linked_unvisited"}):
            try:
                parts = urlsplit(url)
                host = parts.hostname
                if (parts.scheme not in {"http", "https"} or not host or parts.username or parts.password
                        or parts.port not in {None, 80, 443} or parts.query or parts.fragment):
                    continue
                host = host.encode("idna").decode("ascii").lower().rstrip(".")
                if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal", ".localhost")):
                    continue
                try:
                    address = ipaddress.ip_address(host)
                except ValueError:
                    address = None
                if address is not None and not address.is_global:
                    continue
            except (UnicodeError, ValueError):
                continue
            result.append({
                "title": title, "url": url, "label": label, "http_status": status,
                "verification_status": evidence,
            })
    return result


def _start_browser_helper(
    runtime_id: str, env: dict[str, str], *, task_id: str, execution_id: str, visible: bool
) -> str:
    global _browser_helper_unit, _browser_helper_process, _browser_helper_directory, _browser_fatal_signal_path
    _browser_network_properties()
    account = pwd.getpwnam(AGENT_USER)
    safe_id = re.sub(r"[^a-zA-Z0-9_.-]", "-", runtime_id)[:48]
    directory = Path(tempfile.mkdtemp(prefix=f"toothless-browser-{safe_id}-", dir="/run"))
    os.chown(directory, account.pw_uid, account.pw_gid)
    os.chmod(directory, 0o700)
    socket_path = directory / "mcp.sock"
    ready_path = directory / "ready"
    fatal_signal_path = directory / "fatal.json"
    unit = "toothless-browser-mcp-" + safe_id
    env["TOOTHLESS_BROWSER_FATAL_SIGNAL"] = str(fatal_signal_path)
    helper_env = {
        **env,
        "TOOTHLESS_BROWSER_TEXT_LIMIT_BYTES": str(BROWSER_TEXT_LIMIT_BYTES),
        "TOOTHLESS_BROWSER_SOCKET": str(socket_path),
        "TOOTHLESS_BROWSER_FATAL_SIGNAL": str(fatal_signal_path),
        "TOOTHLESS_BROWSER_SOURCE_MANIFEST": str(directory / "sources.json"),
        "TOOTHLESS_TASK_ID": task_id,
        "TOOTHLESS_EXECUTION_ID": execution_id,
        "TOOTHLESS_VISIBLE_BROWSER": "1" if visible else "0",
    }
    command = [
        "/usr/bin/systemd-run", "--scope", "--quiet", "--collect", f"--unit={unit}",
        "--property=MemoryMax=1536M", "--property=TasksMax=128", "--property=CPUQuota=200%",
        *_browser_network_properties(),
        "--", "/usr/sbin/runuser", "-u", AGENT_USER, "--", "/usr/bin/env",
        *(f"{key}={value}" for key, value in helper_env.items()),
        BROWSER_MCP_PYTHON, BROWSER_MCP, "--socket", str(socket_path), "--ready-file", str(ready_path),
    ]
    _browser_helper_unit = unit
    _browser_helper_directory = directory
    _browser_fatal_signal_path = fatal_signal_path
    try:
        helper_process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/sbin:/usr/bin:/bin", "LANG": "C.UTF-8"},
            start_new_session=True,
        )
        _browser_helper_process = helper_process
    except OSError:
        _stop_browser_helper()
        raise
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if ready_path.is_file() and socket_path.is_socket():
            return str(socket_path)
        if helper_process.poll() is not None:
            _stop_browser_helper()
            raise RuntimeError("browser helper scope exited before becoming ready")
        time.sleep(0.05)
    _stop_browser_helper()
    raise TimeoutError("browser helper scope did not become ready")


def _read_browser_fatal_signal(task_id: str, execution_id: str) -> dict[str, object] | None:
    path = _browser_fatal_signal_path
    if path is None:
        return None
    try:
        account = pwd.getpwnam(AGENT_USER)
        parent = path.parent.lstat()
        info = path.lstat()
        if (path.name != "fatal.json" or path.parent.parent != Path("/run")
                or not path.parent.name.startswith("toothless-browser-")
                or not stat.S_ISDIR(parent.st_mode)
                or parent.st_uid != account.pw_uid or stat.S_IMODE(parent.st_mode) != 0o700
                or not stat.S_ISREG(info.st_mode) or info.st_uid != account.pw_uid
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 16_384):
            return None
        value_raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (KeyError, OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value_raw, dict):
        return None
    value = cast(dict[str, object], value_raw)
    category = value.get("failure_category")
    if (value.get("version") != 1 or value.get("task_id") != task_id
            or value.get("execution_id") != execution_id
            or not isinstance(category, str) or category not in VISIBLE_BROWSER_FATAL_CATEGORIES):
        return None
    safe: dict[str, object] = {
        "task_id": task_id, "execution_id": execution_id, "failure_category": category,
    }
    _copy_browser_lifecycle(value, safe)
    return safe


def _fatal_browser_abort_result(
    raw: bytes, *, execution_id: str, task_id: str, signal_data: dict[str, object], elapsed: float,
) -> dict[str, Any]:
    parsed = _events(raw, 0, "", execution_id=execution_id)
    diagnostics_raw = parsed.get("diagnostics")
    diagnostics = cast(dict[str, object], diagnostics_raw) if isinstance(diagnostics_raw, dict) else _partial_event_metadata(raw, execution_id)
    diagnostics["failure_category"] = "fatal_browser_session"
    diagnostics["fatal_browser_session"] = {
        "task_id": task_id,
        "execution_id": execution_id,
        "failure_category": signal_data.get("failure_category", "browser_session_invalid"),
        "lifecycle": signal_data.get("lifecycle", []),
    }
    result: dict[str, Any] = {
        "status": "incomplete",
        "error": "Fatal browser-session failure; OpenCode was stopped and this execution was not retried.",
        "model": MODEL,
        "duration_seconds": round(elapsed, 3),
        "diagnostics": diagnostics,
    }
    text = parsed.get("text")
    if isinstance(text, str) and text.strip() and len(text.encode("utf-8")) <= MAX_RESULT_BYTES:
        result["text"] = text
    return result


def _approved_model(value: object) -> bool:
    if value == MODEL:
        return True
    if isinstance(value, dict):
        model = cast(dict[str, object], value)
        return model.get("providerID") == "opencode-go" and model.get("model") == "glm-5.3"
    return False


def _readiness_report() -> dict[str, str]:
    """Return sanitized runtime readiness without reading credential values."""
    env = {
        "HOME": AGENT_HOME, "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
        "XDG_CONFIG_HOME": CONFIG_HOME, "XDG_DATA_HOME": f"{AGENT_HOME}/.local/share",
        "XDG_STATE_HOME": f"{AGENT_HOME}/.local/state", "XDG_CACHE_HOME": f"{AGENT_HOME}/.cache",
        "OPENCODE_CONFIG": str(CONFIG),
    }
    helper = Path(__file__)
    try:
        helper_info = helper.stat()
        helper_ready = helper_info.st_uid == 0 and bool(helper_info.st_mode & 0o111)
    except OSError:
        helper_ready = False
    dns = "unavailable"
    try:
        resolver = subprocess.run(
            ["/usr/bin/resolvectl", "status", "enp0s1"], capture_output=True, text=True,
            timeout=5, check=False,
        )
        answer = subprocess.run(
            ["/usr/bin/getent", "ahostsv4", "opencode.ai"], capture_output=True, text=True,
            timeout=5, check=False,
        )
        first = answer.stdout.split(maxsplit=1)[0] if answer.stdout else ""
        import ipaddress
        if (resolver.returncode == 0 and ":55353" in resolver.stdout and answer.returncode == 0
                and ipaddress.IPv4Address(first).is_global):
            dns = "ready"
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    workspace = _verify_effective_config(env, browser=False)
    browser_env = dict(env, OPENCODE_CONFIG=str(CONFIG_BROWSER))
    browser = _verify_effective_config(browser_env, browser=True)
    return {
        "execution_agent": "ready" if helper_ready else "unavailable",
        "guest_dns": dns,
        "opencode_auth": "ready" if _has_active_opencode_go_credential() else "unavailable",
        "workspace_profile": "ready" if workspace else "unavailable",
        "browser_profile": "ready" if browser else "unavailable",
    }


def _task_timeout(request: dict[str, object], *, browser: bool) -> int:
    """Allow a longer fixed deadline only for host-marked attachment tasks."""
    file_task = request.get("file_task")
    requested_timeout = request.get("timeout_seconds")
    if browser:
        if file_task is not None or requested_timeout is not None:
            raise ValueError("browser task timeout is fixed")
        return BROWSER_TIMEOUT_SECONDS
    if file_task is True and requested_timeout == FILE_TASK_TIMEOUT_SECONDS:
        return FILE_TASK_TIMEOUT_SECONDS
    if file_task is None and requested_timeout is None:
        return TIMEOUT_SECONDS
    raise ValueError("workspace task timeout is not authorized")


def _permission_summary(value: object) -> str:
    if isinstance(value, dict):
        permission_map = cast(dict[str, object], value)
        return "map:" + ",".join(f"{key}={permission_map[key]}" for key in sorted(permission_map)[:10])
    if isinstance(value, list):
        rules = [cast(dict[str, object], item) for item in cast(list[object], value) if isinstance(item, dict)]
        return "rules:" + ";".join(
            ",".join(
                f"{key}={rule[key]}" for key in sorted(rule)
                if key in {"permission", "action", "tool", "pattern", "effect", "name", "resource"}
            ) + "[" + ",".join(sorted(rule)) + "]"
            for rule in rules[:10]
        )
    return type(value).__name__


def main() -> int:
    global _child, _scope_unit
    if os.geteuid() != 0:
        print("FAIL: fixed OpenCode entry point must be invoked through the root-owned worker helper.")
        return 1
    signal.signal(signal.SIGTERM, _kill_child)
    signal.signal(signal.SIGINT, _kill_child)
    if sys.argv[1:] == ["--readiness"]:
        _emit(_readiness_report())
        return 0
    if sys.argv[1:] in (["--verify-config"], ["--verify-browser-config"]):
        browser = sys.argv[1] == "--verify-browser-config"
        config_path = CONFIG_BROWSER if browser else CONFIG
        config_hash = CONFIG_BROWSER_SHA256 if browser else CONFIG_SHA256
        try:
            valid_hash = hashlib.sha256(config_path.read_bytes()).hexdigest() == config_hash
            if browser:
                _browser_network_properties()
                _browser_control_network_properties()
        except OSError:
            valid_hash = False
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            valid_hash = False
        env = {
            "HOME": AGENT_HOME,
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "XDG_CONFIG_HOME": CONFIG_HOME,
            "XDG_DATA_HOME": f"{AGENT_HOME}/.local/share",
            "XDG_STATE_HOME": f"{AGENT_HOME}/.local/state",
            "XDG_CACHE_HOME": f"{AGENT_HOME}/.cache",
            "OPENCODE_CONFIG": str(config_path),
        }
        if valid_hash and _verify_effective_config(env, browser=browser):
            print("PASS: OpenCode version, model, and " + ("browser-only helper-filtered" if browser else "bounded rootless") + " tool policy verified.")
            return 0
        print("FAIL: OpenCode verification: " + ",".join(_config_diagnostics or ["config_hash_mismatch"]))
        return 1
    if len(sys.argv) != 2 or len(sys.argv[1]) > MAX_REQUEST_BYTES * 2:
        return _emit({"status": "failed", "error": "Invalid bounded task request."})
    try:
        decoded = base64.b64decode(sys.argv[1], validate=True)
        if len(decoded) > MAX_REQUEST_BYTES:
            raise ValueError("request too large")
        request_raw: object = json.loads(decoded)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return _emit({"status": "failed", "error": "Invalid task request encoding."})
    if not isinstance(request_raw, dict):
        return _emit({"status": "failed", "error": "Task request does not match the approved model policy."})
    request = cast(dict[str, object], request_raw)
    prompt = request.get("prompt")
    profile = request.get("profile", "worker")
    if request.get("model") != MODEL or not isinstance(prompt, str) or profile not in {"worker", "browser"}:
        return _emit({"status": "failed", "error": "Task request does not match the approved model policy."})
    browser = profile == "browser"
    visible_browser = request.get("visible_browser", False)
    task_id = request.get("task_id", "")
    execution_id = request.get("execution_id", "")
    if (not isinstance(visible_browser, bool) or (visible_browser and not browser)
            or (browser and (not isinstance(task_id, str) or not re.fullmatch(r"task-[0-9a-f]{32}", task_id)
                             or not isinstance(execution_id, str) or not re.fullmatch(r"opencode-[0-9a-f]{32}", execution_id)))):
        return _emit({"status": "failed", "error": "Browser task authorization metadata is invalid."})
    try:
        timeout_seconds = _task_timeout(request, browser=browser)
    except ValueError:
        return _emit({"status": "failed", "error": "Task runtime did not match the authorized bounded profile."})
    config_path = CONFIG_BROWSER if browser else CONFIG
    config_hash = CONFIG_BROWSER_SHA256 if browser else CONFIG_SHA256
    agent_name = AGENT_BROWSER if browser else AGENT
    try:
        config_bytes = config_path.read_bytes()
    except OSError:
        config_bytes = b""
    if hashlib.sha256(config_bytes).hexdigest() != config_hash:
        return _emit({"status": "failed", "error": "The reviewed bounded OpenCode configuration is missing or changed."})
    if not _has_active_opencode_go_credential():
        return _emit({
            "status": "auth_required",
            "error": "OpenCode Go authentication is missing. Run bash ops/worker/opencode-auth-login.sh from the host and enter the key only at the Ubuntu guest prompt.",
        })

    command = [
        OPENCODE, "run", "--standalone", "--archived", "--format", "json",
        "--agent", agent_name, "--model", MODEL, prompt,
    ]
    env = {
        "HOME": AGENT_HOME,
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "XDG_CONFIG_HOME": CONFIG_HOME,
        "XDG_DATA_HOME": f"{AGENT_HOME}/.local/share",
        "XDG_STATE_HOME": f"{AGENT_HOME}/.local/state",
        "XDG_CACHE_HOME": f"{AGENT_HOME}/.cache",
        "OPENCODE_CONFIG": str(config_path),
    }
    if not _verify_effective_config(env, browser=browser):
        return _emit({"status": "failed", "error": "Effective OpenCode version or bounded tool policy failed verification."})
    started = time.monotonic()
    browser_runtime_id = uuid.uuid4().hex if browser else ""
    if browser:
        _browser_source_records.clear()
        try:
            if visible_browser:
                _authorize_visible_browser(cast(str, task_id), cast(str, execution_id))
            env["TOOTHLESS_BROWSER_SOCKET"] = _start_browser_helper(
                browser_runtime_id, env, task_id=cast(str, task_id),
                execution_id=cast(str, execution_id), visible=visible_browser,
            )
        except (OSError, RuntimeError, TimeoutError, ValueError):
            _stop_browser_helper()
            return _emit({"status": "failed", "error": "The isolated browser helper could not start."})
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            agent_command = ["/usr/sbin/runuser", "-u", AGENT_USER, "--", "/usr/bin/env", *[f"{key}={value}" for key, value in env.items()], *command]
            if browser:
                unit = "toothless-browser-run-" + browser_runtime_id
                _scope_unit = unit
                scope_command = [
                    "/usr/bin/systemd-run", "--scope", "--quiet", "--collect", f"--unit={unit}",
                    "--property=MemoryMax=1536M", "--property=TasksMax=128", "--property=CPUQuota=200%",
                    *_browser_control_network_properties(),
                    "--", *agent_command,
                ]
                spawn_command = scope_command
            else:
                spawn_command = agent_command
            _child = subprocess.Popen(
                spawn_command,
                cwd=WORKSPACE,
                env={"PATH": "/usr/sbin:/usr/bin:/bin", "LANG": "C.UTF-8"},
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
            )
        except OSError:
            _stop_browser_helper()
            return _emit({"status": "failed", "error": "OpenCode could not be started in the worker."})
        reason = ""
        fatal_signal: dict[str, object] | None = None
        browser_helper_lost = False
        while _child.poll() is None:
            if browser and visible_browser:
                fatal_signal = _read_browser_fatal_signal(cast(str, task_id), cast(str, execution_id))
                if fatal_signal is not None:
                    reason = "fatal_browser_session"
                    _kill_child(signal.SIGTERM, None)
                    break
                helper = _browser_helper_process
                if helper is not None and helper.poll() is not None:
                    browser_helper_lost = True
                    reason = "browser_helper_terminated"
                    _kill_child(signal.SIGTERM, None)
                    break
            if time.monotonic() - started >= timeout_seconds:
                reason = "timed_out"
                _kill_child(signal.SIGTERM, None)
                break
            if os.fstat(stdout_file.fileno()).st_size + os.fstat(stderr_file.fileno()).st_size > MAX_OUTPUT_BYTES:
                reason = "output_limited"
                _kill_child(signal.SIGTERM, None)
                break
            time.sleep(0.05)
        returncode = _child.wait()
        _child = None
        _scope_unit = None
        if browser and visible_browser and fatal_signal is None:
            fatal_signal = _read_browser_fatal_signal(cast(str, task_id), cast(str, execution_id))
        if browser:
            _stop_browser_helper()
        if fatal_signal is not None:
            stdout_file.seek(0)
            partial_raw = stdout_file.read(MAX_OUTPUT_BYTES + 1)
            return _emit(_fatal_browser_abort_result(
                partial_raw,
                execution_id=cast(str, execution_id),
                task_id=cast(str, task_id),
                signal_data=fatal_signal,
                elapsed=time.monotonic() - started,
            ))
        if browser_helper_lost:
            stdout_file.seek(0)
            partial_raw = stdout_file.read(MAX_OUTPUT_BYTES + 1)
            diagnostics = _partial_event_metadata(partial_raw, execution_id)
            diagnostics["failure_category"] = "browser_helper_terminated"
            return _emit({
                "status": "incomplete",
                "error": "The browser helper exited unexpectedly; OpenCode was stopped and this execution was not retried.",
                "model": MODEL,
                "duration_seconds": round(time.monotonic() - started, 3),
                "diagnostics": diagnostics,
            })
        if reason:
            stdout_file.seek(0)
            partial_raw = stdout_file.read(MAX_OUTPUT_BYTES + 1)
            return _emit({
                "status": reason,
                "error": f"OpenCode {reason.replace('_', ' ')}.",
                "model": MODEL,
                "duration_seconds": round(time.monotonic() - started, 3),
                **_partial_event_metadata(partial_raw, execution_id),
            })
        stdout_file.seek(0)
        raw = stdout_file.read(MAX_OUTPUT_BYTES + 1)
        stderr_file.seek(0)
        stderr = stderr_file.read(8192).decode("utf-8", errors="replace")
    answer = _events(raw, returncode, stderr, execution_id=execution_id)
    answer["model"] = MODEL
    answer["duration_seconds"] = round(time.monotonic() - started, 3)
    return _emit(answer)


def _emit(value: dict[str, Any]) -> int:
    if _browser_source_records and "sources" not in value:
        value["sources"] = _browser_source_records[:12]
    sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
