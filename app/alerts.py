"""Slack alerting for the failures that silently stop ingestion.

Three things can take this service out with no visible error at the caller:
the seat's rolling quota is exhausted, the CLI login expires, or the
OpenRouter embedding budget runs dry. Each returns an error to agent-mem,
which logs it and moves on — so without an outbound alert the first symptom
is a graph that quietly stopped growing.

Alerts are deduped per (kind, window) so a sustained outage posts once, not
once per request.
"""

import asyncio
import logging
import time

import httpx

from . import config

log = logging.getLogger("llm-gateway.alerts")

_SLACK_POST = "https://slack.com/api/chat.postMessage"

# key -> unix ts when the suppression expires
_suppressed: dict[str, float] = {}
_lock = asyncio.Lock()


async def alert(key: str, text: str, *, critical: bool = True) -> None:
    """Post text to Slack unless an identical key fired recently.

    key identifies the *condition*, not the occurrence — e.g.
    "quota:seven_day_sonnet:1785480000". Reusing the reset timestamp means a
    new window produces a new key and therefore a fresh alert, while the same
    window stays quiet.
    """
    now = time.time()
    async with _lock:
        until = _suppressed.get(key, 0.0)
        if now < until:
            return
        _suppressed[key] = now + config.ALERT_DEDUPE_S
        # Opportunistic sweep; the dict is tiny and this avoids a background task.
        for k, exp in list(_suppressed.items()):
            if exp < now:
                del _suppressed[k]

    level = log.error if critical else log.warning
    level("ALERT [%s] %s", key, text)

    if not config.ALERTS_ENABLED:
        return

    icon = "🚨" if critical else "⚠️"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                _SLACK_POST,
                headers={"Authorization": f"Bearer {config.SLACK_BOT_TOKEN}"},
                json={
                    "channel": config.SLACK_ALERT_CHANNEL,
                    "text": f"{icon} *llm-gateway* — {text}",
                    # Standing rule: never let links expand into preview cards.
                    "unfurl_links": False,
                    "unfurl_media": False,
                },
            )
        body = r.json()
        if not body.get("ok"):
            log.error("Slack rejected alert: %s", body.get("error"))
    except Exception as e:  # noqa: BLE001 — alerting must never break a request
        log.error("Slack alert failed: %s", e)


def _fmt_reset(ts: int | None) -> str:
    if not ts:
        return "unknown"
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))


async def on_rate_limit(info) -> None:
    """Inspect a RateLimitInfo off the SDK stream and alert if it's degraded.

    `status` escalates allowed -> allowed_warning -> rejected. `utilization` is
    None until consumption is high enough for the API to report it, so the
    message has to tolerate its absence rather than assume a number.
    """
    status = getattr(info, "status", None)
    if status not in ("allowed_warning", "rejected"):
        return

    kind = getattr(info, "rate_limit_type", None) or "unknown"
    resets = getattr(info, "resets_at", None)
    util = getattr(info, "utilization", None)
    pct = f"{util * 100:.0f}% used · " if isinstance(util, (int, float)) else ""

    if status == "rejected":
        await alert(
            f"quota:{kind}:{resets}",
            f"*Claude seat quota exhausted* (`{kind}`). {pct}"
            f"Generation is failing now; resets {_fmt_reset(resets)}. "
            f"Embeddings are unaffected. Fall back by pointing agent-mem at OpenRouter.",
            critical=True,
        )
    else:
        await alert(
            f"quota-warn:{kind}:{resets}",
            f"*Claude seat quota running low* (`{kind}`). {pct}"
            f"Window resets {_fmt_reset(resets)}.",
            critical=False,
        )


async def on_claude_error(err: str) -> None:
    """Alert on a terminal AssistantMessage.error from the SDK."""
    if err == "authentication_failed":
        await alert(
            "auth:claude",
            "*Claude CLI login expired on the VPS.* Generation is down until "
            "someone re-authenticates: `claude setup-token` as the service user, "
            "then `sudo systemctl restart llm-gateway`.",
        )
    elif err == "billing_error":
        await alert("billing:claude", f"*Claude billing error* — `{err}`. Generation is down.")
    else:
        await alert(f"claude-error:{err}", f"Claude returned a terminal error: `{err}`.")


async def on_openrouter_error(status: int, body: str) -> None:
    """Alert on the OpenRouter failures that stop embedding writes."""
    if status == 402:
        await alert(
            "openrouter:402",
            "*OpenRouter is out of credit* — the $50 monthly cap is spent. "
            "Embeddings are failing, so the graph has stopped growing. "
            "Raise the cap or wait for the monthly reset.",
        )
    elif status == 429:
        await alert("openrouter:429", "*OpenRouter rate-limited* the embedding calls.", critical=False)
    elif status in (401, 403):
        await alert("openrouter:auth", f"*OpenRouter rejected the key* (HTTP {status}). Embeddings are down.")
    else:
        await alert(f"openrouter:{status}", f"OpenRouter returned HTTP {status}: `{body[:200]}`", critical=False)


async def on_truncated(route: str, model: str, completion_tokens: int | None) -> None:
    """Alert on an OpenRouter response cut off at max_tokens.

    A "length" finish_reason returns a valid 200 with plausible but incomplete
    text, so whatever consumes it (agent-mem's OCR, a downstream parse) can be
    silently wrong. This is visibility, not an outage — the request still
    succeeds — so it posts as a warning, deduped per (route, model) so a
    truncating workload posts once per window, not once per request.
    """
    tokens = completion_tokens if completion_tokens is not None else "unknown"
    cap = "OR_MAX_TOKENS_DESCRIBE" if route == "describe" else "OR_MAX_TOKENS"
    await alert(
        f"truncated:{route}:{model}",
        f"*OpenRouter `/{route}` response truncated* at `max_tokens` "
        f"(model `{model}`, {tokens} completion tokens). The answer is cut off "
        f"mid-stream, so whatever consumes it may be silently wrong. Raise "
        f"`{cap}` if this workload needs the full output.",
        critical=False,
    )
