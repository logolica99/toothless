#!/usr/bin/env python3
"""Disposable guest-local fixture for the browser cgroup IP boundary."""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import cast

POLICY = Path("/etc/toothless/browser-network-policy.json")
POLICY_SHA256 = "0f626085c1bc9121f2bccd32845bd215d1efc55618b885fda5dc5bca3804c873"
ALIASES = ("10.254.253.1", "100.64.253.1", "169.254.253.1", "169.254.169.254", "192.168.253.1")
IPV6_ALIASES = ("fd00::253", "fe80::253")


def _run(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False)


def _service_socket(family: int, address: str, scope: int = 0) -> socket.socket:
    server = socket.socket(family, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if family == socket.AF_INET:
        server.bind((address, 0))
    else:
        server.bind((address, 0, 0, scope))
    server.listen(8)
    server.settimeout(0.2)
    return server


def _serve(server: socket.socket, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            client, _ = server.accept()
            client.close()
        except TimeoutError:
            continue
        except OSError:
            return


def main() -> int:
    if not sys.platform.startswith("linux") or not Path("/run/systemd/system").exists():
        print("FAIL: guest is not running systemd on Linux")
        return 1
    policy_raw = POLICY.read_bytes()
    if hashlib.sha256(policy_raw).hexdigest() != POLICY_SHA256:
        print("FAIL: installed network policy hash mismatch")
        return 1
    policy_raw_value: object = json.loads(policy_raw)
    if not isinstance(policy_raw_value, dict):
        print("FAIL: installed network policy is invalid")
        return 1
    policy = cast(dict[str, object], policy_raw_value)
    allowed_raw = policy.get("allow")
    denied_raw = policy.get("deny")
    if not isinstance(allowed_raw, list) or not isinstance(denied_raw, list):
        print("FAIL: installed network policy schema is invalid")
        return 1
    allowed = cast(list[str], allowed_raw)
    denied = cast(list[str], denied_raw)
    if allowed != ["127.0.0.53/32", "127.0.0.54/32"] or "::/0" not in denied:
        print("FAIL: installed allow/deny boundary differs from reviewed policy")
        return 1

    added: list[tuple[str, str]] = []
    servers: list[socket.socket] = []
    threads: list[threading.Thread] = []
    stop = threading.Event()
    try:
        for address in ALIASES:
            result = _run(["sudo", "-n", "/sbin/ip", "address", "add", f"{address}/32", "dev", "lo"])
            if result.returncode != 0:
                raise RuntimeError(f"could not assign owned IPv4 fixture {address}: {result.stderr.decode(errors='replace')[:160]}")
            if result.returncode == 0:
                added.append(("-4", address))
        for address in IPV6_ALIASES:
            result = _run(["sudo", "-n", "/sbin/ip", "-6", "address", "add", f"{address}/128", "dev", "lo", "nodad"])
            if result.returncode != 0:
                raise RuntimeError(f"could not assign owned IPv6 fixture {address}: {result.stderr.decode(errors='replace')[:160]}")
            if result.returncode == 0:
                added.append(("-6", address))

        targets: list[dict[str, object]] = []
        address_to_listen = [(socket.AF_INET, "127.0.0.1", 0), *[(socket.AF_INET, address, 0) for address in ALIASES]]
        address_to_listen.extend((socket.AF_INET6, address, socket.if_nametoindex("lo") if address.startswith("fe80") else 0) for address in IPV6_ALIASES)
        for family, address, scope in address_to_listen:
            server = _service_socket(family, address, scope)
            servers.append(server)
            port = server.getsockname()[1]
            targets.append({"family": family, "address": address, "port": port, "scope": scope})
            thread = threading.Thread(target=_serve, args=(server, stop), daemon=True)
            thread.start()
            threads.append(thread)

        # Prove each owned endpoint is live from the authorized fixture process.
        for item in targets:
            family = cast(int, item["family"])
            address = cast(str, item["address"])
            port = cast(int, item["port"])
            scope = cast(int, item["scope"])
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                probe.settimeout(1)
                probe.connect((address, port, 0, scope) if family == socket.AF_INET6 else (address, port))
        targets.append({"family": socket.AF_INET6, "address": "::ffff:127.0.0.1", "port": targets[0]["port"], "scope": 0})
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as mapped_probe:
            mapped_probe.settimeout(1)
            mapped_probe.connect(("::ffff:127.0.0.1", cast(int, targets[-1]["port"]), 0, 0))

        child = r'''import json,socket,sys,urllib.request
targets=json.loads(sys.argv[1])
for item in targets:
    family=int(item["family"]); address=item["address"]; port=int(item["port"]); scope=int(item["scope"])
    sock=socket.socket(family,socket.SOCK_STREAM); sock.settimeout(0.7)
    try:
        sock.connect((address,port,0,scope) if family==socket.AF_INET6 else (address,port))
        print("BYPASS:"+address); sys.exit(31)
    except OSError as exc:
        print("BLOCKED:"+address+":"+type(exc).__name__)
    finally: sock.close()
socket.getaddrinfo("www.python.org",443,type=socket.SOCK_STREAM)
with urllib.request.urlopen("https://example.com/",timeout=12) as response:
    assert response.status == 200
print("PUBLIC_DNS_HTTPS_OK")
'''
        encoded_targets = json.dumps(targets, separators=(",", ":"))
        agent_command = ["/usr/sbin/runuser", "-u", "toothless-agent", "--", "/usr/bin/python3", "-c", child, encoded_targets]
        unit = "toothless-browser-boundary-probe"
        command = [
            "sudo", "-n", "/usr/bin/systemd-run", "--scope", "--quiet", "--collect", f"--unit={unit}",
            "--property=MemoryMax=1536M", "--property=TasksMax=128", "--property=CPUQuota=200%",
            "--property=IPAddressAllow=" + " ".join(allowed),
            "--property=IPAddressDeny=" + " ".join(denied),
            "--", *agent_command,
        ]
        result = _run(command)
        output = (result.stdout + result.stderr).decode("utf-8", errors="replace")[:16_000]
        if result.returncode != 0 or "PUBLIC_DNS_HTTPS_OK" not in output or "BYPASS:" in output or sum(line.startswith("BLOCKED:") for line in output.splitlines()) != len(targets):
            print("FAIL: filtered fixture result; all endpoints were independently reachable before filtering")
            print(f"exit={result.returncode}\n{output}")
            return 1
        print("PASS: each guest loopback/private/CGNAT/link-local/metadata/ULA/IPv6 fixture listener was reachable before filtering and blocked inside the exact systemd IP filter.")
        print("PASS: public DNS and HTTPS remained usable inside the filtered rootless process tree.")
        print(output)
        return 0
    finally:
        stop.set()
        for server in servers:
            server.close()
        for thread in threads:
            thread.join(timeout=1)
        for family, address in reversed(added):
            flag = ["sudo", "-n", "/sbin/ip", family, "address", "del", f"{address}/" + ("128" if family == "-6" else "32"), "dev", "lo"]
            _run(flag)


if __name__ == "__main__":
    raise SystemExit(main())
