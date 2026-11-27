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

## How model selection works

A **backend** is the integration that handles a request (for example, Claude
or OpenRouter). A **model ID** or **alias** selects a model in that integration.
A **tier** (`cheap` or `summary`) is a `/generate` request field that selects
a configured default backend, model, and effort; it is not a model ID and is
not accepted on `/codex` or `/gemini/*`.

| Route | Where the model and effort are selected | Defaults when omitted | Fallback and override behavior |
|---|---|---|---|
| `/generate` | Server maps `tier` to configured backend, model, and effort; optional per-request `model` and/or `effort` (`low`, `medium`, `high`, `xhigh`, `max`) override the Claude settings | `tier`: `cheap` | With no per-request `model`/`effort`, a configured Claude to OpenRouter fallback can change the backend and model. Setting non-null `model` or `effort` pins Claude (omitting the tier's `thinking` setting), answers `422` if OpenRouter is primary, and answers `503` if the seat is blocked or quota-exhausted. |
| `/describe` | Uses `BACKEND_DESCRIBE` (`MODEL_CHEAP`/`EFFORT_CHEAP` on Claude, `OR_MODEL_DESCRIBE` on OpenRouter); optional per-request `model` and/or `effort` (`low`, `medium`, `high`, `xhigh`, `max`) override the Claude settings | Configured describe settings | Same as `/generate`: setting non-null `model` or `effort` pins Claude (omitting `THINKING_CHEAP`), answers `422` if OpenRouter is primary, and answers `503` if the seat is blocked or quota-exhausted. |
| `/gemini/v1beta/models/{model}:{action}` | Caller names the Google model in the URL; for `generateContent` and `streamGenerateContent`, pass effort in the native request body or via optional `?effort=` (`low`, `medium`, `high`) | URL model is required; native body or provider default when `?effort=` is omitted | Pool failover changes the **API key**, never the requested model or action. Non-generation actions (`countTokens`, `embedContent`, `batchEmbedContents`) forward unchanged. |
| `/codex` | Caller passes optional `model` and/or `effort` (`low`, `medium`, `high`, `xhigh`) in the JSON body (legacy `tier` is rejected with `422`, even if `null`) | `model`: `gpt-6.2-sol`, `effort`: `medium` (independent defaults) | No provider fallback. `meta.model` and `meta.effort` report the requested settings, not provider confirmation. |

### `/generate` and `/describe`: tier routing or explicit Claude overrides

For a legacy tier-routed request (where both `model` and `effort` are omitted
or `null`), such as `{"tier":"summary","user":"Summarize this incident."}`,
the gateway:

1. Reads `BACKEND_SUMMARY`. If it is `claude`, it passes `MODEL_SUMMARY`,
   `EFFORT_SUMMARY`, and the tier's explicit thinking configuration
   (`THINKING_SUMMARY`, default `adaptive`) to the Claude SDK. If it is
   `openrouter`, it sends `OR_MODEL_SUMMARY` to OpenRouter instead. `cheap`
   and `/describe` follow the same path using `_CHEAP` settings (passing
   `THINKING_CHEAP`, default `disabled`, on the legacy Claude path). Claude
   effort and thinking settings are not forwarded to OpenRouter.
2. If a legacy tier-routed Claude call fails with a quota or adapter error,
   `FALLBACK_ON_QUOTA` controls that request: `true` tries the route's
   OpenRouter model; `false` returns the error (`503` on quota exhaustion,
   `502` on a Claude adapter error). A **subsequent** legacy request behaves
   differently if a prior quota rejection has already marked the seat
   unavailable: the handler skips Claude and goes straight to OpenRouter even
   when `FALLBACK_ON_QUOTA` is `false`. Selecting OpenRouter as primary does
   not set up a reverse fallback to Claude.
3. If the caller passes a non-null per-request `model` (`sonnet`, `opus`,
   `haiku`, `fable`, `best`, or `claude-<family-id>`, each with an optional
   `[1m]` suffix) and/or `effort` (`low`, `medium`, `high`, `xhigh`, `max`) on
   `/generate` or `/describe`, the request **pins the Claude seat**:
   - Any omitted `model` or `effort` field still inherits its default from the
     selected tier on `/generate` (or `MODEL_CHEAP` / `EFFORT_CHEAP` on
     `/describe`), and `meta.model` and `meta.effort` are included in the
     response.
   - **Thinking differs from the legacy path:** instead of passing the tier's
     configured `THINKING_SUMMARY` or `THINKING_CHEAP` (`thinking={"type": ...}`),
     an override request passes `thinking=None` (omitting `thinking`) so the
     Claude CLI uses its model-specific thinking default with the requested
     `effort` (avoiding a `400` when overriding a `THINKING_CHEAP=disabled`
     route onto a model that requires adaptive thinking).
   - **No silent OpenRouter routing or fallback:** if the route's configured
     primary backend (`BACKEND_SUMMARY`, `BACKEND_CHEAP`, or `BACKEND_DESCRIBE`)
     is `openrouter`, the gateway rejects the override before calling any
     provider with HTTP `422` (`"Claude model and effort overrides are unavailable when OpenRouter is primary"`).
     If the Claude seat is currently blocked by a prior quota rejection, or if
     the call itself hits `QuotaExhausted`, the gateway returns HTTP `503` even
     when `FALLBACK_ON_QUOTA=true`. A non-quota `ClaudeError` on an override
     returns HTTP `502`.
4. Returns `backend` for the integration that actually answered. `meta.model`
   on a successful Claude generation is the model identifier the adapter
   requested; if that identifier is an alias, it does not prove the exact
   provider model version. Adding `schema` changes the answer from `text` to
   parsed `output`, not the backend or model selection.

**Mac mini snapshot (November 27, 2026).** These are the live deployment settings
observed on the Mac mini via `/health` and `/config` on that date (distinguishing
what was running live on the host from the per-request override capabilities on
this branch), not promises about every installation:

| Tier | Primary backend | Claude model / effort | Configured OpenRouter alternative |
|---|---|---|---|
| `cheap` (default) | `claude` | `haiku` / `low` | `~anthropic/claude-haiku-latest` |
| `summary` | `claude` | `sonnet` / `medium` | `~anthropic/claude-sonnet-latest` |

`FALLBACK_ON_QUOTA` was **false** on this mini even though its code default
is `true`. The OpenRouter entries above are configured alternatives, not the
primary models. An operator can change the tier mapping without changing
callers. Callers that want operator-managed routing and OpenRouter fallback
should send `tier` only; callers that send `model` or `effort` on this branch
explicitly pin Claude for that request.

### `/gemini`: URL model plus native body or optional `?effort=`

In `/gemini/v1beta/models/gemini-2.5-flash:generateContent`, the model is
`gemini-2.5-flash` and the action is `generateContent`. The gateway selects
a pooled key and forwards the request to that exact Google model and action.
If that key is rate-limited or rejected, the proxy can try another pooled key;
it never switches models, reinterprets the body as a gateway tier, or falls
back to OpenRouter. To choose another model, change the **URL** to a model ID
that supports the action and is available to your pooled keys.

For `generateContent` and `streamGenerateContent` (including `?alt=sse`), you
can control reasoning effort either in the native Google JSON payload (under
`generationConfig.thinkingConfig`) or with the optional `?effort=low|medium|high`
query shortcut (combinable with `alt=sse` as `?alt=sse&effort=low`). When
`?effort=` is omitted, the gateway forwards the request body bytes unchanged.
When `?effort=` is present, the gateway merges the translated setting into
`generationConfig.thinkingConfig` (preserving an existing boolean
`includeThoughts` / `include_thoughts` field) according to this gateway policy
table:

| Exact URL `{model}` ID | `?effort=` translation in `thinkingConfig` | `low` | `medium` | `high` |
|---|---|---|---|---|
| `gemini-2.5-flash` | `thinkingBudget` (gateway policy token budget) | `256` | `4096` | `16384` |
| `gemini-2.5-flash-lite` | `thinkingBudget` (gateway policy token budget) | `512` | `4096` | `16384` |
| `gemini-2.5-pro` | `thinkingBudget` (gateway policy token budget) | `1024` | `8192` | `24576` |
| `gemini-3-pro-preview` | `thinkingLevel` | `"LOW"` | Rejected (`400 unsupported_effort_level`) | `"HIGH"` |
| `gemini-3-flash-preview`, `gemini-3.1-pro-preview`, `gemini-3.1-pro-preview-customtools`, `gemini-3.1-flash-lite`, `gemini-3.5-flash-lite`, `gemini-3.6-flash`, `gemini-3.8-flash` | `thinkingLevel` | `"LOW"` | `"MEDIUM"` | `"HIGH"` |

The `thinkingBudget` integers above are gateway policy mappings for the
`?effort=` shortcut, not Google-mandated constants; pass native
`thinkingConfig` fields directly (without `?effort=`) when you need a custom
budget or use another model ID. Model matching for `?effort=` is exact (aliases
and unlisted variants do not inherit table entries). The gateway returns HTTP
`400` before contacting upstream if `?effort=` is used on a non-generation
action (`unsupported_effort_action`), an unlisted model
(`unsupported_effort_model`), an unsupported level such as `medium` on
`gemini-3-pro-preview` (`unsupported_effort_level`), or when the JSON body
already contains `thinkingBudget` / `thinking_budget` / `thinkingLevel` /
`thinking_level` (`conflicting_thinking_config`). For the incident summary
example, send the native payload shape
`{"contents":[{"parts":[{"text":"Summarize this incident."}]}]}` to
`:generateContent` or `:streamGenerateContent?alt=sse`.

Non-generation actions (`:countTokens`, `:embedContent`, `:batchEmbedContents`)
are unchanged: keep the model ID in the URL, swap the action suffix, and send
the native request body. The response is always Google's native payload
(such as `candidates` on generation, SSE `data:` chunks on
`streamGenerateContent?alt=sse`, or `totalTokens` on `countTokens`) without a
gateway `meta` wrapper. The separate `/embed` route does not use this
URL-based model selection or the pooled credentials.

### `/codex`: explicit optional `model` and `effort`

Send `{"user":"Summarize this incident."}` (with optional `model` and `effort`)
to `/codex` using your project's **dedicated** `X-API-Key`. The gateway
authenticates the project and forwards the validated request over a private
Unix socket to the worker. `model` and `effort` have independent defaults:

| Field | When omitted | Supported values |
|---|---|---|
| `model` | `gpt-6.2-sol` | Explicit Codex model ID (default `gpt-6.2-sol`) |
| `effort` | `medium` | `low`, `medium`, `high`, `xhigh` |

Legacy `tier` requests are rejected on `/codex` with HTTP `422`, even when
sent as `"tier": null`. Migrate existing callers from `"tier":"cheap"` to
`"effort":"low"`, and from `"tier":"summary"` to `"effort":"medium"` (or omit
`effort` to use the `medium` default). Because `model` and `effort` are
independent, you can pass either field alone or both together.

The worker passes `--ignore-user-config` and runs `codex exec` with the
requested `--model` and `model_reasoning_effort`. A successful reply returns
`backend: "codex"`, `text` or parsed `output`, and `meta` containing
`meta.model` and `meta.effort`. Those `meta` fields echo the settings requested
by the caller (or the gateway defaults), not upstream provider confirmation of
the executed model or reasoning effort.

### Check your deployment without calling a model

```sh
GW="${GW:-http://127.0.0.1:8750}"
curl -fsS "$GW/health" | jq '{backends, models}'
curl -fsS "$GW/config" -H "X-API-Key: $LLM_GATEWAY_API_KEY" |
  jq '{BACKEND_CHEAP, MODEL_CHEAP, OR_MODEL_CHEAP, EFFORT_CHEAP,
       BACKEND_SUMMARY, MODEL_SUMMARY, OR_MODEL_SUMMARY, EFFORT_SUMMARY,
       FALLBACK_ON_QUOTA}'
```

`/health` shows the configured primary model for each core route, not which
provider served a particular request. For that, inspect the generation
response's `backend` and `meta`.

## Caller examples

Run these on the gateway host, or set `GW` to the gateway address reachable
from your application. Inside another container, `127.0.0.1` refers to that
container, not the gateway. Have your secret store supply `LLM_GATEWAY_API_KEY`
for the shared routes or `CODEX_PROJECT_KEY` for Codex. Clients do **not**
need a Claude login, an OpenRouter key, a Google key, or the pool encryption secret.

```sh
GW="${GW:-http://127.0.0.1:8750}"
curl -fsS "$GW/health"
```

**Claude or OpenRouter: generate (tier-routed or Claude-pinned).** Pick `cheap`
or `summary` when you want the server's configured backend and quota fallback.
Pass optional `model` and/or `effort` (`low`, `medium`, `high`, `xhigh`, `max`)
to pin the request to the Claude seat (omitting the tier's `thinking` mode,
returning `422` if OpenRouter is primary or `503` if the seat is unavailable,
with no silent OpenRouter fallback). Check `backend` and `meta` in the response
to see what answered. To get parsed `output` instead of `text`, pass a nonempty
`schema` as in the Codex example below.

```sh
# Tier-routed (uses configured backend for cheap, with fallback if enabled)
curl -fsS "$GW/generate" -H "X-API-Key: $LLM_GATEWAY_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"tier":"cheap","user":"Return a JSON object with a short greeting."}'

# Explicit override (pins Claude with model="sonnet" and effort="high", no OpenRouter fallback)
curl -fsS "$GW/generate" -H "X-API-Key: $LLM_GATEWAY_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"user":"Summarize the root cause.","model":"sonnet","effort":"high"}'
```

**Claude or OpenRouter: describe a file.** Supply the MIME type and raw
base64 (no `data:` prefix). Like `/generate`, optional `model` and/or `effort`
pin the request to Claude without OpenRouter fallback. This example uses `jq`
and a local `receipt.png`; for a PDF, change the filename and MIME type to
`application/pdf`.

```sh
base64 < receipt.png | tr -d '\r\n' | jq -Rs \
  '{mime:"image/png",data_b64:.,prompt:"Extract the total as JSON.",effort:"low"}' |
  curl -fsS "$GW/describe" -H "X-API-Key: $LLM_GATEWAY_API_KEY" \
    -H 'Content-Type: application/json' --data-binary @-
```

**OpenRouter: embed.** Set `dims` to the width of your vector index. Use
`"fallback":false` if a request must stay on OpenRouter. Otherwise the gateway
may use its separate `GEMINI_API_KEY` when OpenRouter hits its monthly cap;
this fallback does **not** draw from the Gemini proxy pool.

```sh
curl -fsS "$GW/embed" -H "X-API-Key: $LLM_GATEWAY_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"texts":["payment received","invoice overdue"],"dims":768,"fallback":false}'
```

**Gemini: native API (unary and SSE streaming).** Put the Google model ID in
the path and send Google's native JSON payload shape (`contents`, optional
`generationConfig`, and related fields). For `generateContent` and
`streamGenerateContent`, pass reasoning effort in the native body or via
optional `?effort=low|medium|high`; use `?alt=sse` (or `?alt=sse&effort=low`)
and `curl -N` for SSE streaming. The proxy also accepts `countTokens`,
`embedContent`, and `batchEmbedContents` unchanged. On `503`, `gemini_disabled`
means the [pool](#optional-gemini-key-pool) is off; `no_gemini_keys` means
it is enabled but has no key to try.

```sh
# Unary generateContent with optional ?effort=
curl -fsS "$GW/gemini/v1beta/models/gemini-2.5-flash:generateContent?effort=low" \
  -H "x-goog-api-key: $LLM_GATEWAY_API_KEY" -H 'Content-Type: application/json' \
  -d '{"contents":[{"parts":[{"text":"Reply with a short greeting."}]}]}'

# Streaming streamGenerateContent with alt=sse and optional effort
curl -fsSN "$GW/gemini/v1beta/models/gemini-2.5-flash:streamGenerateContent?alt=sse&effort=medium" \
  -H "x-goog-api-key: $LLM_GATEWAY_API_KEY" -H 'Content-Type: application/json' \
  -d '{"contents":[{"parts":[{"text":"Stream a two-sentence summary."}]}]}'
```

**Codex: internal projects only.** Use the secret issued to `p-agent` or
`agent-mem` as `CODEX_PROJECT_KEY`, **not** the shared gateway key. `model`
defaults to `gpt-6.2-sol` and `effort` (`low`, `medium`, `high`, `xhigh`)
defaults to `medium` independently; legacy `tier` is rejected (`422`), even
when `null`. The response returns `backend: "codex"`, `text` or parsed
`output`, and `meta.model` / `meta.effort` (which report the requested
settings, not provider confirmation). Codex does not fall back to another
provider. See the [Codex runbook](docs/codex.md) for provisioning, migration
from `tier`, and error responses.

```sh
curl -fsS --max-time 200 "$GW/codex" -H "X-API-Key: $CODEX_PROJECT_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"user":"Report status OK.","model":"gpt-6.2-sol","effort":"low","schema":{"type":"object","properties":{"status":{"type":"string","enum":["OK"]}},"required":["status"],"additionalProperties":false}}'
```

## Endpoints

`/generate`, `/describe`, `/embed`, `/usage`, and `/config` require the shared
`X-API-Key: $LLM_GATEWAY_API_KEY`; `/health` is unauthenticated. `/gemini/*`
accepts `$LLM_GATEWAY_API_KEY` in `x-goog-api-key` (or `X-API-Key`), while
`/codex` requires the calling project's dedicated `CODEX_P_AGENT_KEY` or
`CODEX_AGENT_MEM_KEY` in `X-API-Key` (and rejects the shared gateway key).

| Route | Backend | Body |
|---|---|---|
| `POST /generate` | seat or OpenRouter (pinned to seat if `model` or `effort` is set; `422` if OpenRouter is primary, `503` if seat unavailable) | `{system?, user, tier?: "summary"\|"cheap", model?, effort?: "low"\|"medium"\|"high"\|"xhigh"\|"max", schema?}`; default tier: `cheap` |
| `POST /describe` | seat or OpenRouter (pinned to seat if `model` or `effort` is set; `422` if OpenRouter is primary, `503` if seat unavailable) | `{system?, prompt, mime, data_b64, model?, effort?: "low"\|"medium"\|"high"\|"xhigh"\|"max", schema?}` |
| `POST /embed` | OpenRouter, then Gemini on cap exhaustion | `{texts: [...], dims, fallback?}` (`fallback` defaults to `true`; `false` disables Gemini fallback for that request) |
| `POST /gemini/v1beta/models/{model}:{action}` | Gemini key pool | Native Google JSON body (`{contents: [...], ...}`); optional `?effort=low\|medium\|high` shortcut on `generateContent` and `streamGenerateContent` (plus `?alt=sse` for SSE) |
| `POST /codex` | Dedicated Codex worker | `{system?, user, model?, effort?: "low"\|"medium"\|"high"\|"xhigh", schema?}`; independent defaults `model: "gpt-6.2-sol"`, `effort: "medium"` (`tier` rejected, even `null`) |
| `GET /config` | - | current editable, non-secret runtime configuration |
| `PUT /config` | - | partial configuration update, applied live and persisted to `.env` |
| `GET /health` | - | gateway liveness, quota state, counters, active backends, and optional pool readiness |
| `GET /usage` | - | seat and OpenRouter budget, plus per-caller `callers` counters |

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
object. Omit it and you get raw `text` back. This holds on **both** backends,
whereas earlier versions of the OpenRouter path accepted `schema` and ignored
it, so a backend switch or a quota fallback changed the response shape with
nothing in the response saying so.

On `/generate`, callers can send `tier` alone to use the operator-configured
backend and tier thinking mode (`THINKING_SUMMARY` or `THINKING_CHEAP`), or
pass non-null `model` and/or `effort` on `/generate` or `/describe` to pin the
Claude seat and let the Claude CLI use its model-specific thinking default.
The native Gemini route requires the Google model ID in the URL (with native
`thinkingConfig` or the optional `?effort=` shortcut on generation and
streaming), while `/codex` accepts optional `model` and `effort` in the JSON
body.

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
server side with no rebuild. On the legacy tier path (no per-request `model` or
`effort` override), a new tier model that rejects the configured `THINKING_*`
mode fails with a 400 until the pin or the mode changes; per-request Claude
overrides omit `THINKING_*` and defer to the CLI's model-specific thinking
default.

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
