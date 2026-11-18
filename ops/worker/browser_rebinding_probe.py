#!/usr/bin/env python3
"""Owned DNS-rebinding simulation against the guest browser cgroup boundary."""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import threading
from pathlib import Path
from typing import cast

POLICY = Path("/etc/toothless/browser-network-policy.json")
POLICY_SHA256 = "0f626085c1bc9121f2bccd32845bd215d1efc55618b885fda5dc5bca3804c873"
FIXTURE_IP = "169.254.169.254"
FIXTURE_HOST = "rebind.toothless.test"


def main() -> int:
    policy_raw = POLICY.read_bytes()
    if hashlib.sha256(policy_raw).hexdigest() != POLICY_SHA256:
        raise SystemExit("FAIL: network policy hash mismatch")
    policy_raw_value: object = json.loads(policy_raw)
    if not isinstance(policy_raw_value, dict):
        raise SystemExit("FAIL: network policy invalid")
    policy = cast(dict[str, object], policy_raw_value)
    allow = cast(list[str], policy["allow"])
    deny = cast(list[str], policy["deny"])
    alias = subprocess.run(["sudo", "-n", "/sbin/ip", "address", "add", f"{FIXTURE_IP}/32", "dev", "lo"], capture_output=True, check=False)
    if alias.returncode != 0:
        raise SystemExit("FAIL: could not assign the owned metadata-shaped address: " + alias.stderr.decode(errors="replace")[:200])
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((FIXTURE_IP, 0))
    listener.listen(4)
    listener.settimeout(0.2)
    port = listener.getsockname()[1]
    accepted = 0
    stop = threading.Event()

    def serve() -> None:
        nonlocal accepted
        while not stop.is_set():
            try:
                client, _ = listener.accept()
                accepted += 1
                client.sendall(b"OWNED_FIXTURE_REACHED")
                client.close()
            except TimeoutError:
                continue
            except OSError:
                return

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        # Prove the test server is alive and reachable from the authorized setup process.
        with socket.create_connection((FIXTURE_IP, port), timeout=1) as probe:
            assert probe.recv(100) == b"OWNED_FIXTURE_REACHED"
        baseline_connections = accepted
        source = f'''import sys
sys.path.insert(0, "/usr/local/libexec/toothless")
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright
import browser_mcp
host={FIXTURE_HOST!r}
original=browser_mcp._public_url
browser_mcp._public_url=lambda url: url if urlsplit(url).hostname == host else original(url)
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True, executable_path=str(browser_mcp.CHROMIUM), args=["--host-resolver-rules=MAP " + host + " {FIXTURE_IP}"])
    context=browser.new_context()
    context.route("**/*", browser_mcp._route_guard)
    page=context.new_page()
    try:
        page.goto("http://" + host + ":{port}/", wait_until="domcontentloaded", timeout=6000)
    except Exception as exc:
        print("DNS_REBIND_BLOCKED:" + type(exc).__name__)
    else:
        print("DNS_REBIND_BYPASS:" + page.url + ":" + page.locator("body").inner_text())
        sys.exit(31)
    finally:
        context.close()
        browser.close()
'''
        unit = "toothless-browser-rebinding-probe"
        command = [
            "sudo", "-n", "/usr/bin/systemd-run", "--scope", "--quiet", "--collect", f"--unit={unit}",
            "--property=MemoryMax=1536M", "--property=TasksMax=128", "--property=CPUQuota=200%",
            "--property=IPAddressAllow=" + " ".join(allow),
            "--property=IPAddressDeny=" + " ".join(deny),
            "--", "/usr/sbin/runuser", "-u", "toothless-agent", "--",
            "/home/toothless-agent/.local/share/toothless/browser-smoke/venv/bin/python", "-c", source,
        ]
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=False)
        output = (result.stdout + result.stderr).decode(errors="replace")
        if result.returncode != 0 or "DNS_REBIND_BLOCKED" not in output:
            print("FAIL: browser did not confirm private-answer denial")
            print(f"exit={result.returncode}\n{output[:2000]}")
            return 1
        if accepted != baseline_connections:
            print("FAIL: filtered browser reached the independently reachable metadata-shaped listener")
            return 1
        print("PASS: controlled rebinding simulation returned a public validation result to the helper, mapped Chromium to the owned metadata-shaped IP, and the kernel filter blocked the connection.")
        print(output.strip())
        return 0
    finally:
        stop.set()
        listener.close()
        thread.join(timeout=1)
        subprocess.run(["sudo", "-n", "/sbin/ip", "address", "del", f"{FIXTURE_IP}/32", "dev", "lo"], capture_output=True, check=False)


if __name__ == "__main__":
    raise SystemExit(main())
