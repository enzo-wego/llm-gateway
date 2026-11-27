# llm-gateway

One HTTP gateway for Claude, OpenRouter, Google Gemini, and OpenAI Codex.
Use `/generate` when you want the gateway to choose a backend. Use the Gemini
or Codex route when your application specifically needs that provider.

## Choose a provider

| I want to… | Route | Caller credential | What comes back |
|---|---|---|---|
| Generate text or JSON with Claude or OpenRouter | `POST /generate` | `X-API-Key: $LLM_GATEWAY_API_KEY` | `backend` (`claude` or `openrouter`), `text` or `output`, `meta` |
| Describe an image or PDF with Claude or OpenRouter | `POST /describe` | Same gateway key | Same shape as `/generate` |
| Embed text for vector search | `POST /embed` | Same gateway key | `embeddings` in input order; OpenRouter first, direct Gemini on cap exhaustion if enabled |
| Call a Gemini model directly through the key pool | `POST /gemini/v1beta/models/{model}:{action}` | `x-goog-api-key: $LLM_GATEWAY_API_KEY` | Gemini's native response, not the gateway `text` wrapper |
| Generate text or JSON with Codex | `POST /codex` | `X-API-Key` with the calling project's **dedicated Codex key** | `backend: "codex"`, `text` or `output`, `meta` |

### Caller examples

Run these on the gateway host, or set `GW` to the gateway address reachable
from your application. Inside another container, `127.0.0.1` refers to that
container, not the gateway. Have your secret store supply `LLM_GATEWAY_API_KEY`
for the shared routes or `CODEX_PROJECT_KEY` for Codex. Clients do **not**
need a Claude login, an OpenRouter key, a Google key, or the pool encryption secret.

```sh
GW="${GW:-http://127.0.0.1:8750}"
curl -fsS "$GW/health"
```

**Claude or OpenRouter — generate.** Pick `cheap` or `summary`; there is no
per-request provider switch. For example, `LLM_GATEWAY_BACKEND_CHEAP=openrouter`
on the server routes `cheap` requests to OpenRouter. The same request works on
a Claude tier. Check `backend` in the response to see which actually answered,
including after fallback. This example requests JSON text; to get parsed
`output` instead, pass a nonempty `schema` as in the Codex example below.

```sh
curl -fsS "$GW/generate" -H "X-API-Key: $LLM_GATEWAY_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"tier":"cheap","user":"Return a JSON object with a short greeting."}'
```

**Claude or OpenRouter — describe a file.** Supply the MIME type and raw
base64 (no `data:` prefix). This example uses `jq` and a local `receipt.png`;
for a PDF, change the filename and MIME type to `application/pdf`.

```sh
base64 < receipt.png | tr -d '\r\n' | jq -Rs \
  '{mime:"image/png",data_b64:.,prompt:"Extract the total as JSON."}' |
  curl -fsS "$GW/describe" -H "X-API-Key: $LLM_GATEWAY_API_KEY" \
    -H 'Content-Type: application/json' --data-binary @-
```

**OpenRouter — embed.** Set `dims` to the width of your vector index. Use
`"fallback":false` if a request must stay on OpenRouter. Otherwise the gateway
may use its separate `GEMINI_API_KEY` when OpenRouter hits its monthly cap;
this fallback does **not** draw from the Gemini proxy pool.

```sh
curl -fsS "$GW/embed" -H "X-API-Key: $LLM_GATEWAY_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"texts":["payment received","invoice overdue"],"dims":768,"fallback":false}'
```

