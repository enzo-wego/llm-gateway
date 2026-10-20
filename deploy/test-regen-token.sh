#!/usr/bin/env bash
# Tests for deploy/regen-token.sh. Plain bash, dummy tokens only, temp dirs only.
# Exits 0 only if every case passes.
set -u
REPO=$(cd "$(dirname "$0")/.." && pwd)
NEW=sk-ant-oat01-NEWDUMMY1234
OLD=sk-ant-oat01-OLDDUMMY9876
TK=CLAUDE_CODE_OAUTH_TOKEN
BASE=$'# comment\n\nFOO=bar\n'"$TK=$OLD"$'\nBAZ=qux\n'
OR='{"backend":"openrouter","text":"ok"}'
ALLTMP=()
FAILS=0

mkcase() { # sets T; $1 = env file content
  T=$(mktemp -d); ALLTMP+=("$T")
  mkdir -p "$T/deploy" "$T/config" "$T/bin"
  cp "$REPO/deploy/regen-token.sh" "$T/deploy/"
  printf '%s' "$1" > "$T/config/.env"
  cat > "$T/bin/docker" <<'EOF'
#!/bin/bash
echo "$*" >> "$STUB_DIR/docker.log"
case "$1 $2" in
  "compose ps") [ "${STUB_NO_CONTAINER:-}" = 1 ] || echo fakeid ;;
  "compose restart")
    n=$(( $(cat "$STUB_DIR/restart.n" 2>/dev/null || echo 0) + 1 )); echo $n > "$STUB_DIR/restart.n"
    [ "${STUB_RESTART_SLEEP_ON:-}" = "$n" ] && /bin/sleep 2
    [ "${STUB_RESTART_FAIL_ON:-}" = "$n" ] && exit 1; exit 0 ;;
  "compose exec")
    echo GENERATE >> "$STUB_DIR/docker.log"
    [ -n "${STUB_EXEC_SLEEP:-}" ] && /bin/sleep "$STUB_EXEC_SLEEP"
    def='{"backend":"claude","text":"ok"}'
    printf '%s' "${STUB_GENERATE-$def}"
    exit "${STUB_EXEC_RC:-0}" ;;
  inspect*)
    case "$*" in
      *working_dir*) if [ -n "${STUB_WORKDIR+x}" ]; then echo "$STUB_WORKDIR"; else (cd "$STUB_DIR" && pwd -P); fi ;;
      *) [ "${STUB_UNHEALTHY:-}" = 1 ] && echo starting || echo healthy ;;
    esac ;;
  *) echo "unexpected docker call"; exit 99 ;;
esac
EOF
  cat > "$T/bin/mv" <<'EOF'
#!/bin/bash
n=$(( $(cat "$STUB_DIR/mv.n" 2>/dev/null || echo 0) + 1 )); echo $n > "$STUB_DIR/mv.n"
case ",${STUB_MV_FAIL_ON:-}," in *",$n,"*) exit 1 ;; esac
/bin/mv "$@" || exit $?
[ "${STUB_MV_KILL_ON:-}" = "$n" ] && kill -TERM $PPID
exit 0
EOF
  printf '#!/bin/sh\nexit 0\n' > "$T/bin/sleep"
  chmod +x "$T/bin/"*
  : > "$T/docker.log"
}

SPATH() { echo "$T/bin:/usr/bin:/bin"; }

# run_script <stdin string> [VAR=val ...]; sets OUT, RC
run_script() {
  local input=$1; shift
  OUT=$(cd "$T" && printf '%s' "$input" | env PATH="$(SPATH)" STUB_DIR="$T" "$@" bash deploy/regen-token.sh 2>&1); RC=$?
}

