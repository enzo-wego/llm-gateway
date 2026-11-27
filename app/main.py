"""HTTP surface. Intent tiers supply defaults; Claude overrides require the seat."""

import asyncio
import logging
import re
import time
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import admin, alerts, claude, codex_gateway, config, gemini, gemini_proxy, openrouter
from .access_log import path_access_log
from .claude_overrides import ClaudeOverrides, ClaudeOverrideUnavailable
from .health import register_health

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("llm-gateway")
app = FastAPI(title="llm-gateway", docs_url=None, redoc_url=None)
app.middleware("http")(path_access_log)
app.include_router(gemini_proxy.router)
app.include_router(admin.router)


# ── quota state ──────────────────────────────────────────────────────────────
class _Quota:
    """Remembers a seat rejection so we stop retrying a window we know is spent.

    Without this, every request after exhaustion pays a full round-trip to the
    CLI before failing over — turning a degradation into a latency collapse.
    """

    def __init__(self) -> None:
        self.blocked_until: float = 0.0
        self.family_blocks: dict[str, float] = {}
        self.last: dict[str, Any] | None = None
        self.calls = 0
        self.cost_usd = 0.0
        self.fallbacks = 0

    def seat_available(self, model: str = "") -> bool:
        parts = model.removesuffix("[1m]").split("-")
        deadline = max((self.family_blocks.get(part, 0.0) for part in parts), default=0.0)
        return time.time() >= max(self.blocked_until, deadline)

    def note_rejection(self, resets_at: int | None, window: str = "") -> None:
        # Trust the reported reset; if absent, back off 15 minutes so a
        # missing timestamp can't wedge the seat off permanently.
        deadline = float(resets_at) if resets_at else time.time() + 900
        if window in {"seven_day_opus", "seven_day_sonnet", "seven_day_haiku", "seven_day_fable"}:
            self.family_blocks[window.removeprefix("seven_day_")] = deadline
        else:
            self.blocked_until = deadline

    def record(self, meta: dict[str, Any]) -> None:
        self.calls += 1
        if isinstance(meta.get("cost_usd"), (int, float)):
            self.cost_usd += meta["cost_usd"]
        if meta.get("rate_limit"):
            self.last = meta["rate_limit"]


quota = _Quota()


# ── auth ─────────────────────────────────────────────────────────────────────
def require_key(x_api_key: str = Header(default="")) -> None:
    if x_api_key != config.API_KEY:
        raise HTTPException(status_code=401, detail="bad or missing X-API-Key")


# ── caller label ─────────────────────────────────────────────────────────────
_CALLER_RE = re.compile(r"[a-z0-9][a-z0-9/_-]{0,63}")
_MIME_RE = re.compile(r"[A-Za-z0-9.+-]{1,32}/[A-Za-z0-9.+-]{1,64}")
_MAX_NAMED_CALLERS = 64
_RESERVED = ("unknown", "other")

# bucket -> {"calls", "errors", "cost_usd"}; since process start.
callers: dict[str, dict[str, Any]] = {}


def caller_label(x_llm_caller: str = Header(default="")) -> str:
    """Self-asserted bookkeeping label; anything malformed becomes `unknown`."""
    return x_llm_caller if _CALLER_RE.fullmatch(x_llm_caller) else "unknown"


def _meta_cost(meta: dict[str, Any]) -> float:
    c = meta.get("cost_usd")
    return float(c) if isinstance(c, (int, float)) else 0.0


def _count_call(label: str, status: int | str, cost: float) -> None:
    bucket = label
    if bucket not in callers and bucket not in _RESERVED:
        named = sum(1 for k in callers if k not in _RESERVED)
        if named >= _MAX_NAMED_CALLERS:
            bucket = "other"
    entry = callers.setdefault(bucket, {"calls": 0, "errors": 0, "cost_usd": 0.0})
    entry["calls"] += 1
    if status != 200:
        entry["errors"] += 1
    entry["cost_usd"] += cost


