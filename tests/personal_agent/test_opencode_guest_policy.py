from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops.worker import opencode_task_runner
from ops.worker.opencode_task_runner import (
    _events,
    _partial_event_metadata,
    _task_timeout,
    _tool_policy_is_safe,
)


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


def test_browser_source_manifest_requires_private_helper_directory_and_bounds_metadata(tmp_path: Path) -> None:
    directory = tmp_path / "toothless-browser-test"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    source = directory / "sources.json"
    source.write_text(json.dumps({"version": 1, "sources": [
        {"title": "AI", "url": "https://blogs.example.com/tags/ai", "label": "AI",
         "http_status": 200, "verification_status": "observed"},
        {"title": "Guess", "url": "https://blogs.example.com/tags/guess", "label": "Guess",
         "http_status": None, "verification_status": "inferred"},
        {"title": "Private", "url": "http://127.0.0.1/", "label": "Private",
         "http_status": 200, "verification_status": "observed"},
    ]}), encoding="utf-8")
    source.chmod(0o600)

    result = opencode_task_runner._read_browser_source_manifest(directory, owner_uid=directory.stat().st_uid)

    assert result == [{
        "title": "AI", "url": "https://blogs.example.com/tags/ai", "label": "AI", "http_status": 200,
        "verification_status": "observed",
    }]
    source.chmod(0o640)
    assert opencode_task_runner._read_browser_source_manifest(directory, owner_uid=directory.stat().st_uid) == []


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


def test_file_task_gets_a_fixed_extended_deadline_only_when_host_marked() -> None:
    assert _task_timeout({"profile": "worker"}, browser=False) == 120
    assert _task_timeout(
        {"profile": "worker", "file_task": True, "timeout_seconds": 300}, browser=False
    ) == 300
    assert _task_timeout({"profile": "browser"}, browser=True) == 300
    with pytest.raises(ValueError, match="not authorized"):
        _task_timeout({"profile": "worker", "timeout_seconds": 300}, browser=False)
    with pytest.raises(ValueError, match="fixed"):
        _task_timeout(
            {"profile": "browser", "file_task": True, "timeout_seconds": 300}, browser=True
        )


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


def test_browser_tool_failure_keeps_only_structured_sanitized_diagnostics() -> None:
    execution_id = "opencode-" + "f" * 32
    marker = {
        "failure_category": "browser_target_crashed",
        "tool_name": "navigate",
        "invocation_sequence": 2,
        "session_state": "invalidated",
        "browser_connected": False,
        "browser_process_alive": False,
        "exception_class": "TargetCrashedError",
        "elapsed_ms": 148,
        "execution_id": execution_id,
    }
    state = {
        "status": "error",
        "output": "RuntimeError: TOOTHLESS_BROWSER_DIAGNOSTIC:" + json.dumps(marker)
        + " private page text https://example.com/?secret=token",
    }
    raw = json.dumps({
        "type": "tool_use",
        "part": {"tool": "toothless_browser_navigate", "state": state},
    }, separators=(",", ":")).encode()

    diagnostics = _events(raw, 0, "", execution_id=execution_id)["diagnostics"]

    assert diagnostics["execution_id"] == execution_id
    assert diagnostics["browser_tool_failure_count"] == 1
    assert diagnostics["timeline"][0] == {
        "sequence": 1,
        "type": "tool_use",
        "activity": "browser_failure",
        "tool": "toothless_browser_navigate",
        "tool_status": "error",
        "failure_category": "browser_target_crashed",
        "exception_class": "TargetCrashedError",
        "tool_name": "navigate",
        "invocation_sequence": 2,
        "elapsed_ms": 148,
        "session_state": "invalidated",
        "browser_connected": False,
        "browser_process_alive": False,
        "execution_id": execution_id,
    }
    serialized = json.dumps(diagnostics)
    assert "private page text" not in serialized
    assert "secret=token" not in serialized


