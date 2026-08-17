#!/usr/bin/env bash
# invoke-codebuddy-bridge.sh - fire-and-await wrapper for codebuddy subagent calls.
#
# USAGE
#   invoke-codebuddy-bridge.sh "<task prompt>"
#
# WHAT IT DOES
#   1. invoke-codebuddy --background <prompt>  -> spawn codebuddy, return HANDLE
#   2. invoke-codebuddy --await      <HANDLE>  -> inotifywait on state/done-<handle>
#                                              (0 CPU; falls back to 1s poll)
#   3. cat the result file to stdout
#
# WHY IT EXISTS
#   Designed for the mavis worker's bash tool. The worker only has to call this
#   one command and copy stdout back as its final answer. No need for the worker
#   to reason about --background / --await / HANDLE_FILE / inotifywait — that
#   is all already implemented in `invoke-codebuddy`. The bridge is just the
#   two-line sequence collapsed into one command so the worker prompt can be
#   one line and the LLM cost is near zero.
#
# EXIT CODES
#   0  codebuddy returned a result (success or business-level error inside result)
#   2  bad arguments (no prompt)
#   any non-zero from invoke-codebuddy (e.g. codebuddy binary missing, timeout)
set -eo pipefail

PROMPT="${1:-}"
[ -z "$PROMPT" ] && {
  cat >&2 <<'EOF'
usage: invoke-codebuddy-bridge.sh "<task prompt>"

Examples:
  invoke-codebuddy-bridge.sh "translate to English: 你好世界"
  invoke-codebuddy-bridge.sh "summarize this spec: $(cat spec.md)"
  invoke-codebuddy-bridge.sh "review this Python LRU cache for race conditions"

Side effects: spawns one orca/codebuddy terminal, appends one line to
<plugin-root>/logs/invocations.log, leaves state/result-<handle>.md and
state/done-<handle> behind. Pass --no-log to the underlying invoke-codebuddy
to skip the log line.
EOF
  exit 2
}

# Resolve sibling invoke-codebuddy: <plugin-root>/bin/invoke-codebuddy
# Falls back to $PATH for users who symlink the bridge instead of placing
# it next to invoke-codebuddy.
SELF="$(readlink -f "$0")"
SELF_DIR="$(dirname "$SELF")"
INVOKE="$SELF_DIR/invoke-codebuddy"
if [ ! -x "$INVOKE" ]; then
  INVOKE="invoke-codebuddy"
fi
command -v "$INVOKE" >/dev/null 2>&1 || {
  echo "invoke-codebuddy-bridge.sh: cannot find invoke-codebuddy (looked in $SELF_DIR and \$PATH)" >&2
  exit 4
}

# Spawn codebuddy, then await its completion. exec replaces this shell with
# the await process so its exit code (and signal handling) propagate cleanly.
# Use --json + jq to robustly extract the handle: --background prints a
# multi-line block (handle + events/status/result paths + await/metrics
# hints) in human mode, and a single JSON object with a `handle` field in
# --json mode. The previous `HANDLE=$(...)` capture silently took the
# entire multi-line block, which then crashed --await with "no such handle".
if ! command -v jq >/dev/null 2>&1; then
  echo "invoke-codebuddy-bridge.sh: 'jq' is required (not in PATH)" >&2
  exit 4
fi
HANDLE=$("$INVOKE" --json --background "$PROMPT" | jq -r .handle) || exit $?
exec "$INVOKE" --await "$HANDLE"