eff() { CLAUDE_CODE_OAUTH_TOKEN=INHERITED LLM_GATEWAY_ENV_FILE="$T/config/.env" sh "$REPO/docker-entrypoint.sh" env | grep "^$TK="; }
sum() { cksum < "$T/config/.env"; }
restarts() { cat "$T/restart.n" 2>/dev/null || echo 0; }
generated() { grep -c '^GENERATE$' "$T/docker.log" || true; }
leftovers() { ls -A "$T/config" | grep -c '^\.regen-token\.' || true; }
mode() { stat -f %Lp "$T/config/.env" 2>/dev/null || stat -c %a "$T/config/.env"; }

# check <cond-string-result> ; collects errors for the current case
ERRS=
ok() { [ "$1" = "$2" ] || ERRS+=" [$3: got '$1' want '$2']"; }
has() { case "$1" in *"$2"*) ;; *) ERRS+=" [$3: output lacks '$2']" ;; esac; }
hasnt() { case "$1" in *"$2"*) ERRS+=" [$3: output has '$2']" ;; esac; }
nosecrets() { hasnt "$OUT" "$NEW" "new token leaked"; hasnt "$OUT" "$OLD" "old token leaked"; }
done_case() {
  if [ -z "$ERRS" ]; then echo "PASS $1"; else echo "FAIL $1:$ERRS"; FAILS=$((FAILS+1)); fi
  ERRS=
}
outcomes() { printf '%s\n' "$OUT" | grep -c '^new token failed'; }

# 1. bash -n
bash -n "$REPO/deploy/regen-token.sh" 2>/dev/null; a=$?
bash -n "$REPO/deploy/test-regen-token.sh" 2>/dev/null; b=$?
OUT=; nosecrets; ok "$a$b" 00 "bash -n"; done_case "bash -n both scripts"

# 2. success variants
for v in "newline:$NEW"$'\n' "nonewline:$NEW" "spaces:  $NEW  "; do
  mkcase "$BASE"; run_script "${v#*:}"
  ok "$RC" 0 rc; ok "$(eff)" "$TK=$NEW" eff
  ok "$(cat "$T/config/.env")" "$(printf '%s' "$BASE" | sed "s/$OLD/$NEW/")" content
  ok "$(mode)" 600 mode; ok "$(leftovers)" 0 leftovers
  ok "$(restarts)" 1 restarts; ok "$(generated)" 1 generate
  hasnt "$OUT" "new token failed" "failure line"; has "$OUT" "token rotated (…1234)" success
  nosecrets; done_case "success ($(printf %s "${v%%:*}"))"
done

# 3. invalid input
for v in "empty:" "hello:hello" "internal space:sk-ant-oat01-A B" "two lines:$NEW"$'\n'"$NEW"$'\n'; do
  mkcase "$BASE"; before=$(sum); run_script "${v#*:}"
  ok "$RC" 2 rc; ok "$(sum)" "$before" unchanged; ok "$(restarts)" 0 "no restart"; ok "$(generated)" 0 "no generate"
  ok "$(leftovers)" 0 leftovers; nosecrets; done_case "invalid input (${v%%:*})"
done

# 4. file shapes: name|input|expected
shape() {
  mkcase "$2"; run_script "$NEW"
  ok "$RC" 0 rc; ok "$(eff)" "$TK=$NEW" eff; nosecrets
  ok "$(cat "$T/config/.env"; echo x)" "$3"$'\nx' content
  ok "$(grep -cE "^[[:space:]]*(export )?CLAUDE_CODE_OAUTH_ ?TOKEN" "$T/config/.env")" 1 "token lines"
  done_case "shape: $1"
}
shape "export prefix" "export $TK=old"$'\nA=1\n' "$TK=$NEW"$'\nA=1'
shape "two token lines" "$TK=old1"$'\nA=1\n'"$TK=old2"$'\n' "$TK=$NEW"$'\nA=1'
shape "whitespace key" "$TK=a"$'\nCLAUDE_CODE_OAUTH_ TOKEN=old\nA=1\n' "$TK=$NEW"$'\nA=1'
shape "no token, no final newline" $'A=1\nB=2' $'A=1\nB=2\n'"$TK=$NEW"
shape "CRLF non-token" $'A=1\r\nB=2\r\n'"$TK=old"$'\n' $'A=1\r\nB=2\r\n'"$TK=$NEW"
shape "comments and blanks" $'# c\n\nA=1\n#'"$TK=x"$'\n'"$TK=old"$'\n\n' $'# c\n\nA=1\n#'"$TK=x"$'\n'"$TK=$NEW"$'\n'

