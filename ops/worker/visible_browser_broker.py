#!/usr/bin/env python3
"""Task-scoped Playwright broker inside the isolated desktop service namespace."""

from __future__ import annotations

import grp
import hashlib
import json
import logging
import os
import pwd
import re
import signal
import socket
import stat
import sys
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import browser_mcp

SOCKET_PATH = Path("/run/toothless-desktop/visible-browser.sock")
AUTH_ROOT = Path("/run/toothless-browser-authorizations")
CHROME = Path("/opt/toothless/visible-browser/chrome-linux-arm64/chrome")
RUNTIME = Path("/var/lib/toothless-desktop/visible-browser")
MAX_MESSAGE = 65_536
MAX_TOOL_RESULT_BYTES = 8192
TASK_ID = re.compile(r"task-[0-9a-f]{32}\Z")
EXECUTION_ID = re.compile(r"opencode-[0-9a-f]{32}\Z")
TOOL_NAMES = {"navigate", "extract_text", "click_link", "close_session"}
_stop = False
_active: tuple[str, str] | None = None
_invalidated: tuple[str, str] | None = None
_invocation_sequence = 0
_invocation_execution: str | None = None
_lifecycle_identity: tuple[str, str] | None = None
_lifecycle_started: float | None = None
_lifecycle_events: deque[dict[str, object]] = deque(maxlen=12)
_resource_baseline: dict[str, dict[str, int]] = {}
_last_browser_pid: int | None = None
_last_browser_process_alive: bool | None = None
_revoked: tuple[str, str] | None = None
_logger = logging.getLogger("toothless.visible_browser")

_BROWSER_FAILURE_CATEGORIES = {
    "browser_target_crashed", "browser_context_closed", "browser_startup_failed",
    "browser_runtime_conflict", "navigation_timeout", "browser_session_invalid",
    "browser_request_invalid", "browser_transport_disconnected", "authorization_revoked",
    "navigation_http_error", "browser_operation_failed",
}
_FATAL_BROWSER_CATEGORIES = {
    "authorization_revoked", "browser_context_closed", "browser_runtime_conflict",
    "browser_session_invalid", "browser_startup_failed", "browser_target_crashed",
    "browser_transport_disconnected",
}
_RECOVERABLE_BROWSER_CATEGORIES = {
    "browser_operation_failed", "browser_request_invalid", "navigation_http_error", "navigation_timeout",
}
_SAFE_EXCEPTION_CLASSES = {
    "Error", "TargetClosedError", "TimeoutError", "RuntimeError", "ValueError",
    "OSError", "BrowserError", "TargetCrashedError", "PlaywrightError", "NavigationHTTPError",
    "PermissionError", "FileNotFoundError",
}


class _BrowserOperationFailureError(Exception):
    def __init__(
        self,
        *,
        category: str,
        exception_class: str,
        browser_connected: bool | None,
        browser_process_alive: bool | None,
        fatal: bool | None = None,
        lifecycle: list[dict[str, object]] | None = None,
    ) -> None:
        super().__init__("Visible browser operation failed")
        self.category = category
        self.exception_class = exception_class
        self.browser_connected = browser_connected
        self.browser_process_alive = browser_process_alive
        self.fatal = category in _FATAL_BROWSER_CATEGORIES if fatal is None else fatal
        self.lifecycle = lifecycle or []


def _stop_signal(_signum: int, _frame: object) -> None:
    global _stop
    _stop = True


def _authorized(task_id: object, execution_id: object) -> tuple[str, str]:
    if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
        raise PermissionError("Visible browser task identity is invalid")
    if not isinstance(execution_id, str) or not EXECUTION_ID.fullmatch(execution_id):
        raise PermissionError("Visible browser execution identity is invalid")
    record = AUTH_ROOT / f"{execution_id}.json"
    metadata = record.lstat()
    browser_gid = grp.getgrnam("toothless-browser").gr_gid
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_gid != browser_gid
            or stat.S_IMODE(metadata.st_mode) != 0o640):
        raise PermissionError("Visible browser approval record is unavailable")
    value = json.loads(record.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise PermissionError("Visible browser approval record is invalid")
    auth = cast(dict[str, object], value)
    return _validate_authorization(auth, task_id, execution_id)


def _validate_authorization(
    auth: dict[str, object], task_id: str, execution_id: str, *, now: float | None = None
) -> tuple[str, str]:
    expires_at = auth.get("expires_at")
    if (auth.get("task_id") != task_id or auth.get("execution_id") != execution_id
            or auth.get("profile") != "browser" or auth.get("mode") != "visible"
            or not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool)
            or expires_at <= (time.time() if now is None else now)):
        raise PermissionError("Visible browser approval is expired or does not match")
    return task_id, execution_id


