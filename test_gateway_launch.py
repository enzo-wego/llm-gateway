"""Verify access redaction on the real Uvicorn listener for shipped invocations."""

import http.client
import json
import os
import re
import shlex
import socket
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent


def _uvicorn_args(launch: str) -> list[str]:
    if launch == "docker":
        dockerfile = (ROOT / "Dockerfile").read_text()
        return json.loads(next(line[4:] for line in dockerfile.splitlines() if line.startswith("CMD ")))[1:]
    if launch == "systemd":
        unit = (ROOT / "deploy/llm-gateway.service").read_text().replace("\\\n", " ")
        command = next(line[10:] for line in unit.splitlines() if line.startswith("ExecStart="))
        command = command.replace("${LLM_GATEWAY_HOST}", "127.0.0.1")
        command = command.replace("${LLM_GATEWAY_PORT}", "8750")
        return shlex.split(command)[1:]
    readme = (ROOT / "README.md").read_text()
    command = next(line for line in readme.splitlines() if ".venv/bin/uvicorn app.main:app" in line)
    args = shlex.split(command)
    return args[args.index(".venv/bin/uvicorn") + 1:]


@pytest.mark.parametrize("launch", ["docker", "systemd", "local"])
def test_shipped_uvicorn_invocation_omits_query_from_access_logs(launch: str) -> None:
    env = {
        "PATH": os.defpath,
        "PYTHONDONTWRITEBYTECODE": "1",
        "LLM_GATEWAY_API_KEY": "test-caller",
    }
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        argv = [sys.executable, "-m", "uvicorn", *_uvicorn_args(launch), "--fd", str(listener.fileno())]
        with subprocess.Popen(
            argv, cwd=ROOT, env=env, pass_fds=(listener.fileno(),),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        ) as server:
            try:
                with closing(http.client.HTTPConnection(*listener.getsockname(), timeout=8)) as client:
                    client.request("GET", "/health?key=QUERY_SENTINEL")
                    response = client.getresponse()
                    assert response.status == 200
                    response.read()
            finally:
                server.terminate()
                try:
                    logs, _ = server.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    logs, _ = server.communicate(timeout=5)
                    pytest.fail(f"Uvicorn did not shut down: {logs}")

    assert re.search(r"\bgw-access GET /health 200 \d+ms\b", logs), logs
    assert "QUERY_SENTINEL" not in logs, logs
