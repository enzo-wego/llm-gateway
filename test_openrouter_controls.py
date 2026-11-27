import os
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("LLM_GATEWAY_API_KEY", "test")
from app import config, main, openrouter


@pytest.mark.parametrize("path", ["generate", "describe"])
@pytest.mark.parametrize("options", [{}, {"effort": "low"}, {"model": "vendor/model"},
                                     {"model": "vendor/model", "effort": "high"}])
def test_explicit_openrouter_wire(monkeypatch, path, options):
    monkeypatch.setattr(config, "BACKEND_CHEAP", "claude")
    monkeypatch.setattr(config, "BACKEND_DESCRIBE", "claude")
    default = config.OR_MODEL_CHEAP if path == "generate" else config.OR_MODEL_DESCRIBE
    wire = AsyncMock(return_value={"choices": [{"message": {"content": '{"ok":true}'}}]})
    monkeypatch.setattr(openrouter, "_post", wire)
    body = {"user": "test"} if path == "generate" else {
        "prompt": "test", "mime": "image/png", "data_b64": "YQ=="}
    body.update(backend="openrouter", schema={"type": "object"}, **options)
    r = TestClient(main.app).post("/"+path, json=body, headers={"X-API-Key": config.API_KEY})
    assert r.status_code == 200 and r.json()["backend"] == "openrouter"
    assert r.json()["output"] == {"ok": True}
    payload = wire.await_args.args[1]
    assert payload["model"] == options.get("model", default) == r.json()["meta"]["model"]
    if "effort" in options:
        assert payload["reasoning"] == {"effort": options["effort"]}
        assert payload["provider"] == {"require_parameters": True}
        assert r.json()["meta"]["effort"] == options["effort"]
    else:
        assert "reasoning" not in payload and "provider" not in payload


@pytest.mark.parametrize("options", [
    {"backend": "typo"}, {"backend": "openrouter", "effort": "max"},
    {"backend": "openrouter", "model": "sonnet"}, {"model": "vendor/model"},
    {"effort": "none"},
])
def test_invalid_provider_combinations_rejected_before_dispatch(options):
    r = TestClient(main.app).post("/generate", json={"user": "test", **options},
                                  headers={"X-API-Key": config.API_KEY})
    assert r.status_code == 422