# rollback assertions helper
rolled() { # name outcome-substring [extra env...]
  ok "$RC" 1 rc; ok "$(eff)" "$TK=$OLD" "old token back"; ok "$(sum)" "$BEFORE" "file restored"
  ok "$(outcomes)" 1 "one outcome"; ok "$(leftovers)" 0 leftovers; nosecrets
}

# 5. OpenRouter answer
mkcase "$BASE"; BEFORE=$(sum); run_script "$NEW" STUB_GENERATE="$OR"
rolled; has "$OUT" OpenRouter msg; ok "$(restarts)" 2 restarts; ok "$(generated)" 1 generate
has "$OUT" "service healthy" outcome; done_case "OpenRouter answer rolls back"

# 6. generate failures
for n in request "empty text" malformed; do
  step="generate response"; [ "$n" = request ] && step="generate request"
  mkcase "$BASE"; BEFORE=$(sum)
  case $n in
    request) run_script "$NEW" STUB_EXEC_RC=1 ;;
    "empty text") run_script "$NEW" STUB_GENERATE='{"backend":"claude","text":""}' ;;
    malformed) run_script "$NEW" STUB_GENERATE='notjson' ;;
  esac
  rolled; has "$OUT" "$step" step; ok "$(generated)" 1 generate; done_case "generate failure ($n)"
done

# 7. unhealthy
mkcase "$BASE"; BEFORE=$(sum); run_script "$NEW" STUB_UNHEALTHY=1
rolled; has "$OUT" "health check" step; ok "$(generated)" 0 "generate not reached"; done_case "unhealthy"

# 8. restart failures
mkcase "$BASE"; BEFORE=$(sum); run_script "$NEW" STUB_RESTART_FAIL_ON=1
rolled; has "$OUT" "service healthy" outcome; done_case "first restart fails"
mkcase "$BASE"; BEFORE=$(sum); run_script "$NEW" STUB_RESTART_FAIL_ON=2 STUB_GENERATE="$OR"
rolled; has "$OUT" "restart/health failed" outcome; done_case "rollback restart fails"

# 9. interrupts
mkcase "$BASE"; BEFORE=$(sum); printf '%s' "$NEW" > "$T/in"
(cd "$T" && exec env PATH="$(SPATH)" STUB_DIR="$T" STUB_EXEC_SLEEP=3 \
  perl -e 'setpgrp(0,0); $SIG{INT}="DEFAULT"; exec @ARGV' bash deploy/regen-token.sh) < "$T/in" > "$T/out" 2>&1 &
pid=$!
for _ in $(seq 100); do grep -q '^GENERATE$' "$T/docker.log" && break; /bin/sleep 0.1; done
kill -INT -- "-$pid" 2>/dev/null
wait $pid; RC=$?; OUT=$(cat "$T/out")
[ "$RC" -ne 0 ] || ERRS+=" [rc zero]"
ok "$(eff)" "$TK=$OLD" "old token back"; ok "$(sum)" "$BEFORE" "file restored"; ok "$(outcomes)" 1 "one outcome"
ok "$(leftovers)" 0 leftovers; ok "$(restarts)" 2 "one rollback restart"; nosecrets
done_case "interrupt SIGINT during generate"