def _auth_root_is_valid(path: Path, *, owner_uid: int, group_gid: int) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == owner_uid
        and info.st_gid == group_gid
        and stat.S_IMODE(info.st_mode) == 0o750
    )


def _close_browser(reason: str = "broker_cleanup") -> None:
    global _active
    try:
        browser_mcp.close_browser_for_broker(reason)
    finally:
        _active = None


def _browser_connected() -> bool | None:
    browser = browser_mcp._browser
    if browser is None:
        return False
    try:
        return bool(browser.is_connected())
    except Exception:
        return None


def _playwright_driver() -> object | None:
    playwright = browser_mcp._playwright
    if playwright is None:
        return None
    for owner in (playwright, getattr(playwright, "_impl_obj", None)):
        if owner is None:
            continue
        for connection in (getattr(owner, "_connection", None),):
            transport = getattr(connection, "_transport", None)
            process = getattr(transport, "_proc", None) or getattr(transport, "_process", None)
            if process is not None and type(getattr(process, "pid", None)) is int:
                return process
    return None


def _read_int(path: Path) -> int | None:
    try:
        value = path.read_text(encoding="ascii").strip()
        return int(value) if value.isdigit() else None
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _read_event_counts(path: Path) -> dict[str, int]:
    try:
        lines = path.read_text(encoding="ascii").splitlines()[:16]
    except (OSError, UnicodeDecodeError):
        return {}
    result: dict[str, int] = {}
    for line in lines:
        fields = line.split()
        if len(fields) == 2 and fields[0] in {
            "low", "high", "max", "oom", "oom_kill", "oom_group_kill", "sock_throttled",
        } and fields[1].isdigit():
            result[fields[0]] = min(2**31 - 1, int(fields[1]))
    return result


def _read_fd_limit(path: Path) -> int | None:
    try:
        lines = path.read_text(encoding="ascii").splitlines()[:128]
    except (OSError, UnicodeDecodeError):
        return None
    for line in lines:
        if not line.startswith("Max open files"):
            continue
        fields = line.split()
        if len(fields) >= 4 and fields[3].isdigit():
            return min(2**31 - 1, int(fields[3]))
    return None


def _read_cpu_stats(path: Path) -> dict[str, int]:
    try:
        lines = path.read_text(encoding="ascii").splitlines()[:16]
    except (OSError, UnicodeDecodeError):
        return {}
    result: dict[str, int] = {}
    for line in lines:
        fields = line.split()
        if len(fields) == 2 and fields[0] in {"usage_usec", "nr_periods", "nr_throttled", "throttled_usec"} and fields[1].isdigit():
            result[fields[0]] = min(2**63 - 1, int(fields[1]))
    return result


def _cgroup_directory() -> Path | None:
    try:
        lines = Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines()[:32]
    except (OSError, UnicodeDecodeError):
        return None
    for line in lines:
        if not line.startswith("0::"):
            continue
        relative = line[3:].strip("/")
        parts = relative.split("/") if relative else []
        if any(part in {"", ".", ".."} for part in parts):
            return None
        candidate = Path("/sys/fs/cgroup").joinpath(*parts)
        return candidate if candidate.is_dir() else None
    return None


def _proc_details(pid: int, proc_root: Path = Path("/proc")) -> dict[str, object] | None:
    try:
        process_root = proc_root / str(pid)
        raw = process_root.joinpath("stat").read_text(encoding="ascii")
        end = raw.rfind(")")
        fields = raw[end + 2 :].split()
        if end < 0 or len(fields) < 2:
            return None
        state = fields[0]
        parent_pid = int(fields[1])
        exit_wait_status = int(fields[49]) if state == "Z" and len(fields) >= 50 else None
        comm = process_root.joinpath("comm").read_text(encoding="ascii").strip()
        try:
            status = process_root.joinpath("status").read_text(encoding="ascii")
            thread_line = next(line for line in status.splitlines() if line.startswith("Threads:"))
            thread_count = int(thread_line.split()[1])
        except (OSError, StopIteration, ValueError, IndexError):
            thread_count = None
        return {
            "pid": pid, "parent_pid": parent_pid, "state": state, "comm": comm[:32],
            "thread_count": thread_count, "exit_wait_status": exit_wait_status,
        }
    except (OSError, UnicodeDecodeError, ValueError, IndexError):
        return None


