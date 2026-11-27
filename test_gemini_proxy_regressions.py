"""Exercise proxy failover and delivery against controlled local transports."""

from collections.abc import AsyncIterator
from pathlib import Path
from sqlite3 import Row
from threading import Event, get_ident

import anyio
import httpx
import pytest
from fastapi import FastAPI
from starlette.types import Message, Scope

from app import config, gemini_proxy, gemini_store as store

PATH = "/gemini/v1beta/models/model:streamGenerateContent"
KEYS = ("test-provider-key-one", "test-provider-key-two")
HEADERS = {"x-goog-api-key": "test-gateway-key"}


@pytest.fixture
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setattr(config, "SECRET_KEY", "test-encryption-key")
    monkeypatch.setattr(config, "API_KEY", HEADERS["x-goog-api-key"])
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "gemini.db"))
    monkeypatch.setattr(config, "GEMINI_UPSTREAM", "http://provider.test")
    monkeypatch.setattr(gemini_proxy, "_rr", 0)
    for key in KEYS:
        store.add_key(key, key)
    app = FastAPI()
    app.include_router(gemini_proxy.router)
    return app


class BrokenBody(httpx.AsyncByteStream):
    def __init__(self, prefix: bytes = b"") -> None:
        self.prefix = prefix
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.prefix
        raise httpx.ReadTimeout("controlled timeout")

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize("upstream_status", [200, 429])
def test_read_failure_before_commit_tries_next_key(
    gateway: FastAPI, monkeypatch: pytest.MonkeyPatch, upstream_status: int,
) -> None:
    first = BrokenBody(b"" if upstream_status == 200 else b'{"partial":')

    def upstream(request: httpx.Request) -> httpx.Response:
        if request.headers["x-goog-api-key"] == KEYS[0]:
            return httpx.Response(upstream_status, stream=first)
        assert first.closed
        return httpx.Response(201, content=b"data: replacement\n\n")

    monkeypatch.setattr(gemini_proxy, "transport", httpx.MockTransport(upstream))

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway), base_url="http://gateway.test",
        ) as caller:
            response = await caller.post(PATH, headers=HEADERS)
        assert (response.status_code, response.content) == (201, b"data: replacement\n\n")

    anyio.run(run)
    assert store.list_keys()[0]["consecutive_failures"] == 1


@pytest.mark.parametrize("after_first_byte", [False, True])
def test_stale_attempt_does_not_penalize_a_key_reenabled_in_flight(
    gateway: FastAPI, monkeypatch: pytest.MonkeyPatch, after_first_byte: bool,
) -> None:
    first, _ = store.list_keys()
    key_id = first["id"]
    chunk = b"data: committed\n\n"
    attempts: list[str] = []

    def reenable_key() -> None:
        store.set_status(key_id, "disabled")
        store.set_status(key_id, "active")

    class LateFailure(BrokenBody):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield chunk
            await anyio.to_thread.run_sync(reenable_key)
            raise httpx.ReadTimeout("controlled timeout after operator action")

    async def upstream(request: httpx.Request) -> httpx.Response:
        key = request.headers["x-goog-api-key"]
        attempts.append(key)
        if key == KEYS[1]:
            return httpx.Response(201, content=b"replacement")
        if after_first_byte:
            return httpx.Response(200, stream=LateFailure())
        await anyio.to_thread.run_sync(reenable_key)
        return httpx.Response(403, content=b"stale provider rejection")

    monkeypatch.setattr(gemini_proxy, "transport", httpx.MockTransport(upstream))

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway), base_url="http://gateway.test",
        ) as caller:
            response = await caller.post(PATH, headers=HEADERS)
        if after_first_byte:
            assert (response.status_code, response.content) == (200, chunk)
            assert attempts == [KEYS[0]]
        else:
            assert (response.status_code, response.content) == (201, b"replacement")
            assert attempts == list(KEYS)

    anyio.run(run)
    assert key_id in {row["id"] for row in store.usable_keys()}


