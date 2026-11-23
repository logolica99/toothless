from __future__ import annotations

import json
import sys
from collections import deque
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ops" / "worker"))
broker = import_module("visible_browser_broker")


TASK = "task-" + "a" * 32
EXECUTION = "opencode-" + "b" * 32


def test_visible_authorization_is_bound_to_task_execution_profile_and_expiry() -> None:
    record: dict[str, object] = {
        "task_id": TASK,
        "execution_id": EXECUTION,
        "profile": "browser",
        "mode": "visible",
        "expires_at": 2000,
    }

    assert broker._validate_authorization(record, TASK, EXECUTION, now=1000) == (TASK, EXECUTION)

    for field, value in (
        ("task_id", "task-" + "c" * 32),
        ("execution_id", "opencode-" + "d" * 32),
        ("profile", "worker"),
        ("mode", "headless"),
        ("expires_at", 999),
    ):
        changed = {**record, field: value}
        with pytest.raises(PermissionError):
            broker._validate_authorization(changed, TASK, EXECUTION, now=1000)


def test_authorization_root_requires_exact_directory_acl_and_rejects_symlinks(tmp_path: Path) -> None:
    auth_root = tmp_path / "toothless-browser-authorizations"
    missing_uid = tmp_path.stat().st_uid
    missing_gid = tmp_path.stat().st_gid
    assert not broker._auth_root_is_valid(auth_root, owner_uid=missing_uid, group_gid=missing_gid)

    auth_root.mkdir(mode=0o750)
    auth_root.chmod(0o750)
    owner_uid = auth_root.stat().st_uid
    group_gid = auth_root.stat().st_gid
    assert broker._auth_root_is_valid(auth_root, owner_uid=owner_uid, group_gid=group_gid)
    assert not broker._auth_root_is_valid(auth_root, owner_uid=owner_uid + 1, group_gid=group_gid)
    assert not broker._auth_root_is_valid(auth_root, owner_uid=owner_uid, group_gid=group_gid + 1)

    auth_root.chmod(0o770)
    assert not broker._auth_root_is_valid(auth_root, owner_uid=owner_uid, group_gid=group_gid)
    auth_root.chmod(0o750)
    auth_root.rmdir()
    auth_root.symlink_to(tmp_path, target_is_directory=True)
    assert not broker._auth_root_is_valid(auth_root, owner_uid=owner_uid, group_gid=group_gid)


def test_visible_broker_rejects_other_operations_and_cross_task_takeover(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(broker, "_authorized", lambda task_id, execution_id: (task_id, execution_id))
    monkeypatch.setattr(broker.browser_mcp, "TOOLS", {
        "navigate": ("navigate", {}, lambda _args: "page loaded"),
        "click_button": ("button", {}, lambda _args: "clicked"),
    })
    monkeypatch.setattr(broker, "_active", (TASK, EXECUTION))

    with pytest.raises(PermissionError):
        broker._dispatch({
            "task_id": TASK,
            "execution_id": "opencode-" + "c" * 32,
            "name": "navigate",
            "arguments": {"url": "https://example.com"},
        })
    with pytest.raises(ValueError):
        broker._dispatch({
            "task_id": TASK,
            "execution_id": EXECUTION,
            "name": "click_button",
            "arguments": {},
        })
    assert broker._dispatch({
        "task_id": TASK,
        "execution_id": EXECUTION,
        "name": "navigate",
        "arguments": {"url": "https://example.com"},
    }) == "page loaded"


def test_browser_operation_failure_invalidates_execution_and_prevents_same_task_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broker, "_authorized", lambda task_id, execution_id: (task_id, execution_id))
    monkeypatch.setattr(broker, "_active", None)
    monkeypatch.setattr(broker, "_invalidated", None)
    closed: list[bool] = []
    monkeypatch.setattr(broker, "_close_browser", lambda _reason="broker_cleanup": closed.append(True))
    monkeypatch.setattr(broker.browser_mcp, "TOOLS", {
        "navigate": ("navigate", {}, lambda _args: (_ for _ in ()).throw(RuntimeError("target crashed"))),
    })
    request = {
        "task_id": TASK, "execution_id": EXECUTION, "name": "navigate",
        "arguments": {"url": "https://example.com"},
    }

    with pytest.raises(broker._BrowserOperationFailureError) as error:
        broker._dispatch(request)
    assert error.value.category == "browser_target_crashed"
    with pytest.raises(broker._BrowserOperationFailureError, match="Visible browser operation failed"):
        broker._dispatch(request)

    assert closed == [True]
    assert broker._invalidated == (TASK, EXECUTION)


