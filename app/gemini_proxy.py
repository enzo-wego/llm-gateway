"""Gemini-native pass-through proxy over a pool of API keys.

Callers (the chatbot's @ai-sdk/google) authenticate with the gateway secret in
``x-goog-api-key``; the gateway swaps in a pooled key, forwards body bytes
unchanged and returns the upstream status/body unchanged. A failing key is put
in cooldown or marked rejected and the next key is tried.

Never logs or stores prompt/response content, and never logs a key.
"""

import hmac
import json
import logging
import re
import time
from typing import Any

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from . import config, gemini_store as store

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


def classify(status: int, body: bytes) -> str:
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


def pick_attempts() -> list[Any]:
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
            attempts: int, tokens: tuple[int | None, int | None] = (None, None)) -> None:
    try:
        store.log_request(route, model, key_id, status, int((time.monotonic() - started) * 1000),
                          attempts, tokens[0], tokens[1])
    except Exception:  # noqa: BLE001 - logging must never break a proxied call
        log.warning("request_log write failed", exc_info=True)


# ── route ────────────────────────────────────────────────────────────────────
@router.post("/gemini/v1beta/models/{spec}")
async def proxy(spec: str, request: Request) -> Response:
    started = time.monotonic()
    if not _check_caller(request):
        if store.enabled():
            _record("gemini", None, None, 401, started, 0)
        return _json(401, "unauthorized")
    if not store.enabled():
        return _json(503, "gemini_disabled")

    model, _, action = spec.rpartition(":")
    if not model or action not in ACTIONS or not _MODEL_RE.match(model):
        _record("gemini:unknown", None, None, 404, started, 0)
        return _json(404, "not_found")

    attempts_keys = pick_attempts()
    if not attempts_keys:
        _record(action, model, None, 503, started, 0)
        return _json(503, "no_gemini_keys")

    body = await request.body()
    params = {"alt": "sse"} if request.query_params.get("alt") == "sse" else {}
    url = f"{config.GEMINI_UPSTREAM}/v1beta/models/{model}:{action}"
    streaming = action == "streamGenerateContent"
    headers = {"Content-Type": request.headers.get("content-type", "application/json")}

    c = client()
    last: tuple[int, bytes, httpx.Headers] | None = None
    tried = 0
    key_id: int | None = None
    try:
        for row in attempts_keys:
            tried += 1
            key_id = row["id"]
            secret = store.plaintext(row)
            if secret is None:  # undecryptable (SECRET_KEY changed): out of rotation
                store.mark_rejected(key_id, "decrypt_failed")
                last = None
                continue
            req = c.build_request("POST", url, params=params, content=body,
                                  headers={**headers, "x-goog-api-key": secret})
            try:
                resp = await c.send(req, stream=streaming)
            except httpx.HTTPError as e:
                store.mark_cooldown(key_id, type(e).__name__, None)
                last = None
                continue

            if streaming and 200 <= resp.status_code < 300:
                store.mark_ok(key_id)
                return _stream(resp, c, route=action, model=model, key_id=key_id,
                               started=started, attempts=tried)

            data = await resp.aread() if streaming else resp.content
            if streaming:
                await resp.aclose()
            verdict = classify(resp.status_code, data)
            if verdict == "ok":
                store.mark_ok(key_id)
                _record(action, model, key_id, resp.status_code, started, tried, _usage(data))
                await c.aclose()
                return Response(data, status_code=resp.status_code,
                                media_type=resp.headers.get("content-type", "application/json"))
            reason = _error_reason(data)
            err = f"{resp.status_code} {reason}".strip()
            if verdict == "retry":
                store.mark_cooldown(key_id, err, _retry_after(resp.headers))
            elif verdict == "rejected":
                store.mark_rejected(key_id, err)
            else:  # caller error: no key penalty, return as is
                _record(action, model, key_id, resp.status_code, started, tried)
                await c.aclose()
                return Response(data, status_code=resp.status_code,
                                media_type=resp.headers.get("content-type", "application/json"),
                                headers=_passthrough_headers(resp.headers))
            last = (resp.status_code, data, resp.headers)
    except BaseException:
        await c.aclose()
        raise
    await c.aclose()

    if last is None:
        _record(action, model, key_id, 502, started, tried)
        return _json(502, "gemini_upstream_unreachable")
    status, data, hdrs = last
    _record(action, model, key_id, status, started, tried)
    return Response(data, status_code=status,
                    media_type=hdrs.get("content-type", "application/json"),
                    headers=_passthrough_headers(hdrs))


def _stream(resp: httpx.Response, c: httpx.AsyncClient, *, route: str, model: str,
            key_id: int, started: float, attempts: int) -> StreamingResponse:
    """Relay chunk by chunk. Past this point an upstream error just ends the stream."""
    status = resp.status_code

    async def gen():
        final = status
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        except httpx.HTTPError as e:
            final = 502
            store.mark_cooldown(key_id, type(e).__name__, None)
        finally:
            await resp.aclose()
            await c.aclose()
            _record(route, model, key_id, final, started, attempts)

    return StreamingResponse(gen(), status_code=status,
                             media_type=resp.headers.get("content-type", "text/event-stream"),
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
