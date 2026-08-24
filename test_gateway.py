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
import logging
from contextlib import contextmanager
import os
from pathlib import Path
import stat
import sys
import tempfile

os.environ.setdefault("LLM_GATEWAY_API_KEY", "test")
os.environ.setdefault("OPENROUTER_API_KEY", "sk-or-test")

from app import claude, config, main, openrouter  # noqa: E402
from app.main import _Quota, _tier_config, app  # noqa: E402
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


@contextmanager
def captured_gateway_logs():
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Capture()
    original_level = main.log.level
    main.log.setLevel(logging.INFO)
    main.log.addHandler(handler)
    try:
        yield records
    finally:
        main.log.removeHandler(handler)
        main.log.setLevel(original_level)


def test_claude_unwraps_entire_fenced_block() -> None:
    cases = [
        ("fenced JSON", '```json\n{"ok":true}\n```', '{"ok":true}'),
        ("bare fence", '```\n{"ok":true}\n```', '{"ok":true}'),
        ("bare JSON", '{"ok":true}', '{"ok":true}'),
        (
            "fence inside prose",
            'Result:\n```json\n{"ok":true}\n```\nDone.',
            'Result:\n```json\n{"ok":true}\n```\nDone.',
        ),
        ("unclosed fence", '```json\n{"ok":true}', '```json\n{"ok":true}'),
        ("non-JSON content", "```text\nnot JSON\n```", "not JSON"),
        # Splicing two blocks together would produce a corrupt body that still
        # looks like a successful response — leave ambiguous input alone.
        (
            "two adjacent blocks",
            '```json\n{"a":1}\n```\n```json\n{"b":2}\n```',
            '```json\n{"a":1}\n```\n```json\n{"b":2}\n```',
        ),
    ]

    for name, response, expected in cases:
        actual = claude._unwrap_fenced_block(response)
        assert actual == expected, f"{name}: got {actual!r}"


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


def test_openrouter_generate_honours_schema_and_returns_output() -> None:
    """A schema request must come back as `output`, exactly as the Claude path does.

    Before this, `schema` was accepted by the route and dropped here, so flipping
    a backend — or a quota fallback firing — changed the response shape silently.
    """
    captured: dict = {}
    original = openrouter._post

    async def fake_post(path, payload, timeout):
        captured["payload"] = payload
        return {"choices": [{"message": {"content": '{"code":"13.1"}'}}], "usage": {}}

    openrouter._post = fake_post
    try:
        schema = {"type": "object", "properties": {"code": {"type": "string"}}}
        res = asyncio.run(openrouter.generate(system="s", user="u", model="m", schema=schema))
        fmt = captured["payload"]["response_format"]
        assert fmt["type"] == "json_schema", f"schema not forwarded: {fmt}"
        assert fmt["json_schema"]["schema"] == schema
        assert res["output"] == {"code": "13.1"}, f"no parsed output: {res}"
        assert "text" not in res, "schema requests must not also return raw text"

        # agent-mem sends no schema and reads only `text` — that must not change.
        res = asyncio.run(openrouter.generate(system="s", user="u", model="m"))
        assert captured["payload"]["response_format"] == {"type": "json_object"}
        assert res["text"] == '{"code":"13.1"}' and "output" not in res
    finally:
        openrouter._post = original


def test_openrouter_generate_max_tokens_is_configurable() -> None:
    """The output ceiling must follow config, and default to the historical 4096."""
    captured: dict = {}
    original = openrouter._post
    old = config.OR_MAX_TOKENS

    async def fake_post(path, payload, timeout):
        captured["max_tokens"] = payload["max_tokens"]
        return {"choices": [{"message": {"content": "{}"}}]}

    openrouter._post = fake_post
    try:
        assert old == 4096, f"default changed under agent-mem: {old}"
        config.OR_MAX_TOKENS = 16384
        asyncio.run(openrouter.generate(system="", user="u", model="m"))
        assert captured["max_tokens"] == 16384, captured
    finally:
        config.OR_MAX_TOKENS = old
        openrouter._post = original


