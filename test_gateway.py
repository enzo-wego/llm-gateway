"""Self-check for the two pieces of logic that fail silently.

Run: .venv/bin/python test_gateway.py

Everything else in this service fails loudly — a bad request 4xxs, a dead
backend 502s. These two do not:

  * embedding order — OpenRouter returns rows carrying their own `index`, and
    arrival order is not guaranteed. Trusting arrival order attaches every
    vector to the wrong row. Search then returns confident nonsense, with no
    error anywhere, and you find out weeks later.

  * quota state — if a seat rejection does not stick, every subsequent request
    pays a full round-trip to the CLI before failing over, turning a graceful
    degradation into a latency collapse.
"""

import asyncio
import os
import sys

os.environ.setdefault("LLM_GATEWAY_API_KEY", "test")
os.environ.setdefault("OPENROUTER_API_KEY", "sk-or-test")

from app import openrouter  # noqa: E402
from app.main import _Quota, _tier_config  # noqa: E402
from app import config  # noqa: E402


def test_embed_reorders_by_index() -> None:
    """A response whose rows arrive shuffled must still come back in input order."""
    captured: dict = {}

    async def fake_post(path, payload, timeout):
        captured["inputs"] = payload["input"]
        # Deliberately out of order, each row tagged with its true index.
        return {"data": [
            {"index": 2, "embedding": [3.0]},
            {"index": 0, "embedding": [1.0]},
            {"index": 1, "embedding": [2.0]},
        ]}

    openrouter._post = fake_post
    got = asyncio.run(openrouter.embed(["a", "b", "c"], 8))
    assert got == [[1.0], [2.0], [3.0]], f"order not restored: {got}"
    assert captured["inputs"] == ["a", "b", "c"]


def test_embed_rejects_bad_index() -> None:
    """An out-of-range index must raise, never silently drop or misplace a row."""
    async def fake_post(path, payload, timeout):
        return {"data": [{"index": 7, "embedding": [1.0]}]}

    openrouter._post = fake_post
    try:
        asyncio.run(openrouter.embed(["a"], 8))
    except openrouter.OpenRouterError:
        return
    raise AssertionError("out-of-range index was accepted")


def test_embed_rejects_count_mismatch() -> None:
    """Fewer vectors than inputs must raise rather than return a short list."""
    async def fake_post(path, payload, timeout):
        return {"data": [{"index": 0, "embedding": [1.0]}]}

    openrouter._post = fake_post
    try:
        asyncio.run(openrouter.embed(["a", "b"], 8))
    except openrouter.OpenRouterError:
        return
    raise AssertionError("count mismatch was accepted")


def test_embed_chunks_large_batches() -> None:
    """Inputs beyond the per-call cap are split, and order survives the split."""
    calls: list[int] = []

    async def fake_post(path, payload, timeout):
        n = len(payload["input"])
        calls.append(n)
        base = sum(calls[:-1])
        return {"data": [{"index": i, "embedding": [float(base + i)]} for i in range(n)]}

    openrouter._post = fake_post
    n = config.EMBED_MAX_BATCH + 5
    got = asyncio.run(openrouter.embed([f"t{i}" for i in range(n)], 8))
    assert len(calls) == 2, f"expected 2 chunks, got {calls}"
    assert got == [[float(i)] for i in range(n)], "order broken across chunk boundary"


def test_quota_blocks_then_recovers() -> None:
    q = _Quota()
    assert q.seat_available()

    q.note_rejection(None)              # no reset timestamp -> bounded backoff
    assert not q.seat_available(), "rejection did not stick"

    q.blocked_until = 0.0               # simulate the window elapsing
    assert q.seat_available(), "seat never recovered"


def test_quota_honours_reset_timestamp() -> None:
    import time
    q = _Quota()
    q.note_rejection(int(time.time()) + 3600)
    assert not q.seat_available()
    q2 = _Quota()
    q2.note_rejection(int(time.time()) - 10)   # already elapsed
    assert q2.seat_available(), "past reset should not block"


def test_tier_routing() -> None:
    b, cm, om, _ = _tier_config("summary")
    assert cm == config.MODEL_SUMMARY and om == config.OR_MODEL_SUMMARY
    b, cm, om, _ = _tier_config("cheap")
    assert cm == config.MODEL_CHEAP and om == config.OR_MODEL_CHEAP


def test_api_key_is_unset_at_import() -> None:
    """config.py must strip the credential that would silently bill the API key."""
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "ANTHROPIC_AUTH_TOKEN" not in os.environ


if __name__ == "__main__":
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-should-be-stripped"
    import importlib
    importlib.reload(config)  # prove the pop happens on import, not by luck

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok   {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
