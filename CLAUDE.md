# llm-gateway: notes for coding agents

HTTP gateway in front of a Claude subscription seat (bundled CLI via
`claude-agent-sdk`) and OpenRouter. See README.md for endpoints and design.

## Where it runs

- The live container `llm-gateway-llm-gateway-1` on the Mac mini is built from
  `~/src/github.com/llm-gateway`. That checkout holds `config/.env`.
- `~/go/src/github.com/llm-gateway` is the development checkout. It has no
  `config/.env`, so `docker compose up` and `deploy/regen-token.sh` refuse to
  run there. Confirm with
  `docker inspect llm-gateway-llm-gateway-1 --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'`.
- Deploying a merged change: `git pull --ff-only` in the live checkout, then
  `docker compose up -d --build` only if files under `app/`, `Dockerfile`,
  `requirements.txt` or `docker-entrypoint.sh` changed.

## Rotating the Claude seat token

The seat is authenticated by `CLAUDE_CODE_OAUTH_TOKEN` in `config/.env`, a
long-lived token from `claude setup-token`. Rotate it with
`deploy/regen-token.sh`, never by editing `config/.env` by hand. `PUT /config`
cannot set this key.

`claude setup-token` needs a browser login, so an agent cannot run the rotation
itself. Ask the human to run, in their own terminal:

```
cd ~/src/github.com/llm-gateway && deploy/regen-token.sh
```

or, with a token already on the clipboard,
`cd ~/src/github.com/llm-gateway && pbpaste | deploy/regen-token.sh`.

The script writes the token, restarts the container, and requires a
`POST /generate` answer with `backend == "claude"`. On any failure or Ctrl-C it
restores the previous token. Its last line says what happened:

| Last line | Meaning |
|---|---|
| `token rotated (…XXXX), Claude answered on the cheap tier.` | done |
| `new token failed: <step>. Old token restored, service healthy.` | new token rejected; nothing to clean up |
| `new token failed: <step>. Old token restored, but restart/health failed.` | check `docker compose logs llm-gateway` |
| `new token failed: <step>. Could not restore the previous token state.` | inspect `config/.env` by hand, then run again |
| `the running container belongs to <dir>; run this script from there` | wrong checkout |
| `another rotation is running, or a killed run left config/.regen-token.lock` | remove the lock if nothing is running |

A `got an OpenRouter answer` failure means the seat did not answer (bad token,
quota exhausted) or the cheap tier is not set to `claude`.

### Checking a rotation after the fact

Run these from `~/src/github.com/llm-gateway`. Never print the token itself.

1. Container restarted and healthy:
   `docker inspect llm-gateway-llm-gateway-1 --format '{{.State.StartedAt}} {{.State.Health.Status}}'`
2. Seat is answering: `docker compose logs --since 30m llm-gateway | grep 'backend=claude' | tail -3`
   shows recent `status=200` lines.
3. Which Claude organization the token belongs to. The token has inference
   scope only, so `/api/oauth/profile` returns 403 (`user:profile` missing).
   Read the organization id from a free `count_tokens` call instead, from
   inside the container so the token never leaves it:

   ```
   docker compose exec -T llm-gateway python -c '
   import os, json, urllib.request
   body = json.dumps({"model":"claude-haiku-4-5","messages":[{"role":"user","content":"hi"}]}).encode()
   req = urllib.request.Request("https://api.anthropic.com/v1/messages/count_tokens", data=body,
       headers={"Authorization": "Bearer " + os.environ["CLAUDE_CODE_OAUTH_TOKEN"],
                "anthropic-beta": "oauth-2025-04-20", "anthropic-version": "2023-06-01",
                "content-type": "application/json"})
   try: h = urllib.request.urlopen(req, timeout=20).headers
   except urllib.error.HTTPError as e: h = e.headers
   print(h.get("anthropic-organization-id"))'
   ```

   Compare it with `organizationUuid` and `organizationName` under
   `oauthAccount` in `~/.claude.json` on the host. A match names the
   organization (on 2026-10-20 it was "Wego #3",
   `9d2d54bd-cdbb-4b6a-84ea-cafeb7a08a60`). This proves the organization, not
   which user in it created the token.

### Changing the script

`deploy/test-regen-token.sh` runs every case against stubbed `docker`, `mv`
and `sleep` with dummy tokens, and never touches `config/.env` or a real
container. Run it after any change to `deploy/regen-token.sh`. Both scripts must
work under macOS `/bin/bash` 3.2.
