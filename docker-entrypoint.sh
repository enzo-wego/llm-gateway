#!/bin/sh
# Load the persisted env file into the process environment on EVERY container
# start, then exec the CMD.
#
# Why this exists: the app reads its runtime config from os.environ only
# (app/config.py: "Values start from the process environment"). Compose's
# `env_file:` snapshots those values once, at container CREATE time —
# `docker compose restart` reuses the container and does NOT re-read the file.
# So without this, `PUT /config` would persist a change to config/.env but a
# restart would silently revert to the stale create-time values.
#
# This is the Docker equivalent of `EnvironmentFile=` in the systemd unit
# (deploy/llm-gateway.service), which re-reads the file on every start. Making
# the persisted file the source of truth on restart is what keeps live-config
# behaviour identical to the native deployment. Packaging only — no app change.
set -e

env_file="${LLM_GATEWAY_ENV_FILE:-/config/.env}"
if [ -f "$env_file" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    # Skip blank lines and comments.
    case "$line" in ''|'#'*) continue ;; esac
    # Require a KEY=VALUE shape; ignore anything else.
    case "$line" in *=*) ;; *) continue ;; esac
    key=${line%%=*}
    key=${key#export }                       # tolerate `export KEY=...`
    key=$(printf '%s' "$key" | tr -d '[:space:]')
    val=${line#*=}                           # value taken verbatim (no shell expansion)
    # Belt-and-braces, mirroring the unit's UnsetEnvironment: never let the
    # API-key auth trap in through the env file. config.py also pops these.
    case "$key" in ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN) continue ;; esac
    export "$key=$val"
  done < "$env_file"
fi

exec "$@"
