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
import json
import logging
from contextlib import contextmanager
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace

os.environ.setdefault("LLM_GATEWAY_API_KEY", "test")
os.environ.setdefault("OPENROUTER_API_KEY", "sk-or-test")

import httpx  # noqa: E402
from app import alerts, claude, config, gemini, main, openrouter  # noqa: E402
from app.main import _Quota, _tier_config, app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# Several embed tests replace openrouter._post without restoring it.
_REAL_OPENROUTER_POST = openrouter._post
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _isolate_alert_globals() -> None:
    """Keep the whole suite off the live state file and off Slack."""
    config.ALERT_STATE_FILE = os.path.join(tempfile.mkdtemp(), "alert-state.json")
    config.ALERTS_ENABLED = False
    config.SLACK_BOT_TOKEN = ""


_isolate_alert_globals()


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

    async def fake_post(path, payload, timeout, **_):
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
    async def fake_post(path, payload, timeout, **_):
        return {"data": [{"index": 7, "embedding": [1.0]}]}

    openrouter._post = fake_post
    try:
        asyncio.run(openrouter.embed(["a"], 8))
    except openrouter.OpenRouterError:
        return
    raise AssertionError("out-of-range index was accepted")


def test_embed_rejects_count_mismatch() -> None:
    """Fewer vectors than inputs must raise rather than return a short list."""
    async def fake_post(path, payload, timeout, **_):
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

    async def fake_post(path, payload, timeout, **_):
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
    b, cm, om, _, th = _tier_config("summary")
    assert cm == config.MODEL_SUMMARY and om == config.OR_MODEL_SUMMARY
    assert th == config.THINKING_SUMMARY
    b, cm, om, _, th = _tier_config("cheap")
    assert cm == config.MODEL_CHEAP and om == config.OR_MODEL_CHEAP
    assert th == config.THINKING_CHEAP


def test_health_reports_the_model_the_backend_calls() -> None:
    # An openrouter tier reporting its Claude id hid a real model swap in prod.
    old = config.BACKEND_SUMMARY, config.BACKEND_CHEAP
    try:
        config.BACKEND_SUMMARY, config.BACKEND_CHEAP = "openrouter", "claude"
        models = TestClient(app).get("/health").json()["models"]
        assert models["summary"] == config.OR_MODEL_SUMMARY
        assert models["cheap"] == config.MODEL_CHEAP
    finally:
        config.BACKEND_SUMMARY, config.BACKEND_CHEAP = old


def test_options_pass_tier_thinking_to_the_sdk() -> None:
    # Sonnet 5.5 400s on thinking.type=disabled, so the mode must reach the SDK
    # as configured instead of being pinned to disabled.
    assert claude._options("m", "medium", "adaptive", "", None).thinking == {"type": "adaptive"}
    assert claude._options("m", "low", "disabled", "", None).thinking == {"type": "disabled"}


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
        ({"THINKING_SUMMARY": "enabled"}, "disabled or adaptive"),
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

    async def fake_post(path, payload, timeout, **_):
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

    async def fake_post(path, payload, timeout, **_):
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


def test_describe_sends_pdfs_as_documents_and_images_as_images() -> None:
    """A PDF in an image slot is a 400 on OpenRouter and unreadable junk on the
    seat, with a 200. Each backend needs its own PDF content type."""
    sent: list = []
    original_post, original_guarded = openrouter._post, claude._guarded

    async def fake_post(path, payload, timeout, **_):
        sent.append(payload["messages"][0]["content"][1])
        return {"choices": [{"message": {"content": "ok"}}]}

    async def fake_guarded(prompt, options):
        async for msg in prompt:
            sent.append(msg["message"]["content"][1])
        return {"text": "ok", "meta": {}}

    openrouter._post, claude._guarded = fake_post, fake_guarded
    try:
        for mime in ("application/pdf", "image/png"):
            asyncio.run(openrouter.describe(prompt="p", mime=mime, data_b64="QQ==", model="m"))
            asyncio.run(claude.describe(system="", prompt="p", mime=mime, data_b64="QQ==",
                                        model="m", effort="low", thinking="disabled", schema=None))
    finally:
        openrouter._post, claude._guarded = original_post, original_guarded

    or_pdf, claude_pdf, or_png, claude_png = sent
    assert or_pdf == {"type": "file", "file": {"filename": "attachment.pdf",
                                               "file_data": "data:application/pdf;base64,QQ=="}}
    assert claude_pdf["type"] == "document"
    assert claude_pdf["source"]["media_type"] == "application/pdf"
    assert or_png == {"type": "image_url", "image_url": {"url": "data:image/png;base64,QQ=="}}
    assert claude_png["type"] == "image"


def test_openrouter_describe_max_tokens_is_configurable() -> None:
    """describe had a hard-coded 2048 cap that truncated dense pages. It must now
    follow config, and default to the historical 2048 so agent-mem is unaffected."""
    captured: dict = {}
    original = openrouter._post
    old = config.OR_MAX_TOKENS_DESCRIBE

    async def fake_post(path, payload, timeout, **_):
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

def test_run_waits_before_spawning_fifth_query() -> None:
    original_client = claude.sdk.ClaudeSDKClient
    original_slots = claude._query_slots

    async def check() -> None:
        started: list[str] = []
        four_started = asyncio.Event()
        release = asyncio.Event()

        class BlockedClient:
            def __init__(self, options):
                pass

            async def connect(self, prompt):
                started.append(prompt)
                if len(started) == 4:
                    four_started.set()

            async def receive_response(self):
                await release.wait()
                if False:
                    yield None

            async def disconnect(self):
                pass

        claude.sdk.ClaudeSDKClient = BlockedClient
        claude._query_slots = asyncio.Semaphore(4)
        tasks = [
            asyncio.create_task(claude._run(str(index), None))
            for index in range(5)
        ]
        try:
            await asyncio.wait_for(four_started.wait(), timeout=1)
            await asyncio.sleep(0)
            assert started == ["0", "1", "2", "3"], started
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            assert started == ["0", "1", "2", "3", "4"], started
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            claude.sdk.ClaudeSDKClient = original_client
            claude._query_slots = original_slots

    asyncio.run(check())

