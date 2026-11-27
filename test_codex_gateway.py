"""Exercise the internal route with a local HTTP worker on a Unix socket."""

import json
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import UnixStreamServer
from tempfile import TemporaryDirectory
from typing import Final

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import JsonValue

os.environ.setdefault("LLM_GATEWAY_API_KEY", "test")

from app import codex_gateway, config  # noqa: E402
from app.main import app  # noqa: E402

_P_KEY: Final = "p-agent-test-key"
_MEM_KEY: Final = "agent-mem-test-key"
_SHARED_KEY: Final = "legacy-test-key"


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(config, "API_KEY", _SHARED_KEY)
    monkeypatch.setenv("CODEX_P_AGENT_KEY", _P_KEY)
    monkeypatch.setenv("CODEX_AGENT_MEM_KEY", _MEM_KEY)
    monkeypatch.delenv("CODEX_WORKER_SOCKET", raising=False)
    with TestClient(app) as client:
        yield client


@contextmanager
def worker_socket(
    monkeypatch: pytest.MonkeyPatch,
    handler: type[BaseHTTPRequestHandler],
) -> Iterator[None]:
    # Keep below macOS's socket path limit regardless of pytest's tmp path.
    with TemporaryDirectory(dir="/tmp") as root:
        socket = str(Path(root) / "worker.sock")
        with UnixStreamServer(socket, handler) as server:
            server.timeout = 2
            monkeypatch.setenv("CODEX_WORKER_SOCKET", socket)
            with ThreadPoolExecutor(max_workers=1) as pool:
                served = pool.submit(server.handle_request)
                yield
                served.result(timeout=3)


@pytest.mark.parametrize(
    ("p_key", "mem_key", "supplied", "status"),
    [
        (_P_KEY, _MEM_KEY, None, 401),
        (_P_KEY, _MEM_KEY, "unknown", 401),
        (_P_KEY, _MEM_KEY, "ÿ", 401),
        (_P_KEY, _MEM_KEY, _SHARED_KEY, 401),
        (_P_KEY, _P_KEY, _P_KEY, 401),
        (_SHARED_KEY, _MEM_KEY, _SHARED_KEY, 401),
        (_SHARED_KEY, _MEM_KEY, _MEM_KEY, 503),
        ("", _MEM_KEY, _P_KEY, 401),
        ("", _MEM_KEY, _MEM_KEY, 503),
        (_P_KEY, "", _MEM_KEY, 401),
        (_P_KEY, "", _P_KEY, 503),
        ("", "", _P_KEY, 401),
    ],
)
def test_only_distinct_enabled_service_keys_are_authenticated(
    gateway: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    p_key: str,
    mem_key: str,
    supplied: str | None,
    status: int,
) -> None:
    monkeypatch.setenv("CODEX_P_AGENT_KEY", p_key)
    monkeypatch.setenv("CODEX_AGENT_MEM_KEY", mem_key)
    headers = httpx.Headers({"X-LLM-Caller": "p-agent"})
    if supplied is not None:
        headers.update([(b"X-API-Key", supplied.encode("latin-1"))])

    response = gateway.post("/codex", json={"user": "prompt"}, headers=headers)

    assert response.status_code == status


def test_duplicate_api_key_headers_are_rejected(gateway: TestClient) -> None:
    response = gateway.post(
        "/codex", json={"user": "prompt"}, headers=[("X-API-Key", _P_KEY), ("X-API-Key", _P_KEY)]
    )
    assert response.status_code == 401


def test_codex_settings_are_not_in_live_config(gateway: TestClient) -> None:
    response = gateway.get("/config", headers={"X-API-Key": _SHARED_KEY})
    assert response.status_code == 200
    assert not any("CODEX" in name for name in response.json())
    assert _P_KEY not in response.text and _MEM_KEY not in response.text


def test_live_config_cannot_change_codex_socket(gateway: TestClient) -> None:
    response = gateway.put(
        "/config",
        json={"CODEX_WORKER_SOCKET": "/replacement.sock"},
        headers={"X-API-Key": _SHARED_KEY},
    )
    assert response.status_code == 400