def test_fatal_browser_signal_is_task_bound_and_keeps_only_lifecycle_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = "task-" + "a" * 32
    execution_id = "opencode-" + "b" * 32
    runtime = tmp_path / "toothless-browser-test"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    fatal_path = runtime / "fatal.json"
    fatal_path.write_text(json.dumps({
        "version": 1,
        "task_id": task_id,
        "execution_id": execution_id,
        "failure_category": "browser_context_closed",
        "page_text": "private page contents must be discarded",
        "lifecycle": [{
            "event": "browser_disconnected", "timestamp": "2026-11-21T20:00:00.000Z",
            "browser_connected": False, "browser_process_alive": False,
            "browser_termination": "process_disappeared", "fd_count": 9,
        }],
    }), encoding="utf-8")
    fatal_path.chmod(0o600)
    real_path = Path
    monkeypatch.setattr(opencode_task_runner, "Path", lambda value: tmp_path if value == "/run" else real_path(value))
    monkeypatch.setattr(opencode_task_runner, "_browser_fatal_signal_path", fatal_path)
    monkeypatch.setattr(opencode_task_runner.pwd, "getpwnam", lambda _name: SimpleNamespace(pw_uid=fatal_path.stat().st_uid))

    signal_data = opencode_task_runner._read_browser_fatal_signal(task_id, execution_id)
    wrong_task = opencode_task_runner._read_browser_fatal_signal("task-" + "c" * 32, execution_id)

    assert signal_data == {
        "task_id": task_id,
        "execution_id": execution_id,
        "failure_category": "browser_context_closed",
        "lifecycle": [{
            "event": "browser_disconnected", "timestamp": "2026-11-21T20:00:00.000Z",
            "browser_connected": False, "browser_process_alive": False,
            "browser_termination": "process_disappeared", "fd_count": 9,
        }],
    }
    assert wrong_task is None
    assert "private page contents" not in json.dumps(signal_data)


def test_fatal_browser_abort_preserves_bounded_partial_text_as_incomplete() -> None:
    execution_id = "opencode-" + "d" * 32
    task_id = "task-" + "e" * 32
    partial = b'{"type":"text","part":{"text":"Visited one page; categories remain unverified."}}\n'
    signal_data = {
        "task_id": task_id, "execution_id": execution_id,
        "failure_category": "browser_context_closed",
        "lifecycle": [{"event": "browser_disconnected", "browser_process_alive": False}],
    }

    result = opencode_task_runner._fatal_browser_abort_result(
        partial, execution_id=execution_id, task_id=task_id, signal_data=signal_data, elapsed=3.25,
    )

    assert result["status"] == "incomplete", result
    assert result["text"] == "Visited one page; categories remain unverified."
    assert result["diagnostics"]["failure_category"] == "fatal_browser_session"
    assert result["diagnostics"]["fatal_browser_session"]["failure_category"] == "browser_context_closed"
    assert result["diagnostics"]["retry_count"] == 0
    assert "not retried" in result["error"]


