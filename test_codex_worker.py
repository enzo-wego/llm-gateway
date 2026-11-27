"""Exercise the worker endpoint against an offline fake executable."""

import json
import os
import socket
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from codex_worker import main

_FAKE = """
import json
import os
import sys
import time
import tomllib
from pathlib import Path

prompt = sys.stdin.read()
args = sys.argv[1:]
auth = Path(os.environ["CODEX_HOME"])
assert "LLM_GATEWAY_API_KEY" not in os.environ
assert prompt not in args and args[-1] == "-"
assert not any(Path.cwd().iterdir())
if prompt in {"hold-one", "hold-two", "timeout", "disconnect"}:
    (auth / prompt).write_text(json.dumps([os.getpid(), str(Path.cwd())]))
    while not (auth / "release").exists():
        time.sleep(.01)
if prompt == "empty":
    raise SystemExit(0)
if prompt == "fail":
    print("private diagnostics", file=sys.stderr)
    raise SystemExit(1)
if prompt == "large":
    print("x" * (1024 * 1024 + 1))
elif prompt == "escaped":
    print(chr(92) * (600 * 1024))
elif prompt == "invalid-json":
    print("not JSON")
elif prompt == "settings":
    model = args[args.index("--model") + 1]
    settings = tomllib.loads("\\n".join(args[i + 1] for i, arg in enumerate(args) if arg == "-c"))
    print(f"{model}/{settings['model_reasoning_effort']}")
elif "--output-schema" in args:
    schema = Path(args[args.index("--output-schema") + 1])
    instructions = next(arg for arg in args if arg.startswith("model_instructions_file="))
    system_path = Path(json.loads(instructions.split("=", 1)[1]))
    print(json.dumps({
        "user": prompt, "system": system_path.read_text(),
        "schema": json.loads(schema.read_text()), "cwd": str(Path.cwd()),
    }))
else:
    print(f"echo: {prompt}")
"""


@pytest.fixture
def fake_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    executable = tmp_path / "codex"
    executable.write_text(f"#!{sys.executable}\n{_FAKE}", encoding="utf-8")
    executable.chmod(0o700)
    auth = tmp_path / "auth"
    auth.mkdir()
    monkeypatch.setenv("CODEX_WORKER_EXECUTABLE", str(executable))
    monkeypatch.setenv("CODEX_HOME", str(auth))
    monkeypatch.setenv("LLM_GATEWAY_API_KEY", "dummy-gateway-secret")
    return auth


@pytest.fixture
def worker_socket(fake_cli: Path) -> Iterator[Path]:
    with TemporaryDirectory(dir="/tmp") as directory:
        path = Path(directory) / "worker.sock"
        config = uvicorn.Config(
            main.app, uds=str(path), access_log=False, log_config=None
        )
        server = uvicorn.Server(config)
        with ThreadPoolExecutor(max_workers=1) as thread:
            run = thread.submit(server.run)
            try:
                _wait_for(path)
                yield path
            finally:
                server.should_exit = True
                run.result(timeout=5)


@pytest.fixture
def worker(worker_socket: Path) -> Iterator[httpx.Client]:
    transport = httpx.HTTPTransport(uds=str(worker_socket), trust_env=False)
    with httpx.Client(
        transport=transport, base_url="http://worker", timeout=5, trust_env=False
    ) as client:
        yield client


def _wait_for(path: Path) -> None:
    deadline = monotonic() + 5
    while not path.exists() and monotonic() < deadline:
        sleep(0.01)
    assert path.exists()


def test_system_and_user_stay_separate_and_schema_uses_a_fresh_workspace(
    worker: httpx.Client,
) -> None:
    schema = {"type": "object"}
    request = {
        "schema": schema,
        "system": "Private instructions",
        "user": "- user input\nwith a second line",
        "effort": "xhigh",
    }
    workspaces = set()
    for _ in range(2):
        response = worker.post("/generate", json=request)
        assert response.status_code == 200
        result = response.json()
        assert result["meta"] == {}
        workspace = Path(result["output"].pop("cwd"))
        assert not workspace.parent.exists()
        workspaces.add(workspace)
        assert result["output"] == {
            "system": request["system"],
            "user": request["user"],
            "schema": schema,
        }
    assert len(workspaces) == 2