def test_cancelled_query_holds_slot_until_subprocess_cleanup() -> None:
    original_client = claude.sdk.ClaudeSDKClient
    original_query = claude.sdk.query
    original_alert = claude.alerts.on_rate_limit
    original_slots = claude._query_slots

    async def check() -> None:
        started: list[str] = []
        four_started = asyncio.Event()
        fifth_started = asyncio.Event()
        alert_release = asyncio.Event()
        cleanup_started = asyncio.Event()
        cleanup_release = asyncio.Event()

        class RateLimitEvent:
            rate_limit_info = type("RateLimitInfo", (), {"status": None})()

        class FakeClient:
            def __init__(self, options):
                self.prompt = ""

            async def connect(self, prompt):
                self.prompt = prompt
                started.append(prompt)
                if len(started) == 4:
                    four_started.set()
                elif len(started) == 5:
                    fifth_started.set()

            async def receive_response(self):
                yield RateLimitEvent()

            async def disconnect(self):
                cleanup_started.set()
                await cleanup_release.wait()

        async def shortcut_must_not_run(**kwargs):
            if False:
                yield None
            raise AssertionError("sdk.query does not expose deterministic cleanup")

        async def blocked_alert(info):
            await alert_release.wait()

        claude.sdk.ClaudeSDKClient = FakeClient
        claude.sdk.query = shortcut_must_not_run
        claude.alerts.on_rate_limit = blocked_alert
        claude._query_slots = asyncio.Semaphore(4)
        tasks = [
            asyncio.create_task(claude._run(str(index), None))
            for index in range(5)
        ]
        try:
            await asyncio.wait_for(four_started.wait(), timeout=1)
            tasks[0].cancel()
            await asyncio.wait_for(cleanup_started.wait(), timeout=1)
            await asyncio.sleep(0)
            assert started == ["0", "1", "2", "3"], started
            cleanup_release.set()
            await asyncio.wait_for(fifth_started.wait(), timeout=1)
            alert_release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            alert_release.set()
            cleanup_release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            claude.sdk.ClaudeSDKClient = original_client
            claude.sdk.query = original_query
            claude.alerts.on_rate_limit = original_alert
            claude._query_slots = original_slots

    asyncio.run(check())

def test_cancellation_during_cleanup_keeps_query_slot() -> None:
    original_client = claude.sdk.ClaudeSDKClient
    original_slots = claude._query_slots

    async def check() -> None:
        started: list[str] = []
        four_cleaning = asyncio.Event()
        fifth_started = asyncio.Event()
        cleanup_release = asyncio.Event()
        cleaning = 0

        class FakeClient:
            def __init__(self, options):
                pass

            async def connect(self, prompt):
                started.append(prompt)
                if len(started) == 5:
                    fifth_started.set()

            async def receive_response(self):
                if False:
                    yield None

            async def disconnect(self):
                nonlocal cleaning
                cleaning += 1
                if cleaning == 4:
                    four_cleaning.set()
                await cleanup_release.wait()

        claude.sdk.ClaudeSDKClient = FakeClient
        claude._query_slots = asyncio.Semaphore(4)
        tasks = [
            asyncio.create_task(claude._run(str(index), None))
            for index in range(5)
        ]
        try:
            await asyncio.wait_for(four_cleaning.wait(), timeout=1)
            tasks[0].cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not tasks[0].done(), "cancellation escaped subprocess cleanup"
            assert not fifth_started.is_set(), started
            cleanup_release.set()
            await asyncio.wait_for(fifth_started.wait(), timeout=1)
            results = await asyncio.gather(*tasks, return_exceptions=True)
            assert isinstance(results[0], asyncio.CancelledError), results[0]
        finally:
            cleanup_release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            claude.sdk.ClaudeSDKClient = original_client
            claude._query_slots = original_slots

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

def test_generate_process_error_returns_classified_502() -> None:
    original_client = claude.sdk.ClaudeSDKClient
    original_query = claude.sdk.query
    original_quota = main.quota
    original_config = (config.BACKEND_CHEAP, config.FALLBACK_ON_QUOTA)

    class ProcessFailedClient:
        def __init__(self, options):
            pass

        async def connect(self, prompt):
            raise claude.sdk.ProcessError("Command failed", exit_code=-9)

        async def disconnect(self):
            pass

    async def shortcut_must_not_run(**kwargs):
        if False:
            yield None
        raise AssertionError("sdk.query does not expose deterministic cleanup")

    try:
        claude.sdk.ClaudeSDKClient = ProcessFailedClient
        claude.sdk.query = shortcut_must_not_run
        main.quota = _Quota()
        config.BACKEND_CHEAP = "claude"
        config.FALLBACK_ON_QUOTA = False

        with captured_gateway_logs() as records:
            response = TestClient(app, raise_server_exceptions=False).post(
                "/generate",
                headers={"X-API-Key": config.API_KEY},
                json={"user": "prompt", "tier": "cheap"},
            )

        assert response.status_code == 502, response.text
        assert response.json()["detail"] == "Command failed (exit code: -9)"
        messages = [record.getMessage() for record in records]
        assert len(messages) == 1, messages
        assert "status=502" in messages[0]
        assert "reason=sdk_process_error" in messages[0]
    finally:
        claude.sdk.ClaudeSDKClient = original_client
        claude.sdk.query = original_query
        main.quota = original_quota
        config.BACKEND_CHEAP, config.FALLBACK_ON_QUOTA = original_config

def test_generate_sdk_connect_error_returns_classified_502() -> None:
    original_client = claude.sdk.ClaudeSDKClient
    original_quota = main.quota
    original_config = (config.BACKEND_CHEAP, config.FALLBACK_ON_QUOTA)

    class ConnectFailedClient:
        def __init__(self, options):
            pass

        async def connect(self, prompt):
            raise Exception("Control request timeout: initialize")

        async def disconnect(self):
            pass

    try:
        claude.sdk.ClaudeSDKClient = ConnectFailedClient
        main.quota = _Quota()
        config.BACKEND_CHEAP = "claude"
        config.FALLBACK_ON_QUOTA = False

        with captured_gateway_logs() as records:
            response = TestClient(app, raise_server_exceptions=False).post(
                "/generate",
                headers={"X-API-Key": config.API_KEY},
                json={"user": "prompt", "tier": "cheap"},
            )

        assert response.status_code == 502, response.text
        assert response.json()["detail"] == "Control request timeout: initialize"
        messages = [record.getMessage() for record in records]
        assert len(messages) == 1, messages
        assert "status=502" in messages[0]
        assert "reason=sdk_connect_error" in messages[0]
    finally:
        claude.sdk.ClaudeSDKClient = original_client
        main.quota = original_quota
        config.BACKEND_CHEAP, config.FALLBACK_ON_QUOTA = original_config