def test_codex_does_not_fall_back_when_worker_socket_is_missing(
    gateway: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    socket = str(tmp_path / "missing.sock")
    monkeypatch.setenv("CODEX_WORKER_SOCKET", socket)
    response = gateway.post("/codex", json={"user": "prompt"}, headers={"X-API-Key": _P_KEY})
    assert response.status_code == 502
    assert socket not in response.text


@pytest.mark.parametrize(
    ("request_body", "wire_body", "worker_result", "key"),
    [
        (
            {"system": "private system", "user": "private prompt", "tier": "summary", "schema": {}},
            {"system": "private system", "user": "private prompt", "tier": "summary", "schema": {}},
            {"output": {"answer": "structured"}, "meta": {"usage": {"input": 2}}},
            _P_KEY,
        ),
        (
            {"user": "plain text", "socket": "/attacker.sock", "url": "http://attacker.invalid"},
            {"system": "", "user": "plain text", "tier": "cheap", "schema": None},
            {"text": "worker answer", "meta": {"model": "test-model"}},
            _MEM_KEY,
        ),
    ],
)
def test_codex_preserves_generate_contract_without_forwarding_caller_headers(
    gateway: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    request_body: dict[str, JsonValue],
    wire_body: dict[str, JsonValue],
    worker_result: dict[str, JsonValue],
    key: str,
) -> None:
    received: list[tuple[str, dict[str, str], JsonValue]] = []
    result = json.dumps(worker_result).encode()

    class Worker(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.path, dict(self.headers), body))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(result)

        def address_string(self) -> str:
            return "unix"

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    with worker_socket(monkeypatch, Worker):
        response = gateway.post(
            "/codex",
            json=request_body,
            headers={
                "X-API-Key": key,
                "Authorization": "Bearer caller-secret",
                "Cookie": "caller=secret",
                "X-LLM-Caller": "spoofed-label",
                "X-Worker-Socket": "/attacker.sock",
                "User-Agent": "caller-agent",
            },
        )

    assert response.status_code == 200
    assert response.json() == {"backend": "codex", **worker_result}
    [(path, worker_headers, body)] = received
    assert path == "/generate"
    assert body == wire_body
    assert set(header.lower() for header in worker_headers).isdisjoint(
        {"x-api-key", "authorization", "cookie", "x-llm-caller", "x-worker-socket"}
    )
    assert worker_headers["User-Agent"] != "caller-agent"


@pytest.mark.parametrize(
    ("body", "status"),
    [(b'{"user":"private","tier":"not-a-tier"}', 422), (b"x" * (1024 * 1024 + 1), 413)],
)
def test_bad_requests_are_bounded_and_do_not_reach_worker(
    gateway: TestClient, monkeypatch: pytest.MonkeyPatch, body: bytes, status: int
) -> None:
    monkeypatch.setenv("CODEX_WORKER_SOCKET", "/no-codex-worker.sock")
    response = gateway.post("/codex", content=body, headers={"X-API-Key": _P_KEY})
    assert response.status_code == status
    assert "private" not in response.text


@pytest.mark.parametrize("prompt", ["", " \t\n"])
def test_blank_prompt_is_rejected_before_worker_access(
    gateway: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, prompt: str
) -> None:
    # This path cannot connect; attempting to forward would return 502, not 422.
    monkeypatch.setenv("CODEX_WORKER_SOCKET", str(tmp_path / "missing.sock"))
    response = gateway.post(
        "/codex", json={"user": prompt}, headers={"X-API-Key": _P_KEY}
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid Codex request"}


@pytest.mark.parametrize(
    ("status", "payload", "expected_status", "detail"),
    [
        (429, b'{"detail":"worker-secret"}', 429, "Codex worker is busy"),
        (503, b'{"detail":"worker-secret"}', 503, "Codex worker unavailable"),
        (504, b'{"detail":"worker-secret"}', 504, "Codex worker timed out"),
        (500, b"worker-secret", 502, "invalid Codex worker response"),
        (302, b"", 502, "invalid Codex worker response"),
        (200, b'{"text":"worker-secret","output":{},"meta":{}}', 502, "invalid Codex worker response"),
        (200, b'{"text":"worker-secret","meta":[]}', 502, "invalid Codex worker response"),
        (
            200,
            b'{"text":"worker-secret","meta":{}}' + b" " * (1024 * 1024),
            502,
            "invalid Codex worker response",
        ),
    ],
)
def test_worker_errors_are_bounded_and_sanitized(
    gateway: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    payload: bytes,
    expected_status: int,
    detail: str,
) -> None:
    class Worker(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.send_response(status)
            self.send_header("Location", "http://127.0.0.1:1/worker-secret")
            self.end_headers()
            self.wfile.write(payload)

        def address_string(self) -> str:
            return "unix"

    with worker_socket(monkeypatch, Worker):
        response = gateway.post("/codex", json={"user": "prompt"}, headers={"X-API-Key": _P_KEY})
    assert response.status_code == expected_status
    assert response.json() == {"detail": detail}


def test_worker_deadline_aborts_in_flight_exchange(
    gateway: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Worker(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.send_response(200)
            self.end_headers()
            # Wait until the gateway closes the socket without ever sending a reply body.
            self.connection.settimeout(2)
            self.rfile.read()

        def address_string(self) -> str:
            return "unix"

    monkeypatch.setattr(codex_gateway, "_WORKER_TIMEOUT_S", 0.15)
    with worker_socket(monkeypatch, Worker):
        response = gateway.post("/codex", json={"user": "prompt"}, headers={"X-API-Key": _P_KEY})
    assert response.status_code == 504
