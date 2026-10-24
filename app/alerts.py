"""Slack alerting for the failures that silently stop ingestion.

Three things can take this service out with no visible error at the caller:
the seat's rolling quota is exhausted, the CLI login expires, or the
OpenRouter embedding budget runs dry. Each returns an error to agent-mem,
which logs it and moves on — so without an outbound alert the first symptom
is a graph that quietly stopped growing.

Quota alerts that carry a usable reset are muted until that reset once Slack
accepts the post; everything else dedupes for ALERT_DEDUPE_S. Suppression
persists to ALERT_STATE_FILE so it survives a restart.
"""

import asyncio
import json
import logging
import math
import os
import tempfile
import time

import httpx

from . import config

log = logging.getLogger("llm-gateway.alerts")

_SLACK_POST = "https://slack.com/api/chat.postMessage"
_MAX_RESET_AHEAD_S = 31 * 86400

# key -> unix ts when the suppression expires
_suppressed: dict[str, float] = {}
_loaded = False
_lock = asyncio.Lock()


def _valid_expiry(v, now: float) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > now


def _load(now: float) -> None:
    """Load persisted suppressions. Caller holds _lock."""
    global _loaded
    _loaded = True
    path = config.ALERT_STATE_FILE
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as e:  # JSONDecodeError is a ValueError
        log.warning("Alert state load failed (%s); starting empty: %s", path, e)
        return
    if not isinstance(data, dict):
        log.warning("Alert state load failed (%s); top level is not an object", path)
        return
    for k, v in data.items():
        if isinstance(k, str) and _valid_expiry(v, now):
            _suppressed[k] = float(v)


def _save() -> None:
    """Persist _suppressed atomically. Never raises. Caller holds _lock."""
    path = config.ALERT_STATE_FILE
    tmp_name = None
    try:
        d = os.path.dirname(path) or "."
        os.makedirs(d, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=d, delete=False, encoding="utf-8") as tmp:
            tmp_name = tmp.name
            json.dump(_suppressed, tmp)
        os.replace(tmp_name, path)
    except Exception as e:  # noqa: BLE001 — alerting must never break a request
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        log.error("Alert state save failed (%s): %s", path, e)


def _reset_state() -> None:
    """Test helper: forget in-memory state so the next alert() reloads the file."""
    _suppressed.clear()
    global _loaded
    _loaded = False


async def alert(key: str, text: str, *, critical: bool = True, until: float | None = None) -> None:
    """Post text to Slack unless an identical key fired recently.

    key identifies the *condition*, not the occurrence — e.g.
    "quota:seven_day_sonnet:1785480000". Reusing the reset timestamp means a
    new window produces a new key and therefore a fresh alert, while the same
    window stays quiet.

    If until (unix ts) is given and Slack accepts the post, the key stays
    muted until then; otherwise it is muted for ALERT_DEDUPE_S.
    """
    now = time.time()
    async with _lock:
        if not _loaded:
            _load(now)
        if now < _suppressed.get(key, 0.0):
            return
        _suppressed[key] = now + config.ALERT_DEDUPE_S
        # Opportunistic sweep; the dict is tiny and this avoids a background task.
        for k, exp in list(_suppressed.items()):
            if exp < now:
                del _suppressed[k]
        _save()

    level = log.error if critical else log.warning
    level("ALERT [%s] %s", key, text)

    delivered = False
    if not config.ALERTS_ENABLED:
        delivered = True
    else:
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
            if body.get("ok"):
                delivered = True
            else:
                log.error("Slack rejected alert: %s", body.get("error"))
        except Exception as e:  # noqa: BLE001 — alerting must never break a request
            log.error("Slack alert failed: %s", e)

    if delivered and until is not None:
        async with _lock:
            _suppressed[key] = until
            _save()


def _usable_reset(resets, now: float) -> float | None:
    """Return resets as a float only if it is a sane future timestamp."""
    if isinstance(resets, bool) or not isinstance(resets, (int, float)):
        return None
    if not math.isfinite(resets):
        return None
    if resets <= now or resets >= now + _MAX_RESET_AHEAD_S:
        return None
    return float(resets)


def _fmt_reset(ts) -> str:
    if not ts:
        return "unknown"
    try:
        return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))
    except (TypeError, ValueError, OverflowError, OSError):
        return "unknown"


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

    usable = _usable_reset(resets, time.time())

    if status == "rejected":
        await alert(
            f"quota:{kind}:{resets}",
            f"*Claude seat quota exhausted* (`{kind}`). {pct}"
            f"Generation is failing now; resets {_fmt_reset(usable)}. "
            f"Embeddings are unaffected. Fall back by pointing agent-mem at OpenRouter."
            + (" Muted until the reset." if usable is not None else ""),
            critical=True,
            until=usable,
        )
    elif isinstance(util, (int, float)) and util * 100 >= config.QUOTA_WARN_PCT:
        await alert(
            f"quota-warn:{kind}:{resets}",
            f"*Claude seat quota running low* (`{kind}`). {pct}"
            f"Window resets {_fmt_reset(usable)}."
            + (" Muted until the reset." if usable is not None else ""),
            critical=False,
            until=usable,
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


async def on_openrouter_error(status: int, body: str, path: str = "") -> None:
    """Alert on the OpenRouter failures that stop embedding writes."""
    if status == 402:
        await alert(
            "openrouter:402",
            "*OpenRouter is out of credit* — the $50 monthly cap is spent. "
            "Embeddings are failing, so the graph has stopped growing. "
            "Raise the cap or wait for the monthly reset.",
        )
    elif status == 429:
        # Repeats inside the dedupe window never reach Slack, so point at the
        # log lines that do show them.
        repeats = ("A single one is noise. To see repeats (this alert is deduped), run "
                   "`docker compose logs --since 1h llm-gateway | grep 'OpenRouter 429'`.")
        if path == "/embeddings":
            text = ("*OpenRouter still rate-limited `/embeddings` after retries.* That call "
                    "failed, so its vector was not stored. " + repeats + " Many lines mean "
                    "a caller is embedding too fast: slow it down or batch texts into fewer "
                    "`/embed` calls.")
        else:
            text = (f"*OpenRouter rate-limited `{path or 'a request'}`.* That call failed "
                    "with 502 (not retried). " + repeats)
        await alert("openrouter:429", text, critical=False)
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
