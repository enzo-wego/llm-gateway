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
from datetime import datetime
import json
import logging
import math
import os
import re
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
    global _loaded, _auth_streak, _auth_since, _auth_alerted_streak, _auth_recovery_owed
    global _auth_outage_task, _auth_recovery_task
    _loaded = False
    _auth_streak = 0
    _auth_since = None
    _auth_alerted_streak = None
    _auth_recovery_owed = False
    for t in (_auth_outage_task, _auth_recovery_task):
        if t is not None and not t.done():
            try:
                t.cancel()
            except RuntimeError:  # its loop is already closed
                pass
    _auth_outage_task = None
    _auth_recovery_task = None


async def alert(
    key: str, text: str, *, critical: bool = True, until: float | None = None
) -> bool:
    """Post text to Slack unless an identical key fired recently.

    key identifies the *condition*, not the occurrence — e.g.
    "quota:seven_day_sonnet:1785480000". Reusing the reset timestamp means a
    new window produces a new key and therefore a fresh alert, while the same
    window stays quiet.

    If until (unix ts) is given and Slack accepts the post, the key stays
    muted until then; otherwise it is muted for ALERT_DEDUPE_S.

    Returns True when the post was delivered (or alerts are disabled), False
    when the key was suppressed or delivery failed.
    """
    now = time.time()
    async with _lock:
        if not _loaded:
            _load(now)
        if now < _suppressed.get(key, 0.0):
            return False
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
    return delivered


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


# ── Claude auth streak ───────────────────────────────────────────────────────
# authentication_failed can be a transient Anthropic-side rejection, so it only
# pages after failures have lasted CLAUDE_AUTH_ALERT_AFTER_S with no success in
# between, and a recovery message follows. State is per process and is touched
# only in synchronous code, so each transition is atomic on one event loop.

_auth_streak = 0
_auth_since: float | None = None
_auth_alerted_streak: int | None = None
_auth_recovery_owed = False
_auth_outage_task: asyncio.Task | None = None
_auth_recovery_task: asyncio.Task | None = None

_AUTH_OUTAGE_TEXT = (
    "*Claude seat auth rejected* (`authentication_failed`) for {minutes} min. "
    "Generation is down. If it does not clear, the token needs rotating: a human "
    "runs `cd ~/src/github.com/llm-gateway && deploy/regen-token.sh` on the Mac mini."
)
_AUTH_RECOVERY_TEXT = (
    "*Claude seat auth accepted again.* Calls are getting past authentication."
)


def _auth_task_done(which: str, task: asyncio.Task) -> None:
    global _auth_outage_task, _auth_recovery_task
    if which == "outage":
        if _auth_outage_task is task:
            _auth_outage_task = None
    elif _auth_recovery_task is task:
        _auth_recovery_task = None
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("Claude auth %s post failed: %r", which, exc)


async def _post_auth_outage(streak_id: int, minutes: int) -> None:
    global _auth_alerted_streak, _auth_recovery_owed
    ok = await alert("auth:claude", _AUTH_OUTAGE_TEXT.format(minutes=minutes))
    if not ok:
        return
    _auth_alerted_streak = streak_id
    _auth_recovery_owed = True
    async with _lock:
        _suppressed.pop("auth:claude:recovered", None)
        _save()
    if _auth_since is None or _auth_streak != streak_id:
        _start_recovery()


async def _post_auth_recovery() -> None:
    global _auth_recovery_owed
    ok = await alert("auth:claude:recovered", _AUTH_RECOVERY_TEXT, critical=False)
    if not ok:
        return
    _auth_recovery_owed = False
    async with _lock:
        _suppressed.pop("auth:claude", None)
        _save()


def _start_recovery() -> None:
    global _auth_recovery_task
    if _auth_recovery_task is None:
        task = asyncio.create_task(_post_auth_recovery())
        _auth_recovery_task = task
        task.add_done_callback(lambda t: _auth_task_done("recovery", t))