def _summarize(
    fmt: str, caller: str, status: int | str, reason: str,
    started: float, cost: float, *fields: Any, tail: tuple[Any, ...] = (),
) -> None:
    """Write the one summary record for a handler invocation and update the store.

    Log args are: caller, *fields, status, reason, duration_ms, *tail.
    """
    duration_ms = int((time.monotonic() - started) * 1000)
    level = (
        logging.ERROR
        if isinstance(status, int) and status >= 500
        else logging.WARNING if reason != "none" else logging.INFO
    )
    log.log(level, fmt, caller, *fields, status, reason, duration_ms, *tail)
    _count_call(caller, status, cost)


# ── schemas ──────────────────────────────────────────────────────────────────
Tier = Literal["summary", "cheap"]


class GenerateIn(ClaudeOverrides):
    system: str = ""
    user: str
    tier: Tier = "cheap"
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")

    model_config = {"populate_by_name": True}


class DescribeIn(ClaudeOverrides):
    system: str = ""
    prompt: str
    mime: str
    data_b64: str
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")

    model_config = {"populate_by_name": True}


class EmbedIn(BaseModel):
    # OpenRouter answers 400 to an empty string, which would page as an alert.
    texts: list[Annotated[str, Field(min_length=1)]]
    dims: int = 3072
    fallback: bool = True


def _tier_config(tier: Tier) -> tuple[str, str, str, str, str]:
    """(backend, claude_model, openrouter_model, effort, thinking) for an intent tier."""
    if tier == "summary":
        return (config.BACKEND_SUMMARY, config.MODEL_SUMMARY, config.OR_MODEL_SUMMARY,
                config.EFFORT_SUMMARY, config.THINKING_SUMMARY)
    return (config.BACKEND_CHEAP, config.MODEL_CHEAP, config.OR_MODEL_CHEAP,
            config.EFFORT_CHEAP, config.THINKING_CHEAP)


def _active_model(tier: Tier) -> str:
    """The model a tier actually calls: the Claude id or the OpenRouter id, per its backend."""
    backend, claude_model, or_model, _, _ = _tier_config(tier)
    return claude_model if backend == "claude" else or_model


# ── routes ───────────────────────────────────────────────────────────────────
@app.post("/codex", include_in_schema=False)
async def generate_codex(request: Request) -> Response:
    return await codex_gateway.generate(request)


@app.post("/generate", dependencies=[Depends(require_key)])
async def generate(body: GenerateIn, caller: str = Depends(caller_label)) -> dict[str, Any]:
    started = time.monotonic()
    backend, cmodel, ormodel, effort, thinking = _tier_config(body.tier)
    cmodel = body.model or cmodel
    effort = body.effort or effort
    actual_backend = backend
    actual_model = cmodel if backend == "claude" else ormodel
    status: int | str = 500
    reason = "none"
    cost = 0.0

    try:
        seat_available = backend == "claude" and quota.seat_available(cmodel)
        body.require_available_seat(backend=backend, seat_available=seat_available)
        if seat_available:
            try:
                res = await claude.generate(
                    system=body.system, user=body.user, model=cmodel,
                    effort=effort, thinking=None if body.explicit else thinking,
                    schema=body.schema_,
                )
                if body.explicit:
                    res["meta"].update(model=cmodel, effort=effort)
                quota.record(res["meta"])
                cost = _meta_cost(res["meta"])
                status = 200
                return {"backend": "claude", **res}
            except claude.QuotaExhausted as e:
                reason = "quota_exhausted"
                quota.note_rejection(e.resets_at, e.window)
                if body.explicit or not config.FALLBACK_ON_QUOTA:
                    status = 503
                    raise HTTPException(503, f"seat quota exhausted ({e})") from e
            except claude.ClaudeError as e:
                reason = e.reason
                if body.explicit or not config.FALLBACK_ON_QUOTA:
                    status = 502
                    raise HTTPException(502, str(e)) from e

        if backend == "claude" and reason == "none":
            reason = "seat_unavailable"
        actual_backend = "openrouter"
        actual_model = ormodel
        try:
            res = await openrouter.generate(
                system=body.system, user=body.user, model=ormodel, schema=body.schema_,
            )
        except openrouter.OpenRouterError as e:
            reason = "openrouter_error"
            status = 502
            raise HTTPException(502, str(e)) from e
        if backend == "claude":
            quota.fallbacks += 1
        status = 200
        return {"backend": "openrouter", **res}
    except ClaudeOverrideUnavailable as e:
        status, reason = e.status_code, e.reason
        raise
    except asyncio.CancelledError:
        status = "cancelled"
        reason = "external_cancelled"
        raise
    except Exception:
        if reason == "none":
            reason = "unexpected_error"
        raise
    finally:
        _summarize(
            "generate caller=%s tier=%s backend=%s model=%s effort=%s status=%s reason=%s duration_ms=%d",
            caller, status, reason, started, cost, body.tier, actual_backend, actual_model, effort,
        )


