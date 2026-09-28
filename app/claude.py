"""Claude backend — single-shot calls over the subscription seat.

The Agent SDK is a coding-agent harness; this module strips it back to a plain
text-in / JSON-out call. Every option here exists to remove something:

  tools=[] / allowed_tools=[]   no file, bash, or web tools
  setting_sources=[]            do NOT load ~/.claude/CLAUDE.md or project
                                settings — otherwise the operator's personal
                                instructions ride along on every request
  thinking per tier             disabled where the model allows it: these are
                                extraction tasks, and thinking tokens come out of
                                the seat's quota. Models that reject disabled
                                (Sonnet 5.5) run adaptive, bounded by effort
  output_format json_schema     the model must return a parsed object; no
                                brace-scraping the way internal/anthropic does

Measured overhead with all of the above: ~700 input tokens per call.
"""

import asyncio
import logging
from typing import Any

import claude_agent_sdk as sdk

from . import alerts, config

log = logging.getLogger("llm-gateway.claude")


class QuotaExhausted(RuntimeError):
    """The seat's rolling window is spent. Caller may fall back to OpenRouter."""


class ClaudeError(RuntimeError):
    """Terminal, non-quota failure (auth expired, billing, CLI crash)."""

    def __init__(self, message: str, *, reason: str = "claude_error"):
        super().__init__(message)
        self.reason = reason


# Structured output is delivered as an internal tool call, so a single-turn cap
# truncates it — an observed run used 2 turns for a trivial prompt. Four leaves
# headroom without letting a misbehaving call loop.
_MAX_TURNS = 4
# The bundled CLI peaked at 319.7 MiB RSS in production. Four processes plus
# the 86.3 MiB service baseline use 1,365.1 MiB of the 2 GiB cgroup, leaving
# 682.9 MiB for shared/runtime overhead and transient spikes.
_MAX_CONCURRENT_QUERIES = 4
_query_slots = asyncio.Semaphore(_MAX_CONCURRENT_QUERIES)


def _unwrap_fenced_block(text: str) -> str:
    """Remove a fence only when it wraps the entire response.

    Callers parse this as JSON because that is what the OpenRouter path
    guaranteed via response_format=json_object. The Agent SDK makes no such
    promise without a schema, so the model fences its answer like it would in
    chat and every json.Unmarshal downstream dies on the backtick.

    Unwrap only the unambiguous case: one fence opening the first line, one
    closing the last, and none in between. Two adjacent blocks would otherwise
    be spliced into a single corrupt body — a silent wrong answer, which is
    worse than the fenced text this exists to fix.
    """
    stripped = text.strip()
    lines = stripped.splitlines()
    if len(lines) < 2 or not lines[0].startswith("```") or lines[-1] != "```":
        return text
    if any(line.startswith("```") for line in lines[1:-1]):
        return text
    return "\n".join(lines[1:-1]).strip()


def _options(
    model: str, effort: str, thinking: str, system: str, schema: dict[str, Any] | None
) -> sdk.ClaudeAgentOptions:
    return sdk.ClaudeAgentOptions(
        model=model,
        system_prompt=system or None,
        tools=[],
        allowed_tools=[],
        setting_sources=[],
        max_turns=_MAX_TURNS,
        effort=effort,
        thinking={"type": thinking},
        max_budget_usd=config.MAX_BUDGET_USD,
        output_format=({"type": "json_schema", "schema": schema} if schema else None),
    )


async def _disconnect(client: sdk.ClaudeSDKClient) -> None:
    """Finish subprocess cleanup before propagating caller cancellation."""
    cleanup = asyncio.create_task(client.disconnect())
    cancellation: asyncio.CancelledError | None = None
    cleanup_error: Exception | None = None

    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError as e:
            cancellation = e
        except Exception as e:
            cleanup_error = e
            break

    if cleanup_error is None:
        try:
            cleanup.result()
        except asyncio.CancelledError as e:
            cancellation = cancellation or e
        except Exception as e:
            cleanup_error = e

    if cancellation is not None:
        if cleanup_error is not None:
            log.error(
                "Claude subprocess cleanup failed during cancellation: %s",
                cleanup_error,
            )
        raise cancellation
    if cleanup_error is not None:
        raise cleanup_error