def test_closed_context_is_fatal_and_same_execution_cannot_issue_another_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TargetClosedError(RuntimeError):
        pass

    monkeypatch.setattr(broker, "_authorized", lambda task_id, execution_id: (task_id, execution_id))
    monkeypatch.setattr(broker, "_active", None)
    monkeypatch.setattr(broker, "_invalidated", None)
    monkeypatch.setattr(broker, "_browser_connected", lambda: False)
    monkeypatch.setattr(broker, "_context_open", lambda: False)
    monkeypatch.setattr(broker, "_page_open", lambda: False)
    monkeypatch.setattr(broker, "_resource_snapshot", lambda: {"browser_process_alive": False})
    monkeypatch.setattr(broker, "_record_lifecycle", lambda _event: None)
    closed: list[str] = []
    monkeypatch.setattr(broker, "_close_browser", lambda reason="broker_cleanup": closed.append(reason))
    monkeypatch.setattr(broker.browser_mcp, "TOOLS", {
        "extract_text": ("extract", {}, lambda _args: (_ for _ in ()).throw(TargetClosedError("not persisted"))),
    })
    request = {"task_id": TASK, "execution_id": EXECUTION, "name": "extract_text", "arguments": {}}

    with pytest.raises(broker._BrowserOperationFailureError) as failure:
        broker._dispatch(request)
    with pytest.raises(broker._BrowserOperationFailureError) as repeated:
        broker._dispatch(request)

    assert failure.value.category == "browser_context_closed"
    assert failure.value.fatal is True
    assert repeated.value.category == "browser_session_invalid"
    assert closed == ["fatal_failure"]


@pytest.mark.parametrize(
    ("event", "cleanup_reason", "resources", "expected"),
    [
        ("browser_disconnected", "", {"browser_process_alive": False, "browser_exit_signal": 9}, "signal_exit"),
        ("browser_disconnected", "", {"browser_process_alive": False}, "process_disappeared"),
        ("browser_disconnected", "explicit_close", {"browser_process_alive": False}, "application_close"),
        ("browser_disconnected", "", {"browser_process_alive": True}, "transport_disconnect"),
    ],
)
def test_lifecycle_termination_classification_is_bounded_and_explicit(
    monkeypatch: pytest.MonkeyPatch,
    event: str,
    cleanup_reason: str,
    resources: dict[str, object],
    expected: str,
) -> None:
    monkeypatch.setattr(broker.browser_mcp, "_LIFECYCLE_EVENTS", {event})
    monkeypatch.setattr(broker.browser_mcp, "_cleanup_reason", cleanup_reason)
    snapshot = {"memory_events": {}, "pids_events": {}, **resources}
    monkeypatch.setattr(broker, "_resource_snapshot", lambda **_kwargs: snapshot)
    monkeypatch.setattr(broker, "_browser_connected", lambda: False)
    monkeypatch.setattr(broker, "_context_open", lambda: False)
    monkeypatch.setattr(broker, "_page_open", lambda: False)
    monkeypatch.setattr(broker, "_lifecycle_started", 1.0)
    monkeypatch.setattr(broker, "_last_browser_process_alive", True)
    monkeypatch.setattr(broker, "_lifecycle_events", deque(maxlen=12))

    broker._record_lifecycle(event)

    assert broker._lifecycle_events[-1]["browser_termination"] == expected


