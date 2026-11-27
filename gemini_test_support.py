"""Opt-in fixtures for the Gemini integration tests, with a fake provider."""

import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

with patch.dict(os.environ, LLM_GATEWAY_API_KEY=os.environ.get("LLM_GATEWAY_API_KEY", "test"),
                OPENROUTER_API_KEY=os.environ.get("OPENROUTER_API_KEY", "sk-or-test")):
    from app import admin, config, gemini_proxy, gemini_store as store
    from app.main import app

SECRET = "gw-secret"
H = {"x-goog-api-key": SECRET}
GEN = "/gemini/v1beta/models/gemini-x:generateContent"
KEYS = tuple(f"AIzaKEY{i}-secretplaintext" for i in range(8))
STREAM = "/gemini/v1beta/models/gemini-x:streamGenerateContent?alt=sse"


class Upstream:
    """Scripted fake of generativelanguage.googleapis.com. Records (url, key) per call."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str]] = []
        self.bodies: list[bytes] = []
        self.script: dict[str, list[tuple[int, dict[str, str], bytes] | httpx.HTTPError]] = {}  # key -> responses (cycled last); default 200

    def __call__(self, req: httpx.Request) -> httpx.Response:
        key = req.headers.get("x-goog-api-key", "")
        self.seen.append((str(req.url), key))
        self.bodies.append(req.content)
        steps = self.script.get(key)
        if steps:
            step = steps.pop(0) if len(steps) > 1 else steps[0]
            if isinstance(step, httpx.HTTPError):
                raise step
            status, hdrs, body = step
            return httpx.Response(status, headers=hdrs, content=body)
        if req.url.path.endswith("/models") and req.method == "GET":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(200, json={"ok": True, "usageMetadata":
                                         {"promptTokenCount": 3, "candidatesTokenCount": 4}})

    @property
    def keys_used(self) -> list[str]:
        return [k for _, k in self.seen]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Upstream:
    monkeypatch.setattr(config, "SECRET_KEY", "s3cret-for-fernet")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "admin-pw")
    monkeypatch.setattr(config, "API_KEY", SECRET)
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "gw.db"))
    monkeypatch.setattr(config, "GEMINI_UPSTREAM", "http://upstream.test")
    up = Upstream()
    monkeypatch.setattr(gemini_proxy, "transport", httpx.MockTransport(up))
    monkeypatch.setattr(gemini_proxy, "_rr", 0)
    monkeypatch.setattr(admin, "_fails", {})
    return up


def add(n: int) -> list[int]:
    ids = []
    for i in range(n):
        key_id = store.add_key(f"k{i}", KEYS[i])
        assert key_id is not None
        ids.append(key_id)
    return ids


def post(client: TestClient, path: str = GEN, *, headers: Mapping[str, str] = H,
         body: bytes = b'{"contents":[]}') -> httpx.Response:
    return client.post(path, headers=headers, content=body)


@pytest.fixture
def client(env: Upstream) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
