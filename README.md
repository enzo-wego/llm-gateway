# llm-gateway

One HTTP surface in front of two LLM backends, so callers ask for an *intent*
and this service decides which model and provider serves it.

Built for `agent-mem`, but nothing in here is agent-mem-specific.

```
caller ──HTTP──▶ llm-gateway ──▶ Claude (subscription seat, via bundled CLI)
                             └──▶ OpenRouter (embeddings, and fallback generation)
```

## Why two backends

They do different jobs and neither can do the other's.

**Generation** — Claude reads a Slack thread and *writes* a summary, or looks at
a screenshot and *describes* it. Output is prose, stored in `TEXT` columns.

**Embedding** — `gemini-embedding-001` writes nothing. It turns text into 3,072
numbers positioned so similar meanings sit close together. Output is a vector,
stored in `halfvec`/`vector` columns behind an HNSW index, and it is what
semantic search actually queries.

They run in sequence, not as alternatives: Claude writes a summary, then that
summary is embedded, and both land in the same database row.

**Embeddings can never move to Claude.** Anthropic has no embeddings API — no
Claude model returns a vector. Even if one shipped, a query vector is only
comparable to stored vectors from the same model, so switching would mean
recomputing every stored vector and would silently degrade search until you did.

## Endpoints

All routes except `/health` require `X-API-Key`.

| Route | Backend | Body |
|---|---|---|
| `POST /generate` | seat or OpenRouter | `{system, user, tier: "summary"\|"cheap", schema?}` |
| `POST /describe` | seat or OpenRouter | `{system, prompt, mime, data_b64, schema?}` |
| `POST /embed` | OpenRouter always | `{texts: [...], dims}` |
| `GET /config` | — | current editable, non-secret runtime configuration |
| `PUT /config` | — | partial configuration update, applied live and persisted to `.env` |
| `GET /health` | — | quota state, counters, active backends |

Pass a JSON Schema as `schema` and the response contains a parsed `output`
object. Omit it and you get raw `text` back. This holds on **both** backends —
before, the OpenRouter path accepted `schema` and ignored it, so a backend
switch or a quota fallback changed the response shape with nothing in the
response saying so.

Callers pick a **tier**, never a model name — that keeps model choice here (a
restart) rather than in a consumer's deploy.

`GET /config` and `PUT /config` require `X-API-Key`. The config response never
contains service or provider credentials. Updates reject unknown keys and
invalid values, rewrite only the supplied `.env` keys via an atomic rename, and
take effect in the running process immediately. `LLM_GATEWAY_CLAUDE_TIMEOUT_S`
must remain below agent-mem's 200-second client timeout.

## Every route has an off switch

| Env var | Values | Default |
|---|---|---|
| `LLM_GATEWAY_BACKEND_SUMMARY` | `claude` · `openrouter` | `claude` |
| `LLM_GATEWAY_BACKEND_CHEAP` | `claude` · `openrouter` | `claude` |
| `LLM_GATEWAY_BACKEND_DESCRIBE` | `claude` · `openrouter` | `claude` |
| `LLM_GATEWAY_FALLBACK_ON_QUOTA` | `true` · `false` | `true` |

Setting a route to `openrouter` reproduces agent-mem's pre-gateway behaviour
exactly. Embeddings have no switch — there is no alternative provider.

With `FALLBACK_ON_QUOTA=true`, exhausting the seat's window degrades that route
to OpenRouter for the rest of the window instead of failing, and still fires the
Slack alert. Quota exhaustion becomes a notification, not an outage.

## Alerting

Three failures stop ingestion with no visible error at the caller, so each posts
to Slack (deduped per window):

| Signal | Meaning |
|---|---|
| `RateLimitInfo.status == "rejected"` | Seat quota spent |
| `status == "allowed_warning"` | Approaching the cap |
| `AssistantMessage.error == "authentication_failed"` | CLI login expired |
| OpenRouter `402` | Embedding budget gone — the graph has stopped growing |

Seat quotas are per-window and per-family: `five_hour`, `seven_day`,
`seven_day_opus`, `seven_day_sonnet`, `overage`. The alert names which tripped.

## ⚠️ The `ANTHROPIC_API_KEY` trap

If that variable is present in the environment, the Claude CLI **silently bills
the API key instead of the subscription seat**. It emits only a stderr warning,
the call succeeds, and no `RateLimitEvent` appears — so the seat looks unused
while you quietly pay per token.

`app/config.py` pops it (and `ANTHROPIC_AUTH_TOKEN`) at import, before the SDK
can spawn a subprocess that inherits it. Don't put it in `.env` either.

