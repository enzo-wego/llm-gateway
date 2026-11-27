"""Gemini-native pass-through proxy over a pool of API keys.

Callers (the chatbot's @ai-sdk/google) authenticate with the gateway secret in
``x-goog-api-key``; the gateway swaps in a pooled key, forwards body bytes
unchanged unless the caller opts into effort, and relays the upstream response.
A failing key is put in cooldown or marked rejected and the next key is tried.

Never logs or stores prompt/response content, and never logs a key.
"""

import hmac
import json
import logging
import re
import sqlite3
import time
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from functools import partial
from typing import Literal, assert_never

import anyio
import httpx
from anyio.lowlevel import RunVar
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask

from . import config, gemini_store as store
from .gemini_proxy_effort import EffortError, apply_effort
from .gemini_proxy_stream import RelayResponse, close_upstream, read_attempt, stream_response

log = logging.getLogger("llm-gateway.gemini")
router = APIRouter()

ACTIONS = {"generateContent", "streamGenerateContent", "embedContent",
           "batchEmbedContents", "countTokens"}
MAX_ATTEMPTS = 5
TIMEOUT = httpx.Timeout(connect=10, write=30, read=120, pool=10)
_MODEL_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_RETRYABLE = {429, 500, 502, 503, 504}

# Tests set this to an httpx.MockTransport / ASGITransport; None means real network.
transport: httpx.AsyncBaseTransport | None = None
_rr = 0
_pool_limiter = RunVar[anyio.CapacityLimiter]("gemini-proxy-pool")
_log_limiter = RunVar[anyio.CapacityLimiter]("gemini-proxy-log")


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT, transport=transport)


# ── helpers ──────────────────────────────────────────────────────────────────
def _retry_after(headers: httpx.Headers) -> float | None:
    v = headers.get("retry-after")
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None  # HTTP-date form: fall back to the default schedule


def _error_reason(body: bytes) -> str:
    """Upstream error reason only (never message text): e.g. API_KEY_INVALID."""
    try:
        err = json.loads(body).get("error", {})
        for d in err.get("details") or []:
            if isinstance(d, dict) and d.get("reason"):
                return str(d["reason"])
        return str(err.get("status") or "")
    except (ValueError, AttributeError):
        return ""


def classify(status: int, body: bytes) -> Literal["ok", "retry", "rejected", "return"]:
    """ok | retry | rejected | return"""
    if 200 <= status < 300:
        return "ok"
    if status in _RETRYABLE:
        return "retry"
    if status in (401, 403):
        return "rejected"
    if status == 400 and b"API_KEY_INVALID" in body[:4096] and _error_reason(body) == "API_KEY_INVALID":
        return "rejected"
    return "return"


def _check_caller(request: Request) -> bool:
    given = request.headers.get("x-goog-api-key") or request.headers.get("x-api-key") or ""
    return hmac.compare_digest(given.encode(), config.API_KEY.encode())


def pick_attempts() -> list[sqlite3.Row]:
    """Round robin over usable keys, at most MAX_ATTEMPTS distinct; one fallback attempt if none."""
    global _rr
    usable = store.usable_keys()
    if usable:
        start = _rr % len(usable)
        _rr += 1
        order = usable[start:] + usable[:start]
        return order[:min(len(usable), MAX_ATTEMPTS)]
    soonest = store.soonest_active()
    return [soonest] if soonest else []


def _usage(body: bytes) -> tuple[int | None, int | None]:
    try:
        u = json.loads(body).get("usageMetadata") or {}
        return u.get("promptTokenCount"), u.get("candidatesTokenCount")
    except (ValueError, AttributeError):
        return None, None


def _passthrough_headers(headers: httpx.Headers) -> dict[str, str]:
    out = {}
    if "retry-after" in headers:
        out["Retry-After"] = headers["retry-after"]
    return out


def _json(status: int, error: str) -> JSONResponse:
    return JSONResponse({"error": error}, status_code=status)


def _record(route: str, model: str | None, key_id: int | None, status: int, started: float,
            attempts: int, tokens: tuple[int | None, int | None] = (None, None)) -> BackgroundTask:
    latency = int((time.monotonic() - started) * 1000)

    async def write() -> None:
        try:
            await anyio.to_thread.run_sync(
                store.log_request, route, model, key_id, status, latency, attempts, *tokens,
                limiter=_worker_limit(_log_limiter, 1),
            )
        except (sqlite3.Error, OSError) as exc:
            log.warning("request_log write failed: %s", type(exc).__name__)

    return BackgroundTask(write)


def _worker_limit(state: RunVar[anyio.CapacityLimiter], workers: int) -> anyio.CapacityLimiter:
    limiter = state.get(None)
    if limiter is None:
        limiter = anyio.CapacityLimiter(workers)
        state.set(limiter)
    return limiter


@dataclass(frozen=True, slots=True)
class _Call:
    model: str
    action: str
    started: float
    body: bytes
    sse: bool
    content_type: str

    @property
    def streaming(self) -> bool:
        return self.action == "streamGenerateContent"

    def record(
        self, key_id: int | None, status: int, attempts: int,
        tokens: tuple[int | None, int | None] = (None, None),
    ) -> BackgroundTask:
        return _record(self.action, self.model, key_id, status, self.started, attempts, tokens)


