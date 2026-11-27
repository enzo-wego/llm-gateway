"""Verify that disconnecting from the public gateway closes the worker UDS."""

import os
import socket
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import Final

import pytest
import uvicorn

os.environ.setdefault("LLM_GATEWAY_API_KEY", "test")

from app.main import app  # noqa: E402

_WAIT_S: Final = 3
_KEY: Final = "disconnect-test-key"


def test_public_client_disconnect_closes_in_flight_worker_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = Event()

    class GatewayServer(uvicorn.Server):
        async def startup(self, sockets: list[socket.socket] | None = None) -> None:
            await super().startup(sockets)
            ready.set()

    server = GatewayServer(
        uvicorn.Config(app, log_config=None, ws="none", timeout_graceful_shutdown=1)
    )
    monkeypatch.setenv("CODEX_P_AGENT_KEY", _KEY)
    monkeypatch.delenv("CODEX_AGENT_MEM_KEY", raising=False)
    with (
        TemporaryDirectory(dir="/tmp") as root,
        socket.socket(socket.AF_UNIX) as worker,
        socket.socket() as public,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        path = str(Path(root) / "worker.sock")
        monkeypatch.setenv("CODEX_WORKER_SOCKET", path)
        worker.bind(path)
        worker.listen()
        worker.settimeout(_WAIT_S)
        public.bind(("127.0.0.1", 0))
        running = pool.submit(server.run, sockets=[public])
        try:
            assert ready.wait(_WAIT_S) and server.started, "gateway did not start"
            body = b'{"user":"hold open"}'
            with socket.create_connection(public.getsockname(), timeout=_WAIT_S) as client:
                client.sendall(
                    (
                        "POST /codex HTTP/1.1\r\nHost: localhost\r\n"
                        f"X-API-Key: {_KEY}\r\nContent-Length: {len(body)}\r\n"
                        "Content-Type: application/json\r\n\r\n"
                    ).encode()
                    + body
                )
                upstream, _ = worker.accept()
                with upstream, upstream.makefile("rb") as request:
                    upstream.settimeout(_WAIT_S)
                    assert request.readline().startswith(b"POST /generate ")
                    headers = {}
                    for line in iter(request.readline, b""):
                        if line == b"\r\n":
                            break
                        name, value = line.split(b":", 1)
                        headers[name.lower()] = value.strip()
                    assert b"hold open" in request.read(int(headers[b"content-length"]))
                    # No worker response exists: only closing the public connection can
                    # stop the gateway's in-flight exchange before the worker deadline.
                    client.shutdown(socket.SHUT_RDWR)
                    assert upstream.recv(1) == b"", "gateway left the worker socket open"
        finally:
            server.should_exit = True
            running.result(timeout=_WAIT_S)