def test_describe_model_defaults_to_the_summary_tier() -> None:
    """agent-mem's OCR quality rides on this default — it must not drop a tier."""
    assert config.OR_MODEL_DESCRIBE == config.OR_MODEL_SUMMARY, (
        f"describe model diverged from summary: {config.OR_MODEL_DESCRIBE}"
    )


def test_openrouter_describe_max_tokens_is_configurable() -> None:
    """describe had a hard-coded 2048 cap that truncated dense pages. It must now
    follow config, and default to the historical 2048 so agent-mem is unaffected."""
    captured: dict = {}
    original = openrouter._post
    old = config.OR_MAX_TOKENS_DESCRIBE

    async def fake_post(path, payload, timeout):
        captured["max_tokens"] = payload["max_tokens"]
        return {"choices": [{"message": {"content": "{}"}}]}

    openrouter._post = fake_post
    try:
        assert old == 2048, f"describe default changed under agent-mem: {old}"
        config.OR_MAX_TOKENS_DESCRIBE = 8192
        asyncio.run(openrouter.describe(prompt="p", mime="image/png", data_b64="Zg==", model="m"))
        assert captured["max_tokens"] == 8192, captured
    finally:
        config.OR_MAX_TOKENS_DESCRIBE = old
        openrouter._post = original


def test_truncation_sets_meta_only_on_length_finish() -> None:
    """finish_reason == "length" flags meta["truncated"] and the partial text is
    still returned; any other finish_reason (or none) must NOT add the key, so a
    complete answer is never mistaken for a clipped one."""
    original = openrouter._post

    async def length_post(path, payload, timeout):
        return {"choices": [{"message": {"content": "partial"}, "finish_reason": "length"}],
                "usage": {"completion_tokens": 2048}}

    async def stop_post(path, payload, timeout):
        return {"choices": [{"message": {"content": "whole"}, "finish_reason": "stop"}],
                "usage": {"completion_tokens": 12}}

    async def no_finish_post(path, payload, timeout):
        return {"choices": [{"message": {"content": "whole"}}], "usage": {"completion_tokens": 12}}

    try:
        openrouter._post = length_post
        res = asyncio.run(openrouter.describe(prompt="p", mime="image/png", data_b64="Zg==", model="m"))
        assert res["meta"]["truncated"] is True, res
        assert res["text"] == "partial", "the partial text must still come back"

        openrouter._post = stop_post
        res = asyncio.run(openrouter.describe(prompt="p", mime="image/png", data_b64="Zg==", model="m"))
        assert "truncated" not in res["meta"], f"a complete answer must not carry the key: {res}"

        openrouter._post = no_finish_post
        res = asyncio.run(openrouter.generate(system="", user="u", model="m"))
        assert "truncated" not in res["meta"], f"absent finish_reason must not flag truncation: {res}"
    finally:
        openrouter._post = original


def test_untruncated_result_is_byte_identical_to_pre_change() -> None:
    """agent-mem ignores meta.truncated and reads only `text`. A response whose
    finish_reason is not "length" must deep-equal the exact dict the pre-change
    _result produced — text + meta{model, usage}, no truncated key anywhere — on
    both the text branch (no schema) and the output branch (schema)."""
    original = openrouter._post
    usage = {"completion_tokens": 12, "prompt_tokens": 5}

    async def complete_text(path, payload, timeout):
        return {"choices": [{"message": {"content": "whole"}, "finish_reason": "stop"}], "usage": usage}

    async def complete_json(path, payload, timeout):
        # finish_reason absent — the exact historic shape agent-mem already sees.
        return {"choices": [{"message": {"content": '{"code":"13.1"}'}}], "usage": usage}

    try:
        openrouter._post = complete_text
        res = asyncio.run(openrouter.describe(prompt="p", mime="image/png", data_b64="Zg==", model="m"))
        assert res == {"text": "whole", "meta": {"model": "m", "usage": usage}}, res

        openrouter._post = complete_json
        schema = {"type": "object", "properties": {"code": {"type": "string"}}}
        res = asyncio.run(openrouter.generate(system="s", user="u", model="m", schema=schema))
        assert res == {"output": {"code": "13.1"}, "meta": {"model": "m", "usage": usage}}, res
    finally:
        openrouter._post = original