def test_runner_stops_child_promptly_when_task_bound_fatal_signal_arrives(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    task_id = "task-" + "f" * 32
    execution_id = "opencode-" + "a" * 32
    request = {
        "model": opencode_task_runner.MODEL, "profile": "browser", "visible_browser": True,
        "task_id": task_id, "execution_id": execution_id, "prompt": "Read one public page.",
    }

    class Child:
        pid = 12345
        returncode: int | None = None
        polls = 0

        def poll(self) -> int | None:
            self.polls += 1
            return self.returncode

        def wait(self) -> int:
            assert self.returncode is not None
            return self.returncode

    child = Child()
    signal_data = {
        "task_id": task_id, "execution_id": execution_id,
        "failure_category": "browser_context_closed", "lifecycle": [],
    }
    fatal_reads = iter((None, signal_data))
    killed: list[int] = []
    helper_stopped: list[bool] = []
    monkeypatch.setattr(opencode_task_runner.os, "geteuid", lambda: 0)
    monkeypatch.setattr(opencode_task_runner.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(
        opencode_task_runner.sys, "argv",
        ["opencode-task-runner", base64.b64encode(json.dumps(request).encode()).decode()],
    )
    browser_config = Path("ops/opencode/phase05/browser.json").resolve()
    monkeypatch.setattr(opencode_task_runner, "CONFIG_BROWSER", browser_config)
    monkeypatch.setattr(opencode_task_runner, "CONFIG_BROWSER_SHA256", hashlib.sha256(browser_config.read_bytes()).hexdigest())
    monkeypatch.setattr(opencode_task_runner, "_has_active_opencode_go_credential", lambda: True)
    monkeypatch.setattr(opencode_task_runner, "_verify_effective_config", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(opencode_task_runner, "_browser_control_network_properties", lambda: [])
    monkeypatch.setattr(opencode_task_runner, "_authorize_visible_browser", lambda *_args: None)
    monkeypatch.setattr(opencode_task_runner, "_start_browser_helper", lambda *_args, **_kwargs: "/run/test.sock")
    monkeypatch.setattr(opencode_task_runner, "_stop_browser_helper", lambda: helper_stopped.append(True))
    monkeypatch.setattr(opencode_task_runner, "_read_browser_fatal_signal", lambda *_args: next(fatal_reads))
    monkeypatch.setattr(opencode_task_runner.subprocess, "Popen", lambda *_args, **_kwargs: child)

    def stop_child(_signum: int, _frame: object) -> None:
        killed.append(child.pid)
        child.returncode = -15

    monkeypatch.setattr(opencode_task_runner, "_kill_child", stop_child)

    assert opencode_task_runner.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "incomplete", result
    assert result["diagnostics"]["fatal_browser_session"]["failure_category"] == "browser_context_closed"
    assert child.polls == 2
    assert killed == [child.pid]
    assert helper_stopped == [True]


def test_guest_event_size_limit_is_exact_bytes_and_keeps_bounded_header_only() -> None:
    execution_id = "opencode-" + "a" * 32
    prefix = b'{"type":"tool_use","part":{"tool":"toothless_browser_extract_text"},"padding":"'
    suffix = b'"}'
    exact = prefix + b"x" * (opencode_task_runner.MAX_EVENT_BYTES - len(prefix) - len(suffix)) + suffix
    assert len(exact) == opencode_task_runner.MAX_EVENT_BYTES
    assert _events(exact, 0, "", execution_id=execution_id)["status"] == "succeeded"

    oversized = prefix + b"SECRET_PAGE_TEXT" + b"x" * (
        opencode_task_runner.MAX_EVENT_BYTES + 1 - len(prefix) - len(suffix) - len(b"SECRET_PAGE_TEXT")
    ) + suffix
    assert len(oversized) == opencode_task_runner.MAX_EVENT_BYTES + 1
    result = _events(oversized, 0, "", execution_id=execution_id)

    assert result["status"] == "output_limited"
    diagnostics = result["diagnostics"]
    assert diagnostics["failure_category"] == "event_size_limit"
    assert diagnostics["event_count"] == 0
    assert diagnostics["oversized_event"] == {
        "sequence": 1,
        "observed_bytes": opencode_task_runner.MAX_EVENT_BYTES + 1,
        "limit_bytes": opencode_task_runner.MAX_EVENT_BYTES,
        "event_type": "tool_use",
        "failure_category": "event_size_limit",
        "execution_id": execution_id,
        "tool_name": "toothless_browser_extract_text",
    }
    assert "SECRET_PAGE_TEXT" not in json.dumps(result)


def test_guest_event_size_uses_encoded_utf8_bytes() -> None:
    line = json.dumps({"type": "step_start", "padding": "é" * 33_000}, ensure_ascii=False).encode("utf-8")
    assert len(line) > opencode_task_runner.MAX_EVENT_BYTES
    result = _events(line, 0, "")
    assert result["status"] == "output_limited"
    assert result["diagnostics"]["oversized_event"]["observed_bytes"] == len(line)
    assert "é" not in json.dumps(result)


def test_guest_event_size_counts_json_escape_expansion() -> None:
    value = {"type": "text", "part": {"text": "\n" * 34_000}}
    line = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert len(line) > opencode_task_runner.MAX_EVENT_BYTES
    result = _events(line, 0, "")
    assert result["status"] == "output_limited"
    assert result["diagnostics"]["oversized_event"]["event_type"] == "text"
    assert result["diagnostics"]["oversized_event"]["observed_bytes"] == len(line)
    assert "\\n" not in json.dumps(result)


def test_total_output_cap_records_lower_bound_for_oversized_event_without_contents() -> None:
    execution_id = "opencode-" + "e" * 32
    prefix = b'{"type":"tool_use","part":{"tool":"toothless_browser_extract_text"},"padding":"'
    raw = prefix + b"PRIVATE_PAGE" + b"x" * (opencode_task_runner.MAX_OUTPUT_BYTES + 1 - len(prefix) - len(b"PRIVATE_PAGE"))

    metadata = _partial_event_metadata(raw, execution_id)

    assert metadata["failure_category"] == "event_size_limit"
    assert metadata["oversized_event"] == {
        "sequence": 1,
        "limit_bytes": opencode_task_runner.MAX_EVENT_BYTES,
        "event_type": "tool_use",
        "failure_category": "event_size_limit",
        "execution_id": execution_id,
        "tool_name": "toothless_browser_extract_text",
        "observed_bytes_at_least": opencode_task_runner.MAX_OUTPUT_BYTES + 1,
    }
    assert "PRIVATE_PAGE" not in json.dumps(metadata)


def test_guest_event_parser_classifies_transport_timeout_without_model_text() -> None:
    raw = b'{"type":"error","error":{"type":"Transport","message":"ETIMEDOUT: Unable to connect. Is the computer able to access the url?"}}'

    result = _events(raw, 0, "")

    assert result["status"] == "network_error"
    assert result["event_types"] == ["error"]
    assert result["tools"] == []
    assert result["text"] == ""


def test_timeout_metadata_keeps_event_structure_without_partial_content() -> None:
    raw = b'\n'.join((
        b'{"type":"tool_use","part":{"tool":"write","state":{"input":{"content":"sensitive"}}}}',
        b'{"type":"text","part":{"text":"unfinished answer"}}',
    ))

    metadata = _partial_event_metadata(raw)

    assert metadata["event_count"] == 2
    assert metadata["event_types"] == ["tool_use", "text"]
    assert metadata["tools"] == ["write"]
    assert metadata["retry_count"] == 0
    assert metadata["timeline"] == [
        {"sequence": 1, "type": "tool_use", "activity": "tool", "tool": "write"},
        {"sequence": 2, "type": "text"},
    ]
    assert metadata["metadata_truncated"] is False
    assert "sensitive" not in json.dumps(metadata)


def test_retry_metadata_keeps_safe_order_timing_and_category_only() -> None:
    raw = b'\n'.join((
        b'{"type":"step_start","timestamp":"2026-11-18T07:24:09Z"}',
        b'{"type":"session.status","timestamp":1794986690000,"status":{"type":"retry","attempt":3,"message":"Rate Limited: private provider payload","next":1794986700000}}',
    ))
    metadata = _partial_event_metadata(raw)

    assert metadata["retry_count"] == 1
    assert metadata["timeline"] == [
        {"sequence": 1, "type": "step_start", "timestamp": "2026-11-18T07:24:09Z"},
        {
            "sequence": 2, "type": "session.status", "activity": "retry",
            "timestamp": 1794986690000, "retry_attempt": 3, "error_category": "rate_limit",
            "retry_at": 1794986700000, "backoff_ms": 10000,
        },
    ]
    assert "private provider payload" not in json.dumps(metadata)


def test_guest_entry_point_refuses_non_root_callers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(opencode_task_runner.os, "geteuid", lambda: 999)

    assert opencode_task_runner.main() == 1
    assert "root-owned worker helper" in capsys.readouterr().out


def test_guest_auth_readiness_checks_active_opencode_go_metadata_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "opencode.db"
    monkeypatch.setattr(opencode_task_runner, "AUTH_DATABASE", database)
    monkeypatch.setattr(
        opencode_task_runner.pwd,
        "getpwnam",
        lambda _name: SimpleNamespace(pw_uid=database.stat().st_uid),
    )
    assert not opencode_task_runner._has_active_opencode_go_credential()

    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE credential (integration_id TEXT, active INTEGER, value TEXT)"
        )
        connection.execute("INSERT INTO credential VALUES (?, ?, ?)", ("opencode-go", 0, "test-secret"))
        connection.execute("INSERT INTO credential VALUES (?, ?, ?)", ("other-provider", 1, "other-secret"))
    assert not opencode_task_runner._has_active_opencode_go_credential()

    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO credential VALUES (?, ?, ?)", ("opencode-go", 1, "go-test-secret"))
    assert opencode_task_runner._has_active_opencode_go_credential()


def test_guest_runner_returns_actionable_auth_error_before_opencode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = Path("ops/opencode/phase05/opencode.json").resolve()
    monkeypatch.setattr(opencode_task_runner, "CONFIG", config)
    monkeypatch.setattr(opencode_task_runner, "CONFIG_SHA256", hashlib.sha256(config.read_bytes()).hexdigest())
    monkeypatch.setattr(opencode_task_runner, "AUTH_DATABASE", tmp_path / "missing-opencode.db")
    monkeypatch.setattr(opencode_task_runner.os, "geteuid", lambda: 0)
    monkeypatch.setattr(opencode_task_runner, "_verify_effective_config", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        opencode_task_runner.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("OpenCode must not start without active auth"),
    )
    request = {"model": "opencode-go/glm-5.3", "profile": "worker", "prompt": "Do not infer."}
    monkeypatch.setattr(
        opencode_task_runner.sys,
        "argv",
        ["opencode-task-runner", base64.b64encode(json.dumps(request).encode()).decode()],
    )

    assert opencode_task_runner.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "auth_required"
    assert "bash ops/worker/opencode-auth-login.sh" in result["error"]
    assert "Ubuntu guest prompt" in result["error"]