**Gemini — native API.** Put the Google model ID in the path. For
`generateContent`, read the answer from `candidates[].content.parts[].text`.
The proxy also accepts `countTokens`, `embedContent`, `batchEmbedContents`,
and `streamGenerateContent` (use `?alt=sse` and `curl -N` for streaming).
Match the model and request body to the action. A `503` with `gemini_disabled`
or `no_gemini_keys` means the [pool](#optional-gemini-key-pool) is off or
has no available keys.

```sh
curl -fsS "$GW/gemini/v1beta/models/gemini-2.5-flash:generateContent" \
  -H "x-goog-api-key: $LLM_GATEWAY_API_KEY" -H 'Content-Type: application/json' \
  -d '{"contents":[{"parts":[{"text":"Reply with a short greeting."}]}]}'
```

**Codex — internal projects only.** Use the secret issued to `p-agent` or
`agent-mem` as `CODEX_PROJECT_KEY`, **not** the shared gateway key. The example
returns `backend: "codex"` and a parsed `output` object; omit `schema` for
`text`. Codex does not fall back to another provider. See the
[Codex runbook](docs/codex.md) for provisioning and error responses.

```sh
curl -fsS --max-time 200 "$GW/codex" -H "X-API-Key: $CODEX_PROJECT_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"user":"Report status OK.","schema":{"type":"object","properties":{"status":{"type":"string","enum":["OK"]}},"required":["status"],"additionalProperties":false}}'
```

## Endpoints

These shared-key routes require `X-API-Key`, except for public `/health`.
The provider-specific Gemini and Codex routes use the credentials listed above.

| Route | Backend | Body |
|---|---|---|
| `POST /generate` | seat or OpenRouter | `{system, user, tier: "summary"\|"cheap", schema?}` |
| `POST /describe` | seat or OpenRouter | `{system, prompt, mime, data_b64, schema?}` |
| `POST /embed` | OpenRouter, then Gemini on cap exhaustion | `{texts: [...], dims, fallback?}` (`fallback` defaults to `true`; `false` disables Gemini fallback for that request) |
| `GET /config` | — | current editable, non-secret runtime configuration |
| `PUT /config` | — | partial configuration update, applied live and persisted to `.env` |
| `GET /health` | — | gateway liveness, quota state, counters, active backends, and optional pool readiness |
| `GET /usage` | — | seat and OpenRouter budget, plus per-caller `callers` counters |

The Gemini fallback retries HTTP 429, 500, 502, 503, 504 and network errors up
to 3 times (1, 2, 4 s, or `Retry-After` up to 10 s for 429 and 503) before
failing.

Every request to `/generate`, `/describe` and `/embed` may carry an optional
`X-LLM-Caller` header naming who made it, e.g. `p-agent/bot-comment` or
`agent-mem`. It must match `^[a-z0-9][a-z0-9/_-]{0,63}$` (lowercase letters,
digits, `/`, `_`, `-`; 1-64 characters). A missing, empty or malformed value is
recorded as `unknown`; it is never trimmed or lower-cased to rescue it. The label
appears in each request's log line (`generate caller=… `, `describe caller=… `,
`embed caller=… `) and in per-caller counters. It is self-asserted by an
already authenticated caller: a bookkeeping label, not authentication.

`unknown` and `other` are reserved buckets: a caller that sends the literal label
`unknown` or `other` lands in that shared bucket. Up to 64 other distinct labels
get their own counter; further new labels are counted in `other` (their log line
still shows their own label). Requests rejected before the handler runs (401, 422)
are not counted. Counters reset on process restart.

`GET /usage` returns `seat`, `openrouter` and `callers`: a map, sorted by label, of
`{calls, errors, notional_cost_usd}`. `errors` counts non-200 outcomes;
`notional_cost_usd` sums the Claude seat's reported cost only.

`/describe` takes an image (`image/png`, `image/jpeg`, `image/gif`,
`image/webp`) or a PDF (`application/pdf`). A PDF goes to the model as a
document, so it reads the text layer rather than a rasterized page.

Pass a JSON Schema as `schema` and the response contains a parsed `output`
object. Omit it and you get raw `text` back. This holds on **both** backends —
before, the OpenRouter path accepted `schema` and ignored it, so a backend
switch or a quota fallback changed the response shape with nothing in the
response saying so.

On `/generate`, callers pick a **tier**, not a model name. The native Gemini
route is different: the caller names the model in the URL.