async def _run(prompt: Any, options: sdk.ClaudeAgentOptions) -> dict[str, Any]:
    """Drive one query to completion and reduce the message stream to a result.

    The stream carries more than the answer: RateLimitEvent is how the seat
    reports quota pressure, and AssistantMessage.error is how the CLI reports
    an expired login. Both are side-channels that must be inspected here —
    neither shows up as an exception.
    """
    structured: Any = None
    text_parts: list[str] = []
    meta: dict[str, Any] = {}
    fatal: str | None = None
    quota_rejected = False

    async with _query_slots:
        client = sdk.ClaudeSDKClient(options)
        try:
            try:
                await client.connect(prompt)
            except sdk.ClaudeSDKError:
                raise
            except Exception as e:
                raise ClaudeError(str(e), reason="sdk_connect_error") from e
            messages = client.receive_response()
            while True:
                try:
                    msg = await anext(messages)
                except StopAsyncIteration:
                    break
                except sdk.ClaudeSDKError:
                    raise
                except Exception as e:
                    # SDK 0.2.128 turns runtime ProcessError instances into a
                    # plain Exception before exposing them to consumers.
                    raise ClaudeError(str(e), reason="sdk_query_error") from e

                kind = type(msg).__name__

                if kind == "RateLimitEvent":
                    info = msg.rate_limit_info
                    meta["rate_limit"] = {
                        "type": getattr(info, "rate_limit_type", None),
                        "status": getattr(info, "status", None),
                        "utilization": getattr(info, "utilization", None),
                        "resets_at": getattr(info, "resets_at", None),
                    }
                    if getattr(info, "status", None) == "rejected":
                        quota_rejected = True
                    await alerts.on_rate_limit(info)

                elif kind == "AssistantMessage":
                    if msg.error:
                        fatal = msg.error
                        await alerts.on_claude_error(msg.error)
                    for block in msg.content:
                        if getattr(block, "text", None):
                            text_parts.append(block.text)

                elif kind == "ResultMessage":
                    structured = msg.structured_output
                    meta.update(
                        cost_usd=msg.total_cost_usd,
                        usage=msg.usage,
                        stop_reason=msg.stop_reason,
                        duration_ms=msg.duration_ms,
                        num_turns=msg.num_turns,
                        model=options.model,
                    )
                    if msg.is_error:
                        fatal = fatal or (
                            msg.errors[0] if msg.errors else "unknown_error"
                        )
        finally:
            # Release the semaphore only after the CLI has been reaped, even
            # when raw asyncio cancellation arrives during disconnect().
            await _disconnect(client)

    if quota_rejected:
        raise QuotaExhausted(meta.get("rate_limit", {}).get("type") or "quota")
    if fatal:
        raise ClaudeError(fatal)

    if structured is None:
        joined = "".join(text_parts).strip()
        if not joined:
            raise ClaudeError("empty response from Claude")
        # Match OpenRouter's bare json_object response contract.
        return {"text": _unwrap_fenced_block(joined), "meta": meta}

    return {"output": structured, "meta": meta}


async def _guarded(prompt: Any, options: sdk.ClaudeAgentOptions) -> dict[str, Any]:
    """_run with a wall-clock cap — the SDK spawns a subprocess that can hang."""
    try:
        return await asyncio.wait_for(_run(prompt, options), timeout=config.CLAUDE_TIMEOUT_S)
    except asyncio.CancelledError as e:
        task = asyncio.current_task()
        if task is None or task.cancelling():
            raise
        raise ClaudeError(f"timed out after {config.CLAUDE_TIMEOUT_S}s") from e
    except TimeoutError as e:
        raise ClaudeError(f"timed out after {config.CLAUDE_TIMEOUT_S}s") from e
    except sdk.ClaudeSDKError as e:
        failure = type(e).__name__.removesuffix("Error").lower()
        raise ClaudeError(str(e), reason=f"sdk_{failure}_error") from e


async def generate(
    *, system: str, user: str, model: str, effort: str, thinking: str,
    schema: dict[str, Any] | None,
) -> dict[str, Any]:
    """Single-shot text generation."""
    return await _guarded(user, _options(model, effort, thinking, system, schema))


async def describe(
    *, system: str, prompt: str, mime: str, data_b64: str, model: str, effort: str,
    thinking: str, schema: dict[str, Any] | None,
) -> dict[str, Any]:
    """Multimodal description. The image rides the streaming-input path, which
    is the only way to attach content blocks rather than a bare string."""

    async def _prompt():
        yield {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data_b64}},
                ],
            },
        }

    return await _guarded(_prompt(), _options(model, effort, thinking, system, schema))