def test_generate_sdk_runtime_error_returns_classified_502() -> None:
    original_client = claude.sdk.ClaudeSDKClient
    original_query = claude.sdk.query
    original_quota = main.quota
    original_config = (config.BACKEND_CHEAP, config.FALLBACK_ON_QUOTA)

    class RuntimeFailedClient:
        def __init__(self, options):
            pass

        async def connect(self, prompt):
            pass

        async def receive_response(self):
            if False:
                yield None
            raise Exception("Command failed with exit code -9")

        async def disconnect(self):
            pass

    async def shortcut_must_not_run(**kwargs):
        if False:
            yield None
        raise AssertionError("sdk.query does not expose deterministic cleanup")

    try:
        claude.sdk.ClaudeSDKClient = RuntimeFailedClient
        claude.sdk.query = shortcut_must_not_run
        main.quota = _Quota()
        config.BACKEND_CHEAP = "claude"
        config.FALLBACK_ON_QUOTA = False

        with captured_gateway_logs() as records:
            response = TestClient(app, raise_server_exceptions=False).post(
                "/generate",
                headers={"X-API-Key": config.API_KEY},
                json={"user": "prompt", "tier": "cheap"},
            )

        assert response.status_code == 502, response.text
        assert response.json()["detail"] == "Command failed with exit code -9"
        messages = [record.getMessage() for record in records]
        assert len(messages) == 1, messages
        assert "status=502" in messages[0]
        assert "reason=sdk_query_error" in messages[0]
    finally:
        claude.sdk.ClaudeSDKClient = original_client
        claude.sdk.query = original_query
        main.quota = original_quota
        config.BACKEND_CHEAP, config.FALLBACK_ON_QUOTA = original_config


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

    original_callers = main.callers
    try:
        main.callers = {}
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
            task = asyncio.create_task(
                main.generate(main.GenerateIn(user="prompt", tier="cheap"), caller="test/cancel")
            )
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
            "caller=test/cancel",
            "backend=claude",
            "model=claude-model",
            "status=cancelled",
            "reason=external_cancelled",
        )
        assert main.callers["test/cancel"]["calls"] == 1
        assert main.callers["test/cancel"]["errors"] == 1
    finally:
        main.callers = original_callers
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


# ── alert mute-until-reset ───────────────────────────────────────────────────


class _AlertCtl:
    def __init__(self, posts: list, state_path: str) -> None:
        self.posts = posts
        self.state_path = state_path
        self.now = 1000.0
        self.resp = {"ok": True}
        self.yield_in_post = False


@contextmanager
def isolated_alerts(posts: list):
    """Fresh alert state, fake clock and fake Slack transport; restore after."""
    ctl = _AlertCtl(posts, "")
    saved = (
        config.ALERT_STATE_FILE, config.ALERTS_ENABLED, config.SLACK_BOT_TOKEN,
        config.SLACK_ALERT_CHANNEL, config.ALERT_DEDUPE_S,
        alerts.time.time, alerts.httpx.AsyncClient,
    )

    class Resp:
        def json(self):
            if isinstance(ctl.resp, Exception):
                raise ctl.resp
            return ctl.resp

    class Client:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            if ctl.yield_in_post:
                await asyncio.sleep(0)
            if isinstance(ctl.resp, httpx.ConnectError):
                raise ctl.resp
            posts.append(k.get("json"))
            return Resp()

    with tempfile.TemporaryDirectory() as tmp:
        ctl.state_path = os.path.join(tmp, "state", "alert-state.json")
        config.ALERT_STATE_FILE = ctl.state_path
        config.ALERTS_ENABLED = True
        config.SLACK_BOT_TOKEN = "x"
        config.SLACK_ALERT_CHANNEL = "x"
        config.ALERT_DEDUPE_S = 1800
        alerts._reset_state()
        alerts.time.time = lambda: ctl.now
        alerts.httpx.AsyncClient = Client
        try:
            yield ctl
        finally:
            (
                config.ALERT_STATE_FILE, config.ALERTS_ENABLED, config.SLACK_BOT_TOKEN,
                config.SLACK_ALERT_CHANNEL, config.ALERT_DEDUPE_S,
                alerts.time.time, alerts.httpx.AsyncClient,
            ) = saved
            alerts._reset_state()


def _info(status="allowed_warning", resets=10000, kind="seven_day", util=0.95):
    return SimpleNamespace(status=status, rate_limit_type=kind, resets_at=resets, utilization=util)


def _fire(ctl, at, info=None):
    ctl.now = float(at)
    asyncio.run(alerts.on_rate_limit(info or _info()))


def _persisted(ctl) -> dict:
    with open(ctl.state_path) as f:
        return json.load(f)


def test_alert_warning_muted_until_reset() -> None:
    posts: list = []
    with isolated_alerts(posts) as ctl:
        _fire(ctl, 1000)
        _fire(ctl, 2801)
        assert len(posts) == 1, posts
        assert posts[0]["text"].endswith("Muted until the reset."), posts[0]["text"]


def test_alert_mute_expires_at_reset() -> None:
    posts: list = []
    with isolated_alerts(posts) as ctl:
        _fire(ctl, 1000)
        _fire(ctl, 9999)
        assert len(posts) == 1
        _fire(ctl, 10000)
        assert len(posts) == 2
    posts = []
    with isolated_alerts(posts) as ctl:
        for at, expected in ((1000, 1), (4999, 1), (5000, 2)):
            ctl.now = float(at)
            asyncio.run(alerts.alert("k", "t", until=5000))
            assert len(posts) == expected, (at, len(posts))


def test_alert_new_window_posts_again() -> None:
    posts: list = []
    with isolated_alerts(posts) as ctl:
        _fire(ctl, 1000)
        _fire(ctl, 2801, _info(resets=20000))
        assert len(posts) == 2


def test_alert_warning_does_not_mute_rejection() -> None:
    posts: list = []
    with isolated_alerts(posts) as ctl:
        _fire(ctl, 1000)
        _fire(ctl, 1000, _info("rejected"))
        assert len(posts) == 2
        _fire(ctl, 2801, _info("rejected"))
        assert len(posts) == 2


def test_alert_warn_threshold() -> None:
    for util, expected in ((0.94, 0), (0.95, 1), (None, 0), (1.0, 1)):
        posts: list = []
        with isolated_alerts(posts) as ctl:
            _fire(ctl, 1000, _info(util=util))
            assert len(posts) == expected, (util, posts)
    posts = []
    with isolated_alerts(posts) as ctl:
        _fire(ctl, 1000, _info("rejected", util=0.5))
        assert len(posts) == 1, posts
        _fire(ctl, 1000, _info("rejected", util=None))
        assert len(posts) == 1, posts