def on_claude_ok() -> None:
    """A Claude call got past authentication. Synchronous; never posts inline."""
    global _auth_since
    if _auth_since is None and not _auth_recovery_owed:
        return
    if _auth_since is not None:
        log.info(
            "Claude auth accepted again after %.0fs of failures",
            time.time() - _auth_since,
        )
        _auth_since = None
    if _auth_recovery_owed:
        _start_recovery()


def _on_auth_failed() -> None:
    global _auth_streak, _auth_since, _auth_outage_task
    now = time.time()
    if _auth_since is None:
        _auth_streak += 1
        _auth_since = now
        log.warning("Claude authentication_failed; streak %d opened", _auth_streak)
    if _auth_recovery_owed:
        if _auth_alerted_streak != _auth_streak:
            _start_recovery()
        return
    elapsed = now - _auth_since
    if (
        elapsed >= config.CLAUDE_AUTH_ALERT_AFTER_S
        and _auth_alerted_streak != _auth_streak
        and _auth_outage_task is None
    ):
        minutes = max(0, int(elapsed // 60))
        task = asyncio.create_task(_post_auth_outage(_auth_streak, minutes))
        _auth_outage_task = task
        task.add_done_callback(lambda t: _auth_task_done("outage", t))


async def on_claude_error(err: str) -> None:
    """Alert on a terminal AssistantMessage.error from the SDK."""
    if err == "authentication_failed":
        _on_auth_failed()
    elif err == "billing_error":
        await alert("billing:claude", f"*Claude billing error* — `{err}`. Generation is down.")
    else:
        await alert(f"claude-error:{err}", f"Claude returned a terminal error: `{err}`.")


def is_cap_error(status: int, text: str) -> bool:
    """Return whether OpenRouter says the configured monthly cap is spent."""
    return status == 402 or (status == 403 and "Key limit exceeded" in text)


def _cap_reset(body: str) -> str | None:
    match = re.search(
        r"\bresets\s+(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\b",
        body,
    )
    return match.group(1) if match else None


async def on_gemini_error(msg: str) -> None:
    """Alert when the direct embedding fallback also fails."""
    await alert(
        "gemini:embed",
        f"*Gemini embedding fallback failed.* Embeddings are failing: `{msg[:200]}`",
    )


async def on_openrouter_error(
    status: int, body: str, path: str = "", cap: bool = False
) -> None:
    """Alert on the OpenRouter failures that stop embedding writes."""
    if cap:
        reset = _cap_reset(body)
        figure = re.search(
            r"\$(\d+(?:\.\d+)?)\s+of\s+\$(\d+(?:\.\d+)?)\s+used\b",
            body,
        )
        details = []
        if figure:
            used, cap_amount = (float(value) for value in figure.groups())
            details.append(f"${used:.2f} of ${cap_amount:.2f} used")
        if reset:
            details.append(f"resets {reset}")
        text = "*OpenRouter monthly cap is spent*"
        if details:
            text += f" ({', '.join(details)})"
        text += "."
        if config.GEMINI_API_KEY and path == "/embeddings":
            until = None
            if reset:
                try:
                    parsed = datetime.fromisoformat(reset.replace("Z", "+00:00")).timestamp()
                    if parsed > time.time():
                        until = parsed
                except ValueError:
                    pass
            await alert(
                "openrouter:cap:fallback",
                text + " Embeddings will be retried on the Gemini API directly.",
                critical=False,
                until=until,
            )
        else:
            failure = (
                " Embeddings are failing: set GEMINI_API_KEY or raise the cap."
                if path == "/embeddings"
                else f" Requests to `{path or 'a request'}` are failing: "
                     "raise the cap or wait for the reset."
            )
            await alert("openrouter:402", text + failure)
    elif status == 429:
        # Repeats inside the dedupe window never reach Slack, so point at the
        # log lines that do show them. [4] keeps the grep from matching this
        # alert's own log line.
        repeats = ("A single one is noise. To see repeats (this alert is deduped), run "
                   "`docker compose logs --since 1h llm-gateway | grep 'OpenRouter [4]29 on'`.")
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