Tier models default to the CLI aliases `sonnet` and `haiku`, which resolve to
the newest model in each family. The CLI that resolves them ships inside
`claude-agent-sdk`, so a new model is only reachable once the image carries a
newer SDK. `deploy/refresh-sdk.sh` rebuilds when PyPI has one, checks a real
call on both `/generate` tiers and `/describe`, and restores the previous image
if anything fails. It posts the outcome to `SLACK_ALERT_CHANNEL` (a user id
gets a DM) when `config/.env` sets it. Run it daily from cron:

```
15 12 * * * PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin /path/to/llm-gateway/deploy/refresh-sdk.sh >>~/.cache/llm-gateway-refresh.log 2>&1
```

Pin a full id (e.g. `LLM_GATEWAY_MODEL_SUMMARY=claude-sonnet-5-5`) when a model
must not move. A full id needs a CLI that knows it: CLI 2.1.283 rejects
`claude-sonnet-5-5` with `[claude-code:unrecognized_model]`.

The refresh only moves the Claude backend. A tier on `openrouter` calls
`OR_MODEL_*`, and OpenRouter's floating aliases (`~anthropic/claude-sonnet-latest`,
`~anthropic/claude-haiku-latest`, `~anthropic/claude-opus-latest`) resolve
server side with no rebuild. A new model that rejects the configured `THINKING_*` mode fails
with a 400 until the pin or the mode changes.

`GET /config` and `PUT /config` require `X-API-Key`. The config response never
contains service or provider credentials. Updates reject unknown keys and
invalid values, rewrite only the supplied `.env` keys via an atomic rename, and
take effect in the running process immediately. `LLM_GATEWAY_CLAUDE_TIMEOUT_S`
must remain below agent-mem's 200-second client timeout.

## Claude and OpenRouter routing

| Env var | Values | Default |
|---|---|---|
| `LLM_GATEWAY_BACKEND_SUMMARY` | `claude` · `openrouter` | `claude` |
| `LLM_GATEWAY_BACKEND_CHEAP` | `claude` · `openrouter` | `claude` |
| `LLM_GATEWAY_BACKEND_DESCRIBE` | `claude` · `openrouter` | `claude` |
| `LLM_GATEWAY_FALLBACK_ON_QUOTA` | `true` · `false` | `true` |
| `GEMINI_API_KEY` | Gemini API credential (secret, restart required) | unset |

Setting a route to `openrouter` reproduces agent-mem's pre-gateway behaviour
exactly. Embeddings have no switch: OpenRouter remains primary, and the direct
Gemini API is used only when OpenRouter reports that its monthly cap is spent.

With `FALLBACK_ON_QUOTA=true`, exhausting the seat's window degrades that route
to OpenRouter for the rest of the window instead of failing, and still fires the
Slack alert. Quota exhaustion becomes a notification, not an outage.

## Optional Gemini key pool

The pool is **off by default**. Leave `LLM_GATEWAY_SECRET_KEY` unset unless you
need the Gemini pass-through (`/gemini/*`). Setting it enables the pool and
provides the secret used to encrypt pooled keys and sign admin sessions. Keep
`LLM_GATEWAY_SECRET_KEY` server-only and stable across restarts. Proxy callers
send `LLM_GATEWAY_API_KEY` in the `x-goog-api-key` header, never in the query
string. The pool does not change `/embed`'s separate fallback
through `GEMINI_API_KEY`.

With the pool off, `/health` omits `gemini_pool`. With the pool on, it reports
key counts when SQLite is accessible, or `{"gemini_pool":{"ready":false}}` if
SQLite cannot be checked within the probe deadline. HTTP 200 and `"ok": true`
still indicate gateway liveness, not pool or provider readiness.

Compose persists the default `/data/gateway.db` in the `gateway-data` named
volume on base, VPS and office deployments. Keep a custom container database
path under `/data` or give it its own persistent mount. For a native launch,
set `LLM_GATEWAY_DB_PATH` to a persistent path writable by the gateway service
user. Preserve both the database and its encryption secret when moving a pool.

