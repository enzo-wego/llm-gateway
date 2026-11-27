"""HTTP and upstream wire contracts for the opt-in Gemini effort parameter."""

import json
from decimal import Decimal
from sqlite3 import connect

import httpx
import pytest
from fastapi.testclient import TestClient

from gemini_test_support import KEYS, Upstream, add, client, config, env, post

_MODELS = "/gemini/v1beta/models"


def test_effort_preserves_arbitrary_precision_numbers(client: TestClient, env: Upstream) -> None:
    add(1)
    body = b'{"contents":[{"parts":[{"functionResponse":{"response":{"amount":9007199254740993.25,"tiny":1e-450}}}]}]}'
    res = post(client, f"{_MODELS}/gemini-2.5-flash:generateContent?effort=low", body=body)
    assert res.status_code == 200
    forwarded = json.loads(env.bodies[-1], parse_float=Decimal)
    assert forwarded["contents"] == json.loads(body, parse_float=Decimal)["contents"]


@pytest.mark.parametrize(
    ("model", "field", "values"),
    [
        ("gemini-3-flash-preview", "thinkingLevel", ("LOW", "MEDIUM", "HIGH")),
        ("gemini-3.1-pro-preview", "thinkingLevel", ("LOW", "MEDIUM", "HIGH")),
        ("gemini-3.1-flash-lite", "thinkingLevel", ("LOW", "MEDIUM", "HIGH")),
        ("gemini-3-pro-preview", "thinkingLevel", ("LOW", None, "HIGH")),
        ("gemini-2.5-flash", "thinkingBudget", (256, 4096, 16384)),
        ("gemini-2.5-flash-lite", "thinkingBudget", (512, 4096, 16384)),
        ("gemini-2.5-pro", "thinkingBudget", (1024, 8192, 24576)),
    ],
)
def test_effort_maps_to_native_setting_on_url_model(
    client: TestClient, env: Upstream, model: str, field: str,
    values: tuple[str | int | None, ...],
) -> None:
    add(1)
    for effort, expected in zip(("low", "medium", "high"), values, strict=True):
        if expected is None:  # Rejected combinations are covered separately.
            continue
        response = post(client, f"{_MODELS}/{model}:generateContent?effort={effort}")

        assert response.status_code == 200
        url = httpx.URL(env.seen[-1][0])
        assert url.path == f"/v1beta/models/{model}:generateContent"
        assert not url.query
        assert json.loads(env.bodies[-1]) == {
            "contents": [], "generationConfig": {"thinkingConfig": {field: expected}},
        }


def test_effort_stream_preserves_native_fields_and_sse(client: TestClient, env: Upstream) -> None:
    add(1)
    event = b'data: {"candidates":[]}\n\n'
    env.script[KEYS[0]] = [(200, {"content-type": "text/event-stream"}, event)]
    body = {
        "contents": [{"parts": [{"text": "hello"}]}],
        "generation_config": {"temperature": 0.4, "thinking_config": {"include_thoughts": True}},
        "tools": [{"googleSearch": {}}],
    }

    response = post(
        client, f"{_MODELS}/gemini-3-flash-preview:streamGenerateContent?alt=sse&effort=medium",
        body=json.dumps(body).encode(),
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.content == event
    [(url, _)] = env.seen
    assert httpx.URL(url).params.multi_items() == [("alt", "sse")]
    [forwarded] = env.bodies
    # The only payload change is the new native thinking control.
    payload = json.loads(forwarded)
    assert payload["generation_config"]["thinking_config"].pop("thinkingLevel") == "MEDIUM"
    assert payload == body


@pytest.mark.parametrize("action", ["generateContent", "streamGenerateContent"])
def test_no_effort_forwards_legacy_body_bytes(
    client: TestClient, env: Upstream, action: str,
) -> None:
    add(1)
    body = b' {\n "generationConfig" : {"thinkingConfig":{"thinkingBudget":-1}} }\n'

    response = post(client, f"{_MODELS}/gemini-x:{action}", body=body)

    assert response.status_code == 200
    assert env.bodies == [body]


@pytest.mark.parametrize(
    ("spec", "query", "body", "status"),
    [
        ("gemini-3-flash-preview:generateContent", "", b"{}", 400),
        ("gemini-3-flash-preview:generateContent", "LOW", b"{}", 400),
        ("gemini-3-flash-preview:generateContent", "low&effort=low", b"{}", 400),
        ("gemini-3-flash-preview:countTokens", "low", b"{}", 400),
        ("gemini-3-flash-preview:unknownAction", "low", b"{}", 404),
        ("gemini-3-pro-preview:generateContent", "medium", b"{}", 400),
        ("gemini-3-flash-preview-unknown:generateContent", "low", b"{}", 400),
        ("gemini-3.1-flash-image-preview:generateContent", "low", b"{}", 400),
        ("gemini-2.5-flash:generateContent", "low", b'{"model":"models/other"}', 400),
        ("gemini-3-flash-preview:generateContent", "low", b"", 400),
        ("gemini-3-flash-preview:generateContent", "low", b"[]", 400),
        ("gemini-3-flash-preview:generateContent", "low", b'{"bad":NaN}', 400),
        ("gemini-3-flash-preview:generateContent", "low", b'{"generationConfig":null}', 400),
        ("gemini-3-flash-preview:generateContent", "low",
         b'{"generationConfig":{},"generation_config":{}}', 400),
        ("gemini-3-flash-preview:generateContent", "low",
         b'{"generationConfig":{},"generationConfig":{}}', 400),
        ("gemini-3-flash-preview:generateContent", "low",
         b'{"generationConfig":{"thinkingConfig":{},"thinking_config":{}}}', 400),
        ("gemini-3-flash-preview:generateContent", "low",
         b'{"generationConfig":{"thinkingConfig":{"includeThoughts":"true"}}}', 400),
        ("gemini-3-flash-preview:generateContent", "low",
         b'{"generationConfig":{"thinkingConfig":{"includeThoughts":true,"include_thoughts":true}}}', 400),
        ("gemini-3-flash-preview:generateContent", "low",
         b'{"generationConfig":{"thinkingConfig":{"thinking_budget":null}}}', 400),
        ("gemini-3-flash-preview:streamGenerateContent", "low",
         b'{"generation_config":{"thinking_config":{"thinkingLevel":"LOW"}}}', 400),
        ("gemini-2.5-flash:generateContent", "low",
         b'{"generationConfig":{"thinkingConfig":{"thinkingBudget":256}}}', 400),
    ],
)
def test_invalid_effort_does_not_touch_upstream_or_write(
    client: TestClient, env: Upstream, spec: str, query: str, body: bytes, status: int,
) -> None:
    add(1)
    with connect(config.DB_PATH) as db:
        # SQLite's connection change counter would miss writes from worker connections.
        data_version = db.execute("PRAGMA data_version").fetchone()

        response = post(client, f"{_MODELS}/{spec}?effort={query}", body=body)

        assert response.status_code == status
        assert env.seen == []
        assert db.execute("PRAGMA data_version").fetchone() == data_version
