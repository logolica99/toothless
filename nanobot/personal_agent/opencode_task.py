"""Dispatch one explicitly approved OpenCode task through the worker interface."""

from __future__ import annotations

import asyncio
import base64
import json
import shlex
import uuid
from dataclasses import dataclass
from typing import ContextManager, Protocol, cast

from nanobot.personal_agent.task_store import TaskRecord, TaskStatus
from nanobot.worker.interface import (
    ExecuteRequest,
    ExecutionStatus,
    WorkerInterface,
    WorkerOperationError,
)

MODEL = "opencode-go/glm-5.3"
TASK_TIMEOUT_SECONDS = 150
TASK_RECONCILE_TIMEOUT_SECONDS = TASK_TIMEOUT_SECONDS + 15
TASK_POLL_INTERVAL_SECONDS = 1.0
MAX_TASK_RESULT_CHARS = 48_000
GUEST_RUNNER = "/usr/local/libexec/toothless/opencode-task-runner"


class WorkerProvider(Protocol):
    def __call__(self) -> ContextManager[WorkerInterface]: ...


@dataclass(frozen=True, slots=True)
class TaskRunResult:
    execution_id: str
    status: TaskStatus
    result: str | None = None
    error: str | None = None


class OpenCodeTaskRunner:
    """Fixed OpenCode entry point; the task prompt is encoded as data for the guest."""

    def __init__(self, worker_provider: WorkerProvider) -> None:
        self._worker_provider = worker_provider

    @staticmethod
    def new_execution_id() -> str:
        return f"opencode-{uuid.uuid4().hex}"

    async def execute(self, task: TaskRecord, execution_id: str) -> TaskRunResult:
        prompt = _task_prompt(task)
        payload = json.dumps(
            {"model": MODEL, "prompt": prompt}, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        encoded = base64.b64encode(payload).decode("ascii")
        command = (
            f"/usr/bin/python3 {shlex.quote(GUEST_RUNNER)} {shlex.quote(encoded)}"
        )
        accepted = False
        try:
            with self._worker_provider() as worker:
                snapshot = await worker.execute(
                    ExecuteRequest(
                        command=command,
                        working_directory=".",
                        timeout_seconds=TASK_TIMEOUT_SECONDS,
                        execution_id=execution_id,
                    )
                )
                accepted = True
                if snapshot.status is ExecutionStatus.RUNNING:
                    loop = asyncio.get_running_loop()
                    deadline = loop.time() + TASK_RECONCILE_TIMEOUT_SECONDS
                    while snapshot.status is ExecutionStatus.RUNNING and loop.time() < deadline:
                        await asyncio.sleep(min(TASK_POLL_INTERVAL_SECONDS, max(0.0, deadline - loop.time())))
                        if loop.time() >= deadline:
                            break
                        snapshot = await worker.poll(execution_id)
                    if snapshot.status is ExecutionStatus.RUNNING:
                        try:
                            snapshot = await worker.terminate(execution_id)
                        except WorkerOperationError:
                            return TaskRunResult(
                                execution_id,
                                TaskStatus.UNKNOWN,
                                error="Worker execution exceeded the bounded wait; termination is unconfirmed and it was not resubmitted.",
                            )
                        if snapshot.status is ExecutionStatus.RUNNING:
                            return TaskRunResult(
                                execution_id,
                                TaskStatus.UNKNOWN,
                                error="Worker execution remains active after the bounded wait; it was not resubmitted.",
                            )
        except WorkerOperationError as exc:
            status = (
                TaskStatus.UNKNOWN
                if accepted or exc.code.value == "execution_unknown"
                else TaskStatus.FAILED
            )
            return TaskRunResult(execution_id, status, error=f"{exc.code.value}: {exc.message}")
        if snapshot.status is ExecutionStatus.UNKNOWN:
            return TaskRunResult(
                execution_id,
                TaskStatus.UNKNOWN,
                error="Worker execution state is unknown; it was not resubmitted.",
            )
        return parse_task_result(execution_id, snapshot.stdout, snapshot.status, snapshot.exit_code)


def _task_prompt(task: TaskRecord) -> str:
    parts = ["Answer the user's task directly. Do not use tools.", "Task request:", task.request]
    if task.instructions:
        parts.extend(("Additional user instructions:", task.instructions))
    return "\n\n".join(parts)


def parse_task_result(
    execution_id: str,
    raw: bytes,
    execution_status: ExecutionStatus = ExecutionStatus.SUCCEEDED,
    exit_code: int | None = 0,
) -> TaskRunResult:
    if execution_status is ExecutionStatus.CANCELLED:
        return TaskRunResult(execution_id, TaskStatus.CANCELLED)
    if execution_status is ExecutionStatus.TIMED_OUT:
        return TaskRunResult(execution_id, TaskStatus.FAILED, error="Worker execution timed out.")
    if execution_status is not ExecutionStatus.SUCCEEDED:
        return TaskRunResult(
            execution_id,
            TaskStatus.FAILED,
            error=f"Worker command ended with {execution_status.value} (exit {exit_code}).",
        )
    try:
        value_raw: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return TaskRunResult(execution_id, TaskStatus.FAILED, error="Guest OpenCode runner returned malformed output.")
    if not isinstance(value_raw, dict):
        return TaskRunResult(execution_id, TaskStatus.FAILED, error="Guest result did not match the authorized model.")
    value = cast(dict[str, object], value_raw)
    model = value.get("model")
    if model not in {MODEL, None}:
        return TaskRunResult(execution_id, TaskStatus.FAILED, error="Guest result did not match the authorized model.")
    result_status = value.get("status")
    if result_status != "succeeded":
        detail = value.get("error")
        if not isinstance(detail, str) or not detail:
            detail = f"OpenCode returned {result_status if isinstance(result_status, str) else 'an unknown result'}"
        return TaskRunResult(execution_id, TaskStatus.FAILED, error=f"{result_status}: {detail[:800]}")
    result = value.get("text")
    if not isinstance(result, str) or not result.strip():
        return TaskRunResult(execution_id, TaskStatus.FAILED, error="OpenCode returned an empty answer.")
    if len(result) > MAX_TASK_RESULT_CHARS:
        return TaskRunResult(execution_id, TaskStatus.FAILED, error="OpenCode answer exceeded the host result limit.")
    return TaskRunResult(execution_id, TaskStatus.SUCCEEDED, result=result)
