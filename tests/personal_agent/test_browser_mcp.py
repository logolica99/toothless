from __future__ import annotations

import io
import json

import pytest

from ops.worker import browser_mcp


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.1.2.3/",
        "http://100.80.1.2/",
        "http://169.254.169.254/",
        "http://192.168.1.4/",
        "http://[::1]/",
        "file:///etc/passwd",
        "https://user:secret@example.com/",
    ],
)
def test_browser_url_guard_rejects_local_and_non_http_targets(url: str) -> None:
    with pytest.raises(ValueError):
        browser_mcp._public_url(url)


def test_browser_url_guard_accepts_public_ipv4_literal() -> None:
    assert browser_mcp._public_url("https://93.184.216.34/") == "https://93.184.216.34/"


def test_browser_artifact_names_cannot_escape_guest_workspace() -> None:
    for value in ("../escape.png", "/tmp/escape.png", "a/b.png", "bad name.png"):
        with pytest.raises(ValueError):
            browser_mcp._screenshot({"filename": value})


def test_mcp_exposes_raw_tool_names_for_opencode_server_prefixing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(browser_mcp, "_close", lambda: "closed")
    request = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'
    output = io.BytesIO()

    assert browser_mcp._serve(io.BytesIO(request), output) == 0

    response = json.loads(output.getvalue())
    names = [tool["name"] for tool in response["result"]["tools"]]
    assert names == list(browser_mcp.TOOLS)
    assert names == [
        "navigate",
        "extract_text",
        "click_link",
        "click_button",
        "screenshot",
        "download",
        "close_session",
    ]


def test_mcp_calls_raw_tool_name_from_opencode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def call(arguments: dict[str, object]) -> str:
        calls.append(arguments)
        return "tool result"

    monkeypatch.setitem(
        browser_mcp.TOOLS,
        "navigate",
        ("Navigate", {"type": "object"}, call),
    )
    monkeypatch.setattr(browser_mcp, "_close", lambda: "closed")
    request = b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"navigate","arguments":{"url":"https://example.com/"}}}\n'
    output = io.BytesIO()

    assert browser_mcp._serve(io.BytesIO(request), output) == 0

    response = json.loads(output.getvalue())
    assert response["result"]["content"][0]["text"] == "tool result"
    assert calls == [{"url": "https://example.com/"}]