# FR2: second SIGINT to the process group while rollback is running
mkcase "$BASE"; BEFORE=$(sum); printf '%s' "$NEW" > "$T/in"
(cd "$T" && exec env PATH="$(SPATH)" STUB_DIR="$T" STUB_GENERATE="$OR" STUB_RESTART_SLEEP_ON=2 \
  perl -e 'setpgrp(0,0); $SIG{INT}="DEFAULT"; exec @ARGV' bash deploy/regen-token.sh) < "$T/in" > "$T/out" 2>&1 &
pid=$!
for _ in $(seq 100); do [ "$(restarts)" = 2 ] && break; /bin/sleep 0.1; done
kill -INT -- "-$pid" 2>/dev/null; /bin/sleep 0.3; kill -INT -- "-$pid" 2>/dev/null
wait $pid; RC=$?; OUT=$(cat "$T/out")
ok "$RC" 1 rc; has "$OUT" "rolling back to the previous token" "rollback notice"
has "$OUT" "service healthy" outcome
ok "$(eff)" "$TK=$OLD" "old token back"; ok "$(sum)" "$BEFORE" "file restored"; ok "$(outcomes)" 1 "one outcome"
ok "$(leftovers)" 0 leftovers; nosecrets
done_case "second SIGINT during rollback"

mkcase "$BASE"; BEFORE=$(sum); run_script "$NEW" STUB_MV_KILL_ON=1
[ "$RC" -ne 0 ] || ERRS+=" [rc zero]"
ok "$(eff)" "$TK=$OLD" "old token back"; ok "$(sum)" "$BEFORE" "file restored"; ok "$(outcomes)" 1 "one outcome"
ok "$(leftovers)" 0 leftovers; ok "$(restarts)" 1 "one rollback restart"; nosecrets
done_case "interrupt SIGTERM right after install"

# 10. restore fails
mkcase "$BASE"; run_script "$NEW" STUB_MV_FAIL_ON=2 STUB_GENERATE="$OR"
ok "$RC" 1 rc; ok "$(printf '%s\n' "$OUT" | grep -c 'Could not restore the previous token state')" 1 outcome
ok "$(outcomes)" 1 "one outcome"; ok "$(leftovers)" 0 leftovers; nosecrets
done_case "restore fails after install"
mkcase "$BASE"; BEFORE=$(sum); run_script "$NEW" STUB_MV_FAIL_ON=1,2 STUB_GENERATE="$OR"
ok "$RC" 1 rc; ok "$(printf '%s\n' "$OUT" | grep -c 'Could not restore the previous token state')" 1 outcome
ok "$(sum)" "$BEFORE" unchanged; ok "$(leftovers)" 0 leftovers; nosecrets
done_case "forward and restore both fail"

# 11. absent vs empty
mkcase $'A=1\nB=2\n'; BEFORE=$(sum); run_script "$NEW" STUB_GENERATE="$OR"
ok "$RC" 1 rc; ok "$(eff)" "$TK=INHERITED" "absent stays absent"; ok "$(sum)" "$BEFORE" unchanged
done_case "rollback keeps token absent"
mkcase $'A=1\n'"$TK="$'\n'; BEFORE=$(sum); run_script "$NEW" STUB_GENERATE="$OR"
ok "$RC" 1 rc; ok "$(eff)" "$TK=" "empty stays empty"; ok "$(sum)" "$BEFORE" unchanged
done_case "rollback keeps token empty"

# 12. lock
mkcase "$BASE"; mkdir "$T/config/.regen-token.lock"; BEFORE=$(sum); run_script "$NEW"
ok "$RC" 3 rc; ok "$(sum)" "$BEFORE" unchanged; [ -d "$T/config/.regen-token.lock" ] || ERRS+=" [lock removed]"
ok "$(restarts)" 0 "no restart"; ok "$(generated)" 0 "no generate"; nosecrets; done_case "lock held"

