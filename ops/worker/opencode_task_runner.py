#!/usr/bin/env python3
"""Fixed, bounded OpenCode entry point installed in the Ubuntu guest."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, cast

MODEL = "opencode-go/glm-5.3"
VERSION = "2.3.0"
CONFIG = Path("/etc/toothless/opencode.json")
CONFIG_SHA256 = "85f4457e7ed330262e3797bceef759b68938042dc757977f764365fa5aa20eff"
WORKSPACE = "/home/admin/opencode-task-workspace"
OPENCODE = "/usr/local/bin/opencode"
MAX_REQUEST_BYTES = 16_384
MAX_OUTPUT_BYTES = 1_048_576
MAX_EVENT_BYTES = 65_536
MAX_RESULT_BYTES = 48_000
TIMEOUT_SECONDS = 120
_child: subprocess.Popen[bytes] | None = None
_config_diagnostics: list[str] = []


def _kill_child(_signum: int, _frame: object) -> None:
    child = _child
    if child is None or child.poll() is not None:
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=1)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()


def _clean_error(value: str) -> str:
    value = re.sub(r"(?i)(api[_-]?key|token|authorization)(\s*[:=]\s*)\S+", r"\1\2[redacted]", value)
    return " ".join(value.split())[:600]


def _events(raw: bytes, returncode: int, stderr: str) -> dict[str, Any]:
    if len(raw) > MAX_OUTPUT_BYTES:
        return {"status": "output_limited", "error": "OpenCode output exceeded the configured limit."}
    text_parts: list[str] = []
    errors: list[str] = []
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
    elif any(term in normalized for term in ("network", "connection", "dns", "fetch failed")):
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
    return {"status": status, "text": result, "error": _clean_error(error_text), "event_count": event_count}


def _verify_effective_config(env: dict[str, str]) -> bool:
    _config_diagnostics.clear()
    try:
        version_result = subprocess.run(
            [OPENCODE, "--version"], cwd=WORKSPACE, env=env,
            stdin=subprocess.DEVNULL, capture_output=True, timeout=8, check=False,
        )
        version = (version_result.stdout + version_result.stderr).decode("utf-8", errors="strict")
        if not re.search(r"(^|[^0-9])2[.]3[.]0([^0-9]|$)", version):
            detail = re.sub(r"(?i)(api[_-]?key|token|authorization)(\s*[:=]\s*)\S+", r"\1\2[redacted]", " ".join(version.split()))
            _config_diagnostics.append("version_probe:" + (detail[:120] or "empty"))
            return False
    except subprocess.CalledProcessError as exc:
        _config_diagnostics.append(f"version_command_exit_{exc.returncode}")
        return False
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        _config_diagnostics.append("version_command_failed")
        return False
    try:
        result = subprocess.run(
            [OPENCODE, "debug", "config"], cwd=WORKSPACE, env=env,
            stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=True,
        )
        if len(result.stdout) > 262_144:
            _config_diagnostics.append("debug_output_over_limit")
            return False
        config_raw: object = json.loads(result.stdout.decode("utf-8"))
    except subprocess.CalledProcessError as exc:
        _config_diagnostics.append(f"debug_config_exit_{exc.returncode}")
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
            if path == str(CONFIG) and isinstance(info, dict):
                loaded.append(source)
            elif path == "/home/admin/.toothless-opencode-config/opencode" and info is None:
                # OpenCode reports the empty XDG config directory as a source after auth login.
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
    if not _deny_all(permissions):
        shape = _permission_summary(permissions)
        _config_diagnostics.append("global_deny_all_not_effective:" + shape)
        return False
    model_config = config.get("model")
    mcp_config = config.get("mcp")
    small_model = config.get("small_model", config.get("smallModel"))
    if (
        not _approved_model(model_config)
        or (small_model is not None and not _approved_model(small_model))
        or config.get("update") != "disable"
        or plugins != []
        or mcp_config != {}
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
        _config_diagnostics.append(f"model_or_extensions_mismatch:model={model_summary};plugins={plugin_summary};mcp={mcp_summary}")
        return False
    agents = cast(dict[str, object], agents_raw)
    agent_raw = agents.get("toothless-readonly")
    if not isinstance(agent_raw, dict):
        _config_diagnostics.append("readonly_agent_missing")
        return False
    agent = cast(dict[str, object], agent_raw)
    if not _approved_model(agent.get("model")):
        _config_diagnostics.append("agent_model_mismatch")
        return False
    agent_permissions = agent.get("permissions", agent.get("permission"))
    if not _deny_all(agent_permissions):
        _config_diagnostics.append("agent_deny_all_not_effective:" + _permission_summary(agent_permissions))
        return False
    return True


def _deny_all(value: object) -> bool:
    if isinstance(value, dict):
        permissions = cast(dict[str, object], value)
        return permissions.get("*") == "deny"
    if isinstance(value, list):
        permission_values = cast(list[object], value)
        rules: list[dict[str, object]] = []
        for item in permission_values:
            if not isinstance(item, dict):
                return False
            rules.append(cast(dict[str, object], item))
        return len(rules) == len(permission_values) and any(
            rule.get("resource") == "*" and rule.get("action") == "*" and rule.get("effect") == "deny"
            for rule in rules
        ) and all(rule.get("effect") == "deny" for rule in rules)
    return False


def _approved_model(value: object) -> bool:
    if value == MODEL:
        return True
    if isinstance(value, dict):
        model = cast(dict[str, object], value)
        return model.get("providerID") == "opencode-go" and model.get("model") == "glm-5.3"
    return False


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
    global _child
    signal.signal(signal.SIGTERM, _kill_child)
    signal.signal(signal.SIGINT, _kill_child)
    if sys.argv[1:] == ["--verify-config"]:
        try:
            valid_hash = hashlib.sha256(CONFIG.read_bytes()).hexdigest() == CONFIG_SHA256
        except OSError:
            valid_hash = False
        env = {
            "HOME": "/home/admin",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "XDG_CONFIG_HOME": "/home/admin/.toothless-opencode-config",
            "XDG_DATA_HOME": "/home/admin/.toothless-opencode-data",
            "XDG_STATE_HOME": "/home/admin/.toothless-opencode-state",
            "XDG_CACHE_HOME": "/home/admin/.toothless-opencode-cache",
            "OPENCODE_CONFIG": str(CONFIG),
        }
        if valid_hash and _verify_effective_config(env):
            print("PASS: OpenCode version, model, and deny-all tools configuration verified.")
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
    if request.get("model") != MODEL or not isinstance(prompt, str):
        return _emit({"status": "failed", "error": "Task request does not match the approved model policy."})
    config_bytes = CONFIG.read_bytes()
    if hashlib.sha256(config_bytes).hexdigest() != CONFIG_SHA256:
        return _emit({"status": "failed", "error": "The reviewed deny-all OpenCode configuration is missing or changed."})

    command = [
        OPENCODE, "run", "--standalone", "--archived", "--format", "json",
        "--agent", "toothless-readonly", "--model", MODEL, prompt,
    ]
    env = {
        "HOME": "/home/admin",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "XDG_CONFIG_HOME": "/home/admin/.toothless-opencode-config",
        "XDG_DATA_HOME": "/home/admin/.toothless-opencode-data",
        "XDG_STATE_HOME": "/home/admin/.toothless-opencode-state",
        "XDG_CACHE_HOME": "/home/admin/.toothless-opencode-cache",
        "OPENCODE_CONFIG": str(CONFIG),
    }
    if not _verify_effective_config(env):
        return _emit({"status": "failed", "error": "Effective OpenCode version or deny-all settings failed verification."})
    started = time.monotonic()
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            _child = subprocess.Popen(
                command,
                cwd=WORKSPACE,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
            )
        except OSError:
            return _emit({"status": "failed", "error": "OpenCode could not be started in the worker."})
        reason = ""
        while _child.poll() is None:
            if time.monotonic() - started >= TIMEOUT_SECONDS:
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
