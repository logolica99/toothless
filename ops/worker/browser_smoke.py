from __future__ import annotations

import json
import os
import shutil
import signal
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, sync_playwright

WORKSPACE = Path("/home/toothless-agent/workspace")
BROWSER = Path(
    "/home/toothless-agent/.local/share/toothless/browser-smoke/chromium/"
    "chrome-headless-shell-linux-arm64/chrome-headless-shell"
)
SCREENSHOT = WORKSPACE / "browser-smoke-example.png"


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/clicked":
            title = "Guest Fixture Link"
            body = "<main>LINK_TARGET_OK</main>"
        elif self.path == "/":
            title = "Guest JavaScript Fixture"
            body = (
                "<main><button id='render'>Render result</button>"
                "<p id='result'>WAITING</p>"
                "<a href='/clicked'>Fixture link</a></main>"
                "<script>document.querySelector('#render').addEventListener('click', () => {"
                "document.querySelector('#result').textContent = 'JS_RENDERED_OK';});</script>"
            )
        else:
            self.send_error(404)
            return

        payload = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{title}</title></head><body>{body}</body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *_args: object) -> None:
        return


def browser_processes(profile: Path, baseline: set[int]) -> list[dict[str, Any]]:
    profile_bytes = os.fsencode(profile)
    browser_name = os.fsencode(BROWSER.name)
    processes: list[dict[str, Any]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        pid = int(entry.name)
        if pid in baseline:
            continue
        try:
            command = (entry / "cmdline").read_bytes()
            if profile_bytes not in command and browser_name not in command:
                continue
            status = (entry / "status").read_text(encoding="utf-8")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        rss_kib = 0
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                try:
                    rss_kib = int(line.split()[1])
                except (IndexError, ValueError):
                    pass
                break
        processes.append(
            {
                "pid": pid,
                "rss_kib": rss_kib,
                "remote_debugging_port": b"--remote-debugging-port" in command,
            }
        )
    return processes


def terminate_leftovers(profile: Path, baseline: set[int]) -> tuple[list[dict[str, Any]], bool]:
    leftovers = browser_processes(profile, baseline)
    forced = False
    for process in leftovers:
        try:
            os.kill(process["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        leftovers = browser_processes(profile, baseline)
        if not leftovers:
            return [], bool(forced)
        time.sleep(0.1)
    forced = bool(leftovers)
    for process in leftovers:
        try:
            os.kill(process["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        leftovers = browser_processes(profile, baseline)
        if not leftovers:
            return [], forced
        time.sleep(0.1)
    return leftovers, forced


def main() -> int:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    if not BROWSER.is_file() or not os.access(BROWSER, os.X_OK):
        raise SystemExit(f"approved Chromium executable is missing or not executable: {BROWSER}")

    profile = WORKSPACE / f".browser-smoke-profile-{uuid.uuid4().hex}"
    SCREENSHOT.unlink(missing_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    server_thread = threading.Thread(target=server.serve_forever, name="browser-fixture", daemon=True)
    server_thread.start()
    baseline = {
        process["pid"]
        for process in browser_processes(profile, set())
    }

    playwright = None
    context: BrowserContext | None = None
    result: dict[str, Any] = {
        "public_url": "https://example.com",
        "browser_binary": str(BROWSER),
        "screenshot_path": str(SCREENSHOT),
        "remote_debugging_port": "not requested",
    }
    failure: str | None = None
    try:
        playwright = sync_playwright().start()
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            executable_path=str(BROWSER),
            headless=True,
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()
        public_response = page.goto(
            "https://example.com",
            wait_until="domcontentloaded",
            timeout=45_000,
        )
        public_title = page.title()
        public_text = page.locator("body").inner_text(timeout=10_000)
        result["public_status"] = public_response.status if public_response is not None else None
        result["public_final_url"] = page.url
        result["public_title"] = public_title
        result["public_text"] = public_text[:300]
        if public_response is None or public_response.status != 200:
            raise RuntimeError(f"public HTTPS page returned HTTP {result['public_status']}")
        if public_title != "Example Domain" or "This domain is for use in documentation examples" not in public_text:
            raise RuntimeError(
                f"public page title/content mismatch: title={public_title!r}, text={public_text[:200]!r}"
            )

        fixture_url = f"http://127.0.0.1:{server.server_port}/"
        fixture_response = page.goto(fixture_url, wait_until="domcontentloaded", timeout=10_000)
        if fixture_response is None or fixture_response.status != 200:
            raise RuntimeError("local JavaScript fixture did not return HTTP 200")
        page.get_by_role("button", name="Render result").click(timeout=10_000)
        rendered = page.locator("#result").inner_text(timeout=5_000)
        if rendered != "JS_RENDERED_OK":
            raise RuntimeError(f"JavaScript result was {rendered!r}")
        result["javascript_result"] = rendered
        page.screenshot(path=str(SCREENSHOT), full_page=True, timeout=10_000)
        if not SCREENSHOT.is_file() or SCREENSHOT.stat().st_size == 0:
            raise RuntimeError("screenshot was not created")
        result["screenshot_bytes"] = SCREENSHOT.stat().st_size

        page.get_by_role("link", name="Fixture link").click(timeout=10_000)
        linked_text = page.locator("body").inner_text(timeout=5_000)
        if page.url != f"http://127.0.0.1:{server.server_port}/clicked" or "LINK_TARGET_OK" not in linked_text:
            raise RuntimeError("fixture link navigation did not reach the expected page")
        result["link_target"] = page.url
        result["link_text"] = linked_text

        processes = browser_processes(profile, baseline)
        if not processes:
            raise RuntimeError("no Chromium process was visible for resource measurement")
        if any(process["remote_debugging_port"] for process in processes):
            raise RuntimeError("Chromium exposed a remote debugging TCP port")
        result["chromium_processes_during_run"] = len(processes)
        result["chromium_rss_kib_during_run"] = sum(p["rss_kib"] for p in processes)
        result["chromium_peak_process_rss_kib"] = max(p["rss_kib"] for p in processes)
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        if context is not None:
            try:
                context.close()
            except Exception as exc:
                failure = failure or f"browser close failed: {type(exc).__name__}: {exc}"
        if playwright is not None:
            try:
                playwright.stop()
            except Exception as exc:
                failure = failure or f"Playwright driver cleanup failed: {type(exc).__name__}: {exc}"
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=3)
        leftovers, forced = terminate_leftovers(profile, baseline)
        result["forced_process_cleanup"] = forced
        result["browser_processes_after_close"] = len(leftovers)
        if leftovers:
            failure = failure or "Chromium processes remained after targeted cleanup"
        elif forced:
            failure = failure or "Playwright left Chromium processes requiring forced cleanup"
        shutil.rmtree(profile, ignore_errors=True)

    result["status"] = "FAIL" if failure else "PASS"
    if failure:
        result["error"] = failure
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
