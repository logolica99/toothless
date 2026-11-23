from __future__ import annotations

import io
import json
import threading
from pathlib import Path

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


def test_observed_source_manifest_contains_only_bounded_page_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "sources.json"
    monkeypatch.setattr(browser_mcp, "_source_manifest_path", lambda: manifest)
    monkeypatch.setattr(browser_mcp, "_public_url", lambda value: value)
    monkeypatch.setattr(browser_mcp, "_observed_sources", [])

    browser_mcp._save_observed_source(
        "AI · Blogs", "https://blogs.example.com/tags/ai?campaign=ignored", label="AI", http_status=200,
    )

    saved = json.loads(manifest.read_text(encoding="utf-8"))
    assert saved == {"version": 1, "sources": [{
        "title": "AI · Blogs", "url": "https://blogs.example.com/tags/ai", "label": "AI", "http_status": 200,
        "verification_status": "observed",
    }]}
    assert "campaign" not in manifest.read_text(encoding="utf-8")


def test_rendered_navigation_links_are_bounded_and_marked_unvisited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Nav:
        def evaluate_all(self, _script: str) -> list[dict[str, str]]:
            return [
                {"label": "AI", "href": "https://blogs.example.com/tags/ai"},
                {"label": "AI duplicate", "href": "https://blogs.example.com/tags/ai"},
                {"label": "External", "href": "https://social.example.net/"},
                {"label": "Private", "href": "http://127.0.0.1/"},
            ]

    class Page:
        url = "https://blogs.example.com/"

        def locator(self, _selector: str) -> Nav:
            return Nav()

    observed: list[dict[str, object]] = []
    monkeypatch.setattr(browser_mcp, "_observed_sources", observed)
    monkeypatch.setattr(browser_mcp, "_source_manifest_path", lambda: None)

    links = browser_mcp._observed_navigation_links(Page())

    assert links == [{"label": "AI", "url": "https://blogs.example.com/tags/ai"}]
    assert browser_mcp._observed_sources[0]["verification_status"] == "linked_unvisited"


def test_visible_browser_context_is_reused_across_navigate_and_extract_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Locator:
        def inner_text(self, *, timeout: int) -> str:
            assert timeout == browser_mcp.PAGE_TIMEOUT_MS
            return "This domain is for use in documentation examples."

    class Page:
        url = "about:blank"

        def is_closed(self) -> bool:
            return False

        def locator(self, _selector: str) -> Locator:
            return Locator()

        def title(self) -> str:
            return "Example Domain"

        def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
            assert wait_until == "domcontentloaded"
            assert timeout == browser_mcp.PAGE_TIMEOUT_MS
            self.url = url
            return None

    page = Page()

    class Context:
        pages = [page]

    class Browser:
        def is_connected(self) -> bool:
            return True

    monkeypatch.setattr(browser_mcp, "_browser", Browser())
    monkeypatch.setattr(browser_mcp, "_context", Context())
    monkeypatch.setattr(browser_mcp, "_page", page)
    monkeypatch.setattr(browser_mcp, "_playwright", object())
    monkeypatch.setattr(browser_mcp, "_runtime_thread_id", threading.get_ident())
    monkeypatch.setattr(browser_mcp, "_public_url", lambda value: value)

    navigated = browser_mcp._navigate({"url": "https://example.com/"})
    extracted = json.loads(browser_mcp._extract({}))

    assert "https://example.com/" in navigated
    assert extracted["title"] == "Example Domain"
    assert extracted["final_url"] == "https://example.com/"
    assert "documentation examples" in extracted["text"]
    assert browser_mcp._page is page


def test_disconnected_browser_is_closed_and_not_silently_relaunched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Browser:
        def is_connected(self) -> bool:
            return False

    closed: list[str] = []
    monkeypatch.setattr(browser_mcp, "_browser", Browser())
    monkeypatch.setattr(browser_mcp, "_context", object())
    monkeypatch.setattr(browser_mcp, "_page", object())
    monkeypatch.setattr(browser_mcp, "_playwright", object())
    monkeypatch.setattr(browser_mcp, "_runtime_thread_id", threading.get_ident())
    monkeypatch.setattr(browser_mcp, "_close", lambda reason="mcp_cleanup": closed.append(reason))

    with pytest.raises(RuntimeError, match="browser_session_invalid"):
        browser_mcp._ensure_browser()

    assert closed == ["mcp_cleanup"]