@pytest.mark.parametrize(
    ("error", "expected_category"),
    [
        (broker.browser_mcp.NavigationHTTPError("safe fixed message"), "navigation_http_error"),
        (TimeoutError("safe fixed message"), "navigation_timeout"),
        (ValueError("safe fixed message"), "browser_request_invalid"),
    ],
)
def test_healthy_navigation_errors_are_recoverable(
    monkeypatch: pytest.MonkeyPatch, error: Exception, expected_category: str,
) -> None:
    monkeypatch.setattr(broker, "_authorized", lambda task_id, execution_id: (task_id, execution_id))
    monkeypatch.setattr(broker, "_active", None)
    monkeypatch.setattr(broker, "_invalidated", None)
    monkeypatch.setattr(broker, "_browser_connected", lambda: True)
    monkeypatch.setattr(broker, "_context_open", lambda: True)
    monkeypatch.setattr(broker, "_page_open", lambda: True)
    monkeypatch.setattr(broker, "_resource_snapshot", lambda: {"browser_process_alive": True})
    monkeypatch.setattr(broker, "_record_lifecycle", lambda _event: None)
    monkeypatch.setattr(broker, "_close_browser", lambda _reason="broker_cleanup": pytest.fail("recoverable failure closed browser"))
    monkeypatch.setattr(broker.browser_mcp, "TOOLS", {
        "navigate": ("navigate", {}, lambda _args: (_ for _ in ()).throw(error)),
    })

    with pytest.raises(broker._BrowserOperationFailureError) as failure:
        broker._dispatch({"task_id": TASK, "execution_id": EXECUTION, "name": "navigate", "arguments": {}})

    assert failure.value.category == expected_category
    assert failure.value.fatal is False
    assert broker._invalidated is None
    assert broker._active == (TASK, EXECUTION)


def test_resource_snapshot_captures_bounded_cgroup_and_browser_counters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "cgroup.procs").write_text("123\n", encoding="ascii")
    for name, value in {
        "pids.current": "7", "pids.max": "128", "memory.current": "4096", "memory.max": "8192",
        "memory.events": "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n",
        "pids.events": "max 0\n", "cpu.stat": "usage_usec 99\nnr_periods 4\nnr_throttled 1\nthrottled_usec 7\n",
    }.items():
        (cgroup / name).write_text(value, encoding="ascii")
    driver = SimpleNamespace(pid=234, returncode=None)
    monkeypatch.setattr(broker, "_cgroup_directory", lambda: cgroup)
    monkeypatch.setattr(broker, "_playwright_driver", lambda: driver)
    monkeypatch.setattr(broker.os, "kill", lambda *_args: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(broker, "_proc_details", lambda pid: {
        "pid": pid, "parent_pid": 234, "comm": "chrome", "thread_count": 5,
        "state": "S", "exit_wait_status": None,
    })
    monkeypatch.setattr(broker, "_process_cgroup_scope", lambda _pid, _cgroup: "service")

    snapshot = broker._resource_snapshot(include_process_inventory=True)

    assert snapshot["process_count"] == 1
    assert snapshot["thread_count"] == 5
    assert snapshot["browser_pid"] == 123
    assert snapshot["browser_thread_count"] == 5
    assert snapshot["cgroup_pids_current"] == 7
    assert snapshot["cgroup_pids_max"] == 128
    assert snapshot["cgroup_memory_current_bytes"] == 4096
    assert snapshot["cgroup_memory_max_bytes"] == 8192
    assert snapshot["cpu_stats"] == {"usage_usec": 99, "nr_periods": 4, "nr_throttled": 1, "throttled_usec": 7}
    assert snapshot["process_roles"]["chrome_browser"] == {"processes": 1, "threads": 5}
    assert snapshot["process_inventory"] == [{
        "pid": 123, "parent_pid": 234, "comm": "chrome", "thread_count": 5, "cgroup_scope": "service",
    }]


def test_playwright_driver_comes_from_playwright_transport_not_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    process = SimpleNamespace(pid=321)
    transport = SimpleNamespace(_proc=process)
    connection = SimpleNamespace(_transport=transport)
    playwright = SimpleNamespace(_impl_obj=SimpleNamespace(_connection=connection))
    monkeypatch.setattr(broker.browser_mcp, "_playwright", playwright)
    monkeypatch.setattr(broker.browser_mcp, "_browser", SimpleNamespace(_impl_obj=SimpleNamespace()))

    assert broker._playwright_driver() is process


def test_nested_cgroup_processes_and_parent_pid_limits_are_counted(tmp_path: Path) -> None:
    service = tmp_path / "service.scope"
    nested = service / "browser.scope"
    nested.mkdir(parents=True)
    (service / "cgroup.procs").write_text("10\n", encoding="ascii")
    (nested / "cgroup.procs").write_text("11\n12\n", encoding="ascii")
    (service / "pids.current").write_text("8", encoding="ascii")
    (service / "pids.max").write_text("128", encoding="ascii")
    (tmp_path / "pids.current").write_text("40", encoding="ascii")
    (tmp_path / "pids.max").write_text("256", encoding="ascii")

    assert broker._cgroup_process_ids(service) == [10, 11, 12]
    assert broker._cgroup_parent_limits(service)[:2] == [
        {"level": "service", "pids_current": 8, "pids_max": 128},
        {"level": "parent_1", "pids_current": 40, "pids_max": 256},
    ]


def test_resource_snapshot_marks_unavailable_cgroup_counters_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broker, "_cgroup_directory", lambda: None)
    monkeypatch.setattr(broker, "_playwright_driver", lambda: None)
    snapshot = broker._resource_snapshot()
    assert snapshot["process_count"] is None
    assert snapshot["thread_count"] is None
    assert snapshot["cgroup_pids_current"] is None
    assert snapshot["cgroup_memory_current_bytes"] is None
    assert snapshot["browser_process_alive"] is None


