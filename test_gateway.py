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
from contextlib import contextmanager
import os
from pathlib import Path
import stat
import sys
import tempfile

os.environ.setdefault("LLM_GATEWAY_API_KEY", "test")
os.environ.setdefault("OPENROUTER_API_KEY", "sk-or-test")

from app import openrouter  # noqa: E402
from app.main import _Quota, _tier_config, app  # noqa: E402
from app import config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@contextmanager
def isolated_config_env(initial: str):
    """Point config writes at a temporary .env and restore live values after."""
    old_path = config.ENV_FILE
    old_values = config.editable_config()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ".env"
        path.write_text(initial)
        path.chmod(0o600)
        config.ENV_FILE = path
        try:
            yield path
        finally:
            for name, value in old_values.items():
                setattr(config, name, value)
            config.ENV_FILE = old_path


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


def test_config_get_is_authenticated_and_never_returns_secrets() -> None:
    client = TestClient(app)
    assert client.get("/config").status_code == 401

    response = client.get("/config", headers={"X-API-Key": config.API_KEY})
    assert response.status_code == 200
    assert set(response.json()) == set(config.EDITABLE_ENV_KEYS)
    encoded = response.text
    assert "API_KEY" not in encoded
    assert config.OPENROUTER_KEY not in encoded


def test_config_put_applies_partial_update_and_preserves_env() -> None:
    original = (
        "# gateway settings\n"
        "UNRELATED=keep-me\n"
        "LLM_GATEWAY_BACKEND_SUMMARY=claude\n"
        "LLM_GATEWAY_MODEL_CHEAP=unchanged-model\n"
    )
    with isolated_config_env(original) as env_path:
        client = TestClient(app)
        response = client.put(
            "/config",
            headers={"X-API-Key": config.API_KEY},
            json={
                "BACKEND_SUMMARY": "openrouter",
                "FALLBACK_ON_QUOTA": False,
                "MAX_BUDGET_USD": "0.75",
                "CLAUDE_TIMEOUT_S": "199",
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["BACKEND_SUMMARY"] == "openrouter"
        assert body["FALLBACK_ON_QUOTA"] is False
        assert body["MAX_BUDGET_USD"] == 0.75
        assert body["CLAUDE_TIMEOUT_S"] == 199
        assert config.BACKEND_SUMMARY == "openrouter"

        persisted = env_path.read_text()
        assert "# gateway settings\n" in persisted
        assert "UNRELATED=keep-me\n" in persisted
        assert "LLM_GATEWAY_MODEL_CHEAP=unchanged-model\n" in persisted
        assert "LLM_GATEWAY_BACKEND_SUMMARY=openrouter\n" in persisted
        assert "LLM_GATEWAY_FALLBACK_ON_QUOTA=false\n" in persisted
        assert "LLM_GATEWAY_MAX_BUDGET_USD=0.75\n" in persisted
        assert "LLM_GATEWAY_CLAUDE_TIMEOUT_S=199\n" in persisted
        assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_config_put_rejects_unknown_and_invalid_values_without_changes() -> None:
    invalid = [
        ({"TYPO_BACKEND": "claude"}, "unknown config key"),
        ({"BACKEND_CHEAP": "gemini"}, "claude or openrouter"),
        ({"EFFORT_SUMMARY": "extreme"}, "must be one of"),
        ({"MAX_BUDGET_USD": 0}, "positive number"),
        ({"CLAUDE_TIMEOUT_S": 200}, "below agent-mem's 200s"),
    ]
    with isolated_config_env("# unchanged\n") as env_path:
        client = TestClient(app)
        before_values = config.editable_config()
        for payload, detail in invalid:
            response = client.put(
                "/config", headers={"X-API-Key": config.API_KEY}, json=payload
            )
            assert response.status_code == 400, response.text
            assert detail in response.json()["detail"]
            assert config.editable_config() == before_values
            assert env_path.read_text() == "# unchanged\n"


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