def _cgroup_process_ids(cgroup: Path) -> list[int]:
    """Read bounded process IDs from this service cgroup and nested scopes."""
    pending = [cgroup]
    seen_dirs: set[Path] = set()
    process_ids: set[int] = set()
    while pending and len(seen_dirs) < 32 and len(process_ids) < 1024:
        current = pending.pop()
        try:
            resolved = current.resolve(strict=True)
            if not resolved.is_relative_to(cgroup.resolve(strict=True)) or resolved in seen_dirs:
                continue
            seen_dirs.add(resolved)
            raw = resolved.joinpath("cgroup.procs").read_text(encoding="ascii").splitlines()[:1024]
            process_ids.update(int(value) for value in raw if value.isdigit())
            pending.extend(child for child in resolved.iterdir() if child.is_dir())
        except (OSError, UnicodeDecodeError, ValueError):
            continue
    return sorted(process_ids)[:1024]


def _cgroup_parent_limits(cgroup: Path | None) -> list[dict[str, int | str]] | None:
    """Return only bounded PID counters for the service and its ancestors."""
    if cgroup is None:
        return None
    result: list[dict[str, int | str]] = []
    current = cgroup
    for depth in range(4):
        try:
            resolved = current.resolve(strict=True)
            item: dict[str, int | str] = {"level": "service" if depth == 0 else f"parent_{depth}"}
            current_count = _read_int(resolved / "pids.current")
            maximum = _read_int(resolved / "pids.max")
            if current_count is not None:
                item["pids_current"] = current_count
            if maximum is not None:
                item["pids_max"] = maximum
            result.append(item)
            if resolved.parent == resolved:
                break
            current = resolved.parent
        except OSError:
            break
    return result


def _process_cgroup_scope(pid: int, service_cgroup: Path) -> str | None:
    """Return only a process's validated path relative to this desktop service."""
    try:
        lines = Path(f"/proc/{pid}/cgroup").read_text(encoding="ascii").splitlines()[:32]
        line = next(item for item in lines if item.startswith("0::"))
        parts = line[3:].strip("/").split("/")
        if any(part in {"", ".", ".."} for part in parts):
            return None
        candidate = Path("/sys/fs/cgroup").joinpath(*parts).resolve(strict=True)
        root = service_cgroup.resolve(strict=True)
        if not candidate.is_relative_to(root):
            return None
        relative = candidate.relative_to(root).as_posix()
        return relative if relative != "." else "service"
    except (OSError, StopIteration, UnicodeDecodeError, ValueError):
        return None


def _is_descendant(pid: int, ancestor_pid: int, processes_by_pid: dict[int, dict[str, object]]) -> bool:
    visited: set[int] = set()
    current = pid
    while current not in visited and current in processes_by_pid:
        visited.add(current)
        parent = processes_by_pid[current].get("parent_pid")
        if type(parent) is not int:
            return False
        if parent == ancestor_pid:
            return True
        current = parent
    return False


def _process_role_counts(
    processes: list[dict[str, object]], *, driver_pid: int | None, browser_pid: int | None
) -> dict[str, dict[str, int]]:
    """Summarize known service roles without logging command lines or environments."""
    by_pid = {cast(int, item["pid"]): item for item in processes if type(item.get("pid")) is int}
    roles = {name: {"processes": 0, "threads": 0} for name in (
        "xvfb", "openbox", "xterm", "broker", "playwright_driver", "chrome_browser",
        "chrome_renderer_or_utility", "other",
    )}
    for item in processes:
        pid = item.get("pid")
        comm = item.get("comm")
        if type(pid) is not int or not isinstance(comm, str):
            continue
        if pid == os.getpid():
            role = "broker"
        elif pid == driver_pid:
            role = "playwright_driver"
        elif pid == browser_pid:
            role = "chrome_browser"
        elif comm.lower() in {"xvfb", "xvfb-99"}:
            role = "xvfb"
        elif comm.lower() == "openbox":
            role = "openbox"
        elif comm.lower() == "xterm":
            role = "xterm"
        elif comm.lower() in {"chrome", "chromium", "chrome-headless-shell", "chromium-browse"} and (
            (driver_pid is not None and _is_descendant(pid, driver_pid, by_pid))
            or (browser_pid is not None and _is_descendant(pid, browser_pid, by_pid))
        ):
            role = "chrome_renderer_or_utility"
        else:
            role = "other"
        roles[role]["processes"] += 1
        threads = item.get("thread_count")
        if type(threads) is int:
            roles[role]["threads"] += threads
    return roles


