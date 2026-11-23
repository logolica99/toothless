"""Read-only, bounded readiness checks for the protected guest lifecycle."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, cast

WORKER_NAME = "toothless-worker"
TART_HOME = "/Volumes/toothless/Tools/Tart"
TART_PATH = "/usr/local/bin:/Volumes/toothless/Tools/bin:/usr/bin:/bin"
MARKER = Path("~/.local/share/toothless/worker/protected-launch.json").expanduser()


def _run(args: list[str], *, timeout: float = 5) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, check=False,
        env={"TART_HOME": TART_HOME, "PATH": TART_PATH, "HOME": str(Path.home()), "LANG": "C.UTF-8"},
    )


def worker_readiness() -> dict[str, Any]:
    result: dict[str, Any] = {
        "vm": "unavailable", "softnet_policy": "unverified", "dns_relay": "unverified",
        "guest_dns": "unverified", "opencode_auth": "unverified", "execution_agent": "unverified",
        "workspace_profile": "unverified", "browser_profile": "unverified", "worker_ready": False,
    }
    try:
        tart = _run(["tart", "get", WORKER_NAME], timeout=8)
        if tart.returncode != 0:
            return result
        lines = tart.stdout.splitlines()
        state = lines[1].split()[-1].lower() if len(lines) > 1 else "unknown"
        result["vm"] = state if state in {"running", "stopped"} else "unknown"
        if state != "running":
            return result
    except (OSError, subprocess.TimeoutExpired):
        return result

    try:
        info = MARKER.lstat()
        parent = MARKER.parent.stat()
        if (not MARKER.is_symlink() and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) & 0o077 == 0
                and parent.st_uid == os.getuid() and stat.S_IMODE(parent.st_mode) & 0o077 == 0
                and info.st_size <= 4096):
            decoded: object = json.loads(MARKER.read_text(encoding="utf-8"))
            marker = cast(dict[str, object], decoded) if isinstance(decoded, dict) else {}
        else:
            marker = {}
    except (OSError, ValueError, json.JSONDecodeError):
        marker = {}
    now = time.time()
    heartbeat = marker.get("heartbeat")
    pid = marker.get("pid")
    protected = (
        isinstance(heartbeat, (int, float)) and now - heartbeat <= 15
        and isinstance(pid, int) and _pid_alive(pid)
        and marker.get("policy_verified") is True
    )
    if not protected:
        return result
    result["softnet_policy"] = "verified"
    result["dns_relay"] = "ready" if marker.get("dns_relay_ready") is True else "unavailable"
    result["guest_dns"] = "ready" if marker.get("guest_dns_verified") is True else "unavailable"

    probe = [
        "tart", "exec", WORKER_NAME, "sudo", "-n", "/usr/bin/python3",
        "/usr/local/libexec/toothless/opencode-task-runner", "--readiness",
    ]
    try:
        guest = _run(probe, timeout=12)
        if guest.returncode != 0 or len(guest.stdout) > 8192:
            return result
        payload = json.loads(guest.stdout)
        if not isinstance(payload, dict):
            return result
        guest_data = cast(dict[str, object], payload)
        for key in ("opencode_auth", "execution_agent", "workspace_profile", "browser_profile", "guest_dns"):
            value = guest_data.get(key)
            if isinstance(value, str) and value in {"ready", "unavailable", "not_configured"}:
                result[key] = value
        result["worker_ready"] = all(
            result[key] == expected for key, expected in (
                ("vm", "running"), ("softnet_policy", "verified"), ("dns_relay", "ready"),
                ("guest_dns", "ready"), ("opencode_auth", "ready"),
                ("execution_agent", "ready"), ("workspace_profile", "ready"),
                ("browser_profile", "ready"),
            )
        )
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        pass
    return result


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
