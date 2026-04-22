#!/usr/bin/env bash
# M-1 VPS spike: prove Max OAuth transfers + N concurrent sessions work from Zeabur.
#
# Expects env var CLAUDE_AUTH_B64 containing the base64-encoded contents of
# ~/.claude.json from the developer laptop.
#
# Prints results to stdout (visible in Zeabur logs) then `sleep infinity` so
# the container doesn't restart-loop.

set -u

echo "============================================================"
echo " M-1 spike: Zeabur container, Max OAuth transfer test"
echo " started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "============================================================"

# --- write the OAuth file from the env var -------------------------------
if [ -z "${CLAUDE_AUTH_B64:-}" ]; then
    echo "FATAL: CLAUDE_AUTH_B64 env var is empty or unset."
    echo "Set it in Zeabur to the base64 of your laptop's ~/.claude.json."
    sleep infinity
fi

echo "$CLAUDE_AUTH_B64" | base64 -d > /root/.claude.json 2> /tmp/b64.err
if [ ! -s /root/.claude.json ]; then
    echo "FATAL: base64 decode produced empty file."
    echo "stderr from base64:"
    cat /tmp/b64.err
    sleep infinity
fi
chmod 600 /root/.claude.json
echo "auth file written: $(wc -c < /root/.claude.json) bytes"

# --- sanity --------------------------------------------------------------
echo ""
echo "--- environment ---"
claude --version || { echo "FATAL: claude CLI not on PATH"; sleep infinity; }
uname -a
cat /etc/os-release | grep -E '^(PRETTY_NAME|VERSION)=' || true

# --- the test ------------------------------------------------------------
N="${N:-10}"
mkdir -p /tmp/spike && cd /tmp/spike
rm -f a*.out a*.err

echo ""
echo "=== $N concurrent Claudes, one OAuth, from Zeabur container ==="
START=$(date +%s)

PIDS=()
for i in $(seq 1 "$N"); do
    ( { time claude -p "You are agent a$i. Reply with exactly: 'a$i here'." ; } 2> "a$i.err" ) > "a$i.out" &
    PIDS+=($!)
done

RCS=()
for pid in "${PIDS[@]}"; do
    wait "$pid"
    RCS+=($?)
done

END=$(date +%s)
echo ""
echo "wall time: $((END - START))s"
echo ""
echo "=== per-agent results ==="
PASS=0
FAIL=0
for i in $(seq 1 "$N"); do
    rc=${RCS[$((i-1))]}
    out=$(tr -d '\n' < "a$i.out" 2>/dev/null | cut -c1-80)
    real=$(grep '^real' "a$i.err" 2>/dev/null | awk '{print $2}')
    err_hint=$(grep -iE 'error|rate|auth|forbidden|401|403|429' "a$i.err" 2>/dev/null | head -1)
    if [ "$rc" = "0" ]; then PASS=$((PASS+1)); else FAIL=$((FAIL+1)); fi
    echo "a$i rc=$rc real=$real out=\"$out\" ${err_hint:+ERR:$err_hint}"
done

echo ""
echo "=== summary: $PASS passed, $FAIL failed, $N total ==="
echo ""

# Keep the container alive so Zeabur doesn't restart-loop and the logs
# remain readable. Delete the service once you've captured the output.
echo "test complete; sleeping forever. delete the Zeabur service when done."
sleep infinity