def _exit_status(wait_status: object) -> tuple[int | None, int | None]:
    if type(wait_status) is not int or wait_status < 0:
        return None, None
    if os.WIFSIGNALED(wait_status):
        return None, os.WTERMSIG(wait_status)
    if os.WIFEXITED(wait_status):
        return os.WEXITSTATUS(wait_status), None
    return None, None


def _resource_snapshot(*, include_process_inventory: bool = False) -> dict[str, object]:
    cgroup = _cgroup_directory()
    pids = _cgroup_process_ids(cgroup) if cgroup is not None else []
    processes = [detail for pid in pids if (detail := _proc_details(pid)) is not None]
    driver = _playwright_driver()
    driver_pid: int | None = None
    driver_exit_code: int | None = None
    driver_alive: bool | None = None
    if driver is not None:
        value = getattr(driver, "pid", None)
        if type(value) is int and 0 < value <= 2**31 - 1:
            driver_pid = value
        exit_value = getattr(driver, "returncode", None)
        if type(exit_value) is int and -255 <= exit_value <= 255:
            driver_exit_code = exit_value
        if driver_pid is not None:
            try:
                os.kill(driver_pid, 0)
                driver_alive = True
            except ProcessLookupError:
                driver_alive = False
            except PermissionError:
                driver_alive = True
    chrome = [item for item in processes if item.get("comm", "").lower() in {"chrome", "chromium", "chrome-headless-shell", "chromium-browse"}]
    processes_by_pid = {cast(int, p["pid"]): p for p in processes if type(p.get("pid")) is int}
    chrome = [
        item for item in chrome
        if driver_pid is not None and type(item.get("pid")) is int
        and _is_descendant(cast(int, item["pid"]), driver_pid, processes_by_pid)
    ]
    roots = [
        item for item in chrome
        if not any(
            p.get("comm", "").lower() in {"chrome", "chromium", "chrome-headless-shell", "chromium-browse"}
            and p.get("pid") != item.get("pid")
            and type(p.get("pid")) is int
            and _is_descendant(cast(int, item["pid"]), cast(int, p["pid"]), processes_by_pid)
            for p in chrome
        )
    ]
    browser_item = roots[0] if len(roots) == 1 else None
    browser_pid = cast(int, browser_item["pid"]) if browser_item is not None else None
    chrome_pids = [cast(int, item["pid"]) for item in chrome if type(item.get("pid")) is int]
    browser_alive: bool | None = bool(chrome_pids) if cgroup is not None and driver_pid is not None else None
    exit_code, exit_signal = _exit_status(browser_item.get("exit_wait_status")) if browser_item else (None, None)
    fd_count: int | None
    try:
        fd_count = len(list(Path("/proc/self/fd").iterdir()))
    except OSError:
        fd_count = None
    browser_fd_count: int | None = None
    browser_fd_limit: int | None = None
    browser_thread_count = cast(int | None, browser_item.get("thread_count")) if browser_item else None
    if browser_pid is not None:
        try:
            browser_fd_count = len(list(Path(f"/proc/{browser_pid}/fd").iterdir()))
        except OSError:
            pass
        browser_fd_limit = _read_fd_limit(Path(f"/proc/{browser_pid}/limits"))
    memory_events = _read_event_counts(cgroup / "memory.events") if cgroup else {}
    pids_events = _read_event_counts(cgroup / "pids.events") if cgroup else {}
    cpu_stats = _read_cpu_stats(cgroup / "cpu.stat") if cgroup else {}
    snapshot: dict[str, object] = {
        "process_count": len(pids) if cgroup is not None else None,
        "thread_count": sum(cast(int, item["thread_count"]) for item in processes if type(item.get("thread_count")) is int) if cgroup is not None else None,
        "fd_count": fd_count,
        "fd_limit": _read_fd_limit(Path("/proc/self/limits")),
        "browser_fd_count": browser_fd_count,
        "browser_fd_limit": browser_fd_limit,
        "browser_thread_count": browser_thread_count,
        "cgroup_pids_current": _read_int(cgroup / "pids.current") if cgroup else None,
        "cgroup_pids_max": _read_int(cgroup / "pids.max") if cgroup else None,
        "cgroup_pid_ancestors": _cgroup_parent_limits(cgroup),
        "cgroup_memory_current_bytes": _read_int(cgroup / "memory.current") if cgroup else None,
        "cgroup_memory_max_bytes": _read_int(cgroup / "memory.max") if cgroup else None,
        "memory_events": memory_events,
        "pids_events": pids_events,
        "cpu_stats": cpu_stats,
        "browser_pid": browser_pid,
        "browser_process_pids": chrome_pids[:8],
        "browser_process_alive": browser_alive,
        "browser_exit_code": exit_code,
        "browser_exit_signal": exit_signal,
        "playwright_driver_pid": driver_pid,
        "playwright_driver_exit_code": driver_exit_code,
        "playwright_driver_alive": driver_alive,
        "process_roles": _process_role_counts(processes, driver_pid=driver_pid, browser_pid=browser_pid),
    }
    if include_process_inventory and cgroup is not None:
        inventory: list[dict[str, object]] = []
        for process in processes[:16]:
            pid = process.get("pid")
            parent_pid = process.get("parent_pid")
            comm = process.get("comm")
            threads = process.get("thread_count")
            if type(pid) is not int or type(parent_pid) is not int or not isinstance(comm, str):
                continue
            safe_comm = re.sub(r"[^A-Za-z0-9_.-]", "_", comm[:32]) or "unknown"
            item: dict[str, object] = {"pid": pid, "parent_pid": parent_pid, "comm": safe_comm}
            if type(threads) is int and 0 <= threads <= 2**16:
                item["thread_count"] = threads
            scope = _process_cgroup_scope(pid, cgroup)
            if scope is not None:
                item["cgroup_scope"] = scope[:128]
            inventory.append(item)
        snapshot["process_inventory"] = inventory
    return snapshot