# ── route ────────────────────────────────────────────────────────────────────
@router.post("/gemini/v1beta/models/{spec}")
async def proxy(spec: str, request: Request) -> Response:
    started = time.monotonic()
    efforts = request.query_params.getlist("effort")
    if not _check_caller(request):
        response = _json(401, "unauthorized")
        if not efforts and store.enabled():
            response.background = _record("gemini", None, None, 401, started, 0)
        return response
    if not store.enabled():
        return _json(503, "gemini_disabled")

    model, _, action = spec.rpartition(":")
    if not model or action not in ACTIONS or not _MODEL_RE.match(model):
        response = _json(404, "not_found")
        if not efforts:
            response.background = _record("gemini:unknown", None, None, 404, started, 0)
        return response

    body = await request.body()
    if efforts:
        try:
            body = apply_effort(model, action, efforts, body)
        except EffortError as exc:
            return _json(400, exc.code)
    call = _Call(model, action, started, body, request.query_params.get("alt") == "sse",
                 request.headers.get("content-type", "application/json"))
    if call.streaming:
        return RelayResponse(partial(_forward, call))
    async with AsyncExitStack() as resources:
        return await _forward(call, resources)


async def _forward(call: _Call, resources: AsyncExitStack) -> Response:
    try:
        return await _try_keys(call, resources)
    except (sqlite3.OperationalError, OSError) as exc:
        log.warning("gemini pool unavailable: %s", type(exc).__name__)
        response = _json(503, "gemini_pool_unavailable")
        response.background = call.record(None, 503, 0)
        return response


async def _try_keys(call: _Call, resources: AsyncExitStack) -> Response:
    # Keep proxy key-state operations serialized, independently of telemetry.
    run_sql = partial(anyio.to_thread.run_sync, limiter=_worker_limit(_pool_limiter, 1))
    attempts_keys = await run_sql(pick_attempts)
    if not attempts_keys:
        response = _json(503, "no_gemini_keys")
        response.background = call.record(None, 503, 0)
        return response

    params = {"alt": "sse"} if call.sse else {}
    url = f"{config.GEMINI_UPSTREAM}/v1beta/models/{call.model}:{call.action}"
    c = client()
    resources.push_async_callback(close_upstream, c.aclose)
    last: tuple[int, bytes, httpx.Headers] | None = None
    tried = 0
    key_id: int | None = None
    for row in attempts_keys:
        tried += 1
        key_id = row["id"]
        version = row["operator_version"]
        mark = partial(_mark_key, key_id, version)
        secret = store.plaintext(row)
        if secret is None:  # undecryptable (SECRET_KEY changed): out of rotation
            await mark(store.mark_rejected, "decrypt_failed")
            last = None
            continue
        req = c.build_request("POST", url, params=params, content=call.body,
                              headers={"Content-Type": call.content_type, "x-goog-api-key": secret})
        try:
            resp = await c.send(req, stream=True)
            data = await read_attempt(resp, call.streaming, resources)
        except httpx.HTTPError as exc:
            await mark(store.mark_cooldown, type(exc).__name__, None)
            last = None
            continue

        if isinstance(data, tuple):
            await mark(store.mark_ok)
            return stream_response(
                resp, data, record=partial(call.record, key_id, attempts=tried),
                cooldown=partial(_stream_cooldown, key_id, version),
            )

        verdict = classify(resp.status_code, data)
        if verdict == "ok":
            await mark(store.mark_ok)
            return Response(data, status_code=resp.status_code,
                            media_type=resp.headers.get("content-type", "application/json"),
                            background=call.record(key_id, resp.status_code, tried, _usage(data)))
        err = f"{resp.status_code} {_error_reason(data)}".strip()
        last = (resp.status_code, data, resp.headers)
        match verdict:
            case "retry":
                await mark(store.mark_cooldown, err, _retry_after(resp.headers))
            case "rejected":
                await mark(store.mark_rejected, err)
            case "return":
                break
            case unreachable:
                assert_never(unreachable)

    if last is None:
        response = _json(502, "gemini_upstream_unreachable")
        response.background = call.record(key_id, 502, tried)
        return response
    status, data, hdrs = last
    return Response(data, status_code=status,
                    media_type=hdrs.get("content-type", "application/json"),
                    headers=_passthrough_headers(hdrs), background=call.record(key_id, status, tried))


async def _mark_key(
    key_id: int, version: int,
    operation: Callable[..., None | float], *args: str | float | None,
) -> None:
    await anyio.to_thread.run_sync(
        partial(operation, key_id, *args, operator_version=version),
        limiter=_worker_limit(_pool_limiter, 1),
    )


async def _stream_cooldown(key_id: int, version: int, error: str) -> None:
    try:
        await _mark_key(key_id, version, store.mark_cooldown, error, None)
    except (sqlite3.OperationalError, OSError) as exc:
        log.warning("gemini stream cooldown write failed: %s", type(exc).__name__)
