#!/usr/bin/env bash
# Replace CLAUDE_CODE_OAUTH_TOKEN in config/.env with a fresh token, restart the
# container, and prove a real Claude call (not an OpenRouter fallback) works.
# On any failure after the file changed, put the old token state back.
#
# Usage: deploy/regen-token.sh            (runs `claude setup-token`, then paste)
#        pbpaste | deploy/regen-token.sh  (token on stdin, setup-token skipped)
#
# The `claude setup-token` screen shows the full token by design. Do not run
# this in a recorded or shared terminal. This script prints at most 4 chars.
set -euo pipefail
cd "$(dirname "$0")/.."

SERVICE=llm-gateway
ENV_FILE=config/.env
LOCK=config/.regen-token.lock
KEY=CLAUDE_CODE_OAUTH_TOKEN
log() { echo "$(date '+%F %T') $*"; }

# The running container must mount THIS checkout's config/.env. Read-only
# (compose ps, inspect), so no lock needed; runs before a token is requested.
check_checkout() {
  local cid live live_phys
  cid=$(docker compose ps -q "$SERVICE" 2>/dev/null) || cid=
  if [[ -z $cid ]]; then echo "no running $SERVICE container; start it first" >&2; exit 2; fi
  live=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$cid" 2>/dev/null) || live=
  live_phys=$(cd "$live" 2>/dev/null && pwd -P) || live_phys=$live
  if [[ -z $live || $live_phys != "$(pwd -P)" ]]; then
    echo "the running container belongs to ${live:-an unknown directory}; run this script from there" >&2
    exit 2
  fi
}
check_checkout

# ---- get and validate the token (nothing is touched yet) -------------------
if [[ -t 0 ]]; then
  command -v claude >/dev/null 2>&1 || { echo "claude CLI not found on PATH" >&2; exit 2; }
  claude setup-token || { echo "claude setup-token failed" >&2; exit 2; }
  read -rsp "Paste the token: " token || true
  echo >&2
else
  token=$(cat)