def _context_open() -> bool | None:
    context = browser_mcp._context
    if context is None:
        return False
    try:
        _ = context.pages
        return True
    except Exception:
        return None


def _page_open() -> bool | None:
    page = browser_mcp._page
    if page is None:
        return False
    try:
        return not bool(page.is_closed())
    except Exception:
        return None


def _record_lifecycle(event: str) -> None:
    global _last_browser_pid, _last_browser_process_alive
    if event not in browser_mcp._LIFECYCLE_EVENTS:
        return
    resources = _resource_snapshot(include_process_inventory=event in {
        "browser_startup_failed", "browser_disconnected",
    })
    if event == "browser_starting":
        _resource_baseline["memory_events"] = cast(dict[str, int], resources["memory_events"])
        _resource_baseline["pids_events"] = cast(dict[str, int], resources["pids_events"])
    memory_delta: dict[str, int] = {}
    pids_delta: dict[str, int] = {}
    for key, target in (("memory_events", memory_delta), ("pids_events", pids_delta)):
        current = cast(dict[str, int], resources[key])
        baseline = _resource_baseline.get(key, {})
        target.update({name: max(0, count - baseline.get(name, count)) for name, count in current.items()})
    browser_connected = _browser_connected()
    context_open = _context_open()
    page_open = _page_open()
    cleanup_reason = browser_mcp._cleanup_reason
    if event in {"cleanup_start", "cleanup_complete", "cleanup_failed"} or (
        event in {"browser_disconnected", "browser_context_closed", "browser_page_closed"} and cleanup_reason
    ):
        termination = "application_close"
    elif resources.get("browser_exit_signal") is not None:
        termination = "signal_exit"
    elif resources.get("browser_exit_code") is not None:
        termination = "normal_exit"
    elif resources.get("browser_process_alive") is False and _last_browser_process_alive is True:
        termination = "process_disappeared"
    elif browser_connected is False and event == "browser_disconnected":
        termination = "transport_disconnect"
    elif browser_connected is False or context_open is False or page_open is False:
        termination = "unknown_exit"
    else:
        termination = "unknown"
    if resources.get("browser_pid") is not None:
        _last_browser_pid = cast(int, resources["browser_pid"])
    if isinstance(resources.get("browser_process_alive"), bool):
        _last_browser_process_alive = cast(bool, resources["browser_process_alive"])
    elapsed_ms = min(300_000, max(0, int((time.monotonic() - (_lifecycle_started or time.monotonic())) * 1000)))
    state = (
        "active" if browser_connected is True and context_open is True and page_open is True
        else "disconnected" if browser_connected is False
        else "closed" if event in {"cleanup_complete", "browser_context_closed", "browser_page_closed"}
        else "launching" if event in {"browser_starting", "browser_process_created", "browser_context_created"}
        else "unknown"
    )
    entry: dict[str, object] = {
        "event": event,
        "tool_invocation_sequence": _invocation_sequence,
        "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "elapsed_ms": elapsed_ms,
        "session_state": state,
        "browser_connected": browser_connected,
        "context_open": context_open,
        "page_open": page_open,
        "browser_termination": termination,
        "cleanup_reason": browser_mcp._cleanup_reason,
        **{key: value for key, value in resources.items() if key not in {"memory_events", "pids_events", "browser_process_pids"}},
        "memory_events_delta": memory_delta,
        "pids_events_delta": pids_delta,
    }
    cpu_stats = resources.get("cpu_stats")
    if isinstance(cpu_stats, dict):
        entry["cpu_stats"] = cpu_stats
    if event == "browser_disconnected" and _last_browser_pid is not None and resources.get("browser_pid") is None:
        entry["browser_pid"] = _last_browser_pid
    _lifecycle_events.append(entry)
    if _lifecycle_started is not None:
        _logger.info("lifecycle %s", json.dumps(entry, separators=(",", ":")))


