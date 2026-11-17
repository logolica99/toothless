from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.worker.fake import FakeCommandPlan, FakeWorkerBackend
from nanobot.worker.interface import (
    DEFAULT_MAX_OUTPUT_BYTES,
    ExecuteRequest,
    ExecutionStatus,
    FileKind,
    WorkerErrorCode,
    WorkerInterface,
    WorkerOperationError,
    WorkerUnavailableError,
)


@pytest.mark.asyncio
async def test_execute_returns_typed_success_with_execution_id() -> None:
    backend = FakeWorkerBackend(
        command_plans={"printf hello": FakeCommandPlan(stdout=b"hello")}
    )
    worker = WorkerInterface(backend)

    result = await worker.execute(ExecuteRequest("printf hello", working_directory="project"))

    assert result.execution_id
    assert result.status is ExecutionStatus.SUCCEEDED
    assert result.exit_code == 0
    assert result.stdout == b"hello"
    assert result.stderr == b""
    assert backend.submitted_requests[0].working_directory == "project"


@pytest.mark.asyncio
async def test_nonzero_exit_is_returned_without_becoming_a_transport_error() -> None:
    backend = FakeWorkerBackend(
        command_plans={"false": FakeCommandPlan(exit_code=1, stderr=b"failed")}
    )

    result = await WorkerInterface(backend).execute(ExecuteRequest("false"))

    assert result.status is ExecutionStatus.FAILED
    assert result.exit_code == 1
    assert result.stderr == b"failed"


@pytest.mark.asyncio
async def test_stdout_and_stderr_are_bounded_independently() -> None:
    backend = FakeWorkerBackend(
        command_plans={
            "large": FakeCommandPlan(stdout=b"0123456789", stderr=b"abcdefghij")
        }
    )

    result = await WorkerInterface(backend, max_output_bytes=5).execute(
        ExecuteRequest("large")
    )

    assert result.stdout == b"01234"
    assert result.stderr == b"abcde"
    assert result.stdout_truncated
    assert result.stderr_truncated
    assert len(result.stdout) <= DEFAULT_MAX_OUTPUT_BYTES


@pytest.mark.asyncio
async def test_execution_timeout_is_a_terminal_typed_state() -> None:
    backend = FakeWorkerBackend(
        command_plans={"slow": FakeCommandPlan(runtime_seconds=2.0)}
    )

    result = await WorkerInterface(backend).execute(
        ExecuteRequest("slow", timeout_seconds=1.0)
    )

    assert result.status is ExecutionStatus.TIMED_OUT
    assert result.exit_code is None
    assert result.finished_at is not None


@pytest.mark.asyncio
async def test_poll_and_terminate_cancel_a_running_execution() -> None:
    backend = FakeWorkerBackend(
        command_plans={"long": FakeCommandPlan(stays_running=True)}
    )
    worker = WorkerInterface(backend)
    started = await worker.execute(ExecuteRequest("long", execution_id="run-1"))

    assert started.status is ExecutionStatus.RUNNING
    assert (await worker.poll("run-1")).status is ExecutionStatus.RUNNING
    cancelled = await worker.terminate("run-1")

    assert cancelled.status is ExecutionStatus.CANCELLED
    assert (await worker.poll("run-1")).finished_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "../escape", "a/../../escape", "bad\\path", "bad\x00path", ""],
)
async def test_malformed_file_paths_return_structured_validation_errors(path: str) -> None:
    backend = FakeWorkerBackend()

    with pytest.raises(WorkerOperationError) as error:
        await WorkerInterface(backend).read_file(path)

    assert error.value.code is WorkerErrorCode.INVALID_REQUEST
    assert error.value.as_dict()["code"] == "invalid_request"
    assert not backend.files


@pytest.mark.asyncio
async def test_command_and_timeout_requests_are_validated() -> None:
    backend = FakeWorkerBackend()
    worker = WorkerInterface(backend)

    with pytest.raises(WorkerOperationError) as empty_command:
        await worker.execute(ExecuteRequest("  "))
    assert empty_command.value.code is WorkerErrorCode.INVALID_REQUEST

    with pytest.raises(WorkerOperationError) as invalid_timeout:
        await worker.execute(ExecuteRequest("ok", timeout_seconds=0))
    assert invalid_timeout.value.code is WorkerErrorCode.INVALID_REQUEST
    assert not backend.submitted_requests


@pytest.mark.asyncio
async def test_duplicate_execution_id_is_rejected_before_second_dispatch() -> None:
    backend = FakeWorkerBackend()
    worker = WorkerInterface(backend)
    request = ExecuteRequest("first", execution_id="stable-id")
    await worker.execute(request)

    with pytest.raises(WorkerOperationError) as error:
        await worker.execute(ExecuteRequest("must not run", execution_id="stable-id"))

    assert error.value.code is WorkerErrorCode.DUPLICATE_EXECUTION_ID
    assert len(backend.submitted_requests) == 1


@pytest.mark.asyncio
async def test_worker_unavailable_is_explicit_and_never_falls_back_to_host(tmp_path: Path) -> None:
    marker = tmp_path / "host-execution-marker"
    unavailable = FakeWorkerBackend(available=False)
    worker = WorkerInterface(unavailable)

    with pytest.raises(WorkerUnavailableError) as error:
        await worker.execute(ExecuteRequest(f"touch {marker}"))

    assert error.value.code is WorkerErrorCode.WORKER_UNAVAILABLE
    assert error.value.as_dict() == {
        "code": "worker_unavailable",
        "message": "Fake worker is unavailable",
    }
    assert not marker.exists()
    assert not unavailable.submitted_requests


@pytest.mark.asyncio
async def test_binary_file_operations_use_only_fake_worker_storage() -> None:
    backend = FakeWorkerBackend()
    worker = WorkerInterface(backend)
    content = b"hello\x00world\xff"

    written = await worker.write_file("reports/result.bin", content)
    restored = await worker.read_file("reports/result.bin")
    entry = (await worker.list_files("reports"))[0]
    root = await worker.stat_file(".")
    file_stat = await worker.stat_file("reports/result.bin")

    assert written.kind is FileKind.FILE
    assert restored == content
    assert entry.path == "reports/result.bin"
    assert entry.size_bytes == len(content)
    assert root.kind is FileKind.DIRECTORY
    assert file_stat.size_bytes == len(content)