@app.post("/describe", dependencies=[Depends(require_key)])
async def describe(body: DescribeIn, caller: str = Depends(caller_label)) -> dict[str, Any]:
    started = time.monotonic()
    backend = config.BACKEND_DESCRIBE
    cmodel = body.model or config.MODEL_CHEAP
    effort = body.effort or config.EFFORT_CHEAP
    actual_backend = backend
    status: int | str = 500
    reason = "none"
    cost = 0.0

    try:
        seat_available = backend == "claude" and quota.seat_available(cmodel)
        body.require_available_seat(backend=backend, seat_available=seat_available)
        if seat_available:
            try:
                res = await claude.describe(
                    system=body.system, prompt=body.prompt, mime=body.mime,
                    data_b64=body.data_b64, model=cmodel,
                    effort=effort, thinking=None if body.explicit else config.THINKING_CHEAP,
                    schema=body.schema_,
                )
                if body.explicit:
                    res["meta"].update(model=cmodel, effort=effort)
                quota.record(res["meta"])
                cost = _meta_cost(res["meta"])
                status = 200
                return {"backend": "claude", **res}
            except claude.QuotaExhausted as e:
                reason = "quota_exhausted"
                quota.note_rejection(e.resets_at, e.window)
                if body.explicit or not config.FALLBACK_ON_QUOTA:
                    status = 503
                    raise HTTPException(503, f"seat quota exhausted ({e})") from e
                log.warning("seat quota exhausted (%s) — falling back to OpenRouter", e)
            except claude.ClaudeError as e:
                reason = e.reason
                if body.explicit or not config.FALLBACK_ON_QUOTA:
                    status = 502
                    raise HTTPException(502, str(e)) from e
                log.error("claude describe failed (%s) — falling back to OpenRouter", e)

        if backend == "claude" and reason == "none":
            reason = "seat_unavailable"
        actual_backend = "openrouter"
        try:
            res = await openrouter.describe(
                prompt=body.prompt, mime=body.mime, data_b64=body.data_b64,
                model=config.OR_MODEL_DESCRIBE, schema=body.schema_,
            )
        except openrouter.OpenRouterError as e:
            reason = "openrouter_error"
            status = 502
            raise HTTPException(502, str(e)) from e
        if backend == "claude":
            quota.fallbacks += 1
        status = 200
        return {"backend": "openrouter", **res}
    except ClaudeOverrideUnavailable as e:
        status, reason = e.status_code, e.reason
        raise
    except asyncio.CancelledError:
        status = "cancelled"
        reason = "external_cancelled"
        raise
    except Exception:
        if reason == "none":
            reason = "unexpected_error"
        raise
    finally:
        mime = body.mime if _MIME_RE.fullmatch(body.mime) else "invalid"
        _summarize(
            "describe caller=%s backend=%s mime=%s status=%s reason=%s duration_ms=%d",
            caller, status, reason, started, cost, actual_backend, mime,
        )