def test_guarded_converts_deadline_cancellation_to_claude_error() -> None:
    original = claude._run

    async def deadline_exceeded(prompt, options):
        raise asyncio.CancelledError("deadline exceeded")

    async def check() -> None:
        claude._run = deadline_exceeded
        try:
            try:
                await claude._guarded("prompt", None)
            except claude.ClaudeError as exc:
                assert str(exc) == f"timed out after {config.CLAUDE_TIMEOUT_S}s"
            except asyncio.CancelledError as exc:
                raise AssertionError("deadline cancellation escaped _guarded") from exc
            else:
                raise AssertionError("deadline cancellation was accepted")
        finally:
            claude._run = original

    asyncio.run(check())


def test_guarded_propagates_external_cancellation() -> None:
    original = claude._run

    async def check() -> None:
        started = asyncio.Event()

        async def blocked(prompt, options):
            started.set()
            await asyncio.Event().wait()

        claude._run = blocked
        task = asyncio.create_task(claude._guarded("prompt", None))
        try:
            await started.wait()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                return
            except claude.ClaudeError as exc:
                raise AssertionError("external cancellation became ClaudeError") from exc
            raise AssertionError("external cancellation was swallowed")
        finally:
            claude._run = original
            if not task.done():
                task.cancel()

    asyncio.run(check())


def test_generate_deadline_returns_classified_502() -> None:
    original_run = claude._run
    original_config = (config.BACKEND_CHEAP, config.FALLBACK_ON_QUOTA)
    original_blocked_until = main.quota.blocked_until

    async def deadline_exceeded(prompt, options):
        raise asyncio.CancelledError("deadline exceeded")

    try:
        claude._run = deadline_exceeded
        config.BACKEND_CHEAP = "claude"
        config.FALLBACK_ON_QUOTA = False
        main.quota.blocked_until = 0
        response = TestClient(app, raise_server_exceptions=False).post(
            "/generate",
            headers={"X-API-Key": config.API_KEY},
            json={"user": "prompt", "tier": "cheap"},
        )
        assert response.status_code == 502, response.text
        assert response.json()["detail"] == f"timed out after {config.CLAUDE_TIMEOUT_S}s"
    finally:
        claude._run = original_run
        config.BACKEND_CHEAP, config.FALLBACK_ON_QUOTA = original_config
        main.quota.blocked_until = original_blocked_until


def test_generate_logs_duration_and_request_metadata() -> None:
    original_generate = claude.generate
    original_quota = main.quota
    original_config = (
        config.BACKEND_CHEAP,
        config.MODEL_CHEAP,
        config.EFFORT_CHEAP,
        config.FALLBACK_ON_QUOTA,
    )

    async def generated(**kwargs):
        return {"text": "ok", "meta": {}}

    try:
        claude.generate = generated
        main.quota = _Quota()
        config.BACKEND_CHEAP = "claude"
        config.MODEL_CHEAP = "test-model"
        config.EFFORT_CHEAP = "low"
        config.FALLBACK_ON_QUOTA = False

        with captured_gateway_logs() as records:
            response = TestClient(app).post(
                "/generate",
                headers={"X-API-Key": config.API_KEY},
                json={"user": "prompt", "tier": "cheap"},
            )
        assert response.status_code == 200, response.text

        messages = [record.getMessage() for record in records]
        assert len(messages) == 1, messages
        message = messages[0]
        assert "tier=cheap" in message
        assert "backend=claude" in message
        assert "model=test-model" in message
        assert "effort=low" in message
        assert "status=200" in message
        duration = next(part for part in message.split() if part.startswith("duration_ms="))
        assert duration.removeprefix("duration_ms=").isdigit(), message
    finally:
        claude.generate = original_generate
        main.quota = original_quota
        (
            config.BACKEND_CHEAP,
            config.MODEL_CHEAP,
            config.EFFORT_CHEAP,
            config.FALLBACK_ON_QUOTA,
        ) = original_config


