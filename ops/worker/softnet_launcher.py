#!/usr/bin/env python3
"""Start the fixed worker only after Softnet acknowledges host-egress isolation."""

from __future__ import annotations

import ipaddress
import json
import pathlib
import re
import select
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from typing import TypeAlias, cast

JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
WORKER_VM_NAME = "toothless-worker"
MODES: dict[str, list[str]] = {
    "headless": ["--no-graphics"],
    "console": ["--no-clipboard", "--no-usb-accessories", "--no-audio"],
    "serial": ["--no-graphics", "--serial", "--no-clipboard", "--no-usb-accessories", "--no-audio"],
}
CONTROL_TIMEOUT_SECONDS = 15
DNS_ENDPOINT_TIMEOUT_SECONDS = 45
ALLOWED_HOST_DNS = ("out proto udp @host port 55353", "out proto tcp @host port 55353")
LEGACY_HOST_DNS = ("out proto udp @host port 53", "out proto tcp @host port 53")


class SoftnetControlError(RuntimeError):
    pass


def apply_host_isolation_policy(
    channel: socket.socket,
    *,
    timeout: float = CONTROL_TIMEOUT_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, JsonValue]:
    policy = _rpc(channel, "softnet.policy.get", {}, 1, timeout, monotonic)
    allow = [rule for rule in _string_list(policy.get("allow"), "allow") if rule not in LEGACY_HOST_DNS]
    block = _string_list(policy.get("block"), "block")
    rule = "out @host"
    for dns_rule in ALLOWED_HOST_DNS:
        if dns_rule not in allow:
            allow.append(dns_rule)
    if rule not in block:
        block.append(rule)
    updated = _rpc(
        channel,
        "softnet.policy.set",
        {"allow": cast(JsonValue, allow), "block": cast(JsonValue, block)},
        2,
        timeout,
        monotonic,
    )
    verified = _rpc(channel, "softnet.policy.get", {}, 3, timeout, monotonic)
    verified_allow = _string_list(verified.get("allow"), "allow")
    verified_block = _string_list(verified.get("block"), "block")
    if (
        rule not in verified_block
        or not set(ALLOWED_HOST_DNS).issubset(verified_allow)
        or verified_allow != _normalized_rules(updated.get("allow"))
    ):
        raise SoftnetControlError("Softnet did not retain the required host isolation policy")
    return verified


def _rpc(
    channel: socket.socket,
    method: str,
    params: dict[str, JsonValue],
    request_id: int,
    timeout: float,
    monotonic: Callable[[], float],
) -> dict[str, JsonValue]:
    request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    try:
        channel.sendall(json.dumps(request, separators=(",", ":")).encode() + b"\n")
        deadline = monotonic() + timeout
        response = bytearray()
        while b"\n" not in response:
            remaining = deadline - monotonic()
            if remaining <= 0 or not select.select([channel], [], [], max(0, remaining))[0]:
                raise SoftnetControlError(f"Softnet control request timed out: {method}")
            chunk = channel.recv(65536)
            if not chunk:
                raise SoftnetControlError("Softnet control channel closed before acknowledging policy")
            response.extend(chunk)
            if len(response) > 1_048_576:
                raise SoftnetControlError("Softnet control response exceeded 1 MiB")
    except OSError as exc:
        raise SoftnetControlError(f"Softnet control channel failed: {exc}") from exc

    try:
        decoded: object = json.loads(bytes(response).split(b"\n", 1)[0])
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SoftnetControlError(f"Softnet returned malformed JSON for {method}") from exc
    if not isinstance(decoded, dict):
        raise SoftnetControlError(f"Softnet returned an invalid response for {method}")
    response_data = cast(dict[str, JsonValue], decoded)
    if response_data.get("id") != request_id:
        raise SoftnetControlError(f"Softnet returned an invalid response for {method}")
    if "error" in response_data:
        raise SoftnetControlError(f"Softnet rejected {method}: {response_data['error']!r}")
    result = response_data.get("result")
    if not isinstance(result, dict):
        raise SoftnetControlError(f"Softnet returned no result for {method}")
    return cast(dict[str, JsonValue], result)


def _string_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list):
        raise SoftnetControlError(f"Softnet returned an invalid {field} rule list")
    items = cast(list[JsonValue], value)
    if any(not isinstance(item, str) for item in items):
        raise SoftnetControlError(f"Softnet returned an invalid {field} rule list")
    return cast(list[str], items)


def _normalized_rules(value: object) -> list[str]:
    if not isinstance(value, list):
        raise SoftnetControlError("Softnet returned an invalid normalized policy")
    items = cast(list[JsonValue], value)
    if any(not isinstance(item, str) for item in items):
        raise SoftnetControlError("Softnet returned an invalid normalized policy")
    return sorted(set(cast(list[str], items)))


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _capture(args: list[str], *, timeout: float = 2.0) -> str:
    result = subprocess.run(args, capture_output=True, text=True, check=True, timeout=timeout)
    return result.stdout.strip()


