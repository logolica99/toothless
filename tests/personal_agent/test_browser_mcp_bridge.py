from __future__ import annotations

import io
import json
import socket
import threading

from ops.worker.browser_mcp_bridge import _relay_socket


def test_browser_bridge_forwards_mcp_requests_over_unix_socket() -> None:
    bridge, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    failures: list[BaseException] = []

    def serve() -> None:
        try:
            with server, server.makefile("rb") as reader, server.makefile("wb") as writer:
                for line in reader:
                    request = json.loads(line)
                    request_id = request.get("id")
                    if request_id is not None:
                        writer.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": {"method": request["method"]}}).encode() + b"\n")
                        writer.flush()
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=serve)
    thread.start()
    requests = b"\n".join(
        (
            b'{"jsonrpc":"2.0","id":1,"method":"initialize"}',
            b'{"jsonrpc":"2.0","method":"notifications/initialized"}',
            b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}',
            b"",
        )
    )
    output = io.BytesIO()

    assert _relay_socket(io.BytesIO(requests), output, bridge) == 0
    bridge.close()
    thread.join(timeout=2)

    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [item["id"] for item in responses] == [1, 2]
    assert [item["result"]["method"] for item in responses] == ["initialize", "tools/list"]
    assert failures == []
