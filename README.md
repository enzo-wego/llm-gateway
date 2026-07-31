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
| `GET /health` | — | quota state, counters, active backends |

Pass a JSON Schema as `schema` and the response contains a parsed `output`
object. Omit it and you get raw `text` back.

Callers pick a **tier**, never a model name — that keeps model choice here (a
restart) rather than in a consumer's deploy.

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

Runs **native, not in Docker** — it needs the host's Claude CLI login.

```bash
cd /var/go/src/github.com/llm-gateway
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

claude setup-token          # interactive, one-time — needs a human
cp .env.example .env        # fill in secrets, incl. CLAUDE_CODE_OAUTH_TOKEN
chmod 600 .env

sudo cp deploy/llm-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now llm-gateway
curl -s http://172.18.0.1:8750/health
```

Three things bite here, all found the hard way during the first deploy.

**1 · The OAuth token must be in `.env`, not your shell.** `claude setup-token`
gives you a token you probably exported in `~/.zshrc`. systemd does not read
shell rc files, so the service starts unauthenticated unless
`CLAUDE_CODE_OAUTH_TOKEN` is in the `EnvironmentFile`.

**2 · Bind to the right bridge.** agent-mem's worker is in Docker, so the host's
`127.0.0.1` is unreachable from it. Bind to the gateway of *the network the
worker is actually on* — `agent-mem_default` is `172.18.0.1`, **not** the
default `docker0` at `172.17.0.1`. Check with:

```bash
docker inspect $(docker ps --filter name=worker -q) \
  --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{$v.Gateway}}{{end}}'
```

**3 · ufw blocks container→host by default.** Binding correctly is not enough;
the firewall drops it and `wget` just times out with no useful error. Open the
subnet explicitly:

```bash
sudo ufw allow from 172.18.0.0/16 to any port 8750 proto tcp \
  comment 'agent-mem worker -> llm-gateway'
```

Verify the whole path from inside the network, not just from the host — the host
can reach a bound port that containers cannot:

```bash
docker run --rm --network agent-mem_default alpine:3 \
  wget -qO- http://172.18.0.1:8750/health
```

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
