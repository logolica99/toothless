"""Loopback-only, image-only desktop viewing sessions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

VIEWER_HOST = "127.0.0.1"
VIEWER_PORT = 8769
SESSION_SECONDS = 600
FRAME_INTERVAL_SECONDS = 2.0
MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_REQUEST_BYTES = 8192
TOKEN_FILE = Path("~/.local/share/toothless/desktop/viewer-token").expanduser()
LOGGER = logging.getLogger(__name__)
REMOTE_ORIGIN = os.environ.get(
    "TOOTHLESS_REMOTE_ORIGIN", "https://toothless.invalid"
).rstrip("/")
REMOTE_BASE_PATH = os.environ.get("TOOTHLESS_DESKTOP_BASE_PATH", "/desktop").rstrip("/")

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1"><meta name="referrer" content="no-referrer"><title>Toothless · Desktop Viewer</title><style>
:root{color-scheme:dark;--background:#161616;--layer:#262626;--layer-hover:#333;--border:#525252;--text:#f4f4f4;--secondary:#c6c6c6;--muted:#a8a8a8;--link:#78a9ff;--focus:#0f62fe;--button:#0f62fe;--button-hover:#0353e9;--support:#42be65}*{box-sizing:border-box}body{margin:0;background:var(--background);color:var(--text);font:16px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}header{height:3rem;padding:0 1.25rem;background:#000;display:flex;align-items:center;font-weight:600;letter-spacing:.02em}main{width:min(100% - 2rem,72rem);margin:3rem auto}.eyebrow{color:var(--link);font-size:.875rem;font-weight:600;letter-spacing:.08em;text-transform:uppercase}h1{font-size:2rem;line-height:1.2;margin:.5rem 0 1rem;font-weight:500}p{color:var(--secondary);max-width:52rem}.notice{background:var(--layer);border-left:3px solid var(--link);padding:1rem 1.25rem;margin:1.5rem 0}.notice strong{color:var(--text)}form{max-width:34rem;display:grid;grid-template-columns:1fr auto;gap:.75rem;align-items:end;margin:2rem 0}label{grid-column:1/-1;font-size:.875rem;font-weight:600}input,button{font:inherit;min-height:3rem;border-radius:0}input{min-width:0;padding:.75rem 1rem;color:var(--text);background:var(--layer);border:0;border-bottom:1px solid var(--muted)}input:focus{outline:2px solid var(--focus);outline-offset:1px}button{padding:.75rem 1.25rem;border:0;background:var(--button);color:white;cursor:pointer}button:hover{background:var(--button-hover)}button:focus-visible{outline:2px solid white;outline-offset:2px}#status{min-height:1.5rem;color:var(--support);font-size:.875rem}figure{margin:2rem 0 0;background:#000;padding:1rem}img{display:block;width:100%;height:auto;max-height:78vh;object-fit:contain}figcaption,footer{color:var(--muted);font-size:.875rem}footer{margin-top:1rem}@media(max-width:600px){main{width:calc(100% - 2rem);margin:2rem auto}h1{font-size:1.65rem}form{grid-template-columns:1fr}label{grid-column:auto}button{width:100%}}[hidden]{display:none!important}
</style></head><body><header>Toothless <span style="color:var(--muted);font-weight:400;margin-left:.5rem">/ Desktop Viewer</span></header><main><div class="eyebrow">Ubuntu workspace</div><h1>Desktop · view only</h1><p>This page receives images only. It has no keyboard, pointer, clipboard, upload, or file-transfer controls.</p><section class="notice"><strong>Read-only session</strong><br>Frames refresh every two seconds. Close the Telegram viewer session to revoke access.</section><form id="auth"><label for="token">One-time viewer code</label><input id="token" type="password" autocomplete="one-time-code" maxlength="96" required><button type="submit">Open viewer</button></form><p id="status" role="status" aria-live="polite"></p><figure><img id="frame" alt="Ubuntu desktop screenshot" hidden><figcaption>Live image from the isolated Ubuntu desktop · refreshes every two seconds</figcaption></figure></main><script nonce="__SCRIPT_NONCE__">const base='__BASE_PATH__',form=document.querySelector('#auth'),status=document.querySelector('#status'),img=document.querySelector('#frame');let timer=null;form.addEventListener('submit',async e=>{e.preventDefault();const token=document.querySelector('#token').value;document.querySelector('#token').value='';const r=await fetch(base+'/authorize',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token})});if(!r.ok){status.textContent='The viewer code is expired or invalid.';return}form.hidden=true;status.textContent='Connected · view only';img.hidden=false;const refresh=async()=>{const f=await fetch(base+'/frame',{cache:'no-store'});if(!f.ok){status.textContent='Viewer ended or expired.';clearInterval(timer);img.hidden=true;return}const b=await f.blob();const old=img.src;img.src=URL.createObjectURL(b);if(old.startsWith('blob:'))URL.revokeObjectURL(old)};await refresh();timer=setInterval(refresh,2000)});</script></body></html>"""


