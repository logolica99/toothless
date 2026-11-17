from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from nanobot.worker.execution_store import SqliteExecutionStore
from nanobot.worker.interface import (
    ExecuteRequest,
    ExecutionSnapshot,
    ExecutionStatus,
    FileKind,
    WorkerErrorCode,
    WorkerInterface,
    WorkerOperationError,
    WorkerUnavailableError,
)
from nanobot.worker.ssh_backend import (
    SSHWorkerBackend,
    SSHWorkerConfig,
    create_persistent_worker,
)


def _worker() -> WorkerInterface:
    config = SSHWorkerConfig(
        host=os.environ["TOOTHLESS_WORKER_HOST"],
        username=os.environ.get("TOOTHLESS_WORKER_USER", "admin"),
        identity_file=Path(os.environ["TOOTHLESS_WORKER_IDENTITY_FILE"]),
        known_hosts_file=Path(os.environ["TOOTHLESS_WORKER_KNOWN_HOSTS_FILE"]),
        request_timeout_seconds=8.0,
    )
    database = os.environ.get("TOOTHLESS_EXECUTION_DB")
    if database:
        return create_persistent_worker(
            config,
            execution_store=SqliteExecutionStore(Path(database)),
        )
    return WorkerInterface(SSHWorkerBackend(config))


async def _finished(worker: WorkerInterface, execution: ExecutionSnapshot) -> ExecutionSnapshot:
    deadline = asyncio.get_running_loop().time() + 12.0
    current = execution
    while current.status is ExecutionStatus.RUNNING and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
        current = await worker.poll(current.execution_id)
    return current


@pytest.mark.asyncio
async def test_real_worker_executes_commands_and_guest_file_operations() -> None:
    if os.environ.get("TOOTHLESS_WORKER_SSH_TEST_MODE") != "running":
        pytest.skip("set TOOTHLESS_WORKER_SSH_TEST_MODE=running after protected VM startup")
    worker = _worker()

    ready = await _finished(
        worker,
        await worker.execute(ExecuteRequest("printf 'TOOTHLESS_WORKER_READY\\n'")),
    )
    assert ready.status is ExecutionStatus.SUCCEEDED
    assert ready.exit_code == 0
    assert ready.stdout == b"TOOTHLESS_WORKER_READY\n"
    assert ready.stderr == b""
    assert ready.started_at.tzinfo is not None
    assert ready.finished_at is not None

    failed = await _finished(
        worker,
        await worker.execute(ExecuteRequest("printf 'expected failure\\n' >&2; exit 7")),
    )
    assert failed.status is ExecutionStatus.FAILED
    assert failed.exit_code == 7
    assert failed.stderr == b"expected failure\n"

    streams = await _finished(
        worker,
        await worker.execute(
            ExecuteRequest("printf 'stdout-value\\n'; printf 'stderr-value\\n' >&2")
        ),
    )
    assert streams.stdout == b"stdout-value\n"
    assert streams.stderr == b"stderr-value\n"

    bounded = await _finished(
        worker,
        await worker.execute(
            ExecuteRequest("head -c 70000 /dev/zero; head -c 70000 /dev/zero >&2")
        ),
    )
    assert bounded.status is ExecutionStatus.SUCCEEDED
    assert len(bounded.stdout) == 64 * 1024
    assert len(bounded.stderr) == 64 * 1024
    assert bounded.stdout_truncated is True
    assert bounded.stderr_truncated is True

    timed_out = await _finished(
        worker,
        await worker.execute(ExecuteRequest("sleep 30", timeout_seconds=1.0)),
    )
    assert timed_out.status is ExecutionStatus.TIMED_OUT
    assert timed_out.exit_code is None

    running = await worker.execute(ExecuteRequest("sleep 30"))
    assert running.status is ExecutionStatus.RUNNING
    cancelled = await worker.terminate(running.execution_id)
    assert cancelled.status is ExecutionStatus.CANCELLED

    unicode_result = await _finished(
        worker,
        await worker.execute(
            ExecuteRequest("printf '%s\\n' 'café \"toothless\"'")
        ),
    )
    assert unicode_result.status is ExecutionStatus.SUCCEEDED
    assert unicode_result.stdout == 'café "toothless"\n'.encode()

    evidence = await _finished(
        worker,
        await worker.execute(
            ExecuteRequest("printf 'kernel='; uname -s; printf 'host='; hostname; cat /etc/os-release; pwd")
        ),
    )
    evidence_text = evidence.stdout.decode("utf-8")
    assert "kernel=Linux" in evidence_text
    assert "host=ubuntu" in evidence_text
    assert "Ubuntu" in evidence_text
    assert "/home/admin/workspace" in evidence_text

    filename = f"phase-02-02-{uuid.uuid4().hex}.bin"
    try:
        content = b"guest-file\x00payload\xff"
        written = await worker.write_file(filename, content)
        assert written.kind is FileKind.FILE
        assert await worker.read_file(filename) == content
        assert any(entry.path == filename for entry in await worker.list_files("."))
        assert (await worker.stat_file(filename)).size_bytes == len(content)
    finally:
        cleanup = await worker.execute(ExecuteRequest(f"rm -f -- {filename}"))
        await _finished(worker, cleanup)


