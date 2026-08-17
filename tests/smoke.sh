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

# Use a throwaway HOME/state so we don't pollute the real install.
# Save the real HOME FIRST so the 0.1.7 tests below can hand codebuddy
# its real auth directory (codebuddy refuses to start with no auth).
TMPHOME="$(mktemp -d)"
REAL_HOME="$HOME"
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
  #     invoke-codebuddy (exit code 4 = "codebuddy not found"). Verifies
  #     sibling resolution + error propagation. We strip PATH down so the
  #     bridge cannot accidentally find a real codebuddy in this test.
  out="$(env -i HOME="$TMPHOME" PATH="/usr/bin:/bin" "$BRIDGE" "smoke test prompt" 2>&1)"; rc=$?
  assert_exit "bridge propagates invoke-codebuddy failure" 4 "$rc" "$out"
  assert_grep "bridge surfaces 'codebuddy' missing" "codebuddy CLI not found" "$out"

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
echo "===== 0.1.7: orca-ide optional / TUI fall-back ====="
echo "(each test below calls the real codebuddy CLI; ~5s per test, ~10s total)"

# In the dev environment codebuddy and orca-ide both live under
# /home/weekbin/.local/bin AND /usr/bin/orca-ide + /bin/orca-ide are
# also real symlinks. There's no PATH-only way to "hide" orca-ide
# from `command -v` (the only directories we can drop from PATH
# would also break the script's `env` shebang and `python3` call).
#
# Instead, we use BASH_ENV to source a tiny file in the script's
# OWN bash process. That file overrides the `command` builtin so
# `command -v orca-ide` returns 1 while every other `command` call
# (e.g. `command -v codebuddy`) keeps working.
FAKE_BIN="$(mktemp -d -t cb-smoke-XXXXXX)/bin"
mkdir -p "$FAKE_BIN"
ln -sf "$(command -v codebuddy 2>/dev/null || echo /home/weekbin/.local/bin/codebuddy)" "$FAKE_BIN/codebuddy"
BASH_ENV_FILE="$(mktemp -t cb-smoke-bashenv-XXXXXX.sh)"
# Override `command` only for orca-ide lookups; fall through to
# the real builtin for everything else. This is sourced by every
# non-interactive bash that BASH_ENV points to (i.e. the script).
cat > "$BASH_ENV_FILE" <<'EOF'
command() {
  if [ "$1" = "-v" ] && [ "$2" = "orca-ide" ]; then
    return 1
  fi
  builtin command "$@"
}
EOF
TEST_PATH="$FAKE_BIN:/usr/bin:/bin"

# Sanity: prove the override works as expected. The script's bash
# process will see orca-ide as missing.
sanity_out="$(env -i PATH="$TEST_PATH" BASH_ENV="$BASH_ENV_FILE" bash -c 'command -v orca-ide; echo "rc=$?"')"
if printf '%s' "$sanity_out" | grep -q 'rc=1'; then
  log "PASS" "BASH_ENV override hides orca-ide from 'command -v' (rc=1)"
  pass=$((pass+1))
else
  log "FAIL" "BASH_ENV override failed; TUI fall-back test will be inconclusive"
  echo "  got: $sanity_out"
  fail=$((fail+1))
fi

# 11) --mode tui without orca-ide falls back to print, exits 0,
#     prints warning to stderr, prints result to stdout.
#     NOTE: HOME must be the REAL home (not $TMPHOME) because codebuddy
#     reads auth from ~/.codebuddy/. env -i is fine — the script's state
#     dir is plugin-relative, not HOME-relative.
out="$(env -i HOME="$REAL_HOME" PATH="$TEST_PATH" BASH_ENV="$BASH_ENV_FILE" "$SCRIPT" --mode tui --no-log '用 5 个字说 hi' 2>/tmp/tui_stderr)"
rc=$?
TUI_STDERR="$(cat /tmp/tui_stderr)"
rm -f /tmp/tui_stderr
assert_exit "TUI without orca-ide falls back, exit=0" 0 "$rc" "$out"
assert_grep "TUI without orca-ide returns codebuddy reply" '你好' "$out"
assert_grep "TUI without orca-ide prints warning to stderr" 'orca-ide' "$TUI_STDERR"

