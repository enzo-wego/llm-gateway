"""Runtime configuration owned and persisted by llm-gateway.

Values start from the process environment. The editable, non-secret subset can
also be changed live through the authenticated config endpoint; those updates
are written atomically to the service's ``.env`` so restarts keep them.
"""

import math
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
from typing import Any

# ── Auth precedence trap ─────────────────────────────────────────────────────
# The Claude CLI resolves credentials in a fixed order and an API key WINS over
# the claude.ai login. If ANTHROPIC_API_KEY is present in this process's
# environment, every call silently bills the API key instead of the
# subscription seat — the CLI only writes a warning to stderr, and the run
# otherwise succeeds. That defeats the entire point of this service, so we
# remove the variable before the SDK can ever spawn a subprocess that inherits
# it. Verified 2026-07-31: with the key set the CLI prints
# "claude.ai connectors are disabled because ANTHROPIC_API_KEY ... takes
# precedence over your claude.ai login" and emits no RateLimitEvent.
for _leaked in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
    os.environ.pop(_leaked, None)


def _req(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise RuntimeError(f"{name} is required but unset")
    return v


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


# ── Service ──────────────────────────────────────────────────────────────────
HOST = os.getenv("LLM_GATEWAY_HOST", "127.0.0.1")
PORT = _int("LLM_GATEWAY_PORT", 8750)

# Shared secret agent-mem sends as X-API-Key. Required: the service speaks to
# the Docker bridge, which is reachable from every container on the host.
API_KEY = _req("LLM_GATEWAY_API_KEY")

# ── Backend switches ─────────────────────────────────────────────────────────
# Every text route can be pointed at either backend independently, so the
# Claude experiment can be turned off per-capability without redeploying
# agent-mem and without touching its gemini client. "claude" uses the
# subscription seat; "openrouter" reproduces today's behaviour exactly.
#
# Embeddings have no switch: Anthropic has no embeddings API, so OpenRouter is
# the only possible backend for them.
def _backend(name: str, default: str = "claude") -> str:
    v = (os.getenv(name, "").strip() or default).lower()
    return v if v in ("claude", "openrouter") else default


BACKEND_SUMMARY = _backend("LLM_GATEWAY_BACKEND_SUMMARY")
BACKEND_CHEAP = _backend("LLM_GATEWAY_BACKEND_CHEAP")
BACKEND_DESCRIBE = _backend("LLM_GATEWAY_BACKEND_DESCRIBE")

# When the seat's quota is exhausted, degrade to OpenRouter for the rest of the
# window instead of failing the request. Ingestion keeps running (on budget
# rather than on seat) and the Slack alert still fires, so the event is visible
# without being an outage. Set false to make quota exhaustion a hard error.
FALLBACK_ON_QUOTA = (os.getenv("LLM_GATEWAY_FALLBACK_ON_QUOTA", "true").strip().lower()
                     not in ("0", "false", "no"))

# ── Claude (subscription seat, via the bundled CLI) ──────────────────────────
# Two tiers so callers pick intent, not a model string. Keeping the mapping
# here means a model swap is a systemd restart, not an agent-mem deploy.
MODEL_SUMMARY = os.getenv("LLM_GATEWAY_MODEL_SUMMARY", "claude-sonnet-5")
MODEL_CHEAP = os.getenv("LLM_GATEWAY_MODEL_CHEAP", "claude-haiku-4-5")

# OpenRouter equivalents, used when a route's backend is "openrouter" and as the
# quota-fallback target. Defaults reproduce what agent-mem runs today.
OR_MODEL_SUMMARY = os.getenv("LLM_GATEWAY_OR_MODEL_SUMMARY", "google/gemini-3.6-flash")
OR_MODEL_CHEAP = os.getenv("LLM_GATEWAY_OR_MODEL_CHEAP", "google/gemini-2.5-flash")

EFFORT_SUMMARY = os.getenv("LLM_GATEWAY_EFFORT_SUMMARY", "medium")
EFFORT_CHEAP = os.getenv("LLM_GATEWAY_EFFORT_CHEAP", "low")

# Hard per-call ceiling handed to the SDK. Notional on a subscription (nothing
# is billed), but it still aborts a runaway generation instead of letting one
# pathological input chew through the seat's window.
MAX_BUDGET_USD = float(os.getenv("LLM_GATEWAY_MAX_BUDGET_USD", "0.50"))

# Wall-clock ceiling per Claude call. The CLI spawns a subprocess, so a hung
# call would otherwise hold a worker slot indefinitely.
CLAUDE_TIMEOUT_S = _int("LLM_GATEWAY_CLAUDE_TIMEOUT_S", 180)

# ── OpenRouter (embeddings only) ─────────────────────────────────────────────
# Embeddings cannot move to Claude: Anthropic has no embeddings API, and the
# stored vectors (768 flat / 3072 graph) are all gemini-embedding-001. Keeping
# the exact model id preserves the vector space, so nothing needs re-embedding.
OPENROUTER_KEY = _req("OPENROUTER_API_KEY")
OPENROUTER_BASE = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
EMBED_MODEL = os.getenv("LLM_GATEWAY_EMBED_MODEL", "google/gemini-embedding-001")

# Google's batchEmbedContents rejects >100 per call; 96 stays under that and
# matches what agent-mem already chunks to, so behaviour is unchanged.
EMBED_MAX_BATCH = _int("LLM_GATEWAY_EMBED_MAX_BATCH", 96)

# ── Slack alerting ───────────────────────────────────────────────────────────
# Optional: unset means alerts are logged and dropped. Quota exhaustion is the
# failure this service exists to make visible, so leaving it unset in prod
# defeats the purpose.
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "").strip()
SLACK_ALERT_CHANNEL = os.getenv("SLACK_ALERT_CHANNEL", "").strip()