fi
token=${token#"${token%%[![:space:]]*}"}
token=${token%"${token##*[![:space:]]}"}
if [[ -z $token ]]; then echo "no token given" >&2; exit 2; fi
if [[ $token == *[[:space:]]* ]]; then echo "token contains whitespace" >&2; exit 2; fi
if [[ $token != sk-ant-oat01-* ]]; then echo "token does not start with sk-ant-oat01-" >&2; exit 2; fi
[[ -f $ENV_FILE ]] || { echo "$ENV_FILE not found" >&2; exit 2; }

# ---- lock --------------------------------------------------------------------
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "another rotation is running, or a killed run left $LOCK; remove it if no rotation is running" >&2
  exit 3
fi
tmp=
cleanup() { [[ -n $tmp ]] && rm -f -- "$tmp"; rmdir "$LOCK" 2>/dev/null || true; }
trap cleanup EXIT


# ponytail: a PUT /config landing between this script's read and its rename can
# be lost. Rotation is a rare manual step; an app-side lock is the upgrade path.

# True if the line is a token assignment, classified like docker-entrypoint.sh.
is_token_line() {
  local line=$1 k
  case "$line" in ''|'#'*) return 1 ;; esac
  case "$line" in *=*) ;; *) return 1 ;; esac
  k=${line%%=*}
  k=${k#export }
  k=${k//[[:space:]]/}
  [[ $k == "$KEY" ]]
}

# set_token <present 0|1> <value>: rewrite only token lines of config/.env.
set_token() {
  local present=$1 value=$2 line nl done_=0 last_nl=1
  [[ -z $tmp ]] || rm -f -- "$tmp"
  tmp=$(umask 077; mktemp config/.regen-token.XXXXXX) || return 1
  (
    while IFS= read -r line || { [[ -n $line ]] && nl=0; }; do
      nl=${nl:-1}
      if is_token_line "$line"; then
        if [[ $present == 1 && $done_ == 0 ]]; then
          printf '%s=%s\n' "$KEY" "$value" || exit 1; done_=1; last_nl=1
        fi
      else
        if [[ $nl == 1 ]]; then printf '%s\n' "$line" || exit 1; last_nl=1
        else printf '%s' "$line" || exit 1; last_nl=0; fi
      fi
      unset nl
    done < "$ENV_FILE"
    if [[ $present == 1 && $done_ == 0 ]]; then
      if [[ $last_nl != 1 ]]; then printf '\n' || exit 1; fi
      printf '%s=%s\n' "$KEY" "$value" || exit 1
    fi
  ) > "$tmp" || return 1
  mv "$tmp" "$ENV_FILE" || return 1
  tmp=
}

# Record the old state: present flag and the last token line's value.
old_present=0 old_value=
while IFS= read -r line || [[ -n $line ]]; do
  if is_token_line "$line"; then old_present=1; old_value=${line#*=}; fi
done < "$ENV_FILE"

healthy() {
  for _ in $(seq 30); do
    [[ "$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q "$SERVICE")" 2>/dev/null)" == healthy ]] && return 0
    sleep 2
  done
  return 1
}

restart() { docker compose restart "$SERVICE" >/dev/null 2>&1; }

# Reads the /generate response body on stdin. 0 ok, 3 openrouter, 4 bad.
check_generate() {
  python3 -c '
import sys, json
try:
    d = json.loads(sys.stdin.read())
    b, t = d.get("backend"), d.get("text")
except Exception:
    sys.exit(4)
if b == "openrouter": sys.exit(3)
sys.exit(0 if b == "claude" and isinstance(t, str) and t.strip() else 4)'
}

phase=pre step=
on_fail() {
  local rc=$1
  trap - ERR
  trap '' INT TERM
  step=${step:-unexpected error}
  if [[ $phase != written ]]; then
    echo "failed: $step" >&2
    exit "$rc"
  fi
  echo "rolling back to the previous token..." >&2
  if set_token "$old_present" "$old_value"; then
    if restart && healthy; then
      echo "new token failed: $step. Old token restored, service healthy."
    else
      echo "new token failed: $step. Old token restored, but restart/health failed. Check docker compose logs $SERVICE."
    fi
  else
    echo "new token failed: $step. Could not restore the previous token state. Check $ENV_FILE and run the script again with a working token."
  fi
  exit 1
}
fail() { step=$1; on_fail 1; }
trap 'step=${step:-interrupted}; on_fail 130' INT
trap 'step=${step:-interrupted}; on_fail 143' TERM
trap 'on_fail 1' ERR

# ---- install, restart, verify ------------------------------------------------
phase=written   # set BEFORE the mv: rollback onto an unchanged file is harmless
set_token 1 "$token" || fail "write $ENV_FILE"

if ! restart; then fail "restart"; fi
if ! healthy; then fail "health check"; fi

if ! out=$(docker compose exec -T "$SERVICE" python -c '
import os, json, urllib.request
req = urllib.request.Request("http://127.0.0.1:8750/generate",
    data=json.dumps({"system": "Answer briefly.", "user": "say ok", "tier": "cheap"}).encode(),
    headers={"X-API-Key": os.environ["LLM_GATEWAY_API_KEY"], "content-type": "application/json"})
print(urllib.request.urlopen(req, timeout=120).read().decode())' 2>/dev/null); then
  fail "generate request"
fi
rc=0
printf '%s' "$out" | check_generate || rc=$?
case $rc in
  0) ;;
  3) fail "generate response: got an OpenRouter answer; the Claude seat did not answer, or the cheap tier is not on claude" ;;
  *) fail "generate response" ;;
esac

phase=committed
trap - ERR INT TERM
echo "token rotated (…${token: -4}), Claude answered on the cheap tier."
exit 0