# 12) --mode acp (default) without orca-ide still works (regression: acp never
#     required orca-ide; this locks that in).
out="$(env -i HOME="$REAL_HOME" PATH="$TEST_PATH" BASH_ENV="$BASH_ENV_FILE" "$SCRIPT" --no-log '用 5 个字说 hi' 2>/dev/null)"
rc=$?
assert_exit "ACP without orca-ide still works" 0 "$rc" "$out"
assert_grep "ACP without orca-ide returns codebuddy reply" '你好' "$out"

# Cleanup fake bin + BASH_ENV
rm -rf "$(dirname "$FAKE_BIN")" "$BASH_ENV_FILE"

echo
echo "===== 0.1.8: friendly codebuddy-missing error ====="
# When `codebuddy` is genuinely not findable, the script should print a
# 5-line diagnostic listing CODEBUDDY_BIN / symlink / npm install paths
# and exit 4. We force this by setting an empty PATH (so command -v fails)
# plus a BASH_ENV that hides `command -v codebuddy` from the script's
# own bash process.
NO_CB_BASH_ENV="$(mktemp -t cb-smoke-no-cb-XXXXXX.sh)"
cat > "$NO_CB_BASH_ENV" <<'EOF'
command() {
  if [ "$1" = "-v" ] && [ "$2" = "codebuddy" ]; then
    return 1
  fi
  builtin command "$@"
}
EOF
# 13) --mode print with no codebuddy in PATH exits 4 with friendly
#     diagnostic on stderr.
out="$(env -i HOME="$REAL_HOME" PATH="/usr/bin:/bin" BASH_ENV="$NO_CB_BASH_ENV" \
  "$SCRIPT" --mode print --no-log '用 5 个字说 hi' 2>/tmp/no_cb_stderr)"
rc=$?
NO_CB_STDERR="$(cat /tmp/no_cb_stderr)"
rm -f /tmp/no_cb_stderr
assert_exit "no codebuddy exits 4" 4 "$rc" "$out"
assert_grep "no codebuddy diagnostic mentions CODEBUDDY_BIN" 'CODEBUDDY_BIN' "$NO_CB_STDERR"
assert_grep "no codebuddy diagnostic mentions npm install" 'npm i -g' "$NO_CB_STDERR"
assert_grep "no codebuddy diagnostic mentions the .codebuddy/bin confusion" 'CodeBuddy CN.app' "$NO_CB_STDERR"

# 14) Default acp mode with no codebuddy in PATH also exits 4 with the
#     same diagnostic (regression: P0-#1 fix must not regress this path).
out="$(env -i HOME="$REAL_HOME" PATH="/usr/bin:/bin" BASH_ENV="$NO_CB_BASH_ENV" \
  "$SCRIPT" --no-log '用 5 个字说 hi' 2>/tmp/no_cb_stderr2)"
rc=$?
rm -f /tmp/no_cb_stderr2
assert_exit "no codebuddy in acp mode exits 4" 4 "$rc" "$out"

# 15) CODEBUDDY_BIN env var overrides the missing-PATH check (the
#     script's behavior when CB_BIN resolves to a real binary). This
#     locks in the documented alt-install path.
out="$(env -i HOME="$REAL_HOME" PATH="/usr/bin:/bin" BASH_ENV="$NO_CB_BASH_ENV" \
  CODEBUDDY_BIN="$(command -v codebuddy)" \
  "$SCRIPT" --mode print --no-log '用 5 个字说 hi' 2>/dev/null)"
rc=$?
assert_exit "CODEBUDDY_BIN override reaches codebuddy, exit=0" 0 "$rc" "$out"
assert_grep "CODEBUDDY_BIN override returns codebuddy reply" '你好' "$out"

# Cleanup
rm -f "$NO_CB_BASH_ENV"

echo
echo "===== smoke.sh: $pass passed, $fail failed ====="
[ "$fail" -eq 0 ]
