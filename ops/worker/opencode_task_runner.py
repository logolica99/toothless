#!/usr/bin/env python3
"""Fixed, bounded OpenCode entry point installed in the Ubuntu guest."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import pwd
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, cast

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
AGENT = "toothless-worker"
AGENT_BROWSER = "toothless-browser"
AGENT_USER = "toothless-agent"
AGENT_HOME = "/home/toothless-agent"
CONFIG_HOME = "/home/toothless-agent/.config"
WORKSPACE = "/home/toothless-agent/workspace"
OPENCODE = "/usr/local/bin/opencode"
BROWSER_MCP = "/usr/local/libexec/toothless/browser_mcp.py"
BROWSER_MCP_PYTHON = "/home/toothless-agent/.local/share/toothless/browser-smoke/venv/bin/python"
MAX_REQUEST_BYTES = 16_384
MAX_OUTPUT_BYTES = 1_048_576
MAX_EVENT_BYTES = 65_536
MAX_RESULT_BYTES = 48_000
TIMEOUT_SECONDS = 120
FILE_TASK_TIMEOUT_SECONDS = 300
BROWSER_TIMEOUT_SECONDS = 300
_child: subprocess.Popen[bytes] | None = None
_scope_unit: str | None = None
_browser_helper_unit: str | None = None
_browser_helper_process: subprocess.Popen[bytes] | None = None
_browser_helper_directory: Path | None = None
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


def _clean_error(value: str) -> str:
    value = re.sub(r"(?i)(api[_-]?key|token|authorization)(\s*[:=]\s*)\S+", r"\1\2[redacted]", value)
    return " ".join(value.split())[:600]


def _events(raw: bytes, returncode: int, stderr: str) -> dict[str, Any]:
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
            return {"status": "output_limited", "error": "OpenCode event exceeded the configured limit."}
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
    }


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
    global _browser_helper_unit, _browser_helper_process, _browser_helper_directory
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
        shutil.rmtree(directory, ignore_errors=True)
    _browser_helper_unit = None
    _browser_helper_process = None
    _browser_helper_directory = None


def _start_browser_helper(execution_id: str, env: dict[str, str]) -> str:
    global _browser_helper_unit, _browser_helper_process, _browser_helper_directory
    _browser_network_properties()
    account = pwd.getpwnam(AGENT_USER)
    safe_id = re.sub(r"[^a-zA-Z0-9_.-]", "-", execution_id)[:48]
    directory = Path(tempfile.mkdtemp(prefix=f"toothless-browser-{safe_id}-", dir="/run"))
    os.chown(directory, account.pw_uid, account.pw_gid)
    os.chmod(directory, 0o700)
    socket_path = directory / "mcp.sock"
    ready_path = directory / "ready"
    unit = "toothless-browser-mcp-" + safe_id
    helper_env = {
        **env,
        "TOOTHLESS_BROWSER_SOCKET": str(socket_path),
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


def _approved_model(value: object) -> bool:
    if value == MODEL:
        return True
    if isinstance(value, dict):
        model = cast(dict[str, object], value)
        return model.get("providerID") == "opencode-go" and model.get("model") == "glm-5.3"
    return False


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
        try:
            env["TOOTHLESS_BROWSER_SOCKET"] = _start_browser_helper(browser_runtime_id, env)
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
        while _child.poll() is None:
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
        if browser:
            _stop_browser_helper()
        if reason:
            return _emit({"status": reason, "error": f"OpenCode {reason.replace('_', ' ')}."})
        stdout_file.seek(0)
        raw = stdout_file.read(MAX_OUTPUT_BYTES + 1)
        stderr_file.seek(0)
        stderr = stderr_file.read(8192).decode("utf-8", errors="replace")
    answer = _events(raw, returncode, stderr)
    answer["model"] = MODEL
    answer["duration_seconds"] = round(time.monotonic() - started, 3)
    return _emit(answer)


def _emit(value: dict[str, Any]) -> int:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