@app.post("/embed", dependencies=[Depends(require_key)])
async def embed(body: EmbedIn, caller: str = Depends(caller_label)) -> dict[str, Any]:
    """Embed through OpenRouter, falling back to Gemini only when its cap is spent."""
    started = time.monotonic()
    status: int | str = 500
    reason = "none"
    backend = "openrouter"
    try:
        if not body.texts:
            status = 200
            return {"embeddings": [], "model": config.EMBED_MODEL}
        # ponytail: intentionally no "OpenRouter capped" state; every request
        # tries the primary first so recovery needs no cache invalidation.
        try:
            vectors = await openrouter.embed(body.texts, body.dims)
        except openrouter.OpenRouterError as e:
            if not e.cap or not config.GEMINI_API_KEY or not body.fallback:
                reason = "openrouter_error"
                status = 502
                raise HTTPException(502, str(e)) from e
            backend = "gemini"
            # ponytail: this best-effort fallback has no request-wide deadline;
            # retries or multiple chunks can exceed agent-mem's 200 s timeout.
            try:
                vectors = await gemini.embed(body.texts, body.dims)
            except gemini.GeminiError as fallback_error:
                safe_error = str(fallback_error).replace(config.GEMINI_API_KEY, "[redacted]")
                await alerts.on_gemini_error(safe_error)
                reason = "gemini_error"
                status = 502
                raise HTTPException(
                    502, f"{e}; fallback: {safe_error}"
                ) from fallback_error
        status = 200
        return {"embeddings": vectors, "model": config.EMBED_MODEL, "dims": body.dims}
    except asyncio.CancelledError:
        status = "cancelled"
        reason = "external_cancelled"
        raise
    except Exception:
        if reason == "none":
            reason = "unexpected_error"
        raise
    finally:
        _summarize(
            "embed caller=%s texts=%d status=%s reason=%s duration_ms=%d backend=%s",
            caller, status, reason, started, 0.0, len(body.texts), tail=(backend,),
        )


@app.get("/usage", dependencies=[Depends(require_key)])
async def usage() -> dict[str, Any]:
    """Budget and quota in one place, for dashboards.

    The OpenRouter key and the Claude seat both live in this process, so this is
    the only place that can answer "how much is left" for either. agent-mem used
    to hold the OpenRouter key purely to render this; it no longer does.

    Authenticated, unlike /health: spend figures are not secret exactly, but they
    are not something to hand to an unauthenticated caller either.
    """
    out: dict[str, Any] = {
        "seat": {
            "available": quota.seat_available(),
            "last_rate_limit": quota.last,
            "calls": quota.calls,
            "notional_cost_usd": round(quota.cost_usd, 4),
            "openrouter_fallbacks": quota.fallbacks,
        },
        "callers": {
            name: {
                "calls": c["calls"],
                "errors": c["errors"],
                "notional_cost_usd": round(c["cost_usd"], 4),
            }
            for name, c in sorted(callers.items())
        },
    }
    try:
        d = await openrouter.key_usage()
        out["openrouter"] = {
            "limit": d.get("limit"),
            "usage": d.get("usage"),
            "limit_remaining": d.get("limit_remaining"),
            "usage_daily": d.get("usage_daily"),
            "usage_monthly": d.get("usage_monthly"),
        }
    except openrouter.OpenRouterError as e:
        # Report the failure instead of 500ing: the seat half is still useful,
        # and a dashboard should show what it can.
        out["openrouter"] = {"error": str(e)}
    return out


@app.get("/config", dependencies=[Depends(require_key)])
async def get_config() -> dict[str, Any]:
    """Return the editable runtime knobs, never credentials or other secrets."""
    return config.editable_config()


@app.put("/config", dependencies=[Depends(require_key)])
async def put_config(body: dict[str, Any]) -> dict[str, Any]:
    """Validate and atomically persist a partial live configuration update."""
    try:
        return config.update_config(body)
    except config.ConfigValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        log.exception("failed to persist gateway configuration")
        raise HTTPException(status_code=500, detail="failed to persist gateway configuration") from exc


register_health(app, lambda: quota, _active_model)


@app.on_event("startup")
async def _startup() -> None:
    log.info(
        "llm-gateway up · summary=%s/%s cheap=%s/%s describe=%s/%s embed=%s · fallback=%s alerts=%s",
        config.BACKEND_SUMMARY, _active_model("summary"),
        config.BACKEND_CHEAP, _active_model("cheap"),
        config.BACKEND_DESCRIBE,
        config.MODEL_CHEAP if config.BACKEND_DESCRIBE == "claude" else config.OR_MODEL_DESCRIBE,
        config.EMBED_MODEL,
        config.FALLBACK_ON_QUOTA, config.ALERTS_ENABLED,
    )
    if not config.OPENROUTER_KEY:
        log.warning("OPENROUTER_API_KEY unset — /generate fallback, /describe and /embed will answer 502")
    if not config.ALERTS_ENABLED:
        log.warning("Slack alerting disabled — quota exhaustion will only be logged")
