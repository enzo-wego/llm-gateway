import os
from collections.abc import Iterator
from typing import Literal
from unittest.mock import AsyncMock

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from fastapi.testclient import TestClient

os.environ.setdefault("LLM_GATEWAY_API_KEY", "test-key")

from app import claude, config, main, openrouter

Route = Literal["generate", "describe"]
Override = dict[str, str | int | None]


@pytest.fixture(params=["generate", "describe"])
def route(request: pytest.FixtureRequest) -> Route:
    return request.param


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(config, "API_KEY", "test-key")
    monkeypatch.setattr(config, "BACKEND_SUMMARY", "claude")
    monkeypatch.setattr(config, "BACKEND_DESCRIBE", "claude")
    monkeypatch.setattr(config, "MODEL_SUMMARY", "sonnet")
    monkeypatch.setattr(config, "MODEL_CHEAP", "haiku")
    monkeypatch.setattr(config, "EFFORT_SUMMARY", "medium")
    monkeypatch.setattr(config, "EFFORT_CHEAP", "low")
    monkeypatch.setattr(config, "FALLBACK_ON_QUOTA", True)
    monkeypatch.setattr(main, "quota", main._Quota())
    monkeypatch.setattr(main, "callers", {})
    monkeypatch.setattr(
        claude, "_guarded", AsyncMock(side_effect=AssertionError("unexpected Claude call"))
    )
    for name in ("generate", "describe"):
        monkeypatch.setattr(
            openrouter, name, AsyncMock(side_effect=AssertionError("unexpected OpenRouter call"))
        )
    with TestClient(main.app, headers={"X-API-Key": "test-key"}) as client:
        yield client


def _body(route: Route) -> dict[str, str]:
    if route == "generate":
        return {"user": "Read this", "tier": "summary"}
    return {"prompt": "Read this", "mime": "image/png", "data_b64": "QQ=="}


@pytest.mark.parametrize(
    ("overrides", "model", "effort", "structured"),
    [
        ({}, None, None, False),
        ({"model": None, "effort": None}, None, None, False),
        ({"model": "claude-sonnet-5-5"}, "claude-sonnet-5-5", None, False),
        ({"effort": "max"}, None, "max", False),
        ({"model": "opus", "effort": "xhigh"}, "opus", "xhigh", True),
    ],
)
def test_claude_overrides_reach_sdk_with_independent_defaults(
    gateway: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    route: Route,
    overrides: Override,
    model: str | None,
    effort: str | None,
    structured: bool,
) -> None:
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
    sdk_call = AsyncMock(
        return_value={
            **({"output": {"answer": "yes"}} if structured else {"text": "yes"}),
            "meta": {"cost_usd": 0.01},
        }
    )
    monkeypatch.setattr(claude, "_guarded", sdk_call)
    body = {**_body(route), **overrides}
    if structured:
        body = {**body, "schema": schema}

    response = gateway.post(f"/{route}", json=body)

    assert response.status_code == 200
    payload = response.json()
    assert payload["backend"] == "claude"
    assert payload["output" if structured else "text"] == (
        {"answer": "yes"} if structured else "yes"
    )
    expected_model = model or ("sonnet" if route == "generate" else "haiku")
    expected_effort = effort or ("medium" if route == "generate" else "low")
    sdk_call.assert_awaited_once()
    options: ClaudeAgentOptions = sdk_call.await_args.args[1]
    assert (options.model, options.effort) == (expected_model, expected_effort)
    assert options.thinking == (
        None if model is not None or effort is not None
        else {"type": "adaptive" if route == "generate" else "disabled"}
    )
    assert options.output_format == (
        {"type": "json_schema", "schema": schema} if structured else None
    )
    assert payload["meta"] == (
        {"cost_usd": 0.01, "model": expected_model, "effort": expected_effort}
        if model is not None or effort is not None
        else {"cost_usd": 0.01}
    )
    assert (config.MODEL_SUMMARY, config.EFFORT_SUMMARY) == ("sonnet", "medium")
    assert (config.MODEL_CHEAP, config.EFFORT_CHEAP) == ("haiku", "low")


@pytest.mark.parametrize(
    ("failure", "override_status", "detail"),
    [
        ("primary", 422, "OpenRouter"),
        ("blocked", 503, "seat"),
        ("quota", 503, "quota"),
        ("provider", 502, "rejected model"),
    ],
)
@pytest.mark.parametrize("explicit", [False, True])
def test_openrouter_routing_cannot_discard_an_override(
    gateway: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    route: Route,
    failure: str,
    override_status: int,
    detail: str,
    explicit: bool,
) -> None:
    if failure == "primary":
        monkeypatch.setattr(config, "BACKEND_SUMMARY", "openrouter")
        monkeypatch.setattr(config, "BACKEND_DESCRIBE", "openrouter")
    if failure == "blocked":
        main.quota.note_rejection(4_000_000_000)
        monkeypatch.setattr(config, "FALLBACK_ON_QUOTA", False)
    if failure == "quota":
        monkeypatch.setattr(claude, "_guarded", AsyncMock(side_effect=claude.QuotaExhausted()))
    if failure == "provider":
        monkeypatch.setattr(
            claude, "_guarded", AsyncMock(side_effect=claude.ClaudeError("rejected model"))
        )
    or_call = AsyncMock(return_value={"text": "OpenRouter answer", "meta": {}})
    if not explicit:
        monkeypatch.setattr(openrouter, route, or_call)
    body = _body(route)
    if explicit:
        body["effort"] = "high"

    response = gateway.post(f"/{route}", json=body)

    if explicit:
        assert response.status_code == override_status
        assert detail in response.json()["detail"]
        assert main.quota.fallbacks == 0
    else:
        assert response.status_code == 200
        assert response.json()["backend"] == "openrouter"
        or_call.assert_awaited_once()
        assert main.quota.fallbacks == (0 if failure == "primary" else 1)


@pytest.mark.parametrize(
    "overrides",
    [
        {"model": "--dangerous"},
        {"model": "anthropic/claude-sonnet"},
        {"model": "claude-run\n"},
        {"model": "claude-" + "a" * 128},
        {"model": 42},
        {"model": ""},
        {"effort": "ultracode"},
        {"effort": 3},
    ],
)
def test_invalid_claude_overrides_are_rejected_before_provider_access(
    gateway: TestClient, route: Route, overrides: Override
) -> None:
    response = gateway.post(f"/{route}", json={**_body(route), **overrides})

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", next(iter(overrides))]