# How long a given (rate_limit_type, resets_at) alert stays suppressed after
# firing once. Prevents a sustained outage from posting on every request.
ALERT_DEDUPE_S = _int("LLM_GATEWAY_ALERT_DEDUPE_S", 1800)

ALERTS_ENABLED = bool(SLACK_BOT_TOKEN and SLACK_ALERT_CHANNEL)


# ── Live editable configuration ─────────────────────────────────────────────
# API names intentionally match the runtime globals while env names retain the
# service prefix. Secrets and network/service settings are absent by design.
EDITABLE_ENV_KEYS = {
    "BACKEND_SUMMARY": "LLM_GATEWAY_BACKEND_SUMMARY",
    "BACKEND_CHEAP": "LLM_GATEWAY_BACKEND_CHEAP",
    "BACKEND_DESCRIBE": "LLM_GATEWAY_BACKEND_DESCRIBE",
    "MODEL_SUMMARY": "LLM_GATEWAY_MODEL_SUMMARY",
    "MODEL_CHEAP": "LLM_GATEWAY_MODEL_CHEAP",
    "OR_MODEL_SUMMARY": "LLM_GATEWAY_OR_MODEL_SUMMARY",
    "OR_MODEL_CHEAP": "LLM_GATEWAY_OR_MODEL_CHEAP",
    "EFFORT_SUMMARY": "LLM_GATEWAY_EFFORT_SUMMARY",
    "EFFORT_CHEAP": "LLM_GATEWAY_EFFORT_CHEAP",
    "FALLBACK_ON_QUOTA": "LLM_GATEWAY_FALLBACK_ON_QUOTA",
    "MAX_BUDGET_USD": "LLM_GATEWAY_MAX_BUDGET_USD",
    "CLAUDE_TIMEOUT_S": "LLM_GATEWAY_CLAUDE_TIMEOUT_S",
}

_BACKENDS = {"claude", "openrouter"}
_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
_MODEL_KEYS = {"MODEL_SUMMARY", "MODEL_CHEAP", "OR_MODEL_SUMMARY", "OR_MODEL_CHEAP"}
_CONFIG_LOCK = threading.Lock()
_ENV_ASSIGNMENT = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=).*$")

# systemd starts the service with the repository as WorkingDirectory. Tests can
# override this path without changing process-wide environment state.
ENV_FILE = Path(os.getenv("LLM_GATEWAY_ENV_FILE", ".env"))


class ConfigValidationError(ValueError):
    """A requested runtime value is unknown or unsafe to apply."""


