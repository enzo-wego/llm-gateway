"""Fixed Codex invocation and subprocess ownership."""

import json
import os
import signal
from contextlib import suppress
from pathlib import Path
from subprocess import DEVNULL, PIPE
from tempfile import TemporaryDirectory, TemporaryFile
from typing import Final, Literal

import anyio
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator
from pydantic_core import PydanticCustomError

MAX_BYTES: Final = 1024 * 1024


class GenerateInput(BaseModel):
    """The internal gateway's normalized generation request."""

    model_config = ConfigDict(
        frozen=True, strict=True, extra="forbid", allow_inf_nan=False
    )

    system: str = ""
    user: str
    # The gateway image ships separately; keep these fields in sync with _CodexInput.
    model: str = Field(
        default="gpt-6.2-sol", max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
    )
    effort: Literal["low", "medium", "high", "xhigh"] = "medium"
    schema_: dict[str, JsonValue] | None = Field(default=None, alias="schema")

    @field_validator("user")
    @classmethod
    def nonblank_user(cls, value: str) -> str:
        """Reject blank prompts before launching the provider."""
        if not value.strip():
            raise PydanticCustomError("empty_prompt", "Empty user prompt")
        return value


class WorkerUnavailableError(Exception):
    """No dedicated worker runtime was configured."""


class ExecutionError(Exception):
    """The configured executable did not return a usable result."""


async def generate_text(body: GenerateInput) -> str:
    """Capture only the final message from a fresh, isolated CLI invocation."""
    executable = Path(os.environ.get("CODEX_WORKER_EXECUTABLE", ""))
    codex_home = Path(os.environ.get("CODEX_HOME", ""))
    if (
        not executable.is_absolute()
        or not codex_home.is_absolute()
        or not codex_home.is_dir()
    ):
        raise WorkerUnavailableError

    with TemporaryDirectory(prefix="codex-worker-", dir="/tmp") as directory:
        scratch = Path(directory)
        workspace = scratch / "workspace"
        workspace.mkdir()
        home = scratch / "home"
        home.mkdir()
        env = {
            "CODEX_HOME": str(codex_home),
            "HOME": str(home),
            "TMPDIR": str(scratch),
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
        args = [
            str(executable),
            "exec",
            "--model",
            body.model,
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
            "--color",
            "never",
            "--disable",
            "shell_tool",
            "-c",
            'approval_policy="never"',
            "-c",
            'web_search="disabled"',
            "-c",
            'cli_auth_credentials_store="file"',
            "-c",
            f'model_reasoning_effort="{body.effort}"',
        ]
        if body.system:
            system_file = scratch / "system.txt"
            system_file.write_text(body.system, encoding="utf-8")
            args.extend(
                ("-c", f"model_instructions_file={json.dumps(str(system_file))}")
            )
        if body.schema_ is not None:
            schema_file = scratch / "schema.json"
            schema_file.write_text(
                json.dumps(body.schema_, allow_nan=False), encoding="utf-8"
            )
            args.extend(("--output-schema", str(schema_file)))
        args.append("-")

        # An anonymous input file avoids pipe deadlock while keeping prompts out of argv.
        with TemporaryFile(dir=scratch) as stdin:
            stdin.write(body.user.encode())
            stdin.seek(0)
            with anyio.CancelScope(shield=True):
                process = await anyio.open_process(
                    args,
                    stdin=stdin,
                    stdout=PIPE,
                    stderr=DEVNULL,
                    cwd=workspace,
                    env=env,
                    start_new_session=True,
                )
            try:
                stdout = process.stdout
                if stdout is None:
                    raise ExecutionError
                output = bytearray()
                async for chunk in stdout:
                    if len(output) + len(chunk) > MAX_BYTES:
                        raise ExecutionError
                    output.extend(chunk)
                if await process.wait() != 0:
                    raise ExecutionError
            finally:
                with anyio.CancelScope(shield=True):
                    # Include wrappers and children that survived the direct process.
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    await process.aclose()

        text = output.decode().removesuffix("\n")
        if not text.strip():
            raise ExecutionError
        return text
