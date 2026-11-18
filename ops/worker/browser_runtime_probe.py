#!/usr/bin/env python3
"""Operator-only real Playwright checks, run as toothless-agent in the filter scope."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import atexit
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, "/usr/local/libexec/toothless")

import browser_mcp

atexit.register(browser_mcp._close)


def main() -> int:
    page = browser_mcp._navigate({"url": "https://example.com/"})
    first = json.loads(page)
    assert first["title"] == "Example Domain"
    content = json.loads(browser_mcp._extract({}))
    assert "This domain is for use in documentation examples" in content["text"]
    current = browser_mcp._page
    assert current is not None
    current.set_content("<button id='render' type='button'>Render</button><p id='result'>WAITING</p><script>document.querySelector('#render').addEventListener('click', () => { document.querySelector('#result').textContent = 'JS_RENDERED_OK'; });</script>")
    rendered = browser_mcp._click_button({"selector": "#render"})
    assert "JS_RENDERED_OK" in rendered

    current.set_content("<a id='owned-link' href='https://example.com/'>Open public page</a>")
    clicked = json.loads(browser_mcp._click_link({"selector": "#owned-link"}))
    assert clicked["title"] == "Example Domain"

    screenshot_result = browser_mcp._screenshot({"filename": "phase053b-runtime.png"})
    screenshot_path = browser_mcp.WORKSPACE / "browser-artifacts/phase053b-runtime.png"
    assert screenshot_path.is_file() and screenshot_path.stat().st_size <= browser_mcp.MAX_SCREENSHOT
    assert screenshot_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    for url in ("http://127.0.0.1/", "http://10.254.253.1/", "http://169.254.253.1/", "http://169.254.169.254/", "http://[::1]/", "http://[fd00::253]/"):
        try:
            browser_mcp._public_url(url)
        except ValueError:
            pass
        else:
            raise AssertionError(f"private URL was accepted: {url}")

    def owned_redirect(route: Any) -> None:
        route.fulfill(status=302, headers={"Location": "http://169.254.169.254/"})

    context = current.context
    context.unroute("**/*", browser_mcp._route_guard)
    redirect_events: list[str] = []

    def redirect_guard(route: Any) -> None:
        if route.request.url == "https://example.com/toothless-owned-redirect":
            redirect_events.append("owned-redirect-source")
            owned_redirect(route)
        else:
            if "169.254.169.254" in route.request.url:
                redirect_events.append("metadata-shaped-redirect")
            browser_mcp._route_guard(route)

    context.route("**/*", redirect_guard)
    current.set_default_navigation_timeout(6000)
    try:
        browser_mcp._navigate({"url": "https://example.com/toothless-owned-redirect"})
    except Exception as exc:
        redirect_error = type(exc).__name__
    else:
        redirect_error = "navigation returned"
    assert redirect_events == ["owned-redirect-source"] and redirect_error == "TimeoutError", f"unexpected redirect result {redirect_events}; {redirect_error}"
    browser_mcp._close()
    lingering: list[str] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            if browser_mcp.CHROMIUM.name.encode() in (entry / "cmdline").read_bytes():
                lingering.append(entry.name)
        except (OSError, ProcessLookupError):
            continue
    assert not lingering, f"Chromium processes remain after close: {lingering}"
    print("PASS public HTTPS, rendered page text, JS button action, public link click, bounded PNG screenshot, private URL rejection, owned metadata redirect rejection, and browser close")
    print(f"title={first['title']} final_url={first['final_url']} screenshot={screenshot_path} bytes={screenshot_path.stat().st_size}")
    print(screenshot_result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
