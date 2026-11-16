from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from nanobot.providers.base import LLMProvider, LLMResponse


class FakeProvider(LLMProvider):
    """Deterministic, credential-free provider for controller tests."""

    def __init__(self, responses: Sequence[str] = ("offline response",)) -> None:
        super().__init__(provider_name="personal-agent-fake")
        self._responses = list(responses)
        self.requests: list[list[Mapping[str, object]]] = []

    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        self.requests.append(list(messages))
        content = self._responses.pop(0) if self._responses else "offline response"
        return LLMResponse(content=content, tool_calls=[], finish_reason="stop")

    def get_default_model(self) -> str:
        return "offline/fake-v1"


@dataclass(frozen=True)
class WorkerRequest:
    operation_id: str
    task_id: str
    action: str
    arguments: Mapping[str, object]


@dataclass(frozen=True)
class WorkerResult:
    operation_id: str
    status: str
    output: bytes = b""


class WorkerContract(Protocol):
    async def dispatch(self, request: WorkerRequest) -> WorkerResult: ...


class FakeWorker:
    """In-memory contract fixture; it is not evidence of VM isolation."""

    def __init__(self) -> None:
        self.requests: list[WorkerRequest] = []

    async def dispatch(self, request: WorkerRequest) -> WorkerResult:
        self.requests.append(request)
        return WorkerResult(
            operation_id=request.operation_id,
            status="succeeded",
            output=b"fixture output",
        )