class DesktopViewer:
    """Single-viewer local service; HTTP API exposes only image polling and revocation."""

    def __init__(self, capture: Callable[[], Awaitable[bytes]], *, token_file: Path = TOKEN_FILE) -> None:
        self._capture = capture
        self._token_file = token_file
        self._server: asyncio.AbstractServer | None = None
        self._token_hash: bytes | None = None
        self._access_code: str | None = None
        self._cookie_hash: bytes | None = None
        self._expires_at = 0.0
        self._last_frame_at = 0.0
        self._last_rate_limit_log_at = 0.0
        self._lock = asyncio.Lock()
        self._expiry_task: asyncio.Task[None] | None = None

    def invalidate_stale_token(self) -> None:
        self._token_file.unlink(missing_ok=True)

    @property
    def active(self) -> bool:
        return self._server is not None and self._expires_at > time.monotonic()

    async def open(self) -> bool:
        if self.active:
            return False
        if self._server is not None:
            await self.close()
        token = secrets.token_urlsafe(32)
        self._access_code = token
        self._token_hash = _digest(token)
        self._cookie_hash = None
        self._expires_at = time.monotonic() + SESSION_SECONDS
        self._last_frame_at = 0.0
        self._last_rate_limit_log_at = 0.0
        _prepare_token_file(self._token_file)
        descriptor = os.open(
            self._token_file,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="ascii") as token_stream:
            token_stream.write(token)
        try:
            self._server = await asyncio.start_server(
                self._serve, VIEWER_HOST, VIEWER_PORT, limit=MAX_REQUEST_BYTES + 1
            )
        except OSError:
            await self.close()
            raise
        self._expiry_task = asyncio.create_task(self._expire(), name="toothless-desktop-viewer-expiry")
        return True

    async def close(self) -> None:
        expiry_task, self._expiry_task = self._expiry_task, None
        if expiry_task is not None and expiry_task is not asyncio.current_task():
            expiry_task.cancel()
        server = self._invalidate_session()
        if server is not None:
            await server.wait_closed()

    def _invalidate_session(self) -> asyncio.AbstractServer | None:
        self._expires_at = 0.0
        self._token_hash = None
        self._access_code = None
        self._cookie_hash = None
        self._token_file.unlink(missing_ok=True)
        server, self._server = self._server, None
        if server is not None:
            server.close()
        return server

    async def _expire(self) -> None:
        try:
            await asyncio.sleep(SESSION_SECONDS)
            if self._expires_at and self._expires_at <= time.monotonic():
                await self.close()
        except asyncio.CancelledError:
            raise

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=3)
            if len(request_line) > 1024:
                await _respond(writer, 431, b"Request too large")
                return
            parts = request_line.decode("ascii", "strict").strip().split(" ")
            if len(parts) != 3 or parts[2] != "HTTP/1.1":
                await _respond(writer, 400, b"Invalid request")
                return
            method, target, _ = parts
            headers: dict[str, str] = {}
            total = len(request_line)
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=3)
                total += len(line)
                if total > 4096:
                    await _respond(writer, 431, b"Headers too large")
                    return
                if line in {b"\r\n", b"\n", b""}:
                    break
                key, separator, value = line.decode("latin-1").partition(":")
                normalized_key = key.strip().lower()
                if not separator or not normalized_key or normalized_key in headers:
                    await _respond(writer, 400, b"Invalid headers")
                    return
                headers[normalized_key] = value.strip()
            if "transfer-encoding" in headers:
                await _respond(writer, 400, b"Transfer encoding is not supported")
                return
            expected_remote_host = urlsplit(REMOTE_ORIGIN).netloc.lower() if REMOTE_ORIGIN else ""
            if headers.get("host", "").lower() not in {f"{VIEWER_HOST}:{VIEWER_PORT}", expected_remote_host}:
                await _respond(writer, 421, b"Invalid host")
                return
            origin = headers.get("origin")
            allowed_origins = {f"http://{VIEWER_HOST}:{VIEWER_PORT}"}
            if REMOTE_ORIGIN:
                allowed_origins.add(REMOTE_ORIGIN)
            if origin is not None and origin not in allowed_origins:
                await _respond(writer, 403, b"Cross-origin request denied")
                return
            remote_request = origin == REMOTE_ORIGIN or headers.get("host", "").lower() == expected_remote_host
            route = _route_target(target, remote=remote_request)
            if route == "invalid":
                await _respond(writer, 404, b"Not found")
            elif method == "GET" and route == "/":
                nonce = secrets.token_urlsafe(18)
                page_base = REMOTE_BASE_PATH if remote_request else ""
                page = _PAGE.replace("__SCRIPT_NONCE__", nonce).replace("__BASE_PATH__", page_base).encode()
                await _respond(
                    writer, 200, page, content_type="text/html; charset=utf-8",
                    extra=("Content-Security-Policy: default-src 'none'; img-src 'self' blob:; "
                           f"script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; connect-src 'self'; "
                           "form-action 'self'; base-uri 'none'; frame-ancestors 'none'\r\n"),
                )
            elif method == "POST" and route == "/authorize":
                if origin not in allowed_origins:
                    await _respond(writer, 403, b"Origin required")
                    return
                body = await _body(reader, headers)
                try:
                    token = json.loads(body).get("token", "")
                except (json.JSONDecodeError, AttributeError):
                    token = ""
                if (not isinstance(token, str) or self._token_hash is None or not self.active
                        or not secrets.compare_digest(_digest(token), self._token_hash)):
                    await _respond(writer, 403, b"Invalid viewer code")
                    return
                cookie_value = secrets.token_urlsafe(32)
                self._cookie_hash = _digest(cookie_value)
                self._token_hash = None
                self._access_code = None
                await _respond(
                    writer, 200, b"{\"ok\":true}", content_type="application/json",
                    extra=f"Set-Cookie: toothless_view={cookie_value}; HttpOnly; SameSite=Strict; Path={_cookie_path(origin)}; Max-Age={SESSION_SECONDS}{'; Secure' if origin == REMOTE_ORIGIN and REMOTE_ORIGIN else ''}\r\n",
                )
            elif method == "GET" and route == "/frame":
                if not self._authorized(headers) or not self.active:
                    await _respond(writer, 401, b"Viewer authorization required")
                    return
                async with self._lock:
                    request_started = time.monotonic()
                    elapsed = time.monotonic() - self._last_frame_at
                    if elapsed < FRAME_INTERVAL_SECONDS:
                        await _respond(writer, 429, b"Refresh interval exceeded")
                        now = time.monotonic()
                        if now - self._last_rate_limit_log_at >= 10.0:
                            self._last_rate_limit_log_at = now
                            LOGGER.info(
                                "desktop_viewer_frame timestamp=%s status=429 interval_ms=%d",
                                _utc_now(), round(elapsed * 1000),
                            )
                        return
                    interval_ms = "none" if self._last_frame_at == 0.0 else str(round(elapsed * 1000))
                    self._last_frame_at = time.monotonic()
                    try:
                        image = await self._capture()
                    except Exception as exc:
                        await _respond(writer, 503, b"Frame capture failed")
                        LOGGER.warning(
                            "desktop_viewer_frame timestamp=%s status=503 capture_error=%s",
                            _utc_now(), type(exc).__name__,
                        )
                        return
                    if not _valid_png(image):
                        await _respond(writer, 503, b"Frame is invalid or exceeds the configured limit")
                        LOGGER.warning(
                            "desktop_viewer_frame timestamp=%s status=503 frame=invalid",
                            _utc_now(),
                        )
                        return
                    digest = hashlib.sha256(image).hexdigest()
                    await _respond(writer, 200, image, content_type="image/png")
                    LOGGER.info(
                        "desktop_viewer_frame timestamp=%s status=200 interval_ms=%s response_ms=%d sha256=%s",
                        _utc_now(), interval_ms,
                        round((time.monotonic() - request_started) * 1000), digest,
                    )
            elif method == "POST" and route == "/revoke":
                if not self._authorized(headers) or not self.active:
                    await _respond(writer, 401, b"Viewer authorization required")
                    return
                server = self._invalidate_session()
                await _respond(writer, 200, b"{\"ok\":true}", content_type="application/json")
                if server is not None:
                    asyncio.create_task(server.wait_closed())
            else:
                await _respond(writer, 404, b"Not found")
        except (asyncio.TimeoutError, UnicodeError, ValueError, asyncio.IncompleteReadError):
            await _respond(writer, 400, b"Invalid request")
        except Exception:
            await _respond(writer, 503, b"Viewer unavailable")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    def _authorized(self, headers: dict[str, str]) -> bool:
        if self._cookie_hash is None:
            return False
        if headers.get("sec-fetch-site") not in {None, "same-origin", "none"}:
            return False
        cookie = headers.get("cookie", "")
        value = next((part.partition("=")[2] for part in cookie.split("; ") if part.startswith("toothless_view=")), "")
        return bool(value) and secrets.compare_digest(_digest(value), self._cookie_hash)

    @property
    def access_code(self) -> str | None:
        return self._access_code if self.active else None


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def _cookie_path(origin: str | None) -> str:
    return REMOTE_BASE_PATH + "/" if origin == REMOTE_ORIGIN and REMOTE_ORIGIN else "/"