def test_extracted_page_text_is_bounded_by_utf8_and_serialized_tool_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Locator:
        def inner_text(self, *, timeout: int) -> str:
            assert timeout == browser_mcp.PAGE_TIMEOUT_MS
            return "α\\\n" * 20_000

    class Page:
        url = "https://example.com/final?private=ignored#fragment"

        def locator(self, _selector: str) -> Locator:
            return Locator()

        def title(self) -> str:
            return "Rendered ✓"

    monkeypatch.setenv("TOOTHLESS_BROWSER_TEXT_LIMIT_BYTES", "8192")
    result = json.loads(browser_mcp._extract_page_result(Page()))

    assert result["title"] == "Rendered ✓"
    assert result["final_url"] == "https://example.com/final"
    assert result["truncated"] is True
    assert result["limit_bytes"] == 8192
    assert result["returned_characters"] == len(result["text"])
    assert result["returned_bytes"] <= result["limit_bytes"]
    assert len(result["text"].encode("utf-8")) == result["returned_bytes"]
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= browser_mcp.MAX_TOOL_RESULT_BYTES
    assert result["text"].startswith("α\\\n")
    broker_wire = json.dumps({"ok": True, "result": json.dumps(result, ensure_ascii=False, separators=(",", ":"))},
                             ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    mcp_wire = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": browser_mcp._text(json.dumps(result, ensure_ascii=False, separators=(",", ":")))},
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    assert len(broker_wire) <= browser_mcp.MAX_MESSAGE
    assert len(mcp_wire) <= browser_mcp.MAX_MESSAGE


def test_page_text_limit_rejects_out_of_review_range(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOOTHLESS_BROWSER_TEXT_LIMIT_BYTES", "65536")
    with pytest.raises(ValueError, match="reviewed bounds"):
        browser_mcp._page_text_limit_bytes()


def test_mcp_text_response_reports_oversized_result_without_truncating() -> None:
    response = browser_mcp._text("x" * (browser_mcp.MAX_TOOL_RESULT_BYTES + 1))
    assert response["isError"] is True
    assert "bounded tool-result limit" in response["content"][0]["text"]


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


@pytest.mark.asyncio
async def test_sync_playwright_runtime_conflict_is_rejected_before_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(browser_mcp, "_page", None)
    monkeypatch.setattr(browser_mcp, "_browser", None)
    monkeypatch.setattr(browser_mcp, "_context", None)
    monkeypatch.setattr(browser_mcp, "_playwright", None)
    monkeypatch.setattr(browser_mcp, "_runtime_thread_id", None)

    with pytest.raises(RuntimeError, match="browser_runtime_conflict"):
        browser_mcp._ensure_browser()


def test_disconnected_browser_invalidates_stale_references_without_relaunch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []

    class DisconnectedBrowser:
        def is_connected(self) -> bool:
            return False

        def close(self) -> None:
            closed.append("browser")

    class Context:
        pages: list[object] = []

        def close(self) -> None:
            closed.append("context")

    class Playwright:
        def stop(self) -> None:
            closed.append("playwright")

    monkeypatch.setattr(browser_mcp, "_browser", DisconnectedBrowser())
    monkeypatch.setattr(browser_mcp, "_context", Context())
    monkeypatch.setattr(browser_mcp, "_page", object())
    monkeypatch.setattr(browser_mcp, "_playwright", Playwright())
    monkeypatch.setattr(browser_mcp, "_runtime_thread_id", threading.get_ident())

    with pytest.raises(RuntimeError, match="browser_session_invalid"):
        browser_mcp._ensure_browser()

    assert closed == ["context", "browser", "playwright"]
    assert browser_mcp._page is None
    assert browser_mcp._context is None
    assert browser_mcp._browser is None
    assert browser_mcp._playwright is None
    assert browser_mcp._runtime_thread_id is None