def test_new_approved_execution_does_not_reuse_prior_invalidated_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other_execution = "opencode-" + "c" * 32
    monkeypatch.setattr(broker, "_authorized", lambda task_id, execution_id: (task_id, execution_id))
    monkeypatch.setattr(broker, "_active", None)
    monkeypatch.setattr(broker, "_invalidated", (TASK, EXECUTION))
    monkeypatch.setattr(broker.browser_mcp, "TOOLS", {
        "navigate": ("navigate", {}, lambda _args: "opened"),
    })

    assert broker._dispatch({
        "task_id": TASK, "execution_id": other_execution, "name": "navigate", "arguments": {},
    }) == "opened"
    assert broker._invalidated is None


def test_broker_error_wire_contains_bounded_metadata_without_exception_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Connection:
        payload: bytes = b""

        def sendall(self, value: bytes) -> None:
            self.payload = value

    monkeypatch.setattr(broker, "_dispatch", lambda _request: (_ for _ in ()).throw(
        broker._BrowserOperationFailureError(
            category="browser_target_crashed", exception_class="TargetCrashedError",
            browser_connected=False, browser_process_alive=False,
        )
    ))
    monkeypatch.setattr(broker, "_invocation_sequence", 0)
    monkeypatch.setattr(broker, "_invocation_execution", None)
    connection = Connection()
    broker._response(connection, {
        "name": "navigate", "task_id": TASK, "execution_id": EXECUTION,
    })
    result = json.loads(connection.payload)

    assert result["ok"] is False
    diagnostic = result["diagnostic"]
    assert diagnostic["failure_category"] == "browser_target_crashed"
    assert diagnostic["tool_name"] == "navigate"
    assert diagnostic["invocation_sequence"] == 1
    assert diagnostic["session_state"] == "invalidated"
    assert diagnostic["browser_connected"] is False
    assert diagnostic["browser_process_alive"] is False
    assert diagnostic["exception_class"] == "TargetCrashedError"
    assert diagnostic["execution_id"] == EXECUTION
    assert diagnostic["task_id"] == TASK
    assert diagnostic["fatal_signal"] is True
    assert diagnostic["lifecycle"] == []
    assert "exception message" not in connection.payload.decode()
    assert type(diagnostic["elapsed_ms"]) is int and 0 <= diagnostic["elapsed_ms"] <= 120_000
    assert "exception message" not in connection.payload.decode()