def test_generate_logs_each_outcome_once() -> None:
    original_claude_generate = claude.generate
    original_openrouter_generate = openrouter.generate
    original_quota = main.quota
    original_config = (
        config.BACKEND_CHEAP,
        config.MODEL_CHEAP,
        config.OR_MODEL_CHEAP,
        config.EFFORT_CHEAP,
        config.FALLBACK_ON_QUOTA,
    )

    async def claude_failed(**kwargs):
        raise claude.ClaudeError("failed")

    async def openrouter_generated(**kwargs):
        return {"text": "ok", "meta": {}}

    async def openrouter_failed(**kwargs):
        raise openrouter.OpenRouterError("failed")

    def assert_log(records: list[logging.LogRecord], *fields: str) -> None:
        messages = [record.getMessage() for record in records]
        assert len(messages) == 1, messages
        message = messages[0]
        assert "tier=cheap" in message
        assert "effort=low" in message
        for field in fields:
            assert field in message
        duration = next(part for part in message.split() if part.startswith("duration_ms="))
        assert duration.removeprefix("duration_ms=").isdigit(), message

    try:
        main.quota = _Quota()
        config.BACKEND_CHEAP = "claude"
        config.MODEL_CHEAP = "claude-model"
        config.OR_MODEL_CHEAP = "openrouter-model"
        config.EFFORT_CHEAP = "low"
        config.FALLBACK_ON_QUOTA = True
        claude.generate = claude_failed
        openrouter.generate = openrouter_generated

        with captured_gateway_logs() as records:
            response = TestClient(app).post(
                "/generate",
                headers={"X-API-Key": config.API_KEY},
                json={"user": "prompt", "tier": "cheap"},
            )
        assert response.status_code == 200, response.text
        assert_log(
            records,
            "backend=openrouter",
            "model=openrouter-model",
            "status=200",
            "reason=claude_error",
        )

        config.BACKEND_CHEAP = "openrouter"
        openrouter.generate = openrouter_failed
        with captured_gateway_logs() as records:
            response = TestClient(app).post(
                "/generate",
                headers={"X-API-Key": config.API_KEY},
                json={"user": "prompt", "tier": "cheap"},
            )
        assert response.status_code == 502, response.text
        assert_log(
            records,
            "backend=openrouter",
            "model=openrouter-model",
            "status=502",
            "reason=openrouter_error",
        )

        started = asyncio.Event()

        async def blocked(**kwargs):
            started.set()
            await asyncio.Event().wait()

        async def cancel_generate() -> None:
            claude.generate = blocked
            config.BACKEND_CHEAP = "claude"
            config.FALLBACK_ON_QUOTA = False
            task = asyncio.create_task(main.generate(main.GenerateIn(user="prompt", tier="cheap")))
            await started.wait()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                return
            raise AssertionError("external cancellation was swallowed")

        with captured_gateway_logs() as records:
            asyncio.run(cancel_generate())
        assert_log(
            records,
            "backend=claude",
            "model=claude-model",
            "status=cancelled",
            "reason=external_cancelled",
        )
    finally:
        claude.generate = original_claude_generate
        openrouter.generate = original_openrouter_generate
        main.quota = original_quota
        (
            config.BACKEND_CHEAP,
            config.MODEL_CHEAP,
            config.OR_MODEL_CHEAP,
            config.EFFORT_CHEAP,
            config.FALLBACK_ON_QUOTA,
        ) = original_config


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