def _lifecycle_snapshot() -> list[dict[str, object]]:
    return [dict(item) for item in list(_lifecycle_events)[-8:]]


def _failure_category(
    exc: Exception,
    *,
    browser_connected: bool | None,
    context_open: bool | None = None,
    page_open: bool | None = None,
) -> str:
    name = type(exc).__name__
    message = str(exc).lower()
    if name in {"TargetCrashedError"} or "target crashed" in message:
        return "browser_target_crashed"
    if name == "TargetClosedError":
        return "browser_context_closed"
    if "sync api inside asyncio loop" in message or "asyncio loop" in message and "sync" in message:
        return "browser_runtime_conflict"
    if name == "NavigationHTTPError":
        return "navigation_http_error"
    if name in {"ValueError", "PermissionError"}:
        return "browser_request_invalid"
    if "navigate to a page first" in message:
        return "browser_request_invalid"
    if name == "TimeoutError":
        if browser_connected is False:
            return "browser_transport_disconnected"
        if context_open is False or page_open is False:
            return "browser_context_closed"
        return "navigation_timeout"
    if browser_connected is False:
        return "browser_startup_failed" if browser_mcp._browser is None else "browser_transport_disconnected"
    if context_open is False or page_open is False:
        return "browser_context_closed"
    if name == "RuntimeError" and "browser_runtime_conflict" in message:
        return "browser_runtime_conflict"
    if browser_connected is True and context_open is True and page_open is True:
        return "browser_operation_failed"
    return "browser_session_invalid"