@pytest.mark.asyncio
async def test_stopped_worker_is_unavailable_without_host_execution() -> None:
    if os.environ.get("TOOTHLESS_WORKER_SSH_TEST_MODE") != "stopped":
        pytest.skip("set TOOTHLESS_WORKER_SSH_TEST_MODE=stopped after stopping the VM")
    marker = Path(os.environ["TOOTHLESS_HOST_MARKER_PATH"])
    worker = _worker()
    execution_id = f"phase-02-03-stopped-{uuid.uuid4().hex}"

    with pytest.raises(WorkerUnavailableError):
        await worker.execute(ExecuteRequest(f"touch {marker}", execution_id=execution_id))

    assert not marker.exists()
    record = worker.get_execution_record(execution_id)
    if record is not None:
        assert record.dispatch_state == "not_accepted"
        assert record.error_code == WorkerErrorCode.WORKER_UNAVAILABLE.value


@pytest.mark.asyncio
async def test_protected_worker_keeps_public_dns_and_https() -> None:
    if os.environ.get("TOOTHLESS_WORKER_SSH_TEST_MODE") != "running":
        pytest.skip("set TOOTHLESS_WORKER_SSH_TEST_MODE=running after protected VM startup")
    worker = _worker()

    dns = await _finished(
        worker,
        await worker.execute(ExecuteRequest("getent ahostsv4 example.com")),
    )
    assert dns.status is ExecutionStatus.SUCCEEDED
    assert dns.stdout.strip()

    https = await _finished(
        worker,
        await worker.execute(
            ExecuteRequest(
                "curl -4 --connect-timeout 5 --max-time 12 -sS -o /dev/null "
                "-w 'HTTPS %{http_code}\\n' https://example.com/",
                timeout_seconds=20.0,
            )
        ),
    )
    assert https.status is ExecutionStatus.SUCCEEDED
    assert https.stdout.startswith(b"HTTPS 2")


@pytest.mark.asyncio
async def test_persistent_normal_execution_returns_host_record_and_guest_file() -> None:
    if os.environ.get("TOOTHLESS_WORKER_SSH_TEST_MODE") != "running":
        pytest.skip("set TOOTHLESS_WORKER_SSH_TEST_MODE=running after protected VM startup")
    worker = _worker()
    filename = f"phase-02-03-normal-{uuid.uuid4().hex}.txt"
    result = await worker.execute(
        ExecuteRequest(
            f"printf 'persisted:{filename}\\n' > {filename}; printf 'PERSISTED_OK\\n'",
            execution_id=f"phase-02-03-{uuid.uuid4().hex}",
        )
    )
    result = await _finished(worker, result)

    assert result.status is ExecutionStatus.SUCCEEDED
    record = worker.get_execution_record(result.execution_id)
    assert record is not None
    assert record.dispatch_state == "accepted"
    assert record.accepted_at is not None
    assert record.stdout == b"PERSISTED_OK\n"
    assert await worker.read_file(filename) == f"persisted:{filename}\n".encode()
    await worker.execute(ExecuteRequest(f"rm -f -- {filename}"))