def test_clean_empty_success_is_not_retried(
    gateway: FastAPI, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-goog-api-key"] == KEYS[0]
        return httpx.Response(204)

    monkeypatch.setattr(gemini_proxy, "transport", httpx.MockTransport(upstream))

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway), base_url="http://gateway.test",
        ) as caller:
            response = await caller.post(PATH, headers=HEADERS)
        assert (response.status_code, response.content) == (204, b"")

    anyio.run(run)


@pytest.mark.parametrize("prefix", [b"", b"data: first\n\n"])
def test_disconnect_closes_upstream_during_body_read(
    gateway: FastAPI, monkeypatch: pytest.MonkeyPatch, prefix: bytes,
) -> None:
    async def run() -> None:
        reading = anyio.Event()
        closed = anyio.Event()
        messages: list[Message] = []

        class HeldBody(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                yield prefix
                reading.set()
                await anyio.sleep_forever()

            async def aclose(self) -> None:
                await anyio.lowlevel.checkpoint()
                closed.set()

        def upstream(request: httpx.Request) -> httpx.Response:
            assert request.headers["x-goog-api-key"] == KEYS[0]
            return httpx.Response(200, stream=HeldBody())

        monkeypatch.setattr(gemini_proxy, "transport", httpx.MockTransport(upstream))
        sent = False

        async def receive() -> Message:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": b"{}"}
            await reading.wait()
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            messages.append(message)

        with anyio.fail_after(2):
            await gateway(_scope(), receive, send)
        assert closed.is_set()
        chunks = [m["body"] for m in messages if m["type"] == "http.response.body"]
        if not prefix:
            assert [m["status"] for m in messages if m["type"] == "http.response.start"] == [499]
            assert chunks == [b""]
        else:
            assert chunks == [prefix]

    anyio.run(run)


def _scope() -> Scope:
    return {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1", "method": "POST", "scheme": "http", "path": PATH,
        "query_string": b"", "headers": [(b"x-goog-api-key", b"test-gateway-key")],
    }


def test_telemetry_waits_until_unauthorized_body_is_sent(
    gateway: FastAPI, monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal_body = Event()
    original = store.log_request

    async def run() -> None:
        loop_thread = get_ident()

        def log_request(
            route: str, model: str | None, key_id: int | None, status: int,
            latency_ms: int, attempts: int, prompt_tokens: int | None, output_tokens: int | None,
        ) -> None:
            assert terminal_body.is_set()
            assert get_ident() != loop_thread
            original(route, model, key_id, status, latency_ms, attempts, prompt_tokens, output_tokens)

        monkeypatch.setattr(store, "log_request", log_request)

        async def receive() -> Message:
            raise AssertionError("auth should not read the body")

        async def send(message: Message) -> None:
            if message["type"] == "http.response.start":
                assert message["status"] == 401
            if message["type"] == "http.response.body" and not message.get("more_body"):
                terminal_body.set()

        await gateway({**_scope(), "headers": []}, receive, send)

    anyio.run(run)
    assert [row["status"] for row in store.recent_requests()] == [401]


@pytest.mark.parametrize("operation", ["usable_keys", "mark_ok"])
def test_blocked_pool_io_does_not_block_an_unrelated_request(
    gateway: FastAPI, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    @gateway.get("/unrelated")
    async def unrelated() -> str:
        return "available"

    monkeypatch.setattr(
        gemini_proxy, "transport", httpx.MockTransport(lambda _: httpx.Response(200, content=b"ok")),
    )
    original = getattr(store, operation)

    async def run() -> None:
        entered = anyio.Event()
        release = Event()
        loop_thread = get_ident()

        def blocked(*args: int, **kwargs: int) -> list[Row] | None:
            assert get_ident() != loop_thread
            anyio.from_thread.run_sync(entered.set)
            assert release.wait(2), "unrelated request could not advance while pool SQL was blocked"
            return original(*args, **kwargs)

        monkeypatch.setattr(store, operation, blocked)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway), base_url="http://gateway.test",
        ) as caller:
            async def relay() -> None:
                response = await caller.post(PATH, headers=HEADERS)
                assert response.content == b"ok"

            with anyio.fail_after(2):
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(relay)
                    try:
                        await entered.wait()
                        assert (await caller.get("/unrelated")).json() == "available"
                    finally:
                        release.set()

    anyio.run(run)
