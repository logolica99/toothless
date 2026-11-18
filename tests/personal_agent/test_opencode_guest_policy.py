from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ops.worker import opencode_task_runner
from ops.worker.opencode_task_runner import _events, _tool_policy_is_safe


def test_guest_config_allows_only_bounded_tools_and_denies_elevation() -> None:
    config = json.loads(Path("ops/opencode/phase05/opencode.json").read_text(encoding="utf-8"))
    permissions = config["permissions"]
    agent = config["agents"]["toothless-worker"]

    assert _tool_policy_is_safe(permissions)
    assert _tool_policy_is_safe(agent["permissions"])
    assert agent["steps"] == 12
    assert config["model"] == "opencode-go/glm-5.3"
    assert config["plugin"] == []
    assert config["mcp"] == {}
    assert hashlib.sha256(Path("ops/opencode/phase05/opencode.json").read_bytes()).hexdigest() == opencode_task_runner.CONFIG_SHA256


def test_browser_profiles_keep_loopback_allowance_separate_from_helper_filter() -> None:
    browser = json.loads(Path("ops/opencode/phase05/browser.json").read_text(encoding="utf-8"))
    strict = json.loads(Path("ops/opencode/phase05/browser-network-policy.json").read_text(encoding="utf-8"))
    control = json.loads(Path("ops/opencode/phase05/browser-control-network-policy.json").read_text(encoding="utf-8"))
    command = browser["mcp"]["servers"]["toothless_browser"]["command"]
    timeout = browser["mcp"]["servers"]["toothless_browser"]["timeout"]

    assert command[1].endswith("browser_mcp_bridge.py")
    assert timeout == {"startup": 20_000, "catalog": 20_000, "execution": 20_000}
    assert control["allow"] == ["127.0.0.1/32", *strict["allow"]]
    assert control["deny"] == strict["deny"]
    assert "127.0.0.0/8" in strict["deny"]
    assert hashlib.sha256(Path("ops/opencode/phase05/browser.json").read_bytes()).hexdigest() == opencode_task_runner.CONFIG_BROWSER_SHA256
    assert hashlib.sha256(Path("ops/opencode/phase05/browser-control-network-policy.json").read_bytes()).hexdigest() == opencode_task_runner.BROWSER_CONTROL_POLICY_SHA256


def test_guest_config_rejects_unapproved_tool_permission() -> None:
    config = json.loads(Path("ops/opencode/phase05/opencode.json").read_text(encoding="utf-8"))
    unsafe = [*config["permissions"], {"action": "webfetch", "resource": "*", "effect": "allow"}]

    assert not _tool_policy_is_safe(unsafe)


def test_guest_event_parser_records_tool_names_without_arguments() -> None:
    events = b'\n'.join(
        (
            b'{"type":"tool_use","part":{"tool":"write","state":{"input":{"filePath":"private.txt"}}}}',
            b'{"type":"tool_use","part":{"tool":"shell","state":{"input":{"command":"secret command"}}}}',
            b'{"type":"text","part":{"text":"Done."}}',
        )
    )

    result = _events(events, 0, "")

    assert result["status"] == "succeeded"
    assert result["tools"] == ["write", "shell"]
    assert result["event_types"] == ["tool_use", "tool_use", "text"]
    assert "secret command" not in json.dumps(result)


def test_guest_event_parser_classifies_transport_timeout_without_model_text() -> None:
    raw = b'{"type":"error","error":{"type":"Transport","message":"ETIMEDOUT: Unable to connect. Is the computer able to access the url?"}}'

    result = _events(raw, 0, "")

    assert result["status"] == "network_error"
    assert result["event_types"] == ["error"]
    assert result["tools"] == []
    assert result["text"] == ""


def test_guest_entry_point_refuses_non_root_callers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(opencode_task_runner.os, "geteuid", lambda: 999)

    assert opencode_task_runner.main() == 1
    assert "root-owned worker helper" in capsys.readouterr().out