def _dispatch(request: dict[str, object]) -> str:
    global _active, _invalidated, _revoked, _lifecycle_identity, _lifecycle_started
    task_id = request.get("task_id")
    execution_id = request.get("execution_id")
    try:
        identity = _authorized(task_id, execution_id)
    except (OSError, ValueError, PermissionError, json.JSONDecodeError) as exc:
        supplied_identity = (task_id, execution_id) if isinstance(task_id, str) and isinstance(execution_id, str) else None
        if supplied_identity is not None and supplied_identity == _active:
            _revoked = cast(tuple[str, str], supplied_identity)
            _invalidated = _revoked
            _record_lifecycle("tool_failure")
            resources = _resource_snapshot()
            process_alive_value = resources.get("browser_process_alive")
            _close_browser("authorization_revoked")
            raise _BrowserOperationFailureError(
                category="authorization_revoked",
                exception_class=type(exc).__name__ if type(exc).__name__ in _SAFE_EXCEPTION_CLASSES else "Other",
                browser_connected=_browser_connected(),
                browser_process_alive=process_alive_value if isinstance(process_alive_value, bool) else None,
                fatal=True,
                lifecycle=_lifecycle_snapshot(),
            ) from None
        raise
    if _lifecycle_identity != identity:
        _lifecycle_identity = identity
        _lifecycle_started = time.monotonic()
        _lifecycle_events.clear()
        _resource_baseline.clear()
        _last_browser_pid = None
        _last_browser_process_alive = None
    if _invalidated is not None and identity != _invalidated:
        _invalidated = None
    if _revoked is not None and identity != _revoked:
        _revoked = None
    if identity == _invalidated:
        resources = _resource_snapshot()
        process_alive_value = resources.get("browser_process_alive")
        raise _BrowserOperationFailureError(
            category="browser_session_invalid", exception_class="RuntimeError",
            browser_connected=_browser_connected(),
            browser_process_alive=process_alive_value if isinstance(process_alive_value, bool) else None,
            fatal=True, lifecycle=_lifecycle_snapshot(),
        )
    if _active is not None and identity != _active:
        raise PermissionError("Another approved visible-browser execution owns the desktop")
    name = request.get("name")
    arguments = request.get("arguments", {})
    if not isinstance(name, str) or name not in TOOL_NAMES or not isinstance(arguments, dict):
        raise ValueError("Visible browser operation is not allowed")
    args = cast(dict[str, object], arguments)
    if name == "close_session":
        try:
            _close_browser("explicit_close")
        except Exception:
            _invalidated = identity
            raise
        return "Visible browser session closed."
    if name not in browser_mcp.TOOLS:
        raise ValueError("Visible browser tool is unavailable")
    _active = identity
    try:
        result = str(browser_mcp.TOOLS[name][2](args))
    except Exception as exc:
        connected = _browser_connected()
        context_open = _context_open()
        page_open = _page_open()
        resource_state = _resource_snapshot()
        process_alive_raw = resource_state.get("browser_process_alive")
        process_alive = process_alive_raw if isinstance(process_alive_raw, bool) else None
        category = _failure_category(
            exc, browser_connected=connected, context_open=context_open, page_open=page_open
        )
        exception_class = type(exc).__name__ if type(exc).__name__ in _SAFE_EXCEPTION_CLASSES else "Other"
        fatal = category in _FATAL_BROWSER_CATEGORIES
        _record_lifecycle("tool_failure")
        if fatal:
            _invalidated = identity
            try:
                _close_browser("fatal_failure")
            except Exception:
                pass
        raise _BrowserOperationFailureError(
            category=category,
            exception_class=exception_class,
            browser_connected=connected,
            browser_process_alive=process_alive,
            fatal=fatal,
            lifecycle=_lifecycle_snapshot(),
        ) from None
    if len(result.encode("utf-8")) > MAX_TOOL_RESULT_BYTES:
        raise ValueError("Visible browser result exceeded its bounded tool-result limit")
    return result


def _response(connection: socket.socket, request: object) -> None:
    global _invocation_execution, _invocation_sequence
    if not isinstance(request, dict):
        payload = {"ok": False, "error": "Invalid visible browser request"}
    else:
        started = time.monotonic()
        request_data = cast(dict[str, object], request)
        name = request_data.get("name")
        execution_id = request_data.get("execution_id")
        sequence = 0
        if isinstance(name, str) and name in TOOL_NAMES and name != "close_session":
            if isinstance(execution_id, str) and execution_id != _invocation_execution:
                _invocation_execution = execution_id
                _invocation_sequence = 0
            _invocation_sequence += 1
            sequence = _invocation_sequence
        try:
            payload = {"ok": True, "result": _dispatch(request_data)}
        except _BrowserOperationFailureError as exc:
            diag: dict[str, object] = {
                "failure_category": exc.category,
                "tool_name": name if isinstance(name, str) and name in TOOL_NAMES else "unknown",
                "invocation_sequence": sequence,
                "session_state": "invalidated" if exc.fatal else "active",
                "browser_connected": exc.browser_connected,
                "browser_process_alive": exc.browser_process_alive,
                "exception_class": exc.exception_class,
                "elapsed_ms": min(120_000, max(0, int((time.monotonic() - started) * 1000))),
                "fatal_signal": exc.fatal,
                "lifecycle": exc.lifecycle,
            }
            task_id = request_data.get("task_id")
            if isinstance(task_id, str) and TASK_ID.fullmatch(task_id):
                diag["task_id"] = task_id
            if isinstance(execution_id, str) and EXECUTION_ID.fullmatch(execution_id):
                diag["execution_id"] = execution_id
            payload = {
                "ok": False,
                "error": (
                    "The visible browser session is invalidated; this execution must stop."
                    if exc.fatal else "The visible browser operation failed; the session may still be usable."
                ),
                "diagnostic": diag,
            }
        except Exception as exc:
            if isinstance(exc, PermissionError | FileNotFoundError):
                category = "browser_session_invalid"
                message = "Visible browser approval is missing, expired, or does not match this task."
            else:
                category = _failure_category(
                    exc, browser_connected=_browser_connected(), context_open=_context_open(), page_open=_page_open()
                )
                message = "The visible browser operation failed; the session may still be usable."
            diag: dict[str, object] = {
                "failure_category": category,
                "tool_name": name if isinstance(name, str) and name in TOOL_NAMES else "unknown",
                "invocation_sequence": sequence,
                "session_state": "invalidated" if isinstance(execution_id, str) and _invalidated == (request_data.get("task_id"), execution_id) else "unchanged",
                "browser_connected": _browser_connected(),
                "browser_process_alive": _resource_snapshot().get("browser_process_alive"),
                "exception_class": type(exc).__name__ if type(exc).__name__ in _SAFE_EXCEPTION_CLASSES else "Other",
                "elapsed_ms": min(120_000, max(0, int((time.monotonic() - started) * 1000))),
                "fatal_signal": False,
            }
            task_id = request_data.get("task_id")
            if isinstance(task_id, str) and TASK_ID.fullmatch(task_id):
                diag["task_id"] = task_id
            if isinstance(execution_id, str) and EXECUTION_ID.fullmatch(execution_id):
                diag["execution_id"] = execution_id
            if _lifecycle_events:
                diag["lifecycle"] = _lifecycle_snapshot()
            payload = {"ok": False, "error": message, "diagnostic": diag}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_MESSAGE:
        raw = json.dumps(
            {"ok": False, "error": "Visible browser response exceeded its bounded message limit."},
            separators=(",", ":"),
        ).encode("utf-8")
    connection.sendall(raw + b"\n")


