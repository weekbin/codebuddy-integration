#!/usr/bin/env bash
# smoke.sh - Local-only end-to-end smoke test for invoke-codebuddy.
#
# Does NOT require codebuddy, orca-ide, or a live orca worktree. It exercises
# the parts of the script that fail fast on its own: --help, --bogus-flag,
# --kill with no handle, --log with no log file, --status with no handle, and
# the readlink -f based plugin-root resolution under a symlink.
#
# Usage:
#   tests/smoke.sh
#   tests/smoke.sh /abs/path/to/plugin-root   # auto-resolve SCRIPT under it
set -uo pipefail

SCRIPT="${1:-}"
if [ -z "$SCRIPT" ]; then
  # Resolve <plugin-root>/bin/invoke-codebuddy relative to this file
  HERE="$(cd "$(dirname "$0")" && pwd)"
  PLUGIN_ROOT="$(cd "$HERE/.." && pwd)"
  SCRIPT="$PLUGIN_ROOT/bin/invoke-codebuddy"
fi

pass=0
fail=0
log() { printf '%-7s %s\n' "$1" "$2"; }

assert_exit() {
  local label="$1" expected="$2" actual="$3" out="$4"
  if [ "$expected" = "$actual" ]; then
    log "PASS" "$label (exit=$expected)"
    pass=$((pass+1))
  else
    log "FAIL" "$label (expected exit $expected, got $actual)"
    echo "----- output -----"
    echo "$out"
    echo "------------------"
    fail=$((fail+1))
  fi
}

assert_grep() {
  local label="$1" pattern="$2" out="$3"
  if printf '%s' "$out" | grep -qE -- "$pattern"; then
    log "PASS" "$label (matched /$pattern/)"
    pass=$((pass+1))
  else
    log "FAIL" "$label (no match for /$pattern/)"
    echo "----- output -----"
    echo "$out"
    echo "------------------"
    fail=$((fail+1))
  fi
}

# Sanity: script exists and is executable
if [ ! -x "$SCRIPT" ]; then
  log "FAIL" "script not found or not executable: $SCRIPT"
  exit 1
fi

# Use a throwaway HOME/state so we don't pollute the real install
TMPHOME="$(mktemp -d)"
trap 'rm -rf "$TMPHOME"' EXIT
export HOME="$TMPHOME"

# 1) --help exits 0 and prints the new name
out="$("$SCRIPT" --help 2>&1)"; rc=$?
assert_exit "--help exits 0" 0 "$rc" "$out"
assert_grep "--help mentions invoke-codebuddy" '^invoke-codebuddy ' "$out"
assert_grep "--help mentions all renamed flags" 'invoke-codebuddy --await' "$out"

# 2) --bogus-flag exits 2 and prefixes errors with invoke-codebuddy:
out="$("$SCRIPT" --bogus-flag 2>&1)"; rc=$?
assert_exit "--bogus-flag exits 2" 2 "$rc" "$out"
assert_grep "--bogus-flag error prefix" '^invoke-codebuddy: ' "$out"

# 3) Empty task prints usage and exits 2
out="$("$SCRIPT" 2>&1)"; rc=$?
assert_exit "empty task exits 2" 2 "$rc" "$out"
assert_grep "empty task shows usage" '^用法:' "$out"

# 4) --log with no log file prints the new path. Clear any prior log first
#    because earlier invocations in the same checkout will have created it.
rm -f "$(dirname "$SCRIPT")/../logs/invocations.log"
out="$("$SCRIPT" --log 2>&1)"; rc=$?
assert_exit "--log exits 0" 0 "$rc" "$out"
assert_grep "--log mentions plugin-root path" 'no log file: ' "$out"

# 5) --kill with no handle exits 2 and uses new error prefix
out="$("$SCRIPT" --kill 2>&1)"; rc=$?
assert_exit "--kill no handle exits 2" 2 "$rc" "$out"
assert_grep "--kill error prefix" '^invoke-codebuddy: ' "$out"

# 6) --status with no stored handle exits 2
out="$("$SCRIPT" --status 2>&1)"; rc=$?
assert_exit "--status no handle exits 2" 2 "$rc" "$out"
assert_grep "--status error prefix" '^invoke-codebuddy: ' "$out"

# 7) Plugin root resolution under a symlink
SYMDIR="$(mktemp -d)"
ln -s "$SCRIPT" "$SYMDIR/invoke-codebuddy"
out="$("$SYMDIR/invoke-codebuddy" --help 2>&1)"; rc=$?
assert_exit "symlinked script --help works" 0 "$rc" "$out"
assert_grep "symlinked --help shows the new name" '^invoke-codebuddy ' "$out"
rm -rf "$SYMDIR"

# 7b) Bridge script: no args -> exit 2 + usage to stderr
BRIDGE="$(dirname "$SCRIPT")/invoke-codebuddy-bridge.sh"
if [ -f "$BRIDGE" ]; then
  out="$("$BRIDGE" 2>&1)"; rc=$?
  assert_exit "bridge no args exits 2" 2 "$rc" "$out"
  assert_grep "bridge usage mentions --background" -- '--background' "$out"
  assert_grep "bridge usage shows example" 'translate to English' "$out"

  # 7c) Bridge with a prompt but no codebuddy on PATH -> forwards error from
  #     invoke-codebuddy (exit code 4 = "codebuddy not in PATH"). Verifies
  #     sibling resolution + error propagation. We strip PATH down so the
  #     bridge cannot accidentally find a real codebuddy in this test.
  out="$(env -i HOME="$TMPHOME" PATH="/usr/bin:/bin" "$BRIDGE" "smoke test prompt" 2>&1)"; rc=$?
  assert_exit "bridge propagates invoke-codebuddy failure" 4 "$rc" "$out"
  assert_grep "bridge surfaces 'codebuddy' missing" "codebuddy.*not in PATH" "$out"

  # 7d) bash -n on the bridge
  if bash -n "$BRIDGE" >/dev/null 2>&1; then
    log "PASS" "bridge bash -n passes"
    pass=$((pass+1))
  else
    log "FAIL" "bridge bash -n fails"
    fail=$((fail+1))
  fi
fi

# 8) Bash syntax check
if bash -n "$SCRIPT" >/dev/null 2>&1; then
  log "PASS" "bash -n passes"
  pass=$((pass+1))
else
  log "FAIL" "bash -n fails"
  fail=$((fail+1))
fi

# 9) Python worker syntax check (only if it exists)
WORKER="$(dirname "$SCRIPT")/invoke-codebuddy-acp-worker.py"
if [ -f "$WORKER" ]; then
  if python3 -m py_compile "$WORKER" >/dev/null 2>&1; then
    log "PASS" "acp worker compiles"
    pass=$((pass+1))
  else
    log "FAIL" "acp worker does not compile"
    fail=$((fail+1))
  fi
fi

# 10) SKILL.md frontmatter sanity
SKILL="$(dirname "$SCRIPT")/../skills/codebuddy-integration/SKILL.md"
if [ -f "$SKILL" ]; then
  out="$(head -n 6 "$SKILL")"
  if printf '%s' "$out" | grep -q '^name: codebuddy-integration$'; then
    log "PASS" "SKILL.md frontmatter name matches directory"
    pass=$((pass+1))
  else
    log "FAIL" "SKILL.md frontmatter name mismatch"
    echo "----- frontmatter -----"
    echo "$out"
    fail=$((fail+1))
  fi
fi

echo
echo "===== smoke.sh: $pass passed, $fail failed ====="
[ "$fail" -eq 0 ]
