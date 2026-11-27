"""FastAPI entrypoint for `uvicorn codex_worker.main:app --uds …`."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Final

import anyio
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import JsonValue, TypeAdapter, ValidationError
from starlette.requests import ClientDisconnect

from .cli import (
    MAX_BYTES,
    ExecutionError,
    GenerateInput,
    WorkerUnavailableError,
    generate_text,
)

_CONCURRENCY: Final = 2
_BODY_TIMEOUT: Final = 10
_EXEC_TIMEOUT: Final = 155
_JSON_OUTPUT: Final = TypeAdapter[JsonValue](JsonValue)


@asynccontextmanager
async def _lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Own the in-flight gate for the lifetime of this server process."""
    application.state.slots = anyio.CapacityLimiter(_CONCURRENCY)
    yield


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)


def _slots(request: Request) -> anyio.CapacityLimiter:
    return request.app.state.slots


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


async def _cancel_on_disconnect(request: Request, scope: anyio.CancelScope) -> None:
    """Uvicorn does not cancel an in-flight handler when its client disconnects."""
    while (await request.receive())["type"] != "http.disconnect":
        continue
    scope.cancel()


async def _run(body: GenerateInput) -> JSONResponse:
    """Turn CLI output or a known runtime failure into the internal wire format."""
    try:
        with anyio.fail_after(_EXEC_TIMEOUT):
            text = await generate_text(body)
        response = (
            JSONResponse({"text": text, "meta": {}})
            if body.schema_ is None
            else JSONResponse({"output": _JSON_OUTPUT.validate_json(text), "meta": {}})
        )
        if len(response.body) > MAX_BYTES:
            raise ExecutionError
    except TimeoutError:
        return _error(504, "Codex execution timed out")
    except WorkerUnavailableError:
        return _error(503, "Codex worker is not configured")
    except (
        ExecutionError,
        OSError,
        ValueError,
        RecursionError,
        anyio.BrokenResourceError,
        anyio.ClosedResourceError,
    ):
        return _error(502, "Codex execution failed")
    return response


async def _generate(request: Request) -> JSONResponse:
    payload = bytearray()
    try:
        with anyio.fail_after(_BODY_TIMEOUT):
            async for chunk in request.stream():
                if len(payload) + len(chunk) > MAX_BYTES:
                    return _error(413, "Codex request too large")
                payload.extend(chunk)
        body = GenerateInput.model_validate_json(payload)
    except TimeoutError:
        return _error(408, "Codex request timed out")
    except ClientDisconnect:
        return _error(400, "Incomplete Codex request")
    except ValidationError:
        return _error(422, "Invalid Codex request")

    response = _error(400, "Incomplete Codex request")
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(_cancel_on_disconnect, request, tasks.cancel_scope)
        response = await _run(body)
        tasks.cancel_scope.cancel()
    return response


@app.post("/generate")
async def generate(
    request: Request, slots: Annotated[anyio.CapacityLimiter, Depends(_slots)]
) -> JSONResponse:
    """Accept only Uvicorn's UDS scope and reject overload without queueing."""
    server = request.scope["server"]
    if server is not None and server[1] is not None:
        return _error(403, "Unix socket required")
    try:
        slots.acquire_nowait()
    except anyio.WouldBlock:
        return _error(429, "Codex worker is busy")
    try:
        return await _generate(request)
    finally:
        slots.release()
