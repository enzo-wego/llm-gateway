# Dockerize llm-gateway

## Context

`llm-gateway` runs natively today: uvicorn under systemd on the VPS
(`deploy/llm-gateway.service`), and a bare uvicorn process on the author's Mac.
The README states it "runs native, not in Docker — it needs the host's Claude
CLI login". That reason is now stale: `CLAUDE_CODE_OAUTH_TOKEN` (from
`claude setup-token`) is a headless credential passed by environment variable,
and the VPS `.env` already uses it. Nothing about the Claude path requires a
host login any more.

The goal is to run the service as a Docker container in both places, without
losing three properties that work today:

1. **Claude subscription-seat auth** — calls must bill the seat, never an API key.
2. **Live config updates** — `PUT /config` must keep applying immediately *and*
   persisting, because agent-mem is getting a config GUI that drives it.
3. **agent-mem reachability** — the worker container must be able to reach the
   gateway.

### Established facts (verified 2026-08-05, do not re-litigate)

- **The bundled CLI is a self-contained binary, not a Node script.**
  `claude_agent_sdk/_bundled/claude` is a ~257 MB compiled executable
  (`Mach-O arm64` on the Mac). **No Node runtime is needed in the image.**
  It is platform-specific, so dependencies MUST be `pip install`ed inside the
  image for the target platform. Never `COPY .venv` and never bind-mount host
  `site-packages` — that would ship the Mac binary into Linux.
- **`PUT /config` needs no restart.** `config.update_config()` calls
  `_rewrite_env()` (persist) and then `globals().update(validated)` (apply
  live). Restart only re-reads the persisted file.
- **The `.env` mount is the one real Docker hazard.** `_rewrite_env` finishes
  with `os.replace(tmp, path)`. Renaming *onto* a single-file bind mount fails
  with `EBUSY` because the target is a mount point. It fails closed and loud
  (HTTP 500, live value untouched), not silently — but `PUT /config` would be
  dead in Docker while working natively.
- **No code change is required for that.** `config.py` already reads
  `ENV_FILE = Path(os.getenv("LLM_GATEWAY_ENV_FILE", ".env"))`. Point it into a
  bind-mounted *directory*.
- **VPS**: x86_64, Python 3.12.3, `enzo` is **uid 1001 / gid 1001** (not 1000).
  `agent-mem-worker-1` is on network `agent-mem_default`, gateway `172.18.0.1`.
  Port 8751 is free. A ufw rule already allows `172.18.0.0/16 → 8750/tcp`.
- **Nothing calls the gateway yet.** `agent-mem-worker-1` has no gateway URL in
  its environment. Cutover blast radius is zero, and no agent-mem change is in
  scope.

## Goal

Run llm-gateway as a Docker container on the Mac (arm64) and the VPS (x86_64),
with seat auth, live config, and container-to-container reachability all
verified working in both places.

## Non-goals

- No changes to `app/**`. This is packaging and deployment only. If you believe
  a source change is required, **stop and report** rather than making it.
- No changes to agent-mem (it does not call the gateway yet).
- Do not delete `deploy/llm-gateway.service` — it stays on disk as rollback.
- Do not remove the ufw rule. It becomes unnecessary but is harmless; leave it.

## Files to create / change

| File | Action |
|---|---|
| `Dockerfile` | new |
| `docker-compose.yml` | new — local (Mac) defaults |
| `docker-compose.vps.yml` | new — override adding the external network |
| `.dockerignore` | new |
| `config/.gitkeep` | new — the bind-mount source dir must exist in the repo |
| `README.md` | update the "VPS deployment" section |

`.gitignore` already contains a bare `.env` pattern, which matches `config/.env`
at any depth. Verify this with `git check-ignore -v config/.env` and only touch
`.gitignore` if that check fails.

## Approach

### Dockerfile

- Base `python:3.12-slim` (matches the VPS Python 3.12.3). No Node.
- `ARG APP_UID=1001` / `ARG APP_GID=1001`, create user `app` with those ids so
  the container can write to the host-owned `config/` bind mount on the VPS.
  Docker Desktop virtualizes ownership, so the Mac is unaffected by the value.
- `pip install --no-cache-dir -r requirements.txt` as a separate layer before
  copying `app/`, so dependency layers cache across source edits.
- `ENV HOME=/home/app`, directory created and owned by `app` — the CLI writes
  session state there, and the named volume inherits that ownership on first
  mount.
- `USER app`, `EXPOSE 8750`.
- `CMD` runs uvicorn on `0.0.0.0:8750`. Bind `0.0.0.0` *inside* the container;
  host exposure is controlled by the compose port publish, not by the app.
- `HEALTHCHECK` via a Python one-liner against `/health` (the only route not
  requiring `X-API-Key`) — do not install `curl` just for this.

### Compose — local (`docker-compose.yml`)

- `env_file: ./config/.env`
- `environment:` sets `LLM_GATEWAY_ENV_FILE=/config/.env` and
  `LLM_GATEWAY_HOST=0.0.0.0`.
- `volumes:` `./config:/config` (**directory**, never `./config/.env:/config/.env`)
  and named volume `claude-home:/home/app`.
- `ports: "127.0.0.1:8750:8750"` — loopback only, never `0.0.0.0`.
- `mem_limit: 2g` to match `MemoryMax=2G` in the systemd unit.
- `restart: unless-stopped`.
- Do **not** pass `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` through. Do not
  use bare `environment:` passthrough of host variables anywhere in the file.