def test_alert_unusable_resets_fall_back_to_dedupe() -> None:
    for bad in (None, "bad", float("nan"), float("inf"), True, 500, 1000):
        for status in ("allowed_warning", "rejected"):
            posts: list = []
            with isolated_alerts(posts) as ctl:
                _fire(ctl, 1000, _info(status, bad))
                text = posts[0]["text"]
                assert "unknown" in text, (bad, status, text)
                assert "Muted until" not in text, (bad, status, text)
                _fire(ctl, 2801, _info(status, bad))
                assert len(posts) == 2, (bad, status)


def _delivery_failure_case(first_resp) -> None:
    posts: list = []
    with isolated_alerts(posts) as ctl:
        key = "quota-warn:seven_day:10000"
        ctl.resp = first_resp
        _fire(ctl, 1000)
        assert len(posts) == (0 if isinstance(first_resp, Exception) else 1)
        attempts = len(posts)
        assert _persisted(ctl)[key] == 2800
        _fire(ctl, 1001)
        assert len(posts) == attempts
        ctl.resp = {"ok": True}
        _fire(ctl, 2801)
        assert len(posts) == attempts + 1
        assert _persisted(ctl)[key] == 10000
        alerts._reset_state()
        _fire(ctl, 2802)
        _fire(ctl, 9999)
        assert len(posts) == attempts + 1


def test_alert_delivery_failure_keeps_retrying() -> None:
    _delivery_failure_case({"ok": False, "error": "x"})
    _delivery_failure_case(httpx.ConnectError("boom"))


def test_alert_concurrent_same_key_posts_once() -> None:
    posts: list = []
    with isolated_alerts(posts) as ctl:
        ctl.yield_in_post = True

        async def both():
            await asyncio.gather(alerts.on_rate_limit(_info()), alerts.on_rate_limit(_info()))

        asyncio.run(both())
        assert len(posts) == 1, posts


def test_alert_restart_keeps_mute() -> None:
    posts: list = []
    with isolated_alerts(posts) as ctl:
        _fire(ctl, 1000)
        alerts._reset_state()
        _fire(ctl, 2801)
        assert len(posts) == 1
    posts = []
    with isolated_alerts(posts) as ctl:
        os.makedirs(os.path.dirname(ctl.state_path))
        with open(ctl.state_path, "w") as f:
            json.dump({"quota-warn:seven_day:10000": 1500}, f)
        _fire(ctl, 2000)
        assert len(posts) == 1


def test_alert_bad_state_files_never_raise() -> None:
    cases = ["{", "[]", "null", '{"k": "later"}', '{"k": true}', '{"good": 9000, "bad": "x"}', "DIR"]
    for content in cases:
        posts: list = []
        with isolated_alerts(posts) as ctl:
            os.makedirs(os.path.dirname(ctl.state_path))
            if content == "DIR":
                os.makedirs(ctl.state_path)
            else:
                with open(ctl.state_path, "w") as f:
                    f.write(content)
            with captured_alert_logs() as records:
                ctl.now = 1000.0
                asyncio.run(alerts.alert("good", "t"))
                asyncio.run(alerts.alert("bad", "t"))
                asyncio.run(alerts.alert("other", "t"))
            loads = [r for r in records if "state load failed" in r.getMessage()]
            assert len(loads) <= 1, (content, len(loads))
            texts = len(posts)
            if content == '{"good": 9000, "bad": "x"}':
                assert texts == 2, (content, texts)  # good stays muted
            else:
                assert texts == 3, (content, texts)


@contextmanager
def captured_alert_logs():
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Capture()
    alerts.log.addHandler(handler)
    try:
        yield records
    finally:
        alerts.log.removeHandler(handler)


def test_alert_write_failure_keeps_old_file() -> None:
    posts: list = []
    with isolated_alerts(posts) as ctl:
        os.makedirs(os.path.dirname(ctl.state_path))
        seeded = json.dumps({"other-key": 99999})
        with open(ctl.state_path, "w") as f:
            f.write(seeded)
        original_replace = alerts.os.replace

        def boom(*a, **k):
            raise OSError("disk full")

        alerts.os.replace = boom
        try:
            _fire(ctl, 1000)
        finally:
            alerts.os.replace = original_replace
        assert len(posts) == 1
        with open(ctl.state_path) as f:
            assert f.read() == seeded
        assert os.listdir(os.path.dirname(ctl.state_path)) == ["alert-state.json"]


def test_alert_non_quota_alerts_unchanged() -> None:
    posts: list = []
    with isolated_alerts(posts) as ctl:
        ctl.now = 1000.0
        asyncio.run(alerts.on_openrouter_error(429, ""))
        ctl.now = 2801.0
        asyncio.run(alerts.on_openrouter_error(429, ""))
        assert len(posts) == 2


# ── caller label ─────────────────────────────────────────────────────────────


@contextmanager
def caller_env(backend_describe: str = "claude", fallback: bool = True):
    """Fresh caller store + quota, config and backend stubs; everything restored after."""
    saved = (
        main.callers, main.quota, claude.generate, claude.describe,
        openrouter.generate, openrouter.describe, openrouter.embed, openrouter.key_usage,
        config.BACKEND_CHEAP, config.BACKEND_DESCRIBE, config.FALLBACK_ON_QUOTA,
    )
    main.callers = {}
    main.quota = _Quota()
    config.BACKEND_CHEAP = "claude"
    config.BACKEND_DESCRIBE = backend_describe
    config.FALLBACK_ON_QUOTA = fallback

    async def ok_claude(**kw):
        return {"text": "ok", "meta": {"cost_usd": 0.01}}

    async def ok_or(**kw):
        return {"text": "ok", "meta": {}}

    async def ok_embed(texts, dims):
        return [[0.0]] * len(texts)

    async def usage_stub():
        return {"limit": 10, "usage": 1}

    claude.generate = ok_claude
    claude.describe = ok_claude
    openrouter.generate = ok_or
    openrouter.describe = ok_or
    openrouter.embed = ok_embed
    openrouter.key_usage = usage_stub
    try:
        yield
    finally:
        (
            main.callers, main.quota, claude.generate, claude.describe,
            openrouter.generate, openrouter.describe, openrouter.embed, openrouter.key_usage,
            config.BACKEND_CHEAP, config.BACKEND_DESCRIBE, config.FALLBACK_ON_QUOTA,
        ) = saved


