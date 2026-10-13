#!/usr/bin/env bash
# Rebuild the container when PyPI has a newer claude-agent-sdk than the image.
#
# The model aliases (sonnet, haiku) are resolved by the CLI bundled in the SDK,
# so a new Claude model only becomes reachable once the image carries a newer
# SDK. The pip layer is cached, so a plain `up --build` never picks one up.
# Run from cron; it is a no-op when the image is already current.
#
# On a failed rebuild or health check it restores the previous image, so a bad
# SDK release costs one restart, not an outage. Either outcome is posted to
# SLACK_ALERT_CHANNEL (a user id posts as a DM) when config/.env sets it.
set -euo pipefail
cd "$(dirname "$0")/.."

SERVICE=llm-gateway
IMAGE=llm-gateway:latest
log() { echo "$(date '+%F %T') $*"; }

# Same keys the gateway's own alerts use. Read from the host file, not the
# container, because the container may be the thing that just broke.
env_get() { grep -m1 "^$1=" config/.env 2>/dev/null | cut -d= -f2- | tr -d '"'\''\r'; }
notify() {
  local token channel
  token=$(env_get SLACK_BOT_TOKEN); channel=$(env_get SLACK_ALERT_CHANNEL)
  [[ -n $token && -n $channel ]] || return 0
  python3 -c 'import json,sys; print(json.dumps({"channel": sys.argv[1], "text": sys.argv[2], "unfurl_links": False}))' \
    "$channel" "*llm-gateway* $1" \
    | curl -fsS -m 10 https://slack.com/api/chat.postMessage \
        -H "Authorization: Bearer $token" -H 'content-type: application/json; charset=utf-8' -d @- \
    | grep -q '"ok":true' || log "slack notify failed"
}

latest=$(curl -fsS https://pypi.org/pypi/claude-agent-sdk/json \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["info"]["version"])')
current=$(docker compose exec -T "$SERVICE" python -c \
  'import importlib.metadata as m; print(m.version("claude-agent-sdk"))' 2>/dev/null || echo none)

if [[ "$latest" == "$current" ]]; then
  log "claude-agent-sdk $current is current"
  exit 0
fi

log "claude-agent-sdk $current -> $latest, rebuilding"
docker tag "$IMAGE" llm-gateway:previous

healthy() {
  for _ in $(seq 30); do
    [[ "$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q "$SERVICE")" 2>/dev/null)" == healthy ]] && return 0
    sleep 2
  done
  return 1
}

# /health never touches the CLI, so prove a real call works on the new SDK for
# every route that reaches it. Each tier has its own model and thinking mode,
# so a new model can reject one tier's settings while another still passes.
generates() {
  docker compose exec -T "$SERVICE" python -c '
import os, json, urllib.request
def call(path, body):
    req = urllib.request.Request("http://127.0.0.1:8750" + path, data=json.dumps(body).encode(),
        headers={"X-API-Key": os.environ["LLM_GATEWAY_API_KEY"], "content-type": "application/json"})
    out = json.loads(urllib.request.urlopen(req, timeout=120).read())
    assert out["text"], path
# 1x1 white PNG.
png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC"
for tier in ("cheap", "summary"):
    call("/generate", {"system": "Answer briefly.", "user": "say ok", "tier": tier})
call("/describe", {"prompt": "What colour is this image? One word.", "mime": "image/png", "data_b64": png})'
}

if docker compose build --no-cache --pull && docker compose up -d && healthy && generates; then
  log "rebuilt on claude-agent-sdk $latest"
  notify "rebuilt on claude-agent-sdk $current -> $latest. Bundled CLI $(docker compose exec -T "$SERVICE" sh -c '"$(python -c "import claude_agent_sdk,os;print(os.path.dirname(claude_agent_sdk.__file__))")/_bundled/claude" --version'). All tiers answered."
else
  log "rebuild failed, restoring previous image"
  docker tag llm-gateway:previous "$IMAGE"
  docker compose up -d --no-build
  notify "rebuild on claude-agent-sdk $latest failed, rolled back to $current. A tier probably rejects the new model's settings; see ~/.cache/llm-gateway-refresh.log."
  exit 1
fi