### Compose — VPS override (`docker-compose.vps.yml`)

- Attaches the service to the pre-existing external network `agent-mem_default`
  so the worker resolves the gateway by service DNS name `llm-gateway:8750`.
  This removes the need for the `172.18.0.1` bridge binding.
- Port publish `127.0.0.1:8751:8750` during side-by-side verification, changed
  to `127.0.0.1:8750:8750` at cutover (systemd holds 8750 until then).

```yaml
networks:
  agent-mem_default:
    external: true
```

Used as `docker compose -f docker-compose.yml -f docker-compose.vps.yml up -d`.

### README

Replace the "Runs **native, not in Docker** — it needs the host's Claude CLI
login" claim and the surrounding deploy steps with the Docker flow. Keep the
three hard-won gotchas but reframe them: gotcha 1 (OAuth token must be in the
env file, not a shell rc) still applies; gotchas 2 and 3 (bridge-IP binding and
the ufw rule) no longer apply once the gateway is on `agent-mem_default` —
say so explicitly rather than deleting them, and add the `.env`-must-be-a-
directory-mount hazard as a new gotcha.

## Acceptance criteria

1. `docker compose up -d` on the Mac yields a healthy container; `GET /health`
   returns `"ok": true`.
2. A real `POST /generate` through the container returns text and increments
   the Claude counters in `/health` — proving the bundled Linux binary runs and
   the seat is authenticated. `/health` must show `last_rate_limit.type` set
   with no API-key involvement.
3. `PUT /config` returns 200, the change is visible in `GET /config`
   immediately with **no restart**, and the new value is present in the host
   file `config/.env`.
4. After `docker compose restart`, `GET /config` still shows the updated value
   (persistence survived).
5. On the VPS, a container on `agent-mem_default` reaches the gateway by name.
6. The systemd unit is stopped and disabled, the container serves 8750, and
   `/health` is good.

## Verification

### Local (Mac, arm64)

The native dev server currently holds port 8750 (`uvicorn app.main:app --host
0.0.0.0 --port 8750`). Stop that process first.

Seed config — the token is reused from the VPS by explicit decision:

```bash
mkdir -p config && cp .env.example config/.env
ssh enzo@enzogo.io.vn 'grep -E "CLAUDE_CODE_OAUTH_TOKEN|OPENROUTER_API_KEY|LLM_GATEWAY_API_KEY" \
  /var/go/src/github.com/llm-gateway/.env'
# paste those three values into config/.env, then:
chmod 600 config/.env
```

```bash
docker compose up -d --build
curl -s http://127.0.0.1:8750/health | jq '.ok, .seat'

# 2 — real seat call
curl -s -X POST http://127.0.0.1:8750/generate \
  -H "X-API-Key: $KEY" -H 'content-type: application/json' \
  -d '{"system":"Reply with one word.","user":"Say OK","tier":"cheap"}'

# 3 — live config, no restart
curl -s -X PUT http://127.0.0.1:8750/config \
  -H "X-API-Key: $KEY" -H 'content-type: application/json' \
  -d '{"EFFORT_CHEAP":"medium"}'
curl -s http://127.0.0.1:8750/config -H "X-API-Key: $KEY" | jq .EFFORT_CHEAP
grep EFFORT_CHEAP config/.env          # must read medium on the HOST file

# 4 — persistence
docker compose restart && sleep 5
curl -s http://127.0.0.1:8750/config -H "X-API-Key: $KEY" | jq .EFFORT_CHEAP
```

Step 3 is the one that proves the `os.replace` hazard is actually avoided. A
500 `"failed to persist gateway configuration"` means the mount is wrong —
almost certainly a file mount where a directory mount was required.

Also run the existing suite against the source tree: `.venv/bin/pytest -q`.

### VPS (x86_64) — side-by-side, then cut over

Build on the VPS so pip resolves the linux-x86_64 wheel natively.

```bash
ssh enzo@enzogo.io.vn
cd /var/go/src/github.com/llm-gateway && git pull
mkdir -p config && cp .env config/.env && chmod 600 config/.env
ls -n config          # confirm 1001:1001

# systemd still owns 8750; container comes up on 8751
docker compose -f docker-compose.yml -f docker-compose.vps.yml up -d --build
curl -s http://127.0.0.1:8751/health | jq '.ok, .seat'
```

Repeat acceptance checks 2–4 against `:8751`, then prove the path that actually
matters — reachability by service name from inside the worker's network:

```bash
docker run --rm --network agent-mem_default alpine:3 \
  wget -qO- http://llm-gateway:8750/health
```

Note the port is **8750** here: that is the container's internal port on the
shared network, independent of the 8751 host publish.

Only once all of the above pass:

```bash
sudo systemctl disable --now llm-gateway
# edit the port publish in docker-compose.vps.yml to 127.0.0.1:8750:8750
docker compose -f docker-compose.yml -f docker-compose.vps.yml up -d
curl -s http://127.0.0.1:8750/health | jq '.ok, .seat'
```

Leave the unit file installed and the `.venv` in place — `sudo systemctl enable
--now llm-gateway` is then a one-command rollback.

## Rollback

`docker compose down` and `sudo systemctl enable --now llm-gateway`. Nothing
consumes the gateway yet, so a failed cutover has no downstream effect.
