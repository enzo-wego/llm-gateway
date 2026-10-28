"""Direct Gemini API client for embedding fallback only."""

import asyncio
import logging
from typing import Any

import httpx

from . import config
from .openrouter import _retry_after

log = logging.getLogger("llm-gateway.gemini")

_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
# ponytail: retries can outlast the caller's timeout when Gemini hangs; upgrade:
# an overall per-request deadline.
_RETRY_DELAYS_S = (1, 2, 4)

_GEMINI_MAX_BATCH = 96
_TIMEOUT_S = 60


class GeminiError(RuntimeError):
    pass


def _error(message: str) -> GeminiError:
    key = config.GEMINI_API_KEY
    if key:
        message = message.replace(key, "[redacted]")
    return GeminiError(message[:400])


async def embed(texts: list[str], dims: int) -> list[list[float]]:
    """Embed texts in request order through Gemini's batch endpoint."""
    model = config.EMBED_MODEL.removeprefix("google/")
    url = f"{config.GEMINI_BASE}/models/{model}:batchEmbedContents"
    headers = {"x-goog-api-key": config.GEMINI_API_KEY, "Content-Type": "application/json"}
    out: list[list[float]] = []

    for start in range(0, len(texts), _GEMINI_MAX_BATCH):
        chunk = texts[start:start + _GEMINI_MAX_BATCH]
        payload: dict[str, Any] = {
            "requests": [
                {
                    "model": f"models/{model}",
                    "content": {"parts": [{"text": text}]},
                    "outputDimensionality": dims,
                }
                for text in chunk
            ]
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            for delay in (*_RETRY_DELAYS_S, None):
                try:
                    response = await client.post(url, headers=headers, json=payload)
                except httpx.HTTPError as exc:
                    if delay is None:
                        raise _error(
                            f"Gemini transport error: {type(exc).__name__}"
                        ) from exc
                    log.warning(
                        "Gemini transport error %s on embed, retrying in %.1fs",
                        type(exc).__name__,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue

                if response.status_code not in _RETRYABLE_STATUSES or delay is None:
                    break
                wait = (
                    _retry_after(response, delay)
                    if response.status_code in {429, 503}
                    else delay
                )
                log.warning(
                    "Gemini %s on embed, retrying in %.1fs",
                    response.status_code,
                    wait,
                )
                await asyncio.sleep(wait)

        if response.status_code != 200:
            raise _error(f"Gemini {response.status_code}: {response.text}")
        try:
            rows = response.json()["embeddings"]
            vectors = [row["values"] for row in rows]
            if len(vectors) != len(chunk):
                raise ValueError(f"got {len(vectors)} embeddings for {len(chunk)} inputs")
            if any(not isinstance(vector, list) or len(vector) != dims for vector in vectors):
                raise ValueError(f"embedding vector length differs from {dims}")
        except (ValueError, KeyError, TypeError) as exc:
            raise _error(f"Gemini invalid response: {type(exc).__name__}: {exc}") from exc
        out.extend(vectors)

    return out
