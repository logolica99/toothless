from pathlib import Path

import pytest

from tests.personal_agent.fakes import FakeProvider, FakeWorker, WorkerRequest


@pytest.mark.asyncio
async def test_offline_provider_conversation() -> None:
    provider = FakeProvider(["hello from the offline provider"])

    response = await provider.chat([{"role": "user", "content": "hello"}])

    assert response.content == "hello from the offline provider"
    assert provider.get_default_model() == "offline/fake-v1"
    assert provider.api_key is None
    assert provider.requests == [[{"role": "user", "content": "hello"}]]


@pytest.mark.asyncio
async def test_worker_fixture_preserves_operation_identity() -> None:
    worker = FakeWorker()
    request = WorkerRequest(
        operation_id="op-001",
        task_id="task-001",
        action="stat",
        arguments={"path": "artifact.csv"},
    )

    result = await worker.dispatch(request)

    assert result.operation_id == request.operation_id
    assert result.status == "succeeded"
    assert worker.requests == [request]


def test_secret_example_contains_only_placeholders() -> None:
    example = Path(".env.example").read_text(encoding="utf-8")

    assert "replace-with-local-secret" in example
    assert "example.invalid" in example
    assert "sk-" not in example