_RESTART_CONTROLLER = '''
import asyncio, json, os
from pathlib import Path
from nanobot.worker.execution_store import SqliteExecutionStore
from nanobot.worker.interface import ExecuteRequest, ExecutionStatus
from nanobot.worker.ssh_backend import SSHWorkerConfig, create_persistent_worker

config = SSHWorkerConfig(
    host=os.environ["TOOTHLESS_WORKER_HOST"], username="admin",
    identity_file=Path(os.environ["TOOTHLESS_WORKER_IDENTITY_FILE"]),
    known_hosts_file=Path(os.environ["TOOTHLESS_WORKER_KNOWN_HOSTS_FILE"]),
    request_timeout_seconds=8.0,
)
store = SqliteExecutionStore(Path(os.environ["TOOTHLESS_EXECUTION_DB"]))
worker = create_persistent_worker(config, execution_store=store)

async def main():
    action = os.environ["TOOTHLESS_CONTROLLER_ACTION"]
    if action == "start":
        filename = os.environ["TOOTHLESS_GUEST_MARKER"]
        result = await worker.execute(ExecuteRequest(
            f"sleep 4; printf 'x' >> {filename}; printf 'CONTROLLER_RESTART_DONE\\n'",
            execution_id=os.environ["TOOTHLESS_EXECUTION_ID"],
        ))
    else:
        result = await worker.reconcile(os.environ["TOOTHLESS_EXECUTION_ID"])
        while result.status is ExecutionStatus.RUNNING:
            await asyncio.sleep(0.1)
            result = await worker.poll(result.execution_id)
    print(json.dumps({"execution_id": result.execution_id,
        "status": result.status.value, "exit_code": result.exit_code,
        "stdout": result.stdout.decode()}))

asyncio.run(main())
'''