def _post(path: str, body: dict, caller: str | None = None, raise_exc: bool = True):
    headers = {"X-API-Key": config.API_KEY}
    if caller is not None:
        headers["X-LLM-Caller"] = caller
    return TestClient(app, raise_server_exceptions=raise_exc).post(path, headers=headers, json=body)


GEN = {"user": "p", "tier": "cheap"}
DESC = {"prompt": "p", "mime": "image/png", "data_b64": "AAAA"}


def _summaries(records, name: str) -> list[str]:
    return [r.getMessage() for r in records if r.getMessage().startswith(f"{name} caller=")]


def test_caller_generate_labelled_and_counted() -> None:
    with caller_env(), captured_gateway_logs() as records:
        assert _post("/generate", GEN, "p-agent/bot-comment").status_code == 200
        (msg,) = _summaries(records, "generate")
        assert "caller=p-agent/bot-comment" in msg and "status=200" in msg
        c = main.callers["p-agent/bot-comment"]
        assert (c["calls"], c["errors"], c["cost_usd"]) == (1, 0, 0.01)
        _post("/generate", GEN)
        assert main.callers["unknown"]["calls"] == 1


def test_caller_invalid_labels_become_unknown() -> None:
    bad = ["P-Agent", "has space", "/lead", "a" * 65]
    with caller_env(), captured_gateway_logs() as records:
        for value in bad + [""]:
            assert _post("/generate", GEN, value).status_code == 200
        msgs = _summaries(records, "generate")
        assert len(msgs) == 5 and all("caller=unknown" in m for m in msgs)
        assert not any("caller= " in m for m in msgs)
        for r in records:
            text = r.getMessage() + repr(r.args)
            assert not any(v in text for v in bad)
        assert main.callers["unknown"]["calls"] == 5


def test_caller_generate_error_counted() -> None:
    with caller_env(fallback=False), captured_gateway_logs() as records:
        _post("/generate", GEN, "x")

        async def fail(**kw):
            raise claude.ClaudeError("boom")

        claude.generate = fail
        assert _post("/generate", GEN, "x").status_code == 502
        msgs = _summaries(records, "generate")
        assert len(msgs) == 2 and "status=502" in msgs[1]
        c = main.callers["x"]
        assert c["calls"] == 2 and c["errors"] == 1 and round(c["cost_usd"], 4) == 0.01


def test_caller_describe_outcomes() -> None:
    with caller_env(), captured_gateway_logs() as records:
        # 5: claude ok
        async def cl(**kw):
            return {"text": "ok", "meta": {"cost_usd": 0.002}}

        claude.describe = cl
        assert _post("/describe", DESC, "d").status_code == 200
        (msg,) = _summaries(records, "describe")
        assert "backend=claude mime=image/png status=200 reason=none" in msg
        assert main.callers["d"]["cost_usd"] == 0.002

    # 6: openrouter direct
    with caller_env("openrouter"), captured_gateway_logs() as records:
        assert _post("/describe", DESC, "d").status_code == 200
        (msg,) = _summaries(records, "describe")
        assert "backend=openrouter" in msg and "status=200" in msg
        assert main.callers["d"]["cost_usd"] == 0

    # 7: claude error, fallback ok, plus the diagnostic
    with caller_env(), captured_gateway_logs() as records:
        async def fail(**kw):
            raise claude.ClaudeError("boom")

        claude.describe = fail
        assert _post("/describe", DESC, "d").status_code == 200
        (msg,) = _summaries(records, "describe")
        assert "backend=openrouter" in msg and "status=200 reason=claude_error" in msg
        assert any("claude describe failed" in r.getMessage() for r in records)
        c = main.callers["d"]
        assert (c["calls"], c["errors"], c["cost_usd"]) == (1, 0, 0)

    # 8: seat pre-blocked
    with caller_env(), captured_gateway_logs() as records:
        main.quota.blocked_until = 4e12
        assert _post("/describe", DESC, "d").status_code == 200
        (msg,) = _summaries(records, "describe")
        assert "reason=seat_unavailable" in msg and "status=200" in msg
        assert main.callers["d"]["calls"] == 1

    # 9: openrouter error
    with caller_env("openrouter"), captured_gateway_logs() as records:
        async def fail(**kw):
            raise openrouter.OpenRouterError("boom")

        openrouter.describe = fail
        assert _post("/describe", DESC, "d").status_code == 502
        (msg,) = _summaries(records, "describe")
        assert "status=502 reason=openrouter_error" in msg
        assert (main.callers["d"]["calls"], main.callers["d"]["errors"]) == (1, 1)


def test_caller_describe_mime_cannot_forge_log() -> None:
    with caller_env(), captured_gateway_logs() as records:
        body = {**DESC, "mime": "image/png\nforged caller=x"}
        _post("/describe", body, "d")
        (msg,) = _summaries(records, "describe")
        assert "mime=invalid" in msg and "\n" not in msg and "\r" not in msg
        assert "forged" not in msg
        assert main.callers["d"]["calls"] == 1


def test_caller_embed() -> None:
    with caller_env(), captured_gateway_logs() as records:
        assert _post("/embed", {"texts": ["a", "b"]}, "e").status_code == 200
        assert _post("/embed", {"texts": []}, "e").status_code == 200
        assert _post("/embed", {"texts": ["a", ""]}, "e").status_code == 422

        async def fail(texts, dims):
            raise openrouter.OpenRouterError("boom")

        openrouter.embed = fail
        assert _post("/embed", {"texts": ["a"]}, "e").status_code == 502
        msgs = _summaries(records, "embed")
        assert len(msgs) == 3
        assert "texts=2 status=200" in msgs[0]
        assert "texts=0 status=200" in msgs[1]
        assert "status=502 reason=openrouter_error" in msgs[2]
        assert (main.callers["e"]["calls"], main.callers["e"]["errors"]) == (3, 1)


def test_caller_unexpected_exception() -> None:
    with caller_env(), captured_gateway_logs() as records:
        async def boom(**kw):
            raise RuntimeError("boom")

        claude.generate = boom
        assert _post("/generate", GEN, "u", raise_exc=False).status_code == 500
        (msg,) = _summaries(records, "generate")
        assert "status=500 reason=unexpected_error" in msg
        assert (main.callers["u"]["calls"], main.callers["u"]["errors"]) == (1, 1)


