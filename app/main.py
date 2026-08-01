"""HTTP surface. Callers pick an intent tier; the gateway picks the backend.

agent-mem asks for "a cheap judgement" or "a good summary", never for a model
name. That keeps model and provider choices here — a systemd restart — instead
of in a Go deploy, and it is what makes the Claude experiment reversible
without touching agent-mem's gemini client at all.
"""

import logging
import time
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import alerts, claude, config, openrouter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("llm-gateway")

app = FastAPI(title="llm-gateway", docs_url=None, redoc_url=None)


# ── quota state ──────────────────────────────────────────────────────────────
class _Quota:
    """Remembers a seat rejection so we stop retrying a window we know is spent.

    Without this, every request after exhaustion pays a full round-trip to the
    CLI before failing over — turning a degradation into a latency collapse.
    """

    def __init__(self) -> None:
        self.blocked_until: float = 0.0
        self.last: dict[str, Any] | None = None
        self.calls = 0
        self.cost_usd = 0.0
        self.fallbacks = 0

    def seat_available(self) -> bool:
        return time.time() >= self.blocked_until

    def note_rejection(self, resets_at: int | None) -> None:
        # Trust the reported reset; if absent, back off 15 minutes so a
        # missing timestamp can't wedge the seat off permanently.
        self.blocked_until = float(resets_at) if resets_at else time.time() + 900

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


# ── schemas ──────────────────────────────────────────────────────────────────
Tier = Literal["summary", "cheap"]


class GenerateIn(BaseModel):
    system: str = ""
    user: str
    tier: Tier = "cheap"
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")

    model_config = {"populate_by_name": True}


class DescribeIn(BaseModel):
    system: str = ""
    prompt: str
    mime: str
    data_b64: str
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")

    model_config = {"populate_by_name": True}


class EmbedIn(BaseModel):
    texts: list[str]
    dims: int = 3072


def _tier_config(tier: Tier) -> tuple[str, str, str, str]:
    """(backend, claude_model, openrouter_model, effort) for an intent tier."""
    if tier == "summary":
        return config.BACKEND_SUMMARY, config.MODEL_SUMMARY, config.OR_MODEL_SUMMARY, config.EFFORT_SUMMARY
    return config.BACKEND_CHEAP, config.MODEL_CHEAP, config.OR_MODEL_CHEAP, config.EFFORT_CHEAP


# ── routes ───────────────────────────────────────────────────────────────────
@app.post("/generate", dependencies=[Depends(require_key)])
async def generate(body: GenerateIn) -> dict[str, Any]:
    backend, cmodel, ormodel, effort = _tier_config(body.tier)

    if backend == "claude" and quota.seat_available():
        try:
            res = await claude.generate(
                system=body.system, user=body.user, model=cmodel,
                effort=effort, schema=body.schema_,
            )
            quota.record(res["meta"])
            return {"backend": "claude", **res}
        except claude.QuotaExhausted as e:
            quota.note_rejection((quota.last or {}).get("resets_at"))
            if not config.FALLBACK_ON_QUOTA:
                raise HTTPException(503, f"seat quota exhausted ({e})") from e
            log.warning("seat quota exhausted (%s) — falling back to OpenRouter", e)
        except claude.ClaudeError as e:
            if not config.FALLBACK_ON_QUOTA:
                raise HTTPException(502, str(e)) from e
            log.error("claude failed (%s) — falling back to OpenRouter", e)

    try:
        res = await openrouter.generate(system=body.system, user=body.user, model=ormodel)
    except openrouter.OpenRouterError as e:
        raise HTTPException(502, str(e)) from e
    if backend == "claude":
        quota.fallbacks += 1
    return {"backend": "openrouter", **res}


@app.post("/describe", dependencies=[Depends(require_key)])
async def describe(body: DescribeIn) -> dict[str, Any]:
    backend = config.BACKEND_DESCRIBE

    if backend == "claude" and quota.seat_available():
        try:
            res = await claude.describe(
                system=body.system, prompt=body.prompt, mime=body.mime,
                data_b64=body.data_b64, model=config.MODEL_CHEAP,
                effort=config.EFFORT_CHEAP, schema=body.schema_,
            )
            quota.record(res["meta"])
            return {"backend": "claude", **res}
        except claude.QuotaExhausted as e:
            quota.note_rejection((quota.last or {}).get("resets_at"))
            if not config.FALLBACK_ON_QUOTA:
                raise HTTPException(503, f"seat quota exhausted ({e})") from e
            log.warning("seat quota exhausted (%s) — falling back to OpenRouter", e)
        except claude.ClaudeError as e:
            if not config.FALLBACK_ON_QUOTA:
                raise HTTPException(502, str(e)) from e
            log.error("claude describe failed (%s) — falling back to OpenRouter", e)

    try:
        res = await openrouter.describe(
            prompt=body.prompt, mime=body.mime, data_b64=body.data_b64,
            model=config.OR_MODEL_SUMMARY,
        )
    except openrouter.OpenRouterError as e:
        raise HTTPException(502, str(e)) from e
    if backend == "claude":
        quota.fallbacks += 1
    return {"backend": "openrouter", **res}


@app.post("/embed", dependencies=[Depends(require_key)])
async def embed(body: EmbedIn) -> dict[str, Any]:
    """Always OpenRouter — see openrouter.py for why there is no switch."""
    if not body.texts:
        return {"embeddings": [], "model": config.EMBED_MODEL}
    try:
        vectors = await openrouter.embed(body.texts, body.dims)
    except openrouter.OpenRouterError as e:
        raise HTTPException(502, str(e)) from e
    return {"embeddings": vectors, "model": config.EMBED_MODEL, "dims": body.dims}


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


@app.get("/health")
async def health() -> dict[str, Any]:
    """Unauthenticated: it exposes no secrets and systemd/uptime checks need it."""
    return {
        "ok": True,
        "backends": {
            "summary": config.BACKEND_SUMMARY,
            "cheap": config.BACKEND_CHEAP,
            "describe": config.BACKEND_DESCRIBE,
            "embed": "openrouter",
        },
        "models": {
            "summary": config.MODEL_SUMMARY,
            "cheap": config.MODEL_CHEAP,
            "embed": config.EMBED_MODEL,
        },
        "seat": {
            "available": quota.seat_available(),
            "blocked_until": quota.blocked_until or None,
            "last_rate_limit": quota.last,
        },
        "counters": {
            "claude_calls": quota.calls,
            "notional_cost_usd": round(quota.cost_usd, 4),
            "openrouter_fallbacks": quota.fallbacks,
        },
        "fallback_on_quota": config.FALLBACK_ON_QUOTA,
        "alerts_enabled": config.ALERTS_ENABLED,
    }


@app.on_event("startup")
async def _startup() -> None:
    log.info(
        "llm-gateway up · summary=%s/%s cheap=%s/%s describe=%s embed=%s · fallback=%s alerts=%s",
        config.BACKEND_SUMMARY, config.MODEL_SUMMARY,
        config.BACKEND_CHEAP, config.MODEL_CHEAP,
        config.BACKEND_DESCRIBE, config.EMBED_MODEL,
        config.FALLBACK_ON_QUOTA, config.ALERTS_ENABLED,
    )
    if not config.ALERTS_ENABLED:
        log.warning("Slack alerting disabled — quota exhaustion will only be logged")
