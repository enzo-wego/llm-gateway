"""Gemini SSE behavior on loopback sockets and before stream commitment."""

import socket
import threading
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from functools import wraps

import anyio
import httpx
import pytest
import uvicorn
from starlette.types import ASGIApp, Receive, Scope, Send

from gemini_test_support import H, KEYS, STREAM, add, app, client, config, env, gemini_proxy, post, store

WAIT = 5.0


class UpstreamStreamReset(Exception):
    """Terminate the fake ASGI stream without a final response frame."""


@contextmanager
def _serve(asgi_app: ASGIApp) -> Iterator[int]:
    ready = threading.Event()

    class ReadyServer(uvicorn.Server):
        async def startup(self, sockets: list[socket.socket] | None = None) -> None:
            await super().startup(sockets)
            ready.set()

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = ReadyServer(uvicorn.Config(asgi_app, log_config=None, ws="none",
                                           timeout_graceful_shutdown=2))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            assert ready.wait(WAIT) and server.started, "loopback server failed to start"
            yield port
        finally:
            server.should_exit = True
            thread.join(WAIT)
            assert not thread.is_alive(), "loopback server failed to stop"


@dataclass(frozen=True, slots=True)
class LiveStream:
    url: str
    hits: list[str]
    release: threading.Event
    finished: threading.Event
    logged: threading.Event


@pytest.fixture
def live(env, monkeypatch):
    monkeypatch.setattr(gemini_proxy, "transport", None)
    with ExitStack() as servers:
        def start(*, crash: bool = False) -> LiveStream:
            release, finished, logged = threading.Event(), threading.Event(), threading.Event()
            hits: list[str] = []
            log_request = store.log_request

            @wraps(log_request)
            def observed_log(*args, **kwargs) -> None:
                log_request(*args, **kwargs)
                logged.set()

            monkeypatch.setattr(store, "log_request", observed_log)

            async def fake(scope: Scope, receive: Receive, send: Send) -> None:
                if scope["type"] != "http":
                    return
                hits.append(dict(scope["headers"])[b"x-goog-api-key"].decode())
                await send({"type": "http.response.start", "status": 200,
                            "headers": [(b"content-type", b"text/event-stream")]})
                await send({"type": "http.response.body", "body": b"data: chunk1\n\n", "more_body": True})
                if crash:
                    raise UpstreamStreamReset
                assert await anyio.to_thread.run_sync(release.wait, 2 * WAIT), "test never released upstream"
                await send({"type": "http.response.body", "body": b"data: chunk2\n\n"})

            async def gateway(scope: Scope, receive: Receive, send: Send) -> None:
                try:
                    await app(scope, receive, send)
                finally:
                    if scope["type"] == "http":
                        finished.set()

            upstream_port = servers.enter_context(_serve(fake))
            monkeypatch.setattr(config, "GEMINI_UPSTREAM", f"http://127.0.0.1:{upstream_port}")
            gateway_port = servers.enter_context(_serve(gateway))
            servers.callback(release.set)
            return LiveStream(f"http://127.0.0.1:{gateway_port}{STREAM}", hits, release, finished, logged)

        yield start


def _events(response: httpx.Response) -> Iterator[str]:
    data: list[str] = []
    for line in response.iter_lines():
        if line.startswith("data:"):
            data.append(line.removeprefix("data:").removeprefix(" "))
        if not line and data:
            yield "\n".join(data)
            data.clear()


def test_stream_is_incremental(live):
    add(1)
    stream = live()
    with httpx.Client(timeout=WAIT, trust_env=False) as c:
        with c.stream("POST", stream.url, headers=H, content=b"{}") as r:
            assert r.status_code == 200
            events = _events(r)
            assert next(events) == "chunk1" and not stream.release.is_set()
            stream.release.set()
            assert list(events) == ["chunk2"]
    assert stream.hits == [KEYS[0]]


def test_no_failover_after_first_byte(live):
    add(2)
    stream = live(crash=True)
    with httpx.Client(timeout=WAIT, trust_env=False) as c:
        with c.stream("POST", stream.url, headers=H, content=b"{}") as r:
            assert r.status_code == 200
            events = _events(r)
            assert next(events) == "chunk1"
            # The gateway may end the committed stream cleanly or close it short.
            with suppress(httpx.RemoteProtocolError):
                assert list(events) == []
    assert stream.finished.wait(WAIT), "gateway did not finish after upstream reset"
    assert stream.hits == [KEYS[0]]


def test_stream_fails_over_on_429_before_first_byte(client, env):
    add(2)
    env.script[KEYS[0]] = [(429, {}, b'{"error":{"status":"RESOURCE_EXHAUSTED"}}')]
    r = post(client, path=STREAM)
    assert r.status_code == 200
    assert env.keys_used == [KEYS[0], KEYS[1]]


def test_client_disconnect_mid_stream_still_logs(live):
    add(1)
    stream = live()
    with httpx.Client(timeout=WAIT, trust_env=False) as c:
        with c.stream("POST", stream.url, headers=H, content=b"{}") as r:
            assert r.status_code == 200
            assert next(_events(r)) == "chunk1"
            assert not stream.logged.is_set()
    assert stream.logged.wait(WAIT), "gateway did not record the disconnected request"
    assert not stream.release.is_set()
    rows = store.recent_requests(10)
    assert len(rows) == 1 and rows[0]["route"].endswith("streamGenerateContent")