## Local development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env       # set LLM_GATEWAY_API_KEY and OPENROUTER_API_KEY
set -a && . ./.env && set +a
env -u ANTHROPIC_API_KEY .venv/bin/uvicorn app.main:app --port 8750
```

Your Mac is already logged into Claude Code, so the seat path works immediately.

## VPS deployment

Runs as a **Docker container**. The old "native, not in Docker" requirement is
gone: `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) is a headless
credential passed by environment, so no interactive host login is needed.

```bash
cd /var/go/src/github.com/llm-gateway && git pull

# config/ is a bind-mounted DIRECTORY, not a file — see gotcha 3 below.
mkdir -p config && cp .env config/.env && chmod 600 config/.env
ls -n config          # confirm 1001:1001 (the container's app user)

# The committed override publishes 8750, so that port must be free first — on a
# box still running the native service, stop it (validate side-by-side first if
# you want; see below).
sudo systemctl disable --now llm-gateway
docker compose -f docker-compose.yml -f docker-compose.vps.yml up -d --build
curl -s http://127.0.0.1:8750/health | jq '.ok, .seat'
```

The VPS override attaches the container to the pre-existing `agent-mem_default`
network, so agent-mem's worker reaches it by service name at
`http://llm-gateway:8750/` — port 8750 is the container's internal port on the
shared network, independent of the host publish. Verify that path from inside
the network, not just from the host:

```bash
docker run --rm --network agent-mem_default alpine:3 \
  wget -qO- http://llm-gateway:8750/health
```

During a side-by-side migration — validating the container while the native
service still holds 8750 — temporarily publish the container on 8751 instead.
Change the `!override` publish in `docker-compose.vps.yml` to
`127.0.0.1:8751:8750`, bring the stack up, and check `:8751`:

```bash
docker compose -f docker-compose.yml -f docker-compose.vps.yml up -d --build
curl -s http://127.0.0.1:8751/health | jq '.ok, .seat'
```

Reachability by name is unaffected — the internal port stays 8750 on the shared
network regardless of the host publish. Once satisfied, revert the publish to
the committed `127.0.0.1:8750:8750` and run the cutover block above.

`deploy/llm-gateway.service` and the `.venv` stay in place as a one-command
rollback: `docker compose down && sudo systemctl enable --now llm-gateway`.

Three things bit the first (native) deploy. Only the first still applies.

**1 · The OAuth token must be in the env file, not your shell.** `claude
setup-token` gives you a token you probably exported in `~/.zshrc`. The
container inherits nothing from your shell rc, so `CLAUDE_CODE_OAUTH_TOKEN` has
to live in `config/.env` (loaded via `env_file`). Still true.

**2 · Bridge-IP binding — no longer applies.** The native service had to bind
the `agent-mem_default` gateway `172.18.0.1` because the worker could not reach
the host loopback. On the shared network the worker resolves the container by
DNS name, so the gateway binds `0.0.0.0` *inside* the container and no host
bridge IP is involved.

**3 · The ufw rule — no longer applies.** `sudo ufw allow from 172.18.0.0/16 to
any port 8750 proto tcp` existed to let a container reach a host-bound port
through the firewall. Container-to-container traffic on `agent-mem_default`
never touches the host firewall, so the rule is now unnecessary. It is
harmless; leave it in place.

**4 · Mount `config/` as a directory, never `config/.env` as a file.** `PUT
/config` persists with `os.replace(tmp, .env)`, an atomic rename. Renaming
*onto* a single-file bind mount fails with `EBUSY` (the target is itself a
mount point) and the endpoint returns HTTP 500 with the live value left
untouched. Bind-mounting the parent directory (`./config:/config`, with
`LLM_GATEWAY_ENV_FILE=/config/.env`) lets the rename land on a normal file
inside the mount, so live-and-persist works exactly as it does natively.

## Status

Verified live against the seat on 2026-07-31:

- Subscription auth works — `RateLimitEvent type=five_hour status=allowed`, no API key involved
- Structured output works — `output_format` json_schema returns a parsed object
- Vision works — image reaches the model through the streaming-input path
- Backend toggle works — flipping `BACKEND_CHEAP` reroutes to `gemini-2.5-flash`
- Embeddings work — 3072-dim vectors, cosine 0.49 on an unrelated pair
- Harness overhead: **~700–800 input tokens per call** with tools stripped and
  settings disabled — roughly +38% on a 1.8k-token transcript

Not yet done: agent-mem does not call this service yet. Its Gemini client is
untouched by design, pending results from this experiment.