def test_caller_rejected_requests_not_counted() -> None:
    with caller_env(), captured_gateway_logs() as records:
        r = TestClient(app).post("/generate", headers={"X-API-Key": "wrong"}, json=GEN)
        assert r.status_code == 401
        assert _post("/generate", {"tier": "cheap"}, "x").status_code == 422
        assert _summaries(records, "generate") == []
        assert main.callers == {}


def test_caller_cost_rules_and_rounding() -> None:
    with caller_env():
        for cost in ("x", None):
            async def cl(cost=cost, **kw):
                meta = {} if cost is None else {"cost_usd": cost}
                return {"text": "ok", "meta": meta}

            claude.generate = cl
            _post("/generate", GEN, "c")
        assert main.callers["c"]["cost_usd"] == 0

        async def tiny(**kw):
            return {"text": "ok", "meta": {"cost_usd": 0.00001}}

        claude.generate = tiny
        for _ in range(3):
            _post("/generate", GEN, "tiny")

        async def big(**kw):
            return {"text": "ok", "meta": {"cost_usd": 0.12345}}

        claude.generate = big
        _post("/generate", GEN, "big")
        callers = TestClient(app).get("/usage", headers={"X-API-Key": config.API_KEY}).json()["callers"]
        assert callers["tiny"]["notional_cost_usd"] == 0.0
        assert callers["big"]["notional_cost_usd"] == 0.1235
        assert callers["c"]["notional_cost_usd"] == 0


def test_caller_bucket_limit() -> None:
    with caller_env(), captured_gateway_logs() as records:
        for i in range(64):
            _post("/generate", GEN, f"c{i}")
        _post("/generate", GEN, "late")
        _post("/generate", GEN, "c0")
        _post("/generate", GEN)
        _post("/generate", GEN, "other")
        assert "caller=late" in _summaries(records, "generate")[64]
        assert "late" not in main.callers
        assert main.callers["other"]["calls"] == 2
        assert main.callers["c0"]["calls"] == 2
        assert main.callers["unknown"]["calls"] == 1
        assert len(main.callers) == 66


def test_usage_reports_callers_sorted() -> None:
    with caller_env():
        _post("/generate", GEN, "zeta")
        _post("/generate", GEN, "alpha")
        body = TestClient(app).get("/usage", headers={"X-API-Key": config.API_KEY}).json()
        assert list(body["callers"]) == ["alpha", "zeta"]
        assert set(body["callers"]["alpha"]) == {"calls", "errors", "notional_cost_usd"}
        assert set(body["seat"]) == {
            "available", "last_rate_limit", "calls", "notional_cost_usd", "openrouter_fallbacks",
        }
        assert set(body["openrouter"]) >= {"limit", "usage", "limit_remaining"}


