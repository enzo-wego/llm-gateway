"""Own upstream resources through response delivery and client disconnects."""

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AsyncExitStack

import anyio
import httpx
from starlette.background import BackgroundTask
from starlette.requests import ClientDisconnect
from starlette.responses import Response, StreamingResponse
from starlette.types import Message, Receive, Scope, Send

log = logging.getLogger("llm-gateway.gemini")


async def close_upstream(close: Callable[[], Awaitable[None]]) -> None:
    """Cleanup must finish even if the relay's task group just cancelled it."""
    with anyio.CancelScope(shield=True):
        try:
            await close()
        except httpx.HTTPError as exc:
            log.warning("upstream close failed: %s", type(exc).__name__)


async def read_attempt(
    response: httpx.Response, streaming: bool, resources: AsyncExitStack,
) -> bytes | tuple[bytes, AsyncGenerator[bytes, None]]:
    """Buffer non-success bodies, or hand off a primed iterator and its response."""
    chunks = None
    handed_off = False
    try:
        if streaming and response.is_success:
            chunks = response.aiter_bytes()
            first = await anext(chunks, b"")
            resources.push_async_callback(close_upstream, response.aclose)
            resources.push_async_callback(close_upstream, chunks.aclose)
            handed_off = True
            return first, chunks
        return await response.aread()
    finally:
        if not handed_off:
            try:
                if chunks is not None:
                    await close_upstream(chunks.aclose)
            finally:
                await close_upstream(response.aclose)


def stream_response(
    resp: httpx.Response, data: tuple[bytes, AsyncGenerator[bytes, None]], *,
    record: Callable[[int], BackgroundTask], cooldown: Callable[[str], Awaitable[None]],
) -> StreamingResponse:
    """Relay the prefetched chunk, then continue that same iterator without retries."""
    first, chunks = data
    final = resp.status_code

    async def body() -> AsyncGenerator[bytes, None]:
        nonlocal final
        if first:
            yield first
        try:
            async for chunk in chunks:
                yield chunk
        except httpx.HTTPError as exc:
            final = 502
            await cooldown(type(exc).__name__)

    async def finish() -> None:
        await record(final)()

    return StreamingResponse(
        body(), status_code=resp.status_code, background=BackgroundTask(finish),
        media_type=resp.headers.get("content-type", "text/event-stream"),
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class RelayResponse(Response):
    """Defer upstream work until ASGI owns the whole exchange, including priming."""

    def __init__(self, open_response: Callable[[AsyncExitStack], Awaitable[Response]]) -> None:
        super().__init__()
        self._open_response = open_response

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        background = None
        started = False

        async def send_to_peer(message: Message) -> None:
            nonlocal started
            try:
                if message["type"] == "http.response.start":
                    # Do not let the peer watcher interrupt a response-start frame
                    # partway through its handoff to upstream ASGI middleware.
                    with anyio.CancelScope(shield=True):
                        await send(message)
                        started = True
                else:
                    await send(message)
            except OSError as exc:
                started = True  # failed delivery to a closed peer; do not send again
                raise ClientDisconnect() from exc

        async with AsyncExitStack() as resources:
            async def relay() -> None:
                nonlocal background
                response = await self._open_response(resources)
                background, response.background = response.background, None
                try:
                    if isinstance(response, StreamingResponse):
                        await response.stream_response(send_to_peer)
                    else:
                        await response(scope, receive, send_to_peer)
                except ClientDisconnect:
                    return

            # The route already consumed the request body. Watch peer closure
            # during both the first upstream read and the committed stream.
            async with anyio.create_task_group() as tasks:
                async def watch_disconnect() -> None:
                    while (await receive())["type"] != "http.disconnect":
                        continue
                    tasks.cancel_scope.cancel()

                tasks.start_soon(watch_disconnect)
                try:
                    await relay()
                finally:
                    tasks.cancel_scope.cancel()

        if not started:
            # BaseHTTPMiddleware expects a response start even when the peer is gone.
            # The server discards this response on the already closed connection.
            try:
                await Response(status_code=499)(scope, receive, send_to_peer)
            except ClientDisconnect:
                return
        if background is not None:
            await background()
