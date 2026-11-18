#!/usr/bin/env python3
"""Bridge OpenCode stdio MCP traffic to the separately filtered guest helper."""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any, BinaryIO, cast

MAX_MESSAGE = 65_536
RESPONSE_TIMEOUT_SECONDS = 35


def _socket_path() -> str:
    value = os.environ.get("TOOTHLESS_BROWSER_SOCKET", "")
    path = Path(value)
    if not value or not path.is_absolute() or not path.parent.as_posix().startswith("/run/toothless-browser-"):
        raise ValueError("The private browser IPC socket is unavailable")
    if path.name != "mcp.sock":
        raise ValueError("The private browser IPC socket path is invalid")
    return value


def _read_response(reader: Any, expected_id: object, deadline: float) -> bytes:
    while time.monotonic() < deadline:
        raw = reader.readline(MAX_MESSAGE + 1)
        if not raw or len(raw) > MAX_MESSAGE:
            raise RuntimeError("Browser helper closed or exceeded the response limit")
        try:
            response_raw: object = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Browser helper returned malformed JSON-RPC") from exc
        if isinstance(response_raw, dict) and cast(dict[str, object], response_raw).get("id") == expected_id:
            return raw
    raise TimeoutError("Browser helper response timed out")


def _relay_socket(reader: BinaryIO, writer: BinaryIO, connection: socket.socket) -> int:
    connection.settimeout(RESPONSE_TIMEOUT_SECONDS)
    with connection.makefile("rb") as server_reader, connection.makefile("wb") as server_writer:
        while raw := reader.readline(MAX_MESSAGE + 1):
            if len(raw) > MAX_MESSAGE:
                return 1
            try:
                request_raw: object = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return 1
            if not isinstance(request_raw, dict):
                return 1
            request = cast(dict[str, object], request_raw)
            request_id = request.get("id")
            server_writer.write(raw)
            server_writer.flush()
            if request_id is None:
                continue
            response = _read_response(server_reader, request_id, time.monotonic() + RESPONSE_TIMEOUT_SECONDS)
            writer.write(response)
            writer.flush()
    return 0


def _relay(reader: BinaryIO, writer: BinaryIO, path: str) -> int:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(10)
        connection.connect(path)
        return _relay_socket(reader, writer, connection)


def main() -> int:
    try:
        return _relay(sys.stdin.buffer, sys.stdout.buffer, _socket_path())
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        # The caller only sees a generic stderr category; no socket path is logged.
        print(f"Browser MCP bridge {type(exc).__name__}.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
