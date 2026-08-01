"""OpenRouter backend — embeddings always, generation only as a fallback.

Embeddings live here permanently: Anthropic has no embeddings API, and every
stored vector in agent-mem (768 flat, 3072 graph) came from
gemini-embedding-001. A query vector is only comparable to stored vectors from
the same model, so changing it would silently degrade search rather than error.

Generation lives here as the off-switch target — when a route's backend is set
to "openrouter", or when the seat's quota is spent and FALLBACK_ON_QUOTA is on.
It reproduces what agent-mem does today.
"""

import logging
from typing import Any

import httpx

from . import alerts, config

log = logging.getLogger("llm-gateway.openrouter")


class OpenRouterError(RuntimeError):
    pass


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {config.OPENROUTER_KEY}", "Content-Type": "application/json"}


async def _post(path: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(f"{config.OPENROUTER_BASE}{path}", headers=_headers(), json=payload)

    if r.status_code != 200:
        body = r.text[:400]
        # 402 means the monthly cap is spent, which stops embedding writes and
        # therefore stops the graph growing. That has to reach Slack.
        await alerts.on_openrouter_error(r.status_code, body)
        raise OpenRouterError(f"OpenRouter {r.status_code}: {body}")

    data = r.json()
    if isinstance(data, dict) and data.get("error"):
        raise OpenRouterError(str(data["error"])[:400])
    return data


async def generate(*, system: str, user: str, model: str) -> dict[str, Any]:
    """Chat completion, JSON-constrained, matching agent-mem's existing shape."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})

    data = await _post(
        "/chat/completions",
        {
            "model": model,
            "messages": messages,
            "temperature": 0.3,
            "max_tokens": 4096,
            "response_format": {"type": "json_object"},
        },
        timeout=config.CLAUDE_TIMEOUT_S,
    )
    choices = data.get("choices") or []
    if not choices:
        raise OpenRouterError("empty response from OpenRouter")
    return {"text": choices[0]["message"]["content"], "meta": {"model": model, "usage": data.get("usage")}}


async def describe(*, prompt: str, mime: str, data_b64: str, model: str) -> dict[str, Any]:
    """Multimodal description via an image data URI, as agent-mem does today."""
    data = await _post(
        "/chat/completions",
        {
            "model": model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data_b64}"}},
            ]}],
            "temperature": 0.2,
            "max_tokens": 2048,
            "response_format": {"type": "json_object"},
        },
        timeout=config.CLAUDE_TIMEOUT_S,
    )
    choices = data.get("choices") or []
    if not choices:
        raise OpenRouterError("empty describe response from OpenRouter")
    return {"text": choices[0]["message"]["content"], "meta": {"model": model, "usage": data.get("usage")}}


async def embed(texts: list[str], dims: int) -> list[list[float]]:
    """Embed texts in order, chunked to stay under the provider's per-call cap.

    Results are reordered by the response's own index field rather than trusting
    arrival order — a mis-ordered batch would attach every vector to the wrong
    row, which is silent and very expensive to discover later.
    """
    out: list[list[float]] = []
    for start in range(0, len(texts), config.EMBED_MAX_BATCH):
        chunk = texts[start:start + config.EMBED_MAX_BATCH]
        data = await _post(
            "/embeddings",
            {"model": config.EMBED_MODEL, "input": chunk, "dimensions": dims},
            timeout=120,
        )
        rows = data.get("data") or []
        if len(rows) != len(chunk):
            raise OpenRouterError(f"got {len(rows)} embeddings for {len(chunk)} inputs")

        ordered: list[list[float]] = [[]] * len(chunk)
        for row in rows:
            i = row.get("index")
            if not isinstance(i, int) or not 0 <= i < len(chunk):
                raise OpenRouterError(f"embedding index {i!r} out of range for {len(chunk)} inputs")
            ordered[i] = row["embedding"]
        out.extend(ordered)
    return out


async def key_usage() -> dict[str, Any]:
    """Report the OpenRouter key's spend and remaining limit.

    Lives here because this process is the only one holding the key. agent-mem
    used to read it directly to render a budget widget; now it asks the gateway,
    so the credential stays in one place and the dashboard keeps its visibility.
    """
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{config.OPENROUTER_BASE}/key", headers=_headers())
    if r.status_code != 200:
        raise OpenRouterError(f"key usage returned {r.status_code}: {r.text[:200]}")
    return r.json().get("data") or {}