def _openrouter_returning(statuses: list[int]):
    """Swap openrouter's HTTP client for one that answers with these statuses in order."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        status = statuses[len(calls)]
        calls.append(status)
        return httpx.Response(status, json={"data": [{"index": 0, "embedding": [0.1]}]})

    transport = httpx.MockTransport(handler)
    openrouter.httpx.AsyncClient = lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw)
    return calls


def test_openrouter_429_is_retried_before_alerting() -> None:
    sent: list[int] = []

    async def fake_alert(status: int, body: str, path: str = "", cap: bool = False) -> None:
        sent.append(status)

    original = openrouter._post, openrouter._RETRY_429_DELAYS_S, alerts.on_openrouter_error
    openrouter._post = _REAL_OPENROUTER_POST
    openrouter._RETRY_429_DELAYS_S = (0, 0, 0)
    alerts.on_openrouter_error = fake_alert
    try:
        # A 429 that clears on retry succeeds and stays off Slack.
        calls = _openrouter_returning([429, 429, 200])
        assert asyncio.run(openrouter.embed(["a"], 1)) == [[0.1]]
        assert calls == [429, 429, 200] and sent == []

        # One that survives every retry fails and alerts exactly once.
        calls = _openrouter_returning([429] * 4)
        try:
            asyncio.run(openrouter.embed(["a"], 1))
            raise AssertionError("expected OpenRouterError")
        except openrouter.OpenRouterError:
            pass
        assert len(calls) == 4 and sent == [429]

        # Other errors are not retried.
        calls = _openrouter_returning([402])
        try:
            asyncio.run(openrouter.embed(["a"], 1))
        except openrouter.OpenRouterError:
            pass
        assert calls == [402]

        # Generate does not retry: its 180 s timeout leaves no room under the
        # caller's 200 s.
        calls = _openrouter_returning([429, 200])
        try:
            asyncio.run(openrouter.generate(system="", user="u", model="m"))
            raise AssertionError("expected OpenRouterError")
        except openrouter.OpenRouterError:
            pass
        assert calls == [429]
    finally:
        openrouter.httpx.AsyncClient = _REAL_ASYNC_CLIENT
        openrouter._post, openrouter._RETRY_429_DELAYS_S, alerts.on_openrouter_error = original




GEMINI_DUMMY_KEY = "GEMINI-DUMMY-7f3a"


@contextmanager
def gemini_fallback_env():
    saved = (
        config.GEMINI_API_KEY, config.EMBED_MAX_BATCH,
        openrouter.embed, gemini.embed, alerts.on_gemini_error,
    )
    config.GEMINI_API_KEY = GEMINI_DUMMY_KEY
    config.EMBED_MAX_BATCH = 96
    try:
        yield
    finally:
        (
            config.GEMINI_API_KEY, config.EMBED_MAX_BATCH,
            openrouter.embed, gemini.embed, alerts.on_gemini_error,
        ) = saved


def test_embed_falls_back_for_cap_statuses_and_full_403_body() -> None:
    cases = [
        (402, "spent"),
        (403, "Key limit exceeded"),
        (403, "x" * 401 + "Key limit exceeded"),
    ]
    for status, body in cases:
        with gemini_fallback_env(), captured_gateway_logs() as records:
            original_post = openrouter._post
            original_client = openrouter.httpx.AsyncClient

            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(status, text=body)

            transport = httpx.MockTransport(handler)
            openrouter._post = _REAL_OPENROUTER_POST
            openrouter.httpx.AsyncClient = (
                lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw)
            )

            async def fallback(texts, dims):
                return [[float(i)] for i, _ in enumerate(texts)]

            gemini.embed = fallback
            try:
                response = _post("/embed", {"texts": ["a", "b"], "dims": 1}, "fallback")
            finally:
                openrouter._post = original_post
                openrouter.httpx.AsyncClient = original_client
            assert response.status_code == 200
            assert response.json()["embeddings"] == [[0.0], [1.0]]
            assert _summaries(records, "embed")[0].endswith("backend=gemini")


def test_embed_does_not_fallback_for_auth_errors_or_openrouter_success() -> None:
    for err in (
        openrouter.OpenRouterError("OpenRouter 401", status=401),
        openrouter.OpenRouterError("OpenRouter 403", status=403),
    ):
        with gemini_fallback_env():
            called = False

            async def fail(texts, dims, error=err):
                raise error

            async def forbidden(texts, dims):
                nonlocal called
                called = True
                return []

            openrouter.embed = fail
            gemini.embed = forbidden
            assert _post("/embed", {"texts": ["a"]}, "auth").status_code == 502
            assert not called

    with gemini_fallback_env(), captured_gateway_logs() as records:
        called = False

        async def primary(texts, dims):
            return [[1.0]]

        async def forbidden(texts, dims):
            nonlocal called
            called = True
            return []

        openrouter.embed = primary
        gemini.embed = forbidden
        assert _post("/embed", {"texts": ["a"], "dims": 1}, "primary").status_code == 200
        assert not called
        assert _summaries(records, "embed")[0].endswith("backend=openrouter")


def test_embed_cap_without_key_does_not_fallback_and_alert_explains_recovery() -> None:
    with gemini_fallback_env():
        config.GEMINI_API_KEY = ""
        called = False

        async def fail(texts, dims):
            raise openrouter.OpenRouterError("OpenRouter 402: spent", status=402, cap=True)

        async def forbidden(texts, dims):
            nonlocal called
            called = True
            return []

        openrouter.embed = fail
        gemini.embed = forbidden
        assert _post("/embed", {"texts": ["a"]}, "no-key").status_code == 502
        assert not called

    posts: list = []
    with isolated_alerts(posts):
        config.GEMINI_API_KEY = ""
        asyncio.run(alerts.on_openrouter_error(402, "spent", cap=True))
        assert "set GEMINI_API_KEY or raise the cap" in posts[0]["text"]


def test_gemini_request_shape_chunking_and_order() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = json.loads(request.content)
        start = sum(len(json.loads(c.content)["requests"]) for c in calls[:-1])
        return httpx.Response(200, json={"embeddings": [
            {"values": [float(start + i)]} for i in range(len(body["requests"]))
        ]})

    original = gemini.httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    gemini.httpx.AsyncClient = lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw)
    saved = config.GEMINI_API_KEY, config.EMBED_MAX_BATCH
    config.GEMINI_API_KEY = GEMINI_DUMMY_KEY
    config.EMBED_MAX_BATCH = 500
    try:
        got = asyncio.run(gemini.embed([f"t{i}" for i in range(200)], 1))
        assert got == [[float(i)] for i in range(200)]
        assert [len(json.loads(c.content)["requests"]) for c in calls] == [96, 96, 8]
        for call in calls:
            body = json.loads(call.content)
            assert call.headers["x-goog-api-key"] == GEMINI_DUMMY_KEY
            assert GEMINI_DUMMY_KEY not in call.url.query.decode()
            assert all("taskType" not in r and r["outputDimensionality"] == 1 for r in body["requests"])
    finally:
        gemini.httpx.AsyncClient = original
        config.GEMINI_API_KEY, config.EMBED_MAX_BATCH = saved


def test_gemini_failures_include_both_errors_and_alert_once() -> None:
    failures = [
        httpx.Response(500, text="bad"),
        httpx.ConnectTimeout("slow"),
        httpx.Response(200, text="{"),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"embeddings": []}),
        httpx.Response(200, json={"embeddings": [{"values": []}]}),
    ]
    for failure in failures:
        alerted: list[str] = []
        with gemini_fallback_env():
            original_client = gemini.httpx.AsyncClient

            async def primary(texts, dims):
                raise openrouter.OpenRouterError("OpenRouter 402: spent", status=402, cap=True)

            def handler(request: httpx.Request, value=failure) -> httpx.Response:
                if isinstance(value, Exception):
                    raise value
                return value

            transport = httpx.MockTransport(handler)
            gemini.httpx.AsyncClient = (
                lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw)
            )

            async def capture(msg):
                alerted.append(msg)

            openrouter.embed = primary
            gemini.embed = __import__("app.gemini", fromlist=["embed"]).embed
            alerts.on_gemini_error = capture
            try:
                response = _post("/embed", {"texts": ["a"], "dims": 1}, "failure")
            finally:
                gemini.httpx.AsyncClient = original_client
            assert response.status_code == 502
            assert "OpenRouter 402" in response.json()["detail"]
            assert "fallback: Gemini" in response.json()["detail"]
            assert len(alerted) == 1


def test_gemini_secret_boundary() -> None:
    requests: list[httpx.Request] = []
    with gemini_fallback_env(), captured_gateway_logs() as records:
        original_client = gemini.httpx.AsyncClient

        async def primary(texts, dims):
            raise openrouter.OpenRouterError("OpenRouter 402: spent", status=402, cap=True)

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(500, text=f"echoed {GEMINI_DUMMY_KEY}")

        transport = httpx.MockTransport(handler)
        gemini.httpx.AsyncClient = (
            lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw)
        )
        alerted: list[str] = []

        async def capture(msg):
            alerted.append(msg)

        openrouter.embed = primary
        alerts.on_gemini_error = capture
        try:
            response = _post("/embed", {"texts": ["a"], "dims": 1}, "secret")
        finally:
            gemini.httpx.AsyncClient = original_client
        assert response.status_code == 502
        assert requests[0].headers["x-goog-api-key"] == GEMINI_DUMMY_KEY
        assert GEMINI_DUMMY_KEY not in response.text
        assert GEMINI_DUMMY_KEY not in "".join(r.getMessage() for r in records)
        assert GEMINI_DUMMY_KEY not in "".join(alerted)
        health = TestClient(app).get("/health").text
        get_config = TestClient(app).get("/config", headers={"X-API-Key": config.API_KEY}).text
        assert GEMINI_DUMMY_KEY not in health + get_config
        rejected = TestClient(app).put(
            "/config", headers={"X-API-Key": config.API_KEY},
            json={"GEMINI_API_KEY": GEMINI_DUMMY_KEY},
        )
        assert rejected.status_code == 400


def test_cap_alert_classification_and_dedupe_key() -> None:
    assert alerts.is_cap_error(402, "anything")
    assert alerts.is_cap_error(403, "x" * 401 + "Key limit exceeded")
    assert not alerts.is_cap_error(401, "Key limit exceeded")
    assert not alerts.is_cap_error(403, "forbidden")
    posts: list = []
    with isolated_alerts(posts) as ctl:
        reset = 1793491200.0
        ctl.now = reset - config.ALERT_DEDUPE_S - 10
        config.GEMINI_API_KEY = GEMINI_DUMMY_KEY
        body = '{"message":"$76.25 of $75 used; resets 2026-11-01T00:00:00.000Z.","code":403}'
        asyncio.run(alerts.on_openrouter_error(402, body, cap=True))
        asyncio.run(alerts.on_openrouter_error(403, "Key limit exceeded", cap=True))
        assert len(posts) == 1
        assert posts[0]["text"].startswith("⚠️")
        assert "rejected the key" not in posts[0]["text"]
        assert posts[0]["text"].count("cap is spent") == 1
        assert "$75.00 used" in posts[0]["text"] and "$50" not in posts[0]["text"]
        assert "resets 2026-11-01T00:00:00.000Z" in posts[0]["text"]
        assert "code" not in posts[0]["text"]
        assert "Gemini API directly" in posts[0]["text"]
        assert _persisted(ctl)["openrouter:cap:fallback"] == reset
        ctl.now += config.ALERT_DEDUPE_S + 1
        asyncio.run(alerts.on_openrouter_error(403, "Key limit exceeded", cap=True))
        assert len(posts) == 1
        ctl.now = reset + 1
        asyncio.run(alerts.on_openrouter_error(403, "Key limit exceeded", cap=True))
        assert len(posts) == 2


def test_cap_alert_without_usable_reset_uses_normal_warning_dedupe() -> None:
    for body in (
        "Key limit exceeded",
        "$50.00 of $50 used; resets 1970-01-01T00:00:01Z.",
        "$50.00 of $50 used; resets 2026-13-01T00:00:00Z.",
        "$50.00 of $50 used; resets invalid.",
    ):
        posts: list = []
        with isolated_alerts(posts) as ctl:
            config.GEMINI_API_KEY = GEMINI_DUMMY_KEY
            asyncio.run(alerts.on_openrouter_error(403, body, cap=True))
            assert posts[0]["text"].startswith("⚠️")
            assert _persisted(ctl)["openrouter:cap:fallback"] == ctl.now + config.ALERT_DEDUPE_S
            ctl.now += config.ALERT_DEDUPE_S + 1
            asyncio.run(alerts.on_openrouter_error(403, body, cap=True))
            assert len(posts) == 2


def test_cap_alert_without_gemini_key_stays_critical() -> None:
    posts: list = []
    with isolated_alerts(posts) as ctl:
        config.GEMINI_API_KEY = ""
        asyncio.run(alerts.on_openrouter_error(
            402,
            "$50.00 of $50 used; resets 2026-11-01T00:00:00.000Z.",
            cap=True,
        ))
        assert posts[0]["text"].startswith("🚨")
        assert "set GEMINI_API_KEY or raise the cap" in posts[0]["text"]
        assert posts[0]["text"].count("cap is spent") == 1
        assert "rejected the key" not in posts[0]["text"]
        assert _persisted(ctl)["openrouter:402"] == ctl.now + config.ALERT_DEDUPE_S


def test_cap_warning_does_not_mute_critical_after_restart() -> None:
    posts: list = []
    with isolated_alerts(posts) as ctl:
        ctl.now = 1793480000.0
        config.GEMINI_API_KEY = GEMINI_DUMMY_KEY
        asyncio.run(alerts.on_openrouter_error(
            402,
            "$50.00 of $50 used; resets 2026-11-01T00:00:00.000Z.",
            cap=True,
        ))
        alerts._reset_state()
        config.GEMINI_API_KEY = ""
        asyncio.run(alerts.on_openrouter_error(402, "cap spent", cap=True))
        assert len(posts) == 2
        assert posts[1]["text"].startswith("🚨")
        asyncio.run(alerts.on_openrouter_error(402, "cap spent", cap=True))
        assert len(posts) == 2


def test_embed_late_chunk_cap_replays_whole_request_on_gemini() -> None:
    with gemini_fallback_env():
        seen: list[list[str]] = []
        openrouter_calls = 0
        original_post = openrouter._post

        async def post(path, payload, timeout, **kwargs):
            nonlocal openrouter_calls
            openrouter_calls += 1
            if openrouter_calls == 1:
                return {"data": [
                    {"index": i, "embedding": [-1.0]} for i in range(len(payload["input"]))
                ]}
            raise openrouter.OpenRouterError("late cap", status=402, cap=True)

        async def fallback(texts, dims):
            seen.append(list(texts))
            return [[float(i)] for i in range(len(texts))]

        openrouter._post = post
        openrouter.embed = __import__("app.openrouter", fromlist=["embed"]).embed
        gemini.embed = fallback
        texts = [f"t{i}" for i in range(100)]
        try:
            response = _post("/embed", {"texts": texts, "dims": 1}, "late")
        finally:
            openrouter._post = original_post
        assert openrouter_calls == 2
        assert response.json()["embeddings"] == [[float(i)] for i in range(100)]
        assert seen == [texts]


def test_gemini_alert_dedupes_inside_window() -> None:
    posts: list = []
    with isolated_alerts(posts) as ctl:
        asyncio.run(alerts.on_gemini_error("first"))
        ctl.now += config.ALERT_DEDUPE_S - 1
        asyncio.run(alerts.on_gemini_error("second"))
        ctl.now += 2
        asyncio.run(alerts.on_gemini_error("third"))
        assert len(posts) == 2

def test_retry_after_is_capped_and_ignores_garbage() -> None:
    def r(value: str) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": value})

    assert openrouter._retry_after(r("999"), 1) == openrouter._RETRY_AFTER_CAP_S
    assert openrouter._retry_after(r("3"), 1) == 3
    assert openrouter._retry_after(r("-5"), 1) == 0
    # NaN would crash asyncio.sleep into a 500 that skips the alert.
    assert openrouter._retry_after(r("nan"), 1) == 1
    assert openrouter._retry_after(r("soon"), 2) == 2
    assert openrouter._retry_after(httpx.Response(429), 4) == 4

if __name__ == "__main__":
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-should-be-stripped"
    import importlib
    importlib.reload(config)  # prove the pop happens on import, not by luck
    _isolate_alert_globals()

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