def test_plain_response_and_tcp_denial(worker: httpx.Client) -> None:
    response = worker.post("/generate", json={"user": "hello"})
    assert response.json() == {"text": "echo: hello", "meta": {}}
    with TestClient(main.app) as tcp:
        assert tcp.post("/generate", json={"user": "hello"}).status_code == 403


@pytest.mark.parametrize(
    ("settings", "resolved"),
    [
        ({}, "gpt-6.2-sol/medium"),
        ({"model": "gpt-6.2-astra"}, "gpt-6.2-astra/medium"),
        ({"effort": "low"}, "gpt-6.2-sol/low"),
        ({"model": "gpt-6.2-astra", "effort": "xhigh"}, "gpt-6.2-astra/xhigh"),
    ],
)
def test_cli_resolves_requested_model_and_effort_independently(
    worker: httpx.Client, settings: dict[str, str], resolved: str
) -> None:
    response = worker.post("/generate", json={"user": "settings", **settings})
    assert response.status_code == 200
    assert response.json()["text"] == resolved


@pytest.mark.parametrize(
    ("body", "status"),
    [
        (b'{"user":""}', 422),
        (b'{"user":"a","tier":"other"}', 422),
        (b'{"user":"a","tier":"cheap"}', 422),
        (b'{"user":"a","tier":null}', 422),
        (b'{"user":"a","model":"--other"}', 422),
        (b'{"user":"a","effort":"other"}', 422),
        (b'{"user":"a","schema":[]}', 422),
        (b"{" + b"x" * (1024 * 1024), 413),
    ],
)
def test_request_rejection(worker: httpx.Client, body: bytes, status: int) -> None:
    assert worker.post("/generate", content=body).status_code == status


@pytest.mark.parametrize(
    ("prompt", "schema"),
    [
        ("empty", None),
        ("fail", None),
        ("large", None),
        ("escaped", None),
        ("invalid-json", {"type": "object"}),
    ],
)
def test_bad_results_are_opaque(
    worker: httpx.Client, prompt: str, schema: dict[str, str] | None
) -> None:
    response = worker.post("/generate", json={"user": prompt, "schema": schema})
    assert response.status_code == 502
    assert response.json() == {"detail": "Codex execution failed"}


def test_busy_rejects_without_queue_and_releases_slots(
    worker: httpx.Client, fake_cli: Path
) -> None:
    with ThreadPoolExecutor(max_workers=2) as threads:
        calls = [
            threads.submit(worker.post, "/generate", json={"user": prompt})
            for prompt in ("hold-one", "hold-two")
        ]
        try:
            _wait_for(fake_cli / "hold-one")
            _wait_for(fake_cli / "hold-two")
            assert worker.post("/generate", json={"user": "busy"}).status_code == 429
        finally:
            (fake_cli / "release").touch()
        assert [call.result(timeout=5).status_code for call in calls] == [200, 200]
    assert worker.post("/generate", json={"user": "free"}).status_code == 200


def test_timeout_reaps_process_and_removes_scratch(
    worker: httpx.Client,
    fake_cli: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main, "_EXEC_TIMEOUT", 1)
    response = worker.post("/generate", json={"user": "timeout"})
    assert response.status_code == 504
    pid, cwd = json.loads((fake_cli / "timeout").read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not Path(cwd).parent.exists()


def test_client_disconnect_cancels_and_removes_scratch(
    worker_socket: Path, fake_cli: Path
) -> None:
    body = b'{"user":"disconnect"}'
    request = (
        b"POST /generate HTTP/1.1\r\nHost: worker\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\n\r\n"
        + body
    )
    marker = fake_cli / "disconnect"
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(str(worker_socket))
        client.sendall(request)
        _wait_for(marker)
        pid, cwd = json.loads(marker.read_text(encoding="utf-8"))
    deadline = monotonic() + 5
    scratch = Path(cwd).parent
    while scratch.exists() and monotonic() < deadline:
        sleep(0.01)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not scratch.exists()