@pytest.mark.asyncio
async def test_controller_process_restart_reconnects_without_resubmitting() -> None:
    if os.environ.get("TOOTHLESS_WORKER_SSH_TEST_MODE") != "running":
        pytest.skip("set TOOTHLESS_WORKER_SSH_TEST_MODE=running after protected VM startup")
    database = os.environ.get("TOOTHLESS_EXECUTION_DB")
    if not database:
        pytest.skip("set TOOTHLESS_EXECUTION_DB to a host-only SQLite test path")
    execution_id = f"phase-02-03-restart-{uuid.uuid4().hex}"
    filename = f"phase-02-03-restart-{uuid.uuid4().hex}.txt"
    env = os.environ.copy()
    env["TOOTHLESS_CONTROLLER_ACTION"] = "start"
    env["TOOTHLESS_EXECUTION_ID"] = execution_id
    env["TOOTHLESS_GUEST_MARKER"] = filename
    first = subprocess.run(
        [sys.executable, "-c", _RESTART_CONTROLLER],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    initial = json.loads(first.stdout)
    assert initial["execution_id"] == execution_id
    assert initial["status"] == "running"

    env["TOOTHLESS_CONTROLLER_ACTION"] = "reconcile"
    second = subprocess.run(
        [sys.executable, "-c", _RESTART_CONTROLLER],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    recovered = json.loads(second.stdout)
    assert recovered["execution_id"] == execution_id
    assert recovered["status"] == "succeeded"
    assert recovered["stdout"] == "CONTROLLER_RESTART_DONE\n"

    worker = _worker()
    assert await worker.read_file(filename) == b"x"
    await worker.execute(ExecuteRequest(f"rm -f -- {filename}"))


@pytest.mark.asyncio
async def test_accepted_ssh_interruption_reconciles_without_retry() -> None:
    if os.environ.get("TOOTHLESS_WORKER_SSH_TEST_MODE") != "running":
        pytest.skip("set TOOTHLESS_WORKER_SSH_TEST_MODE=running after protected VM startup")
    database = os.environ.get("TOOTHLESS_EXECUTION_DB")
    if not database:
        pytest.skip("set TOOTHLESS_EXECUTION_DB to a host-only SQLite test path")
    store = SqliteExecutionStore(Path(database))
    filename = f"phase-02-03-interruption-{uuid.uuid4().hex}.txt"
    execution_id = f"phase-02-03-interruption-{uuid.uuid4().hex}"
    config = SSHWorkerConfig(
        host=os.environ["TOOTHLESS_WORKER_HOST"],
        username="admin",
        identity_file=Path(os.environ["TOOTHLESS_WORKER_IDENTITY_FILE"]),
        known_hosts_file=Path(os.environ["TOOTHLESS_WORKER_KNOWN_HOSTS_FILE"]),
        request_timeout_seconds=8.0,
    )

    def interrupt_after_acceptance(_execution_id: str) -> None:
        raise ConnectionError("simulated SSH interruption after guest acceptance")

    interrupted = WorkerInterface(
        SSHWorkerBackend(config, accepted_callback=interrupt_after_acceptance),
        execution_store=store,
    )
    with pytest.raises(WorkerOperationError) as error:
        await interrupted.execute(
            ExecuteRequest(
                f"sleep 2; printf 'x' >> {filename}; printf 'INTERRUPTION_DONE\\n'",
                execution_id=execution_id,
            )
        )
    assert error.value.code is WorkerErrorCode.EXECUTION_UNKNOWN
    record = store.get(execution_id)
    assert record is not None
    assert record.dispatch_state == "uncertain"
    assert record.accepted_at is not None

    reconnected = create_persistent_worker(config, execution_store=store)
    result = await reconnected.reconcile(execution_id)
    while result.status is ExecutionStatus.RUNNING:
        await asyncio.sleep(0.1)
        result = await reconnected.poll(execution_id)
    assert result.status is ExecutionStatus.SUCCEEDED
    assert result.stdout == b"INTERRUPTION_DONE\n"
    assert await reconnected.read_file(filename) == b"x"
    await reconnected.execute(ExecuteRequest(f"rm -f -- {filename}"))


@pytest.mark.asyncio
async def test_persistent_cancel_stops_guest_process() -> None:
    if os.environ.get("TOOTHLESS_WORKER_SSH_TEST_MODE") != "running":
        pytest.skip("set TOOTHLESS_WORKER_SSH_TEST_MODE=running after protected VM startup")
    database = os.environ.get("TOOTHLESS_EXECUTION_DB")
    if not database:
        pytest.skip("set TOOTHLESS_EXECUTION_DB to a host-only SQLite test path")
    worker = _worker()
    pid_file = f"phase-02-03-cancel-{uuid.uuid4().hex}.pid"
    started = await worker.execute(
        ExecuteRequest(f"printf '%s' \"$$\" > {pid_file}; exec sleep 60")
    )
    pid: str | None = None
    for _ in range(30):
        try:
            pid = (await worker.read_file(pid_file)).decode("ascii")
            break
        except WorkerOperationError as exc:
            if exc.code is not WorkerErrorCode.FILE_NOT_FOUND:
                raise
            await asyncio.sleep(0.1)
    assert pid is not None
    cancelled = await worker.terminate(started.execution_id)
    assert cancelled.status is ExecutionStatus.CANCELLED
    process_check = await _finished(
        worker,
        await worker.execute(
            ExecuteRequest(f"if kill -0 {pid} 2>/dev/null; then exit 1; else printf 'TERMINATED\\n'; fi")
        ),
    )
    assert process_check.status is ExecutionStatus.SUCCEEDED
    assert process_check.stdout == b"TERMINATED\n"
    record = worker.get_execution_record(started.execution_id)
    assert record is not None
    assert record.cancel_requested_at is not None
    assert record.status is ExecutionStatus.CANCELLED
    await worker.execute(ExecuteRequest(f"rm -f -- {pid_file}"))
