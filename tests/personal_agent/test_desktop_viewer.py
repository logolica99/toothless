from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import socket
from pathlib import Path

import pytest

from nanobot.personal_agent.desktop_viewer import DesktopViewer

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/n5sAAAAASUVORK5CYII="
)


async def _request(port: int, method: str, target: str, *, headers: dict[str, str] | None = None, body: bytes = b"") -> tuple[int, dict[str, str], bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    values = {"Host": f"127.0.0.1:{port}", "Connection": "close", **(headers or {})}
    if body:
        values["Content-Length"] = str(len(body))
    request_headers = "".join(f"{key}: {value}\r\n" for key, value in values.items())
    writer.write(f"{method} {target} HTTP/1.1\r\n{request_headers}\r\n".encode() + body)
    await writer.drain()
    try:
        response = await asyncio.wait_for(reader.read(), timeout=3)
    except TimeoutError as exc:
        raise AssertionError(f"viewer did not close the HTTP response for {method} {target}") from exc
    writer.close()
    await writer.wait_closed()
    status_line, raw_headers, payload = response.split(b"\r\n", 2)[0], response.split(b"\r\n\r\n", 1)[0], response.split(b"\r\n\r\n", 1)[1]
    status = int(status_line.split()[1])
    parsed = {}
    for line in raw_headers.split(b"\r\n")[1:]:
        key, _, value = line.partition(b":")
        parsed[key.decode().lower()] = value.decode().strip()
    return status, parsed, payload


def _port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.mark.asyncio
async def test_viewer_is_loopback_read_only_authenticated_rate_limited_and_revocable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    import nanobot.personal_agent.desktop_viewer as module

    caplog.set_level("INFO", logger=module.__name__)
    port = _port()
    monkeypatch.setattr(module, "VIEWER_PORT", port)
    captures: list[int] = []

    async def capture() -> bytes:
        captures.append(1)
        return PNG

    token_path = tmp_path / "viewer-token"
    viewer = DesktopViewer(capture, token_file=token_path)
    assert await viewer.open()
    assert token_path.stat().st_mode & 0o777 == 0o600
    token = token_path.read_text()
    status, headers, page = await _request(port, "GET", "/")
    assert status == 200 and b"view only" in page
    assert headers["cache-control"] == "no-store"
    csp = headers["content-security-policy"]
    assert "script-src 'nonce-" in csp and "script-src 'unsafe-inline'" not in csp
    assert (await _request(port, "GET", "/frame"))[0] == 401
    status, auth_headers, _ = await _request(
        port, "POST", "/authorize", headers={"Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"},
        body=json.dumps({"token": token}).encode(),
    )
    assert status == 200
    cookie = auth_headers["set-cookie"].split(";", 1)[0]
    assert "HttpOnly" in auth_headers["set-cookie"] and "SameSite=Strict" in auth_headers["set-cookie"]
    assert (await _request(
        port, "POST", "/authorize", headers={"Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"},
        body=json.dumps({"token": token}).encode(),
    ))[0] == 403
    frame_headers = {"Cookie": cookie, "Sec-Fetch-Site": "same-origin"}
    status, image_headers, image = await _request(port, "GET", "/frame", headers=frame_headers)
    assert status == 200 and image == PNG and image_headers["content-type"] == "image/png"
    assert (await _request(port, "GET", "/frame", headers=frame_headers))[0] == 429
    assert captures == [1]
    frame_logs = [record.getMessage() for record in caplog.records if "desktop_viewer_frame" in record.getMessage()]
    assert len(frame_logs) == 2
    assert "status=200" in frame_logs[0] and f"sha256={hashlib.sha256(PNG).hexdigest()}" in frame_logs[0]
    assert "timestamp=" in frame_logs[0] and "interval_ms=none" in frame_logs[0]
    assert "status=429" in frame_logs[1] and "interval_ms=" in frame_logs[1]
    assert token not in "\n".join(frame_logs) and cookie not in "\n".join(frame_logs)
    revoked = await _request(
        port, "POST", "/revoke",
        headers={"Cookie": cookie, "Origin": f"http://127.0.0.1:{port}", "Sec-Fetch-Site": "same-origin"},
    )
    assert revoked[0] == 200 and not viewer.active
    assert not token_path.exists()


@pytest.mark.asyncio
async def test_viewer_rejects_cross_origin_and_has_no_input_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import nanobot.personal_agent.desktop_viewer as module

    port = _port()
    monkeypatch.setattr(module, "VIEWER_PORT", port)
    viewer = DesktopViewer(lambda: _constant(PNG), token_file=tmp_path / "token")
    await viewer.open()
    token = (tmp_path / "token").read_text()
    status, headers, _ = await _request(
        port, "POST", "/authorize", headers={"Origin": "https://attacker.invalid", "Content-Type": "application/json"},
        body=json.dumps({"token": token}).encode(),
    )
    assert status == 403
    assert (await _request(port, "POST", "/input", body=b'{"key":"x"}'))[0] == 404
    assert (await _request(port, "POST", "/mouse", body=b'{"x":1}'))[0] == 404
    assert (await _request(port, "GET", "/ws"))[0] == 404
    await viewer.close()
    assert not (tmp_path / "token").exists()


@pytest.mark.asyncio
async def test_viewer_accepts_only_configured_remote_origin_and_scopes_secure_cookie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nanobot.personal_agent.desktop_viewer as module

    port = _port()
    origin = "https://toothless.test"
    monkeypatch.setattr(module, "VIEWER_PORT", port)
    monkeypatch.setattr(module, "REMOTE_ORIGIN", origin)
    monkeypatch.setattr(module, "REMOTE_BASE_PATH", "/desktop")
    viewer = DesktopViewer(lambda: _constant(PNG), token_file=tmp_path / "token")
    assert await viewer.open()
    token = viewer.access_code
    assert token

    status, _headers, page = await _request(
        port, "GET", "/desktop/", headers={"Host": "toothless.test"},
    )
    assert status == 200 and b"const base='/desktop'" in page
    assert (await _request(
        port, "GET", "/desktop/", headers={"Host": "attacker.invalid"},
    ))[0] == 421

    status, auth_headers, _ = await _request(
        port, "POST", "/desktop/authorize",
        headers={"Host": "toothless.test", "Origin": origin, "Content-Type": "application/json"},
        body=json.dumps({"token": token}).encode(),
    )
    cookie_header = auth_headers["set-cookie"]
    assert status == 200
    assert "Secure" in cookie_header and "HttpOnly" in cookie_header and "SameSite=Strict" in cookie_header
    assert "Path=/desktop/" in cookie_header
    cookie = cookie_header.split(";", 1)[0]
    frame = await _request(port, "GET", "/desktop/frame", headers={
        "Host": "toothless.test", "Cookie": cookie, "Sec-Fetch-Site": "same-origin",
    })
    assert frame[0] == 200 and frame[2] == PNG
    assert (await _request(
        port, "POST", "/desktop/authorize",
        headers={"Host": "toothless.test", "Origin": origin, "Content-Type": "application/json"},
        body=json.dumps({"token": token}).encode(),
    ))[0] == 403
    assert (await _request(port, "POST", "/desktop/input", headers={"Host": "toothless.test"}))[0] == 404
    await viewer.close()


@pytest.mark.asyncio
async def test_viewer_rejects_symlinked_token_directory(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(target, target_is_directory=True)
    viewer = DesktopViewer(lambda: _constant(PNG), token_file=link / "token")

    with pytest.raises(RuntimeError, match="must not be a symlink"):
        await viewer.open()

    assert not (target / "token").exists()
    await viewer.close()


@pytest.mark.asyncio
async def test_viewer_expiry_closes_listener_and_removes_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nanobot.personal_agent.desktop_viewer as module

    port = _port()
    monkeypatch.setattr(module, "VIEWER_PORT", port)
    monkeypatch.setattr(module, "SESSION_SECONDS", 0.01)
    token_path = tmp_path / "token"
    viewer = DesktopViewer(lambda: _constant(PNG), token_file=token_path)
    await viewer.open()

    await asyncio.sleep(0.05)

    assert not viewer.active
    assert not token_path.exists()
    assert viewer._server is None


async def _constant(value: bytes) -> bytes:
    return value
