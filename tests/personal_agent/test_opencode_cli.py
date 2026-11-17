import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from nanobot.providers.opencode_cli import (
    OpenCodeCLI,
    OpenCodeStatus,
    ProcessOutput,
    SubprocessOpenCodeExecutor,
    TerminationReason,
)
from nanobot.providers.opencode_config import APPROVED_FREE_MODEL, OpenCodeEngineConfig


class FakeExecutor:
    def __init__(self, output: ProcessOutput | OSError) -> None:
        self.output = output
        self.calls: list[tuple[list[str], float, int, Path | None]] = []
        self.cancelled = False

    def execute(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        max_output_bytes: int,
        working_directory: Path | None,
    ) -> ProcessOutput:
        self.calls.append((list(argv), timeout, max_output_bytes, working_directory))
        if isinstance(self.output, OSError):
            raise self.output
        return self.output

    def cancel(self) -> bool:
        self.cancelled = True
        return True


def test_explicit_free_model_parses_structured_text_and_metadata(tmp_path: Path) -> None:
    executor = FakeExecutor(
        ProcessOutput(
            0,
            '{"type":"step_start","sessionID":"session-1"}\n'
            '{"type":"text","sessionID":"session-1","part":{"text":"READY"}}\n',
            duration_seconds=1.25,
        )
    )
    config = OpenCodeEngineConfig(working_directory=tmp_path)

    result = OpenCodeCLI(executor, config=config).run("Reply with exactly READY.", timeout=30)

    assert result.status is OpenCodeStatus.SUCCEEDED
    assert result.text == "READY"
    assert result.duration_seconds == 1.25
    assert result.event_count == 2
    assert result.model == APPROVED_FREE_MODEL
    assert result.session_id == "session-1"
    argv, timeout, max_output, working_directory = executor.calls[0]
    assert argv == [
        "opencode",
        "run",
        "--standalone",
        "--archived",
        "--format",
        "json",
        "--agent",
        "phase00-readonly",
        "--model",
        APPROVED_FREE_MODEL,
        "Reply with exactly READY.",
    ]
    assert timeout == 30
    assert max_output == 1_048_576
    assert working_directory == tmp_path


def test_unknown_model_is_rejected_before_execution() -> None:
    executor = FakeExecutor(ProcessOutput(0, ""))

    with pytest.raises(ValueError, match="not approved"):
        OpenCodeCLI(executor).run("bounded task", model="paid/provider-model")

    assert executor.calls == []


def test_free_limit_stops_without_fallback() -> None:
    executor = FakeExecutor(
        ProcessOutput(
            1,
            '{"type":"error","error":{"type":"provider.free-limit",'
            '"message":"Free usage limit reached"}}\n',
        )
    )

    result = OpenCodeCLI(executor).run("bounded task")

    assert result.status is OpenCodeStatus.RATE_LIMITED
    assert result.error == "Free usage limit reached"
    assert len(executor.calls) == 1


def test_network_failure_is_structured() -> None:
    executor = FakeExecutor(
        ProcessOutput(
            1,
            '{"type":"error","error":{"type":"ProviderError",'
            '"message":"network unavailable"}}\n',
        )
    )

    result = OpenCodeCLI(executor).run("bounded task")

    assert result.status is OpenCodeStatus.NETWORK_ERROR
    assert result.error == "network unavailable"


def test_structured_json_final_response_is_parsed() -> None:
    executor = FakeExecutor(
        ProcessOutput(0, '{"type":"text","part":{"text":"{\\"ready\\":true}"}}\n')
    )

    result = OpenCodeCLI(executor).run("return JSON")

    assert result.parse_json_response() == {"ready": True}


def test_malformed_or_oversized_event_is_not_success() -> None:
    malformed = OpenCodeCLI(FakeExecutor(ProcessOutput(0, "not-json\n"))).run("bounded task")
    limited = OpenCodeCLI(
        FakeExecutor(ProcessOutput(0, '{"type":"text","part":{"text":"READY"}}\n')),
        config=OpenCodeEngineConfig(max_output_bytes=128, max_event_bytes=10),
    ).run("bounded task")

    assert malformed.status is OpenCodeStatus.MALFORMED_OUTPUT
    assert limited.status is OpenCodeStatus.OUTPUT_LIMITED


def test_timeout_terminates_process() -> None:
    executor = SubprocessOpenCodeExecutor()
    output = executor.execute(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        timeout=0.05,
        max_output_bytes=1024,
        working_directory=None,
    )
    result = OpenCodeCLI(
        FakeExecutor(
            ProcessOutput(
                output.returncode,
                output.stdout,
                output.stderr,
                output.duration_seconds,
                output.termination_reason,
            )
        )
    ).run("bounded task")

    assert result.status is OpenCodeStatus.TIMED_OUT
    assert result.duration_seconds < 2
    assert executor.cancel() is False


def test_cancellation_terminates_active_process() -> None:
    executor = SubprocessOpenCodeExecutor()
    results: list[ProcessOutput] = []
    thread = threading.Thread(
        target=lambda: results.append(
            executor.execute(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                timeout=30,
                max_output_bytes=1024,
                working_directory=None,
            )
        )
    )
    thread.start()
    for _ in range(100):
        if executor.cancel():
            break
        time.sleep(0.01)
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert results[0].termination_reason is TerminationReason.CANCELLED


def test_executor_reports_total_output_limit() -> None:
    executor = SubprocessOpenCodeExecutor()
    output = executor.execute(
        [sys.executable, "-c", "import sys,time; sys.stdout.write('x'*200000); sys.stdout.flush(); time.sleep(30)"],
        timeout=5,
        max_output_bytes=1024,
        working_directory=None,
    )

    assert output.termination_reason is TerminationReason.OUTPUT_LIMIT
    assert len(output.stdout.encode()) <= 1024