def _route_target(target: str, *, remote: bool = False) -> str:
    path, separator, query = target.partition("?")
    if not remote:
        return path + (separator + query if separator else "")
    if path.startswith(REMOTE_BASE_PATH + "/"):
        path = path[len(REMOTE_BASE_PATH):]
    elif path == REMOTE_BASE_PATH:
        path = "/"
    elif path not in {"/", "/authorize", "/frame", "/revoke"}:
        return "invalid"
    return path + (separator + query if separator else "")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _valid_png(image: bytes) -> bool:
    if len(image) < 24 or len(image) > MAX_FRAME_BYTES:
        return False
    if image[:8] != b"\x89PNG\r\n\x1a\n" or image[12:16] != b"IHDR":
        return False
    width = int.from_bytes(image[16:20], "big")
    height = int.from_bytes(image[20:24], "big")
    return 1 <= width <= 1280 and 1 <= height <= 800


def _prepare_token_file(path: Path) -> None:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if parent.is_symlink():
        raise RuntimeError("Viewer token directory must not be a symlink")
    info = parent.stat()
    if info.st_uid != os.getuid() or not parent.is_dir():
        raise RuntimeError("Viewer token directory must be owned by the controller user")
    if path.is_symlink():
        path.unlink()
    if path.exists():
        raise RuntimeError("A viewer token already exists; close the previous session first")
    parent.chmod(0o700)


async def _body(reader: asyncio.StreamReader, headers: dict[str, str]) -> bytes:
    value = headers.get("content-length", "")
    if not value.isdecimal() or int(value) > 2048:
        raise ValueError("Invalid request body size")
    return await asyncio.wait_for(reader.readexactly(int(value)), timeout=3)


async def _respond(
    writer: asyncio.StreamWriter,
    status: int,
    body: bytes,
    *,
    content_type: str = "text/plain; charset=utf-8",
    extra: str = "",
) -> None:
    labels = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 413: "Payload Too Large", 421: "Misdirected Request", 429: "Too Many Requests", 431: "Request Header Fields Too Large", 503: "Service Unavailable"}
    headers = (
        f"HTTP/1.1 {status} {labels.get(status, 'Error')}\r\n"
        f"Content-Type: {content_type}\r\nContent-Length: {len(body)}\r\n"
        "Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n"
        "Referrer-Policy: no-referrer\r\nX-Frame-Options: DENY\r\n"
        f"{extra}Connection: close\r\n\r\n"
    )
    writer.write(headers.encode("ascii") + body)
    await writer.drain()
