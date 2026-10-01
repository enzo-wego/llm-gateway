#!/usr/bin/env bash
# Rebuild the container when PyPI has a newer claude-agent-sdk than the image.
#
# The model aliases (sonnet, haiku) are resolved by the CLI bundled in the SDK,
# so a new Claude model only becomes reachable once the image carries a newer
# SDK. The pip layer is cached, so a plain `up --build` never picks one up.
# Run from cron; it is a no-op when the image is already current.
#
# On a failed rebuild or health check it restores the previous image, so a bad
# SDK release costs one restart, not an outage.
set -euo pipefail
cd "$(dirname "$0")/.."

SERVICE=llm-gateway
IMAGE=llm-gateway:latest
log() { echo "$(date '+%F %T') $*"; }

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

# /health never touches the CLI, so prove a real call works on the new SDK.
generates() {
  docker compose exec -T "$SERVICE" python -c '
import os, json, urllib.request
req = urllib.request.Request("http://127.0.0.1:8750/generate",
    data=json.dumps({"system": "Answer briefly.", "user": "say ok", "tier": "cheap"}).encode(),
    headers={"X-API-Key": os.environ["LLM_GATEWAY_API_KEY"], "content-type": "application/json"})
assert json.loads(urllib.request.urlopen(req, timeout=120).read())["text"]'
}

if docker compose build --no-cache --pull && docker compose up -d && healthy && generates; then
  log "rebuilt on claude-agent-sdk $latest"
else
  log "rebuild failed, restoring previous image"
  docker tag llm-gateway:previous "$IMAGE"
  docker compose up -d --no-build
  exit 1
fi
