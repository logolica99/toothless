#!/usr/bin/env python3
"""Bounded stdio MCP server for the guest-local Playwright browser."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import ipaddress
import json
import os
import re
import socket
import sys
import urllib.request
from pathlib import Path
from typing import Any, BinaryIO, cast
from urllib.parse import urlsplit

CHROMIUM = Path(
    "/home/toothless-agent/.local/share/toothless/browser-smoke/chromium/"
    "chrome-headless-shell-linux-arm64/chrome-headless-shell"
)
WORKSPACE = Path("/home/toothless-agent/workspace").resolve()
ARTIFACTS = WORKSPACE / "browser-artifacts"
MAX_MESSAGE = 65_536
MAX_PAGE_TEXT = 32_000
MAX_DOWNLOAD = 10 * 1024 * 1024
MAX_SCREENSHOT = 2 * 1024 * 1024
PAGE_TIMEOUT_MS = 20_000
_page: Any = None
_browser: Any = None
_playwright: Any = None
PLAYWRIGHT_VERSION = "1.65.2"
CHROMIUM_SHA256 = "3e818d32e2fe5749b54c65179d282d14d0af39010ed0fbdd24122af1aca22046"


def _public_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 4096:
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


def _ensure_browser() -> Any:
    global _page, _browser, _playwright
    if _page is not None and not _page.is_closed():
        return _page
    if not CHROMIUM.is_file() or not os.access(CHROMIUM, os.X_OK):
        raise RuntimeError("Approved guest Chromium binary is unavailable")
    with CHROMIUM.open("rb") as browser_file:
        actual_hash = hashlib.file_digest(browser_file, "sha256").hexdigest()
    if actual_hash != CHROMIUM_SHA256:
        raise RuntimeError("Chromium binary hash differs from the reviewed build")
    if importlib.metadata.version("playwright") != PLAYWRIGHT_VERSION:
        raise RuntimeError("Playwright version differs from the reviewed build")
    from playwright.sync_api import sync_playwright

    _playwright = sync_playwright().start()
    _browser = _playwright.chromium.launch(
        headless=True,
        executable_path=str(CHROMIUM),
        args=["--disable-dev-shm-usage"],
        timeout=PAGE_TIMEOUT_MS,
    )
    context = _browser.new_context(accept_downloads=False, service_workers="block")
    context.set_default_timeout(PAGE_TIMEOUT_MS)
    context.set_default_navigation_timeout(PAGE_TIMEOUT_MS)
    context.route("**/*", _route_guard)
    _page = context.new_page()
    return _page


def _close() -> str:
    global _page, _browser, _playwright
    errors: list[str] = []
    for item in (_browser, _playwright):
        if item is not None:
            try:
                item.close() if item is _browser else item.stop()
            except Exception:
                errors.append(type(sys.exc_info()[1]).__name__)
    _page = _browser = _playwright = None
    if errors:
        raise RuntimeError("Browser cleanup reported an error: " + ",".join(errors))
    return "Browser session closed."


def _text(value: str) -> dict[str, object]:
    return {"content": [{"type": "text", "text": value[:MAX_PAGE_TEXT]}]}


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


def _navigate(args: dict[str, object]) -> str:
    url = _public_url(args.get("url"))
    page = _ensure_browser()
    response = page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    if response is not None and response.status >= 400:
        raise RuntimeError(f"Navigation returned HTTP {response.status}")
    _public_url(page.url)
    return json.dumps({"title": page.title()[:500], "final_url": _display_url(page.url)}, ensure_ascii=False)


def _extract(args: dict[str, object]) -> str:
    if _page is None or _page.is_closed():
        raise RuntimeError("Navigate to a page first")
    return json.dumps({"title": _page.title()[:500], "final_url": _display_url(_page.url), "text": _page.locator("body").inner_text(timeout=PAGE_TIMEOUT_MS)[:MAX_PAGE_TEXT]}, ensure_ascii=False)


def _click_link(args: dict[str, object]) -> str:
    if _page is None or _page.is_closed():
        raise RuntimeError("Navigate to a page first")
    selector = args.get("selector")
    if not isinstance(selector, str) or not selector or len(selector) > 512:
        raise ValueError("A CSS selector of at most 512 characters is required")
    target = _page.locator(selector).first
    if target.evaluate("el => el.tagName.toLowerCase()") != "a":
        raise ValueError("The selected element is not a link")
    with _page.expect_navigation(wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS):
        target.click()
    _public_url(_page.url)
    return json.dumps({"title": _page.title()[:500], "final_url": _display_url(_page.url)}, ensure_ascii=False)


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
    return _page.locator("body").inner_text(timeout=PAGE_TIMEOUT_MS)[:MAX_PAGE_TEXT]


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
    return _close()


TOOLS: dict[str, tuple[str, dict[str, object], Any]] = {
    "navigate": ("Open a public HTTP or HTTPS page in an isolated headless Chromium session.", {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}, _navigate),
    "extract_text": ("Read bounded rendered text, page title, and final URL from the current page.", {"type": "object", "properties": {}, "additionalProperties": False}, _extract),
    "click_link": ("Click a link by CSS selector; each resulting URL is checked and private destinations are denied.", {"type": "object", "properties": {"selector": {"type": "string"}}, "required": ["selector"]}, _click_link),
    "click_button": ("Click a standalone type=button outside a form. Do not use for external effects.", {"type": "object", "properties": {"selector": {"type": "string"}}, "required": ["selector"]}, _click_button),
    "screenshot": ("Save a viewport screenshot to the guest browser-artifacts directory.", {"type": "object", "properties": {"filename": {"type": "string"}}, "additionalProperties": False}, _screenshot),
    "download": ("Download a public HTTP(S) file of at most 10 MiB to guest browser-artifacts.", {"type": "object", "properties": {"url": {"type": "string"}, "filename": {"type": "string"}}, "required": ["url", "filename"]}, _download),
    "close_session": ("Close the browser and discard its isolated context.", {"type": "object", "properties": {}, "additionalProperties": False}, _close_tool),
}


def _reply(message: dict[str, object], writer: BinaryIO) -> None:
    writer.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
    writer.flush()


def _serve(reader: BinaryIO, writer: BinaryIO) -> int:
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
                    _reply({"jsonrpc": "2.0", "id": request_id, "result": {"tools": [{"name": name, "description": description, "inputSchema": schema} for name, (description, schema, _fn) in TOOLS.items()]}}, writer)
                elif method == "tools/call":
                    params_raw = request.get("params")
                    if not isinstance(params_raw, dict):
                        raise ValueError("Tool request parameters are invalid")
                    params = cast(dict[str, object], params_raw)
                    tool_name = params.get("name")
                    if not isinstance(tool_name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", tool_name):
                        raise ValueError("Unknown browser tool")
                    tool = TOOLS.get(tool_name)
                    arguments_raw = params.get("arguments", {})
                    if tool is None or not isinstance(arguments_raw, dict):
                        raise ValueError("Unknown browser tool or invalid arguments")
                    arguments = cast(dict[str, object], arguments_raw)
                    try:
                        result = tool[2](arguments)
                        _reply({"jsonrpc": "2.0", "id": request_id, "result": _text(str(result))}, writer)
                    except Exception as exc:
                        _reply({"jsonrpc": "2.0", "id": request_id, "result": {"isError": True, "content": [{"type": "text", "text": f"{type(exc).__name__}: {_safe_error(exc)}"}]}}, writer)
                elif request_id is not None:
                    _reply({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Unsupported MCP method"}}, writer)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
    finally:
        try:
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
