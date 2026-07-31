"""Runtime configuration, read once from the environment at import time.

The service is deliberately configured by env vars only (no settings DB, no
config file) — it is a single native process owned by systemd, and its whole
job is to front two upstreams. Anything that needs to change per-request is a
request field, not config.
"""

import os

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