def editable_config() -> dict[str, Any]:
    """Return current non-secret values safe to expose to authenticated clients."""
    with _CONFIG_LOCK:
        return {name: globals()[name] for name in EDITABLE_ENV_KEYS}


def _positive_float(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ConfigValidationError(f"{name} must be a positive number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigValidationError(f"{name} must be a positive number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ConfigValidationError(f"{name} must be a positive number")
    return parsed


def _positive_int(name: str, value: Any) -> int:
    parsed = _positive_float(name, value)
    if not parsed.is_integer():
        raise ConfigValidationError(f"{name} must be a positive integer")
    return int(parsed)


def _validate_updates(updates: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(updates) - set(EDITABLE_ENV_KEYS))
    if unknown:
        raise ConfigValidationError(f"unknown config key(s): {', '.join(unknown)}")

    validated: dict[str, Any] = {}
    for name, value in updates.items():
        if name.startswith("BACKEND_"):
            if not isinstance(value, str) or value.strip().lower() not in _BACKENDS:
                raise ConfigValidationError(f"{name} must be claude or openrouter")
            validated[name] = value.strip().lower()
        elif name.startswith("EFFORT_"):
            if not isinstance(value, str) or value.strip().lower() not in _EFFORTS:
                raise ConfigValidationError(
                    f"{name} must be one of: {', '.join(sorted(_EFFORTS))}"
                )
            validated[name] = value.strip().lower()
        elif name in _MODEL_KEYS:
            if not isinstance(value, str) or not value.strip() or any(ch.isspace() for ch in value):
                raise ConfigValidationError(f"{name} must be a non-empty model id without whitespace")
            validated[name] = value
        elif name == "FALLBACK_ON_QUOTA":
            if isinstance(value, bool):
                validated[name] = value
            elif isinstance(value, str) and value.strip().lower() in ("true", "false"):
                validated[name] = value.strip().lower() == "true"
            else:
                raise ConfigValidationError(f"{name} must be true or false")
        elif name == "MAX_BUDGET_USD":
            validated[name] = _positive_float(name, value)
        elif name == "CLAUDE_TIMEOUT_S":
            timeout = _positive_int(name, value)
            if timeout >= 200:
                raise ConfigValidationError(
                    "CLAUDE_TIMEOUT_S must be below agent-mem's 200s gateway client timeout"
                )
            validated[name] = timeout
    return validated


def _env_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return format(value, ".15g")
    return str(value)


def _rewrite_env(path: Path, updates: dict[str, Any]) -> None:
    """Atomically rewrite only requested keys, preserving every other line."""
    try:
        original = path.read_text() if path.exists() else ""
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    except OSError as exc:
        raise RuntimeError(f"read {path}: {exc}") from exc

    by_env = {EDITABLE_ENV_KEYS[name]: _env_value(value) for name, value in updates.items()}
    remaining = set(by_env)
    lines = original.splitlines(keepends=True)
    rewritten: list[str] = []
    for line in lines:
        bare = line.rstrip("\r\n")
        newline = line[len(bare):]
        match = _ENV_ASSIGNMENT.match(bare)
        if match and match.group(2) in by_env:
            env_name = match.group(2)
            rewritten.append(
                f"{match.group(1)}{env_name}{match.group(3)}{by_env[env_name]}{newline}"
            )
            remaining.discard(env_name)
        else:
            rewritten.append(line)

    if remaining:
        if rewritten and not rewritten[-1].endswith(("\n", "\r")):
            rewritten[-1] += "\n"
        for env_name in by_env:
            if env_name in remaining:
                rewritten.append(f"{env_name}={by_env[env_name]}\n")

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w") as tmp:
            os.fchmod(tmp.fileno(), mode)
            tmp.writelines(rewritten)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def update_config(updates: dict[str, Any]) -> dict[str, Any]:
    """Validate, persist, then apply a partial update to the live process."""
    validated = _validate_updates(updates)
    with _CONFIG_LOCK:
        if validated:
            _rewrite_env(ENV_FILE, validated)
            globals().update(validated)
        return {name: globals()[name] for name in EDITABLE_ENV_KEYS}
