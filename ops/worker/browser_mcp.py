#!/usr/bin/env python3
"""Bounded stdio MCP server for the guest-local Playwright browser."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import ipaddress
import json
import os
import re
import socket
import stat
import sys
import threading
import urllib.request
import uuid
from pathlib import Path
from typing import Any, BinaryIO, Callable, cast
from urllib.parse import urljoin, urlsplit, urlunsplit

CHROMIUM = Path(
    "/home/toothless-agent/.local/share/toothless/browser-smoke/chromium/"
    "chrome-headless-shell-linux-arm64/chrome-headless-shell"
)
WORKSPACE = Path("/home/toothless-agent/workspace").resolve()
ARTIFACTS = WORKSPACE / "browser-artifacts"
MAX_MESSAGE = 65_536
DEFAULT_MAX_PAGE_TEXT_BYTES = 8192
MIN_MAX_PAGE_TEXT_BYTES = 4096
MAX_MAX_PAGE_TEXT_BYTES = 16_384
MAX_PAGE_TEXT_SOURCE_CHARS = 32_000
MAX_TOOL_RESULT_BYTES = 8192
MAX_DOWNLOAD = 10 * 1024 * 1024
MAX_SCREENSHOT = 2 * 1024 * 1024
PAGE_TIMEOUT_MS = 20_000
_page: Any = None
_browser: Any = None
_context: Any = None
_playwright: Any = None
_runtime_thread_id: int | None = None
_observed_sources: list[dict[str, object]] = []
_last_response_status: int | None = None
_lifecycle_observer: Callable[[str], None] | None = None
_cleanup_reason = ""
_fatal_signal_write_failed = False
PLAYWRIGHT_VERSION = "1.65.2"
CHROMIUM_SHA256 = "3e818d32e2fe5749b54c65179d282d14d0af39010ed0fbdd24122af1aca22046"
_VISIBLE_FAILURE_CATEGORIES = {
    "browser_target_crashed", "browser_context_closed", "browser_startup_failed",
    "browser_runtime_conflict", "navigation_timeout", "browser_session_invalid",
    "browser_request_invalid", "browser_transport_disconnected", "authorization_revoked",
    "navigation_http_error", "browser_operation_failed",
}
_VISIBLE_EXCEPTION_CLASSES = {
    "Error", "TargetClosedError", "TimeoutError", "RuntimeError", "ValueError",
    "OSError", "BrowserError", "TargetCrashedError", "PlaywrightError", "Other",
}
_FATAL_BROWSER_CATEGORIES = {
    "authorization_revoked", "browser_context_closed", "browser_runtime_conflict",
    "browser_session_invalid", "browser_startup_failed", "browser_target_crashed",
    "browser_transport_disconnected",
}
_LIFECYCLE_EVENTS = {
    "browser_starting", "browser_process_created", "browser_context_created",
    "browser_page_created", "before_navigation", "after_navigation",
    "before_extraction", "after_extraction", "browser_disconnected",
    "browser_context_closed", "browser_page_closed", "browser_startup_failed",
    "tool_failure", "cleanup_start", "cleanup_complete", "cleanup_failed",
}


class NavigationHTTPError(RuntimeError):
    """A completed HTTP response failed the ordinary navigation success check."""


class VisibleBrowserDiagnosticError(RuntimeError):
    def __init__(self, diagnostic: dict[str, object]) -> None:
        super().__init__("Visible browser operation failed")
        self.diagnostic = diagnostic


def _public_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 4096 or len(value.encode("utf-8")) > 4096:
        raise ValueError("URL must be a string of at most 4096 characters")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only public HTTP and HTTPS URLs without credentials are allowed")
    host = parsed.hostname.rstrip(".").lower()
    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal]
    except ValueError:
        addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM):
            sockaddr = cast(tuple[object, ...], item[4])
            address_text = sockaddr[0] if sockaddr else None
            if isinstance(address_text, str):
                addresses.append(ipaddress.ip_address(address_text.split("%", 1)[0]))
    ipv4_addresses = [item for item in addresses if item.version == 4]
    if not ipv4_addresses or any(not item.is_global for item in ipv4_addresses):
        raise ValueError("The URL resolves to a non-public network address")
    return value


def _route_guard(route: Any) -> None:
    try:
        _public_url(route.request.url)
    except (OSError, ValueError):
        route.abort("blockedbyclient")
        return
    if route.request.method not in {"GET", "HEAD"}:
        route.abort("blockedbyclient")
        return
    route.continue_()


def set_lifecycle_observer(observer: Callable[[str], None] | None) -> None:
    """Install the broker's metadata-only observer; page contents are never passed."""
    global _lifecycle_observer
    _lifecycle_observer = observer