Leave `LLM_GATEWAY_ADMIN_PASSWORD` unset on any deployment that cannot protect
`/admin` and its subpaths. The optional dashboard is served by the same gateway
listener, not a separate admin service. Before enabling it, restrict these
paths to operators and require HTTPS on their access path. Block the admin
paths on all public or shared ingress, and ensure clients cannot bypass those
controls by reaching the backend listener directly. The office override
publishes the shared listener on `GATEWAY_BIND:8791` over HTTP; that publish is
not a private HTTPS admin endpoint. A Docker host loopback publish also does
not restrict peers on a shared Docker network.

## Optional internal Codex route

`POST /codex` is an explicit opt-in for p-agent and agent-mem. It uses
dedicated gateway keys and a separate worker, without changing existing
routes or default Docker Compose. See the [operator runbook](docs/codex.md).

## Alerting

Three failures stop ingestion with no visible error at the caller, so each posts
to Slack (deduped per window):

| Signal | Meaning |
|---|---|
| `RateLimitInfo.status == "rejected"` | Seat quota spent |
| `status == "allowed_warning"` and utilization ≥ `LLM_GATEWAY_QUOTA_WARN_PCT` (95) | Approaching the cap |
| `AssistantMessage.error == "authentication_failed"` | Seat auth rejected (expired or revoked token, or a transient Anthropic-side rejection). Posted only after `LLM_GATEWAY_CLAUDE_AUTH_ALERT_AFTER_S` (600 s) of failures with no success in between; a recovery message follows when a call gets past auth again in the same process. |
| OpenRouter `402`, or `403` containing `Key limit exceeded` | Monthly cap spent. On `/embeddings` with `GEMINI_API_KEY` set, embeddings retry directly through Gemini and the alert is a warning posted once per cap window when OpenRouter supplies a usable reset time and Slack accepts the post; otherwise it repeats every 30 min. On every other path, or without the key, the alert is critical every 30 min. |
| Gemini embedding fallback error | Both embedding providers failed; embeddings are not being stored |
| OpenRouter `429`: on `/embeddings` after 3 retries (1 s, 2 s, 4 s, or `Retry-After` capped at 10 s); generate and describe do not retry | That call failed. One is noise; many `OpenRouter 429` log lines mean a caller is sending too fast |

Seat quotas are per-window and per-family: `five_hour`, `seven_day`,
`seven_day_opus`, `seven_day_sonnet`, `overage`. The alert names which tripped.

Quota alerts with a usable reset time post once per window: after Slack accepts the post, that alert stays muted until the reset (persisted to `LLM_GATEWAY_ALERT_STATE_FILE`, so it survives restarts); other alerts dedupe for `LLM_GATEWAY_ALERT_DEDUPE_S`. A failed post retries after that interval. After a deploy, prove the mute survives a container recreate with `deploy/alert_selftest.py` (usage in its docstring; it uses a separate self-test state file and posts nothing).

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
env -u ANTHROPIC_API_KEY .venv/bin/uvicorn app.main:app --port 8750 --no-access-log
```

Your Mac is already logged into Claude Code, so the seat path works immediately.

Keep `--no-access-log` on custom Uvicorn launches too. The gateway logs method,
path, status and latency without query strings. Any reverse proxy in front
must also omit query strings from access logs.

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

*Rotating the Claude token.* Run `deploy/regen-token.sh` from the checkout that
owns the running container (on the Mac mini, `~/src/github.com/llm-gateway`), or
`pbpaste | deploy/regen-token.sh` with a token already on the clipboard. It
swaps the token in `config/.env`, restarts, checks a real Claude answer, and
rolls back on failure. Do not run it in a recorded or shared terminal.

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

## Historical checks

Verified live against the seat on 2026-07-31:

- Subscription auth works — `RateLimitEvent type=five_hour status=allowed`, no API key involved
- Structured output works — `output_format` json_schema returns a parsed object
- Vision works — image reaches the model through the streaming-input path
- Backend toggle works — flipping `BACKEND_CHEAP` reroutes to `gemini-2.5-flash`
- Embeddings work — 3072-dim vectors, cosine 0.49 on an unrelated pair
- Harness overhead: **~700–800 input tokens per call** with tools stripped and
  settings disabled — roughly +38% on a 1.8k-token transcript
