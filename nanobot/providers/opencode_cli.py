"""Fail-closed adapter for bounded OpenCode CLI runs."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Protocol, cast

from nanobot.providers.opencode_config import APPROVED_FREE_MODEL, OpenCodeEngineConfig


class TerminationReason(StrEnum):
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    OUTPUT_LIMIT = "output_limit"


@dataclass(frozen=True)
class ProcessOutput:
    returncode: int
    stdout: str
    stderr: str = ""
    duration_seconds: float = 0
    termination_reason: TerminationReason | None = None


class OpenCodeExecutor(Protocol):
    def execute(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        max_output_bytes: int,
        working_directory: Path | None,
    ) -> ProcessOutput: ...

    def cancel(self) -> bool: ...


class OpenCodeStatus(StrEnum):
    SUCCEEDED = "succeeded"
    RATE_LIMITED = "rate_limited"
    MODEL_UNAVAILABLE = "model_unavailable"
    AUTH_REQUIRED = "auth_required"
    NETWORK_ERROR = "network_error"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    OUTPUT_LIMITED = "output_limited"
    FAILED = "failed"
    MALFORMED_OUTPUT = "malformed_output"


@dataclass(frozen=True)
class OpenCodeResult:
    status: OpenCodeStatus
    text: str = ""
    error: str = ""
    duration_seconds: float = 0
    event_count: int = 0
    model: str = APPROVED_FREE_MODEL
    session_id: str = ""

    def parse_json_response(self) -> object:
        if self.status is not OpenCodeStatus.SUCCEEDED:
            raise ValueError("OpenCode result did not succeed")
        return json.loads(self.text)


class SubprocessOpenCodeExecutor:
    """Own one cancellable OpenCode subprocess without invoking a shell."""

    def __init__(self, *, environment: Mapping[str, str] | None = None) -> None:
        self._environment = dict(environment) if environment is not None else _minimal_environment()
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._cancel_requested = False

    def execute(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        max_output_bytes: int,
        working_directory: Path | None,
    ) -> ProcessOutput:
        started = time.monotonic()
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            process = subprocess.Popen(
                list(argv),
                cwd=working_directory,
                env=self._environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
            )
            with self._lock:
                if self._process is not None:
                    _terminate_process(process)
                    raise RuntimeError("OpenCode executor already has an active request")
                self._process = process
                self._cancel_requested = False

            reason: TerminationReason | None = None
            try:
                while process.poll() is None:
                    with self._lock:
                        cancelled = self._cancel_requested
                    if cancelled:
                        reason = TerminationReason.CANCELLED
                    elif time.monotonic() - started >= timeout:
                        reason = TerminationReason.TIMEOUT
                    elif _file_size(stdout_file) + _file_size(stderr_file) > max_output_bytes:
                        reason = TerminationReason.OUTPUT_LIMIT
                    if reason is not None:
                        _terminate_process(process)
                        break
                    time.sleep(0.02)
                process.wait()
            finally:
                with self._lock:
                    self._process = None
                    self._cancel_requested = False

            duration = time.monotonic() - started
            stdout = _read_bounded(stdout_file, max_output_bytes)
            remaining = max(0, max_output_bytes - len(stdout.encode("utf-8")))
            stderr = _read_bounded(stderr_file, remaining)
            return ProcessOutput(process.returncode, stdout, stderr, duration, reason)

    def cancel(self) -> bool:
        with self._lock:
            if self._process is None:
                return False
            self._cancel_requested = True
            return True


class OpenCodeCLI:
    """Run one explicit free model with no model or provider fallback."""

    def __init__(
        self,
        executor: OpenCodeExecutor,
        *,
        config: OpenCodeEngineConfig | None = None,
    ) -> None:
        self._executor = executor
        self._config = config or OpenCodeEngineConfig()

    def run(
        self,
        prompt: str,
        *,
        model: str | None = None,
        timeout: float | None = None,
    ) -> OpenCodeResult:
        if not prompt.strip():
            raise ValueError("OpenCode prompt must not be empty")
        selected_model = model or self._config.model
        if selected_model != self._config.model:
            raise ValueError(f"OpenCode model is not approved for free-only execution: {selected_model}")
        effective_timeout = timeout if timeout is not None else self._config.timeout_seconds
        if effective_timeout <= 0:
            raise ValueError("OpenCode timeout must be positive")
        try:
            output = self._executor.execute(
                self._argv(prompt, selected_model),
                timeout=effective_timeout,
                max_output_bytes=self._config.max_output_bytes,
                working_directory=self._config.working_directory,
            )
        except OSError as exc:
            return OpenCodeResult(OpenCodeStatus.FAILED, error=str(exc), model=selected_model)
        return _parse_json_lines(output, self._config.max_event_bytes, selected_model)

    def cancel(self) -> bool:
        return self._executor.cancel()

    def _argv(self, prompt: str, model: str) -> list[str]:
        return [
            self._config.executable,
            "run",
            "--standalone",
            "--archived",
            "--format",
            "json",
            "--agent",
            self._config.agent,
            "--model",
            model,
            prompt,
        ]


def _parse_json_lines(
    output: ProcessOutput, max_event_bytes: int, model: str
) -> OpenCodeResult:
    if output.termination_reason is not None:
        status = {
            TerminationReason.TIMEOUT: OpenCodeStatus.TIMED_OUT,
            TerminationReason.CANCELLED: OpenCodeStatus.CANCELLED,
            TerminationReason.OUTPUT_LIMIT: OpenCodeStatus.OUTPUT_LIMITED,
        }[output.termination_reason]
        return OpenCodeResult(
            status,
            error=output.termination_reason.value,
            duration_seconds=output.duration_seconds,
            model=model,
        )

    text_parts: list[str] = []
    errors: list[str] = []
    error_name = ""
    event_count = 0
    session_id = ""
    for line in output.stdout.splitlines():
        if not line.strip():
            continue
        if len(line.encode("utf-8")) > max_event_bytes:
            return _result(OpenCodeStatus.OUTPUT_LIMITED, output, model, event_count, "event too large")
        try:
            decoded: object = json.loads(line)
        except json.JSONDecodeError:
            return _result(OpenCodeStatus.MALFORMED_OUTPUT, output, model, event_count, "invalid JSON event")
        if not isinstance(decoded, dict):
            return _result(OpenCodeStatus.MALFORMED_OUTPUT, output, model, event_count, "invalid event shape")
        event = cast(dict[str, object], decoded)
        event_type = event.get("type")
        if not isinstance(event_type, str):
            return _result(OpenCodeStatus.MALFORMED_OUTPUT, output, model, event_count, "invalid event shape")
        event_count += 1
        event_session_id = event.get("sessionID")
        if isinstance(event_session_id, str) and not session_id:
            session_id = event_session_id
        if event_type == "text":
            part = _object_dict(event.get("part"))
            part_text = part.get("text") if part is not None else None
            if not isinstance(part_text, str):
                return _result(OpenCodeStatus.MALFORMED_OUTPUT, output, model, event_count, "invalid text event")
            text_parts.append(part_text)
        elif event_type == "error":
            error = _object_dict(event.get("error"))
            if error is None:
                return _result(OpenCodeStatus.MALFORMED_OUTPUT, output, model, event_count, "invalid error event")
            name = error.get("type", error.get("name"))
            message = error.get("message")
            error_name = name if isinstance(name, str) else ""
            if isinstance(message, str):
                errors.append(message)

    error_text = "\n".join(errors) or output.stderr.strip()
    classified = _classify_error(error_name, error_text)
    if classified is not None:
        return _result(classified, output, model, event_count, error_text, session_id)
    if output.returncode != 0 or errors:
        return _result(
            OpenCodeStatus.FAILED,
            output,
            model,
            event_count,
            error_text or "OpenCode failed",
            session_id,
        )
    if event_count == 0:
        return _result(OpenCodeStatus.MALFORMED_OUTPUT, output, model, 0, "empty event stream")
    return OpenCodeResult(
        OpenCodeStatus.SUCCEEDED,
        text="".join(text_parts),
        duration_seconds=output.duration_seconds,
        event_count=event_count,
        model=model,
        session_id=session_id,
    )


def _classify_error(name: str, message: str) -> OpenCodeStatus | None:
    normalized = f"{name} {message}".lower()
    if name == "provider.free-limit" or "rate limit" in normalized or "usage limit" in normalized:
        return OpenCodeStatus.RATE_LIMITED
    if name in {"provider.model-not-found", "provider.model-unavailable"}:
        return OpenCodeStatus.MODEL_UNAVAILABLE
    if name in {"provider.auth", "provider.auth-required"}:
        return OpenCodeStatus.AUTH_REQUIRED
    if any(term in normalized for term in ("network", "connection", "dns", "fetch failed")):
        return OpenCodeStatus.NETWORK_ERROR
    return None


def _result(
    status: OpenCodeStatus,
    output: ProcessOutput,
    model: str,
    event_count: int,
    error: str,
    session_id: str = "",
) -> OpenCodeResult:
    return OpenCodeResult(
        status,
        error=error,
        duration_seconds=output.duration_seconds,
        event_count=event_count,
        model=model,
        session_id=session_id,
    )


def _object_dict(value: object) -> dict[str, object] | None:
    return cast(dict[str, object], value) if isinstance(value, dict) else None


def _minimal_environment() -> dict[str, str]:
    allowed = ("HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "TERM", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME")
    return {name: value for name in allowed if (value := os.environ.get(name)) is not None}


def _file_size(file: BinaryIO) -> int:
    return os.fstat(file.fileno()).st_size


def _read_bounded(file: BinaryIO, limit: int) -> str:
    file.seek(0)
    return file.read(limit).decode("utf-8", errors="replace")


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=1)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