def _observe_lifecycle(event: str) -> None:
    observer = _lifecycle_observer
    if observer is not None:
        try:
            observer(event)
        except Exception:
            # Diagnostics must not change browser behavior or mask its real failure.
            pass


def _copy_visible_lifecycle_diagnostics(source: dict[str, object], target: dict[str, object]) -> None:
    fatal = source.get("fatal_signal")
    if isinstance(fatal, bool):
        target["fatal_signal"] = fatal
    category = source.get("failure_category")
    if isinstance(category, str) and category in _FATAL_BROWSER_CATEGORIES | {
        "browser_operation_failed", "browser_request_invalid", "navigation_http_error", "navigation_timeout",
    }:
        target["failure_category"] = category
    lifecycle_raw = source.get("lifecycle")
    if not isinstance(lifecycle_raw, list):
        return
    entries: list[dict[str, object]] = []
    for raw_entry in lifecycle_raw[-8:]:
        if not isinstance(raw_entry, dict):
            continue
        entry = cast(dict[str, object], raw_entry)
        event = entry.get("event")
        if not isinstance(event, str) or event not in _LIFECYCLE_EVENTS:
            continue
        safe_entry: dict[str, object] = {"event": event}
        timestamp = entry.get("timestamp")
        if isinstance(timestamp, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT[0-9:.+-]+Z", timestamp[:40]):
            safe_entry["timestamp"] = timestamp[:40]
        for key in (
            "elapsed_ms", "tool_invocation_sequence", "process_count", "thread_count", "fd_count", "fd_limit",
            "browser_fd_count", "browser_fd_limit", "browser_thread_count", "cgroup_pids_current",
            "cgroup_pids_max", "cgroup_memory_current_bytes", "cgroup_memory_max_bytes",
            "browser_pid", "playwright_driver_pid", "browser_exit_code", "browser_exit_signal",
            "playwright_driver_exit_code",
        ):
            value = entry.get(key)
            if type(value) is int and 0 <= value <= 2**63 - 1:
                safe_entry[key] = value
        roles = entry.get("process_roles")
        if isinstance(roles, dict):
            safe_roles: dict[str, dict[str, int]] = {}
            for role, counts_raw in cast(dict[str, object], roles).items():
                if role not in {"xvfb", "openbox", "xterm", "broker", "playwright_driver", "chrome_browser", "chrome_renderer_or_utility", "other"} or not isinstance(counts_raw, dict):
                    continue
                counts = cast(dict[str, object], counts_raw)
                safe_counts = {
                    key: value for key, value in counts.items()
                    if key in {"processes", "threads"} and type(value) is int and 0 <= value <= 4096
                }
                if safe_counts:
                    safe_roles[role] = safe_counts
            if safe_roles:
                safe_entry["process_roles"] = safe_roles
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
                safe_entry["cgroup_pid_ancestors"] = safe_ancestors
        for key in ("browser_connected", "context_open", "page_open", "browser_process_alive", "playwright_driver_alive"):
            value = entry.get(key)
            if isinstance(value, bool):
                safe_entry[key] = value
        for key in ("session_state", "browser_termination", "cleanup_reason"):
            value = entry.get(key)
            if isinstance(value, str) and value in {
                "idle", "launching", "active", "disconnected", "closed", "unknown",
                "application_close", "target_crash", "transport_disconnect", "process_disappeared",
                "normal_exit", "signal_exit", "unknown_exit", "explicit_close", "fatal_failure",
                "authorization_revoked", "startup_failure", "service_shutdown", "mcp_cleanup",
                "cleanup_failed", "",
            }:
                safe_entry[key] = value
        for key in ("memory_events_delta", "pids_events_delta"):
            raw_events = entry.get(key)
            if isinstance(raw_events, dict):
                safe_events = {
                    name: count
                    for name, count in cast(dict[str, object], raw_events).items()
                    if name in {"low", "high", "max", "oom", "oom_kill", "oom_group_kill", "sock_throttled"}
                    and type(count) is int and 0 <= count <= 2**31 - 1
                }
                if safe_events:
                    safe_entry[key] = safe_events
        raw_cpu = entry.get("cpu_stats")
        if isinstance(raw_cpu, dict):
            safe_cpu = {
                name: count
                for name, count in cast(dict[str, object], raw_cpu).items()
                if name in {"usage_usec", "nr_periods", "nr_throttled", "throttled_usec"}
                and type(count) is int and 0 <= count <= 2**63 - 1
            }
            if safe_cpu:
                safe_entry["cpu_stats"] = safe_cpu
        entries.append(safe_entry)
    if entries:
        target["lifecycle"] = entries


def _write_fatal_signal(diagnostic: dict[str, object], *, task_id: str, execution_id: str) -> None:
    path = Path(os.environ.get("TOOTHLESS_BROWSER_FATAL_SIGNAL", ""))
    if (not path.is_absolute() or path.name != "fatal.json"
            or not path.parent.as_posix().startswith("/run/toothless-browser-")):
        raise OSError("Browser fatal-signal path is unavailable")
    parent = path.parent.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid()
            or parent.st_mode & 0o777 != 0o700 or path.is_symlink()):
        raise OSError("Browser fatal-signal directory is unavailable")
    category = diagnostic.get("failure_category")
    if not isinstance(category, str) or category not in _FATAL_BROWSER_CATEGORIES:
        raise ValueError("Browser fatal-signal category is invalid")
    if path.exists():
        try:
            info = path.lstat()
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise OSError("Browser fatal-signal already exists in an invalid state") from exc
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o777 != 0o600 or info.st_size > 16_384
                or not isinstance(existing, dict) or existing.get("task_id") != task_id
                or existing.get("execution_id") != execution_id):
            raise OSError("Browser fatal-signal already exists in an invalid state")
        return
    payload = {
        "version": 1,
        "task_id": task_id,
        "execution_id": execution_id,
        "failure_category": category,
        "lifecycle": diagnostic.get("lifecycle", []),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(raw) > 16_384:
        raise OSError("Browser fatal-signal exceeded its size limit")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=True) as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _ensure_browser() -> Any:
    global _page, _browser, _context, _playwright, _runtime_thread_id, _observed_sources, _last_response_status
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("TOOTHLESS_BROWSER_FAILURE:browser_runtime_conflict")
    if _runtime_thread_id is not None and _runtime_thread_id != threading.get_ident():
        raise RuntimeError("TOOTHLESS_BROWSER_FAILURE:browser_runtime_conflict")
    if _browser is not None or _context is not None or _page is not None or _playwright is not None:
        try:
            if (_browser is not None and _browser.is_connected() and _context is not None
                    and _page is not None and not _page.is_closed() and _page in _context.pages):
                return _page
        except Exception:
            pass
        _close()
        raise RuntimeError("TOOTHLESS_BROWSER_FAILURE:browser_session_invalid")
    if not CHROMIUM.is_file() or not os.access(CHROMIUM, os.X_OK):
        raise RuntimeError("Approved guest Chromium binary is unavailable")
    with CHROMIUM.open("rb") as browser_file:
        actual_hash = hashlib.file_digest(browser_file, "sha256").hexdigest()
    if actual_hash != CHROMIUM_SHA256:
        raise RuntimeError("Chromium binary hash differs from the reviewed build")
    if importlib.metadata.version("playwright") != PLAYWRIGHT_VERSION:
        raise RuntimeError("Playwright version differs from the reviewed build")
    from playwright.sync_api import sync_playwright

    _runtime_thread_id = threading.get_ident()
    _observed_sources = []
    _last_response_status = None
    try:
        _observe_lifecycle("browser_starting")
        _playwright = sync_playwright().start()
        _browser = _playwright.chromium.launch(
            headless=os.environ.get("TOOTHLESS_BROWSER_HEADLESS", "1") != "0",
            executable_path=str(CHROMIUM),
            args=["--disable-dev-shm-usage"],
            timeout=PAGE_TIMEOUT_MS,
        )
        _observe_lifecycle("browser_process_created")
        _browser.on("disconnected", lambda: _observe_lifecycle("browser_disconnected"))
        _context = _browser.new_context(accept_downloads=False, service_workers="block")
        _observe_lifecycle("browser_context_created")
        _context.on("close", lambda: _observe_lifecycle("browser_context_closed"))
        _context.set_default_timeout(PAGE_TIMEOUT_MS)
        _context.set_default_navigation_timeout(PAGE_TIMEOUT_MS)
        _context.route("**/*", _route_guard)
        _page = _context.new_page()
        _page.on("close", lambda: _observe_lifecycle("browser_page_closed"))
        _observe_lifecycle("browser_page_created")
        return _page
    except Exception:
        _observe_lifecycle("browser_startup_failed")
        _close("startup_failure")
        raise


