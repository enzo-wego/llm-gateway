"""Internal Codex authentication and bounded worker exchange."""

import json
import os
from hmac import compare_digest
from pathlib import Path
from typing import TYPE_CHECKING, Final

import anyio
import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError
from starlette.requests import ClientDisconnect

from . import config

if TYPE_CHECKING:
    from .main import GenerateIn

_MAX_BYTES: Final = 1024 * 1024
_BODY_TIMEOUT_S: Final = 10
_WORKER_TIMEOUT_S: Final = 180
_WORKER_URL: Final = "http://codex-worker/generate"  # HTTP target; transport is UDS only.
_BAD_WORKER_RESPONSE: Final = "invalid Codex worker response"
_INCOMPLETE_REQUEST: Final = "incomplete Codex request"
_INVALID_REQUEST: Final = "invalid Codex request"
_WORKER_UNAVAILABLE: Final = "Codex worker unavailable"
_WORKER_TIMED_OUT: Final = "Codex worker timed out"
_WORKER_ERROR_DETAILS: Final = {
    429: "Codex worker is busy",
    503: _WORKER_UNAVAILABLE,
    504: _WORKER_TIMED_OUT,
}


def _require_service_key(request: Request) -> None:
    candidates = request.headers.getlist("x-api-key")
    if len(candidates) != 1:
        raise HTTPException(401, "bad or missing X-API-Key")

    supplied = candidates[0].encode("latin-1")
    p_agent = os.getenv("CODEX_P_AGENT_KEY", "").strip().encode()
    agent_mem = os.getenv("CODEX_AGENT_MEM_KEY", "").strip().encode()
    shared = config.API_KEY.encode()
    matches_p_agent = bool(p_agent) and compare_digest(supplied, p_agent)
    matches_agent_mem = bool(agent_mem) and compare_digest(supplied, agent_mem)
    if matches_p_agent == matches_agent_mem or compare_digest(supplied, shared):
        raise HTTPException(401, "bad or missing X-API-Key")


async def _read_request(request: Request, model: type["GenerateIn"]) -> "GenerateIn":
    payload = bytearray()
    try:
        with anyio.fail_after(_BODY_TIMEOUT_S):
            async for chunk in request.stream():
                if len(payload) + len(chunk) > _MAX_BYTES:
                    raise HTTPException(413, "Codex request too large")
                payload.extend(chunk)
        body = model.model_validate_json(payload)
    except TimeoutError:
        raise HTTPException(408, "Codex request timed out") from None
    except ClientDisconnect:
        raise HTTPException(400, _INCOMPLETE_REQUEST) from None
    except ValidationError:
        raise HTTPException(422, _INVALID_REQUEST) from None
    if not body.user.strip():
        raise HTTPException(422, _INVALID_REQUEST)
    return body


class _WorkerResult(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    meta: dict[str, JsonValue]


class _WorkerText(_WorkerResult):
    text: str


class _WorkerOutput(_WorkerResult):
    output: JsonValue


async def _worker_response(socket: Path, payload: bytes) -> bytearray:
    """Read at most one uncompressed response, with no redirects or retries."""
    data = bytearray()
    transport = httpx.AsyncHTTPTransport(
        uds=str(socket), trust_env=False, limits=httpx.Limits(max_connections=1)
    )
    timeout = httpx.Timeout(connect=2, read=_WORKER_TIMEOUT_S, write=10, pool=2)
    try:
        with anyio.fail_after(_WORKER_TIMEOUT_S):
            async with (
                httpx.AsyncClient(
                    transport=transport,
                    timeout=timeout,
                    trust_env=False,
                    follow_redirects=False,
                    headers={"Accept-Encoding": "identity"},
                ) as client,
                client.stream(
                    "POST",
                    _WORKER_URL,
                    content=payload,
                    headers={"Content-Type": "application/json"},
                ) as response,
            ):
                status = response.status_code
                if status != 200:
                    detail = _WORKER_ERROR_DETAILS.get(status)
                    if detail is not None:
                        raise HTTPException(status, detail)
                    raise HTTPException(502, _BAD_WORKER_RESPONSE)
                encoding = response.headers.get("content-encoding", "identity")
                if encoding.lower() != "identity":
                    raise HTTPException(502, _BAD_WORKER_RESPONSE)
                async for chunk in response.aiter_raw():
                    if len(data) + len(chunk) > _MAX_BYTES:
                        raise HTTPException(502, _BAD_WORKER_RESPONSE)
                    data.extend(chunk)
    except (TimeoutError, httpx.TimeoutException):
        raise HTTPException(504, _WORKER_TIMED_OUT) from None
    except httpx.RequestError:
        raise HTTPException(502, _WORKER_UNAVAILABLE) from None
    return data


async def _cancel_on_disconnect(request: Request, scope: anyio.CancelScope) -> None:
    """Watch the ASGI receive channel after the request body has been read."""
    while (await request.receive())["type"] != "http.disconnect":
        continue
    scope.cancel()


async def generate(request: Request, model: type["GenerateIn"]) -> JSONResponse:
    """Authenticate before reading the request or attempting worker access."""
    _require_service_key(request)
    socket = Path(os.getenv("CODEX_WORKER_SOCKET", ""))
    if not socket.is_absolute():
        raise HTTPException(503, "Codex worker is not configured")

    body = await _read_request(request, model)
    try:
        payload = json.dumps(
            body.model_dump(by_alias=True),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
    except (ValueError, RecursionError):
        raise HTTPException(422, _INVALID_REQUEST) from None
    if len(payload) > _MAX_BYTES:
        raise HTTPException(413, "Codex request too large")

    raw = None
    failure = None
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(_cancel_on_disconnect, request, tasks.cancel_scope)
        try:
            raw = await _worker_response(socket, payload)
        except HTTPException as exc:
            # Re-raise outside the group so FastAPI sees the original status.
            failure = exc
        finally:
            tasks.cancel_scope.cancel()
    if failure is not None:
        raise failure
    if raw is None:
        raise HTTPException(400, _INCOMPLETE_REQUEST)
    result_model = _WorkerText if body.schema_ is None else _WorkerOutput
    try:
        result = result_model.model_validate_json(raw)
        response = JSONResponse({"backend": "codex", **result.model_dump()})
    except (ValueError, RecursionError):
        raise HTTPException(502, _BAD_WORKER_RESPONSE) from None
    if len(response.body) > _MAX_BYTES:
        raise HTTPException(502, _BAD_WORKER_RESPONSE)
    return response