def main() -> int:
    global _stop, _invalidated, _revoked
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    account = pwd.getpwnam("toothless-desktop")
    group = grp.getgrnam("toothless-browser")
    if os.geteuid() != account.pw_uid or os.getegid() != group.gr_gid:
        print("Visible browser broker must run as toothless-desktop", file=sys.stderr)
        return 1
    if os.environ.get("DISPLAY") != ":99" or os.environ.get("XAUTHORITY") != "/run/toothless-desktop/Xauthority":
        print("Visible browser display environment is unavailable", file=sys.stderr)
        return 1
    browser_mcp.CHROMIUM = CHROME
    os.environ["TOOTHLESS_BROWSER_HEADLESS"] = "0"
    os.environ["HOME"] = "/var/lib/toothless-desktop"
    browser_mcp.set_lifecycle_observer(_record_lifecycle)
    try:
        manifest_path = CHROME.parent.parent / "INSTALLATION.json"
        manifest_info = manifest_path.lstat()
        manifest_raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest_raw, dict):
            return 1
        manifest = cast(dict[str, object], manifest_raw)
        if (manifest_info.st_uid != 0 or stat.S_IMODE(manifest_info.st_mode) & 0o022
                or manifest.get("version") != "159.0.8154.6"
                or manifest.get("browser") != "Chrome for Testing"):
            return 1
        expected_hash = manifest.get("chrome_binary_sha256")
        if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            return 1
        with CHROME.open("rb") as browser_file:
            if hashlib.file_digest(browser_file, "sha256").hexdigest() != expected_hash:
                return 1
        browser_mcp.CHROMIUM_SHA256 = expected_hash
    except (OSError, ValueError, json.JSONDecodeError):
        return 1
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    if RUNTIME.stat().st_uid != account.pw_uid or stat.S_IMODE(RUNTIME.stat().st_mode) & 0o077:
        return 1
    if not _auth_root_is_valid(AUTH_ROOT, owner_uid=0, group_gid=group.gr_gid):
        return 1
    if SOCKET_PATH.exists() or SOCKET_PATH.is_symlink():
        return 1
    signal.signal(signal.SIGTERM, _stop_signal)
    signal.signal(signal.SIGINT, _stop_signal)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(SOCKET_PATH))
        os.chown(SOCKET_PATH, account.pw_uid, group.gr_gid)
        os.chmod(SOCKET_PATH, 0o660)
        server.listen(4)
        server.settimeout(1.0)
        while not _stop:
            if _active is not None:
                active_identity = _active
                try:
                    _authorized(*active_identity)
                except (OSError, ValueError, PermissionError, json.JSONDecodeError):
                    _revoked = active_identity
                    _invalidated = active_identity
                    _record_lifecycle("tool_failure")
                    _close_browser("authorization_revoked")
            try:
                connection, _ = server.accept()
            except TimeoutError:
                continue
            with connection:
                connection.settimeout(35)
                with connection.makefile("rb") as reader:
                    raw = reader.readline(MAX_MESSAGE + 1)
                if not raw or len(raw) > MAX_MESSAGE:
                    continue
                try:
                    request = json.loads(raw)
                except json.JSONDecodeError:
                    request = None
                _response(connection, request)
    _close_browser("service_shutdown")
    SOCKET_PATH.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