def _visible_call(name: str, arguments: dict[str, object]) -> str:
    global _fatal_signal_write_failed
    task_id = os.environ.get("TOOTHLESS_TASK_ID", "")
    execution_id = os.environ.get("TOOTHLESS_EXECUTION_ID", "")
    if not re.fullmatch(r"task-[0-9a-f]{32}", task_id) or not re.fullmatch(r"opencode-[0-9a-f]{32}", execution_id):
        raise PermissionError("Visible browser task binding is unavailable")
    request = json.dumps({
        "task_id": task_id,
        "execution_id": execution_id,
        "name": name,
        "arguments": arguments,
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(request) > MAX_MESSAGE:
        raise ValueError("Visible browser request exceeds the size limit")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(PAGE_TIMEOUT_MS / 1000 + 10)
        connection.connect("/run/toothless-desktop/visible-browser.sock")
        connection.sendall(request)
        with connection.makefile("rb") as reader:
            raw = reader.readline(MAX_MESSAGE + 1)
    if not raw or len(raw) > MAX_MESSAGE:
        raise RuntimeError("Visible browser broker returned no bounded response")
    response_raw: object = json.loads(raw)
    if not isinstance(response_raw, dict):
        raise RuntimeError("Visible browser broker returned an invalid response")
    response = cast(dict[str, object], response_raw)
    if response.get("ok") is not True:
        diagnostic_raw = response.get("diagnostic")
        if isinstance(diagnostic_raw, dict):
            diagnostic = cast(dict[str, object], diagnostic_raw)
            safe: dict[str, object] = {}
            for key, allowed in (
                ("failure_category", _VISIBLE_FAILURE_CATEGORIES),
                ("tool_name", set(TOOLS) | {"unknown"}),
                ("session_state", {"invalidated", "unchanged", "active", "idle"}),
                ("exception_class", _VISIBLE_EXCEPTION_CLASSES),
            ):
                value = diagnostic.get(key)
                if isinstance(value, str) and value in allowed:
                    safe[key] = value
            for key in ("invocation_sequence", "elapsed_ms"):
                value = diagnostic.get(key)
                if type(value) is int and 0 <= value <= 120_000:
                    safe[key] = value
            task_id_from_broker = diagnostic.get("task_id")
            if task_id_from_broker == task_id:
                safe["task_id"] = task_id
            connected = diagnostic.get("browser_connected")
            if isinstance(connected, bool):
                safe["browser_connected"] = connected
            process_alive = diagnostic.get("browser_process_alive")
            if isinstance(process_alive, bool):
                safe["browser_process_alive"] = process_alive
            if diagnostic.get("execution_id") == execution_id:
                safe["execution_id"] = execution_id
            _copy_visible_lifecycle_diagnostics(diagnostic, safe)
            fatal_signal = diagnostic.get("fatal_signal") is True
            if fatal_signal and safe.get("task_id") == task_id and safe.get("execution_id") == execution_id:
                safe["fatal_signal"] = True
                try:
                    _write_fatal_signal(safe, task_id=task_id, execution_id=execution_id)
                except OSError:
                    _fatal_signal_write_failed = True
            raise VisibleBrowserDiagnosticError(safe)
        raise RuntimeError("TOOTHLESS_BROWSER_FAILURE:browser_session_invalid")
    result = response.get("result")
    if not isinstance(result, str):
        raise RuntimeError("Visible browser broker returned an invalid result")
    if len(result.encode("utf-8")) > MAX_TOOL_RESULT_BYTES:
        raise RuntimeError("Visible browser result exceeded its bounded tool-result limit")
    _record_visible_source(name, result)
    return result


def _source_manifest_path() -> Path | None:
    configured = os.environ.get("TOOTHLESS_BROWSER_SOURCE_MANIFEST")
    if not configured:
        return None
    path = Path(configured)
    if (not path.is_absolute() or path.name != "sources.json" or path.parent.parent != Path("/run")
            or not path.parent.name.startswith("toothless-browser-")):
        raise OSError("Browser source manifest path is invalid")
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) != 0o700:
        raise OSError("Browser source manifest directory is unavailable")
    if path.is_symlink():
        raise OSError("Browser source manifest must not be a symlink")
    return path


def _save_observed_source(
    title: object,
    url: object,
    *,
    label: object = "",
    http_status: object = None,
    verification_status: str = "observed",
) -> None:
    global _observed_sources
    if not isinstance(title, str) or not isinstance(url, str):
        return
    try:
        safe_url = _display_url(url)
        if verification_status == "observed":
            _public_url(safe_url)
        elif verification_status != "linked_unvisited":
            return
    except (OSError, ValueError):
        return
    clean_title = " ".join(title.split())[:180]
    clean_label = " ".join(label.split())[:120] if isinstance(label, str) else ""
    if not clean_title or len(safe_url) > 1000:
        return
    code = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
    prior = next((item for item in _observed_sources if item.get("url") == safe_url), None)
    if prior is not None:
        clean_label = clean_label or cast(str, prior.get("label", ""))
        code = code if code is not None else cast(int | None, prior.get("http_status"))
    verification_status = (
        "observed" if prior is not None and prior.get("verification_status") == "observed"
        else verification_status
    )
    source = {
        "title": clean_title, "url": safe_url, "label": clean_label, "http_status": code,
        "verification_status": verification_status,
    }
    _observed_sources = [item for item in _observed_sources if item.get("url") != safe_url]
    _observed_sources.append(source)
    _observed_sources = _observed_sources[-12:]
    path = _source_manifest_path()
    if path is None:
        return
    temporary = path.with_name(f".sources-{uuid.uuid4().hex}.tmp")
    raw = json.dumps({"version": 1, "sources": _observed_sources}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > 16_384:
        raise OSError("Browser source manifest exceeds its size limit")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _observed_navigation_links(page: Any) -> list[dict[str, str]]:
    try:
        raw: object = page.locator("nav a[href]").evaluate_all(
            "links => links.slice(0, 8).map(link => ({label: link.innerText || link.getAttribute('aria-label') || '', href: link.href}))"
        )
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    page_host = (urlsplit(page.url).hostname or "").lower().rstrip(".")
    for item in raw[:8]:
        if not isinstance(item, dict):
            continue
        record = cast(dict[str, object], item)
        label, href = record.get("label"), record.get("href")
        if not isinstance(label, str) or not isinstance(href, str):
            continue
        try:
            parts = urlsplit(urljoin(page.url, href))
            host = parts.hostname
            if (parts.scheme.lower() not in {"http", "https"} or not host or parts.username or parts.password
                    or parts.port not in {None, 80, 443}):
                continue
            normalized = host.encode("idna").decode("ascii").lower().rstrip(".")
            if normalized != page_host:
                continue
            if normalized in {"localhost", "localhost.localdomain"} or normalized.endswith((".local", ".internal", ".localhost")):
                continue
            try:
                address = ipaddress.ip_address(normalized)
            except ValueError:
                address = None
            if address is not None and not address.is_global:
                continue
            safe_url = urlunsplit((parts.scheme.lower(), normalized, parts.path or "/", "", ""))
            if len(safe_url) > 500:
                continue
        except (UnicodeError, ValueError):
            continue
        clean_label = " ".join(label.split())[:120]
        if not clean_label or safe_url in seen:
            continue
        seen.add(safe_url)
        result.append({"label": clean_label, "url": safe_url})
        _save_observed_source(
            clean_label, safe_url, label=clean_label, verification_status="linked_unvisited",
        )
    return result


def _record_visible_source(name: str, result: str) -> None:
    if name not in {"navigate", "click_link", "extract_text"}:
        return
    try:
        parsed: object = json.loads(result)
    except json.JSONDecodeError:
        return
    if not isinstance(parsed, dict):
        return
    record = cast(dict[str, object], parsed)
    _save_observed_source(
        record.get("title"), record.get("final_url"), label=record.get("label", ""),
        http_status=record.get("http_status"),
    )
    links = record.get("navigation_links")
    if isinstance(links, list):
        for link in links[:12]:
            if isinstance(link, dict):
                value = cast(dict[str, object], link)
                _save_observed_source(
                    value.get("label"), value.get("url"), label=value.get("label", ""),
                    verification_status="linked_unvisited",
                )


def _close(reason: str = "mcp_cleanup") -> str:
    global _page, _browser, _context, _playwright, _runtime_thread_id, _cleanup_reason
    if _runtime_thread_id is not None and _runtime_thread_id != threading.get_ident():
        raise RuntimeError("TOOTHLESS_BROWSER_FAILURE:browser_runtime_conflict")
    context, browser, playwright = _context, _browser, _playwright
    _cleanup_reason = reason
    _observe_lifecycle("cleanup_start")
    _page = _context = _browser = _playwright = None
    _runtime_thread_id = None
    errors: list[str] = []
    for item in (context, browser, playwright):
        if item is not None:
            try:
                item.close() if item is context or item is browser else item.stop()
            except Exception:
                errors.append(type(sys.exc_info()[1]).__name__)
    if errors:
        _cleanup_reason = "cleanup_failed"
        _observe_lifecycle("cleanup_failed")
        raise RuntimeError("Browser cleanup reported an error: " + ",".join(errors))
    _observe_lifecycle("cleanup_complete")
    _cleanup_reason = ""
    return "Browser session closed."


def _text(value: str) -> dict[str, object]:
    if len(value.encode("utf-8")) > MAX_TOOL_RESULT_BYTES:
        return {"isError": True, "content": [{"type": "text", "text": "Browser result exceeded its bounded tool-result limit."}]}
    return {"content": [{"type": "text", "text": value}]}


def _page_text_limit_bytes() -> int:
    configured = os.environ.get("TOOTHLESS_BROWSER_TEXT_LIMIT_BYTES")
    if configured is None:
        return DEFAULT_MAX_PAGE_TEXT_BYTES
    if not re.fullmatch(r"[0-9]{1,5}", configured):
        raise ValueError("Browser text limit configuration is invalid")
    limit = int(configured)
    if not MIN_MAX_PAGE_TEXT_BYTES <= limit <= MAX_MAX_PAGE_TEXT_BYTES:
        raise ValueError("Browser text limit is outside the reviewed bounds")
    return limit


def _utf8_prefix(value: str, limit_bytes: int) -> tuple[str, int]:
    selected: list[str] = []
    used = 0
    for character in value:
        size = len(character.encode("utf-8"))
        if used + size > limit_bytes:
            break
        selected.append(character)
        used += size
    return "".join(selected), used


def _extract_page_result(page: Any) -> str:
    _save_observed_source(page.title(), page.url, http_status=_last_response_status)
    limit_bytes = _page_text_limit_bytes()
    source_text = page.locator("body").inner_text(timeout=PAGE_TIMEOUT_MS)
    source_truncated = len(source_text) > MAX_PAGE_TEXT_SOURCE_CHARS
    source_text = source_text[:MAX_PAGE_TEXT_SOURCE_CHARS]
    title = page.title()[:500]
    final_url = _display_url(page.url)
    navigation_links = _observed_navigation_links(page)
    if len(title.encode("utf-8")) > 2048 or len(final_url.encode("utf-8")) > 4096:
        raise ValueError("Page title or final URL exceeded the metadata limit")

    source_bytes = len(source_text.encode("utf-8"))

    def serialize(byte_budget: int) -> tuple[str, int]:
        text, returned_bytes = _utf8_prefix(source_text, byte_budget)
        value = {
            "title": title,
            "final_url": final_url,
            "navigation_links": navigation_links,
            "text": text,
            "truncated": source_truncated or returned_bytes < source_bytes,
            "returned_characters": len(text),
            "returned_bytes": returned_bytes,
            "limit_bytes": limit_bytes,
        }
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")), returned_bytes

    low, high = 0, min(limit_bytes, source_bytes)
    selected, _ = serialize(0)
    while low <= high:
        middle = (low + high) // 2
        candidate, _ = serialize(middle)
        if len(candidate.encode("utf-8")) <= MAX_TOOL_RESULT_BYTES:
            selected = candidate
            low = middle + 1
        else:
            high = middle - 1
    if len(selected.encode("utf-8")) > MAX_TOOL_RESULT_BYTES:
        raise ValueError("Structured browser result exceeded its bounded output limit")
    return selected


def _safe_artifacts_dir() -> Path:
    ARTIFACTS.mkdir(mode=0o770, parents=True, exist_ok=True)
    resolved = ARTIFACTS.resolve(strict=True)
    if not resolved.is_relative_to(WORKSPACE):
        raise ValueError("Guest artifact directory resolves outside the workspace")
    return resolved


def _display_url(value: str) -> str:
    parsed = urlsplit(value)
    return parsed._replace(query="", fragment="").geturl()


def _safe_error(exc: Exception) -> str:
    message = str(exc)
    return re.sub(r"https?://[^\s'\"]+", lambda match: _display_url(match.group(0)), message)[:500]


def safe_error_for_broker(exc: Exception) -> str:
    return _safe_error(exc)


def close_browser_for_broker(reason: str = "broker_cleanup") -> None:
    _close(reason)


def _navigate(args: dict[str, object]) -> str:
    global _last_response_status
    url = _public_url(args.get("url"))
    page = _ensure_browser()
    _observe_lifecycle("before_navigation")
    response = page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    _last_response_status = response.status if response is not None else None
    if response is not None and response.status >= 400:
        raise NavigationHTTPError("Navigation returned an unsuccessful HTTP status")
    _public_url(page.url)
    _save_observed_source(page.title(), page.url, http_status=_last_response_status)
    _observe_lifecycle("after_navigation")
    return json.dumps({
        "title": page.title()[:500], "final_url": _display_url(page.url),
        "http_status": _last_response_status,
    }, ensure_ascii=False)


def _extract(args: dict[str, object]) -> str:
    if _page is None or _page.is_closed():
        raise RuntimeError("Navigate to a page first")
    _observe_lifecycle("before_extraction")
    result = _extract_page_result(_page)
    _observe_lifecycle("after_extraction")
    return result


def _click_link(args: dict[str, object]) -> str:
    if _page is None or _page.is_closed():
        raise RuntimeError("Navigate to a page first")
    selector = args.get("selector")
    if not isinstance(selector, str) or not selector or len(selector) > 512:
        raise ValueError("A CSS selector of at most 512 characters is required")
    target = _page.locator(selector).first
    if target.evaluate("el => el.tagName.toLowerCase()") != "a":
        raise ValueError("The selected element is not a link")
    label = target.inner_text(timeout=PAGE_TIMEOUT_MS)[:120]
    with _page.expect_navigation(wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS) as navigation:
        target.click()
    response = navigation.value
    _public_url(_page.url)
    http_status = response.status if response is not None else None
    _save_observed_source(_page.title(), _page.url, label=label, http_status=http_status)
    return json.dumps({"title": _page.title()[:500], "final_url": _display_url(_page.url), "label": label[:120], "http_status": http_status}, ensure_ascii=False)


def _click_button(args: dict[str, object]) -> str:
    if _page is None or _page.is_closed():
        raise RuntimeError("Navigate to a page first")
    selector = args.get("selector")
    if not isinstance(selector, str) or not selector or len(selector) > 512:
        raise ValueError("A CSS selector of at most 512 characters is required")
    target = _page.locator(selector).first
    safe = target.evaluate("el => el.tagName.toLowerCase() === 'button' && (el.type || '').toLowerCase() === 'button' && !el.closest('form')")
    if not safe:
        raise ValueError("Only standalone type=button controls outside forms are allowed")
    target.click()
    return _extract_page_result(_page)


def _screenshot(args: dict[str, object]) -> str:
    name = args.get("filename", "page.png")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}[.]png", name):
        raise ValueError("Screenshot filename must be a simple PNG basename")
    if _page is None or _page.is_closed():
        raise RuntimeError("Navigate to a page first")
    artifacts = _safe_artifacts_dir()
    path = artifacts / name
    temporary = artifacts / f".{name}.{os.getpid()}.tmp.png"
    _page.screenshot(path=str(temporary), full_page=False, timeout=PAGE_TIMEOUT_MS)
    if temporary.stat().st_size > MAX_SCREENSHOT:
        temporary.unlink(missing_ok=True)
        raise ValueError("Screenshot exceeded the 2 MiB limit")
    os.replace(temporary, path)
    return f"Screenshot saved at browser-artifacts/{name} ({path.stat().st_size} bytes)."


def _download(args: dict[str, object]) -> str:
    url = _public_url(args.get("url"))
    name = args.get("filename")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", name):
        raise ValueError("Download filename must be a simple basename")
    artifacts = _safe_artifacts_dir()
    request = urllib.request.Request(url, headers={"User-Agent": "Toothless guest browser/1.0"})
    opener = urllib.request.build_opener(_SafeRedirect())
    path = artifacts / name
    written = 0
    try:
        with opener.open(request, timeout=15) as response, path.open("xb") as output:
            _public_url(response.geturl())
            while block := response.read(min(64 * 1024, MAX_DOWNLOAD - written + 1)):
                written += len(block)
                if written > MAX_DOWNLOAD:
                    raise ValueError("Download exceeded the 10 MiB limit")
                output.write(block)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return f"Downloaded {written} bytes to browser-artifacts/{name}; source {url}."


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        _public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _close_tool(_args: dict[str, object]) -> str:
    return _close("explicit_close")


TOOLS: dict[str, tuple[str, dict[str, object], Any]] = {
    "navigate": ("Open a public HTTP or HTTPS page in the isolated Chromium session approved for this task.", {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}, _navigate),
    "extract_text": ("Read bounded rendered text, page title, and final URL from the current page.", {"type": "object", "properties": {}, "additionalProperties": False}, _extract),
    "click_link": ("Click a link by CSS selector; each resulting URL is checked and private destinations are denied.", {"type": "object", "properties": {"selector": {"type": "string"}}, "required": ["selector"]}, _click_link),
    "click_button": ("Click a standalone type=button outside a form. Do not use for external effects.", {"type": "object", "properties": {"selector": {"type": "string"}}, "required": ["selector"]}, _click_button),
    "screenshot": ("Save a viewport screenshot to the guest browser-artifacts directory.", {"type": "object", "properties": {"filename": {"type": "string"}}, "additionalProperties": False}, _screenshot),
    "download": ("Download a public HTTP(S) file of at most 10 MiB to guest browser-artifacts.", {"type": "object", "properties": {"url": {"type": "string"}, "filename": {"type": "string"}}, "required": ["url", "filename"]}, _download),
    "close_session": ("Close the browser and discard its isolated context.", {"type": "object", "properties": {}, "additionalProperties": False}, _close_tool),
}


def _reply(message: dict[str, object], writer: BinaryIO) -> None:
    encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_MESSAGE:
        encoded = json.dumps(
            {"jsonrpc": "2.0", "id": message.get("id"), "result": {"isError": True, "content": [{"type": "text", "text": "Browser response exceeded the bounded message limit."}]}},
            separators=(",", ":"),
        ).encode("utf-8")
    writer.write(encoded + b"\n")
    writer.flush()


def _serve(reader: BinaryIO, writer: BinaryIO) -> int:
    global _fatal_signal_write_failed
    _fatal_signal_write_failed = False
    try:
        while raw := reader.readline(MAX_MESSAGE + 1):
            if len(raw) > MAX_MESSAGE:
                return 1
            try:
                request_raw: object = json.loads(raw)
                if not isinstance(request_raw, dict):
                    continue
                request = cast(dict[str, object], request_raw)
                method = request.get("method")
                request_id = request.get("id")
                if method == "notifications/initialized":
                    continue
                if method == "initialize":
                    _reply({"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "toothless-browser", "version": "1.0.0"}}}, writer)
                elif method == "tools/list":
                    visible = os.environ.get("TOOTHLESS_VISIBLE_BROWSER") == "1"
                    names = ("navigate", "extract_text", "click_link", "close_session") if visible else tuple(TOOLS)
                    _reply({"jsonrpc": "2.0", "id": request_id, "result": {"tools": [{"name": name, "description": TOOLS[name][0], "inputSchema": TOOLS[name][1]} for name in names]}}, writer)
                elif method == "tools/call":
                    params_raw = request.get("params")
                    if not isinstance(params_raw, dict):
                        raise ValueError("Tool request parameters are invalid")
                    params = cast(dict[str, object], params_raw)
                    tool_name = params.get("name")
                    if not isinstance(tool_name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", tool_name):
                        raise ValueError("Unknown browser tool")
                    visible = os.environ.get("TOOTHLESS_VISIBLE_BROWSER") == "1"
                    tool = TOOLS.get(tool_name)
                    arguments_raw = params.get("arguments", {})
                    if tool is None or not isinstance(arguments_raw, dict) or (visible and tool_name not in {"navigate", "extract_text", "click_link", "close_session"}):
                        raise ValueError("Unknown browser tool or invalid arguments")
                    arguments = cast(dict[str, object], arguments_raw)
                    try:
                        result = _visible_call(tool_name, arguments) if visible else tool[2](arguments)
                        _reply({"jsonrpc": "2.0", "id": request_id, "result": _text(str(result))}, writer)
                    except VisibleBrowserDiagnosticError as exc:
                        marker = "TOOTHLESS_BROWSER_DIAGNOSTIC:" + json.dumps(exc.diagnostic, separators=(",", ":"))
                        _reply({"jsonrpc": "2.0", "id": request_id, "result": {"isError": True, "content": [{"type": "text", "text": marker}]}}, writer)
                        if _fatal_signal_write_failed:
                            return 2
                    except Exception as exc:
                        _reply({"jsonrpc": "2.0", "id": request_id, "result": {"isError": True, "content": [{"type": "text", "text": f"{type(exc).__name__}: {_safe_error(exc)}"}]}}, writer)
                        if _fatal_signal_write_failed:
                            return 2
                elif request_id is not None:
                    _reply({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Unsupported MCP method"}}, writer)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
    finally:
        try:
            if os.environ.get("TOOTHLESS_VISIBLE_BROWSER") == "1":
                _visible_call("close_session", {})
            else:
                _close()
        except Exception:
            pass
    return 0


def _serve_socket(socket_path: str, ready_file: str) -> int:
    socket_target = Path(socket_path)
    ready_target = Path(ready_file)
    parent = socket_target.parent.resolve(strict=True)
    if not parent.as_posix().startswith("/run/toothless-browser-") or ready_target.parent != socket_target.parent:
        raise ValueError("Browser MCP socket path is outside its private runtime directory")
    if parent.stat().st_uid != os.geteuid() or socket_target.exists() or ready_target.exists():
        raise ValueError("Browser MCP socket directory ownership or state is invalid")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(socket_target))
        os.chmod(socket_target, 0o600)
        server.listen(1)
        ready_target.touch(mode=0o600, exist_ok=False)
        with server.accept()[0] as connection:
            reader = connection.makefile("rb")
            writer = connection.makefile("wb")
            try:
                return _serve(reader, writer)
            finally:
                reader.close()
                writer.close()


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--socket")
    parser.add_argument("--ready-file")
    args = parser.parse_args()
    if args.socket is None and args.ready_file is None:
        return _serve(sys.stdin.buffer, sys.stdout.buffer)
    if args.socket is None or args.ready_file is None:
        return 2
    return _serve_socket(args.socket, args.ready_file)


if __name__ == "__main__":
    raise SystemExit(main())