# FR1: failed write inside set_token must fail, not install a truncated file
filler=$(printf 'FILLER_%.0s=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n' $(seq 40))
mkcase "$filler"$'\n'"$TK=$OLD"$'\n'; BEFORE=$(sum)
[ "$(wc -c < "$T/config/.env" | tr -d ' ')" -gt 2048 ] || ERRS+=" [env file too small]"
OUT=$(cd "$T" && trap "" XFSZ && ulimit -f 1 && printf '%s' "$NEW" | env PATH="$(SPATH)" STUB_DIR="$T" bash deploy/regen-token.sh 2>&1); RC=$?
[ "$RC" -ne 0 ] || ERRS+=" [rc zero]"
ok "$(sum)" "$BEFORE" "file unchanged"; ok "$(eff)" "$TK=$OLD" "old token"; ok "$(leftovers)" 0 leftovers
ok "$(restarts)" 0 "no restart"; nosecrets
done_case "write failure (ulimit -f) leaves env file intact"

# FR3: running container belongs to another checkout
mkcase "$BASE"; BEFORE=$(sum); run_script "$NEW" STUB_WORKDIR=/nonexistent/other-checkout
ok "$RC" 2 rc; has "$OUT" "belongs to /nonexistent/other-checkout" msg; ok "$(sum)" "$BEFORE" unchanged
ok "$(restarts)" 0 "no restart"; ok "$(generated)" 0 "no generate"; ok "$(leftovers)" 0 leftovers; nosecrets
done_case "wrong checkout"
mkcase "$BASE"; BEFORE=$(sum); run_script "$NEW" STUB_NO_CONTAINER=1
ok "$RC" 2 rc; has "$OUT" "no running" msg; ok "$(sum)" "$BEFORE" unchanged; ok "$(restarts)" 0 "no restart"
ok "$(leftovers)" 0 leftovers; nosecrets
done_case "no container running"

# 14. interactive branch through a PTY
if command -v script >/dev/null; then
  mkcase "$BASE"; BEFORE=$(sum)
  OUT=$(cd "$T" && PATH="$(SPATH)" STUB_DIR="$T" script -q /dev/null bash deploy/regen-token.sh </dev/null 2>&1); RC=$?
  has "$OUT" "claude CLI not found" msg; ok "$(sum)" "$BEFORE" unchanged
  nosecrets; done_case "interactive: claude missing"
  mkcase "$BASE"; BEFORE=$(sum); printf '#!/bin/sh\nexit 1\n' > "$T/bin/claude"; chmod +x "$T/bin/claude"
  OUT=$(cd "$T" && PATH="$(SPATH)" STUB_DIR="$T" script -q /dev/null bash deploy/regen-token.sh </dev/null 2>&1); RC=$?
  has "$OUT" "claude setup-token failed" msg; ok "$(sum)" "$BEFORE" unchanged; ok "$(restarts)" 0 "no restart"; ok "$(generated)" 0 "no generate"
  nosecrets; done_case "interactive: claude exits 1"
  mkcase "$BASE"; BEFORE=$(sum); printf '#!/bin/sh\ntouch "$STUB_DIR/claude.called"\nexit 0\n' > "$T/bin/claude"; chmod +x "$T/bin/claude"
  OUT=$(cd "$T" && PATH="$(SPATH)" STUB_DIR="$T" STUB_WORKDIR=/nonexistent/other-checkout script -q /dev/null bash deploy/regen-token.sh </dev/null 2>&1); RC=$?
  ok "$RC" 2 rc; has "$OUT" "belongs to /nonexistent/other-checkout" msg; ok "$(sum)" "$BEFORE" unchanged
  [ ! -e "$T/claude.called" ] || ERRS+=" [claude was called]"; ok "$(restarts)" 0 "no restart"
  nosecrets; done_case "interactive: wrong checkout never asks for a token"
else
  echo "FAIL interactive: script(1) not available"; FAILS=$((FAILS+1))
fi

for d in "${ALLTMP[@]}"; do rm -rf "$d"; done
[ "$FAILS" -eq 0 ] && echo "ALL PASS" || echo "$FAILS FAILED"
[ "$FAILS" -eq 0 ]