def find_softnet_endpoint() -> tuple[str, str]:
    worker_ip = str(ipaddress.IPv4Address(_capture(["tart", "ip", WORKER_VM_NAME])))
    route = _capture(["route", "-n", "get", worker_ip])
    route_interface = re.search(r"(?m)^\s*interface:\s*(\S+)\s*$", route)
    if route_interface is None:
        raise SoftnetControlError("Host route to worker has no interface")
    interfaces = _capture(["ifconfig", "-l"]).split()
    matches: list[str] = []
    for interface in interfaces:
        details = _capture(["ifconfig", interface])
        if "member: vmenet0" not in details or interface != route_interface.group(1):
            continue
        address = re.search(r"(?m)^\s*inet\s+(\d+\.\d+\.\d+\.\d+)\s", details)
        if address is not None:
            matches.append(str(ipaddress.IPv4Address(address.group(1))))
    if len(matches) != 1:
        raise SoftnetControlError("Could not identify one live Softnet bridge from vmenet0 and the worker route")
    return matches[0], worker_ip


def wait_for_softnet_endpoint(
    *,
    timeout: float = DNS_ENDPOINT_TIMEOUT_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, str]:
    deadline = monotonic() + timeout
    last_error: Exception | None = None
    while monotonic() < deadline:
        try:
            return find_softnet_endpoint()
        except (OSError, ValueError, subprocess.SubprocessError, SoftnetControlError) as exc:
            last_error = exc
            sleep(0.25)
    raise SoftnetControlError(f"Timed out identifying the live Softnet bridge: {last_error}")


def start_dns_forwarder(bind_address: str, client_address: str) -> subprocess.Popen[str]:
    script = pathlib.Path(__file__).with_name("dns_forwarder.py")
    process = subprocess.Popen(
        [sys.executable, str(script), "--bind", bind_address, "--client", client_address],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    try:
        if process.stdout is None:
            raise SoftnetControlError("DNS forwarder has no readiness channel")
        deadline = time.monotonic() + CONTROL_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if process.poll() is not None:
                details = process.stdout.readline().strip()
                raise SoftnetControlError(f"DNS forwarder exited before readiness: {details}")
            readable, _, _ = select.select([process.stdout], [], [], 0.1)
            if readable:
                line = process.stdout.readline().strip()
                if line.startswith(f"DNS forwarder ready on {bind_address}:55353 for {client_address}"):
                    return process
                if line:
                    raise SoftnetControlError(f"DNS forwarder startup failed: {line}")
        raise SoftnetControlError("DNS forwarder readiness timed out")
    except Exception:
        _terminate_text(process)
        raise


def _terminate_text(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def run_worker(mode: str) -> int:
    if mode not in MODES:
        print("Usage: softnet_launcher.py {headless|console|serial}", file=sys.stderr)
        return 2

    parent, child = socket.socketpair()
    command = [
        "tart",
        "run",
        *MODES[mode],
        "--net-softnet-control-fd",
        str(child.fileno()),
        WORKER_VM_NAME,
    ]
    process = subprocess.Popen(command, pass_fds=(child.fileno(),))
    child.close()
    dns_forwarder: subprocess.Popen[str] | None = None
    try:
        policy = apply_host_isolation_policy(parent)
        bind_address, client_address = wait_for_softnet_endpoint()
        dns_forwarder = start_dns_forwarder(bind_address, client_address)
        print(
            "Softnet isolation acknowledged: blocks new guest flows to @host; "
            f"{len(_string_list(policy.get('block'), 'block'))} block and "
            f"{len(_string_list(policy.get('allow'), 'allow'))} allow rule(s).",
            flush=True,
        )
        print(f"Trusted DNS forwarding active at {bind_address}:55353; direct host port 53 blocked.", flush=True)
        while process.poll() is None:
            if dns_forwarder.poll() is not None:
                raise SoftnetControlError("Trusted DNS forwarder exited while the worker was running")
            time.sleep(0.2)
        return process.returncode or 0
    except (SoftnetControlError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"Worker startup blocked: {exc}", file=sys.stderr, flush=True)
        _terminate(process)
        return 1
    except KeyboardInterrupt:
        _terminate(process)
        return 130
    finally:
        parent.close()
        if dns_forwarder is not None:
            _terminate_text(dns_forwarder)
        if process.poll() is None:
            _terminate(process)


if __name__ == "__main__":
    raise SystemExit(run_worker(sys.argv[1] if len(sys.argv) == 2 else ""))
