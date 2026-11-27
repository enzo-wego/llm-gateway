"""Gateway liveness with best-effort readiness for the optional Gemini pool."""

import sqlite3
from collections.abc import Callable
from threading import Lock
from typing import Any, Final, Literal, Protocol

from anyio import fail_after, to_thread
from fastapi import FastAPI

from . import config, gemini_store

_POOL_PROBE_TIMEOUT_S: Final = 0.5
_pool_probe_lock = Lock()


class SeatQuota(Protocol):
    @property
    def blocked_until(self) -> float: ...

    @property
    def last(self) -> dict[str, Any] | None: ...

    @property
    def calls(self) -> int: ...

    @property
    def cost_usd(self) -> float: ...

    @property
    def fallbacks(self) -> int: ...

    def seat_available(self) -> bool: ...


def _probe_pool() -> dict[str, int] | None:
    # The worker owns this lock even if the HTTP request times out. Repeated
    # probes must not pile up more SQLite work while one read is still stuck.
    if not _pool_probe_lock.acquire(blocking=False):
        return None
    try:
        return gemini_store.pool_counts()
    except (OSError, sqlite3.DatabaseError):
        return None
    finally:
        _pool_probe_lock.release()


async def _pool_health() -> dict[str, int] | None:
    try:
        with fail_after(_POOL_PROBE_TIMEOUT_S):
            return await to_thread.run_sync(_probe_pool, abandon_on_cancel=True)
    except TimeoutError:
        return None


def register_health(
    app: FastAPI,
    seat: Callable[[], SeatQuota],
    active_model: Callable[[Literal["summary", "cheap"]], str],
) -> None:
    @app.get("/health")
    async def health() -> dict[str, Any]:
        """Unauthenticated liveness; never expose details of a pool DB failure."""
        pool = {}
        if gemini_store.enabled():
            counts = await _pool_health()
            pool = {"gemini_pool": counts if counts is not None else {"ready": False}}
        quota = seat()
        return {
            "ok": True,
            **pool,
            "backends": {
                "summary": config.BACKEND_SUMMARY,
                "cheap": config.BACKEND_CHEAP,
                "describe": config.BACKEND_DESCRIBE,
                "embed": "openrouter",
            },
            "models": {
                "summary": active_model("summary"),
                "cheap": active_model("cheap"),
                "describe": config.MODEL_CHEAP if config.BACKEND_DESCRIBE == "claude" else config.OR_MODEL_DESCRIBE,
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
            "embed_fallback": "gemini" if config.GEMINI_API_KEY else None,
            "alerts_enabled": config.ALERTS_ENABLED,
        }
