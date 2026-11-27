# Internal Codex operator runbook

`POST /codex` is opt-in for p-agent and agent-mem. It does not change
`/generate`, the backend switches, or the default Compose deployment. The
gateway authenticates requests and forwards the JSON (not the headers) to
the worker's `POST /generate` over `/run/codex-worker/worker.sock`.

## Enable and provision

1. In the **gateway's** `config/.env`, set `CODEX_P_AGENT_KEY` and
   `CODEX_AGENT_MEM_KEY` to independently generated random secrets, different
   from `LLM_GATEWAY_API_KEY` (for example, generate each with `openssl rand
   -hex 32`). Set only the key for each project you intend to enable. Give each
   project only its own key through its secret store. Never put these keys in
   the worker service. `X-LLM-Caller` does not grant access or select a project.
2. For this deployment, layer the Codex file after the normal Compose file.
   Keep any existing site-specific override(s) in the invocation.

   ```sh
   CODEX_WORKER_EXECUTABLE= \
     docker compose -f docker-compose.yml -f docker-compose.codex.yml up -d --build
   ```

   This starts the worker but leaves execution disabled. `CODEX_WORKER_EXECUTABLE`
   must be an explicitly selected absolute path; unset or empty fails closed.
   The image does not select an executable just because the CLI is installed.

   The worker installs Codex CLI `0.166.0` for the image's Linux architecture
   and publishes no ports. It runs as UID/GID 1001, matching the default
   gateway; keep that match if customizing the gateway image. The socket
   directory is owner-only (`0700`) and mounted read-only on the gateway.
   The worker's separate `CODEX_HOME` volume persists across recreations;
   the gateway cannot mount it through this override. Do not add a gateway
   config, host home, repository or Docker socket mount to the worker.
3. Provision **fresh, authorized** credentials. Use a distinct provider
   service identity and token for this worker (for example a scoped project
   service-account API key), or sign in with a dedicated, approved workspace
   identity if policy permits. Do not copy `auth.json` or any other state
   from an operator's personal or host Codex installation.

   Use your secret manager's output as stdin for an API key (no key in the
   command line, Compose file or gateway environment):

   ```sh
   docker compose -f docker-compose.yml -f docker-compose.codex.yml \
     exec -T codex-worker codex -c 'cli_auth_credentials_store="file"' login --with-api-key
   ```

   For an approved workspace sign-in, run inside the worker and follow the
   device sign-in instructions if workspace policy enables it:

   ```sh
   docker compose -f docker-compose.yml -f docker-compose.codex.yml \
     exec codex-worker codex -c 'cli_auth_credentials_store="file"' login --device-auth
   ```

   Check the identity during sign-in. Treat the worker volume as secret
   storage. To rotate credentials, revoke the old credential at the provider
   and provision its replacement inside this worker. Do not use `down -v`
   for routine restarts: it deletes named volumes, including credentials.

4. **Only if policy permits the stock CLI**, explicitly select it and
   recreate the worker:

   ```sh
   CODEX_WORKER_EXECUTABLE=/usr/local/bin/codex \
     docker compose -f docker-compose.yml -f docker-compose.codex.yml \
     up -d --no-deps codex-worker
   ```

   Keep any site-specific overrides in this command as well. The optional
   Compose file passes this setting only to `codex-worker`. Do not put it
   in the gateway's `config/.env`. To disable execution again, rerun the
   worker command with `CODEX_WORKER_EXECUTABLE=` explicitly.

   The stock CLI is not tool-free. The private socket and container settings
   isolate the worker from gateway files and credentials; they do not
   establish a zero-tool execution guarantee. Leave execution disabled if
   policy requires that guarantee and no approved executable is available.

## Client opt-in and usage

For each existing client, explicitly configure the gateway base URL,
`POST /codex`, and that client's dedicated secret as the `X-API-Key` header.
Do not send the general gateway key or the worker's provider credential.
Clients not opted in should keep their current route and authentication.
Use a client deadline longer than the gateway's 180-second worker deadline.

Example request (assuming `CODEX_PROJECT_KEY` has been injected locally by
the project's secret manager):

```sh
curl -sS --max-time 200 \
  -H "X-API-Key: ${CODEX_PROJECT_KEY:?set the dedicated project key}" \
  -H 'Content-Type: application/json' \
  --data '{"user":"Summarize: the release is postponed.","tier":"cheap"}' \
  http://127.0.0.1:8750/codex
```

| JSON field | Meaning |
| --- | --- |
| `user` | Required prompt string |
| `system` | Optional instruction string |
| `tier` | Optional `cheap` or `summary` |
| `schema` | Optional JSON Schema object for structured output |

Without `schema`, read `text` from the 200 JSON response:
`{"backend":"codex","text":"The release was postponed.","meta":{}}`.
With `schema`, read `output` as JSON rather than decoding a string:
`{"backend":"codex","output":{"summary":"The release was postponed."},"meta":{}}`.
Use reported usage in `meta` when available; absent usage does not mean zero.

| HTTP status | Meaning |
| --- | --- |
| `401` | Missing, invalid, shared, or ambiguous `X-API-Key`; check the project's dedicated key |
| `400` / `408` | Incomplete request or timed out while reading the body |
| `413` / `422` | Request exceeds 1 MiB or fails JSON/payload validation |
| `429` | Worker is busy; back off before retrying |
| `503` | Worker execution is disabled or the socket path is not configured |
| `502` | Cannot reach the worker, or the worker returned an invalid response or upstream error; check worker startup, socket ownership, and worker auth |
| `504` | Worker deadline exceeded |

Errors have a `detail` field. The gateway does not fall back to another
provider or automatically retry `/codex`. Do not automatically retry a timed
out request without considering duplicate work and provider usage.
