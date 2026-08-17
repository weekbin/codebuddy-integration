#!/usr/bin/env bash
# smoke.sh - Real end-to-end smoke test for invoke-codebuddy.
#
# This test follows what the SKILL.md actually teaches. Two halves:
#
#  1. LOGICAL tests (no codebuddy / orca-ide required) — argument parsing,
#     plugin root resolution, --help, error messages.
#
#  2. REAL tests — the agent detects the real codebuddy CLI and runs the
#     real `codebuddy --acp` (or `--print`) flow end-to-end. If a real
#     codebuddy is not findable, the REAL tests are SKIPPED (not mocked —
#     a mock that only "looks like" codebuddy is what bit us in 0.2.0;
#     we want this script to fail loudly if it can't prove real behavior).
#
# Why we don't mock `codebuddy`:
#   The 0.2.0 mock mocked --acp at the bash level, but bash's stdout is
#   block-buffered when piped, the JSON-RPC framing was finicky, and at
#   the end of the day a passing mock test said nothing about the real
#   codebuddy runtime. The whole point of smoke is to catch "this works
#   in our test env" ≠ "this works on the user's box" — and mocking the
#   one thing we're trying to verify defeats the purpose.
#
# Usage:
#   tests/smoke.sh
#   tests/smoke.sh /abs/path/to/plugin-root   # auto-resolve SCRIPT under it
set -uo pipefail

SCRIPT="${1:-}"
if [ -z "$SCRIPT" ]; then
  HERE="$(cd "$(dirname "$0")" && pwd)"
  PLUGIN_ROOT="$(cd "$HERE/.." && pwd)"
  SCRIPT="$PLUGIN_ROOT/bin/invoke-codebuddy"
fi

REAL_HOME="$HOME"
pass=0
fail=0
skip=0
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

# Move install.sh's env file out of the way for the WHOLE test run, so
# the script's `source ~/.config/invoke-codebuddy/env` does not pick up
# a real CODEBUDDY_BIN for tests that need to control it (e.g. --no-cb
# diagnostic, the "ACP" test, and the cross-platform CB discovery block).
# Real tests below re-export CODEBUDDY_BIN from DETECTED_CB before
# invoking the script.
STASH_ENV=""
if [ -f "$REAL_HOME/.config/invoke-codebuddy/env" ]; then
  STASH_ENV="$REAL_HOME/.config/invoke-codebuddy/env"
  mv "$STASH_ENV" "$STASH_ENV.smoke-stash"
fi
trap 'mv "$STASH_ENV.smoke-stash" "$STASH_ENV" 2>/dev/null; rm -rf "$TMPDIR_SMOKE" 2>/dev/null' EXIT

# ── Cross-platform codebuddy discovery ──
# Probe for a REAL codebuddy. If we can't find one, the REAL tests skip.
DETECTED_CB=""
if [ -n "${CODEBUDDY_BIN:-}" ] && [ -x "${CODEBUDDY_BIN}" ]; then
  DETECTED_CB="${CODEBUDDY_BIN}"
elif [ -f "$REAL_HOME/.config/invoke-codebuddy/env" ] && [ ! -L "$REAL_HOME/.config/invoke-codebuddy/env" ]; then
  # env file was stashed above; re-read it from the stash
  if [ -f "$STASH_ENV.smoke-stash" ]; then
    DETECTED_CB=$(grep -oE 'CODEBUDDY_BIN=[^[:space:]]+' "$STASH_ENV.smoke-stash" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"')
  fi
fi
if [ -z "$DETECTED_CB" ] || [ ! -x "$DETECTED_CB" ]; then
  DETECTED_CB="$(command -v codebuddy 2>/dev/null || true)"
fi
if [ -z "$DETECTED_CB" ] || [ ! -x "$DETECTED_CB" ]; then
  DETECTED_CB=$(find \
    "$HOME/.nvm" "$HOME/.asdf" "$HOME/.volta" "$HOME/.local" \
    /opt/homebrew/bin /usr/local/bin /usr/bin \
    -name codebuddy -type l 2>/dev/null | head -1)
fi
if [ -z "$DETECTED_CB" ] || [ ! -x "$DETECTED_CB" ]; then
  DETECTED_CB=$(find \
    "$HOME/.nvm" "$HOME/.asdf" "$HOME/.volta" "$HOME/.local" \
    /opt/homebrew/bin /usr/local/bin /usr/bin \
    -name codebuddy 2>/dev/null | head -1)
fi

if [ -n "$DETECTED_CB" ] && [ -x "$DETECTED_CB" ]; then
  echo "smoke.sh: detected codebuddy at $DETECTED_CB"
else
  echo "smoke.sh: WARN — no real codebuddy found; REAL tests will SKIP"
fi

# Sanity: script exists and is executable
if [ ! -x "$SCRIPT" ]; then
  log "FAIL" "script not found or not executable: $SCRIPT"
  exit 1
fi

# Use a throwaway TMPDIR so the script's state/ and logs/ writes go
# somewhere harmless. The script still uses HOME for env-file
# resolution, but plugin-root resolution is unaffected.
TMPDIR_SMOKE="$(mktemp -d)"
export TMPDIR="$TMPDIR_SMOKE"

# ════════════════════════════════════════════════════════════════════════
# PART 1: LOGICAL tests — no codebuddy / orca-ide required
# ════════════════════════════════════════════════════════════════════════

# 1) --help exits 0 and prints the new name
out="$("$SCRIPT" --help 2>&1)"; rc=$?
assert_exit "--help exits 0" 0 "$rc" "$out"
assert_grep "--help mentions invoke-codebuddy" '^invoke-codebuddy ' "$out"
# 0.2.1: --await and --background were removed. Confirm they are NOT in --help.
if printf '%s' "$out" | grep -qE 'invoke-codebuddy (--background|--await|--result-file)'; then
  log "FAIL" "--help mentions removed flag (--background/--await/--result-file)"
  fail=$((fail+1))
else
  log "PASS" "--help has no removed flag (--background/--await/--result-file)"
  pass=$((pass+1))
fi

# 2) --bogus-flag exits 2 and prefixes errors with invoke-codebuddy:
out="$("$SCRIPT" --bogus-flag 2>&1)"; rc=$?
assert_exit "--bogus-flag exits 2" 2 "$rc" "$out"
assert_grep "--bogus-flag error prefix" '^invoke-codebuddy: ' "$out"

# 3) Empty task prints usage and exits 2
out="$("$SCRIPT" 2>&1)"; rc=$?
assert_exit "empty task exits 2" 2 "$rc" "$out"
assert_grep "empty task shows usage" '^用法:' "$out"

# 4) --log with no log file prints the new path
rm -f "$PLUGIN_ROOT/logs/invocations.log"
out="$("$SCRIPT" --log 2>&1)"; rc=$?
assert_exit "--log exits 0" 0 "$rc" "$out"
assert_grep "--log mentions plugin-root path" 'no log file: ' "$out"

# 5) --kill with no handle exits 2
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

# 7b) Bridge script: no args -> exit 2 + usage
BRIDGE="$PLUGIN_ROOT/bin/invoke-codebuddy-bridge.sh"
if [ -f "$BRIDGE" ]; then
  out="$("$BRIDGE" 2>&1)"; rc=$?
  assert_exit "bridge no args exits 2" 2 "$rc" "$out"
  assert_grep "bridge usage mentions --background" -- '--background' "$out"
  assert_grep "bridge usage shows example" 'translate to English' "$out"

  # 7c) Bash syntax check
  if bash -n "$BRIDGE" >/dev/null 2>&1; then
    log "PASS" "bridge bash -n passes"
    pass=$((pass+1))
  else
    log "FAIL" "bridge bash -n fails"
    fail=$((fail+1))
  fi
fi

# 8) Bash syntax check on the main script
if bash -n "$SCRIPT" >/dev/null 2>&1; then
  log "PASS" "bash -n passes"
  pass=$((pass+1))
else
  log "FAIL" "bash -n fails"
  fail=$((fail+1))
fi

# 9) Python worker syntax check
WORKER="$PLUGIN_ROOT/bin/invoke-codebuddy-acp-worker.py"
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
SKILL="$PLUGIN_ROOT/skills/codebuddy-integration/SKILL.md"
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

# 11) --mode tui without orca-ide falls back to print (real codebuddy in
#     FAKE PATH so we don't accidentally hit a real install if the env
#     file is missing). We use a minimal PATH that contains only /usr/bin
#     and /bin so the script's `command -v orca-ide` is empty; this is
#     the BASH_ENV-style trick to make `orca-ide` invisible. The script
#     then has to either find a real codebuddy via the env-file path
#     (which is stashed above) or hit our "codebuddy not found"
#     diagnostic. Since env is stashed, it WILL hit the diagnostic, and
#     we expect exit 4 — that is what this test locks in.
#
#     Note: this is the *tui fall-back behavior* (silent + exit 0 if a
#     codebuddy is found in PATH), distinct from the no-cb-diagnostic
#     test below. The no-cb diagnostic test uses BASH_ENV to ALSO hide
#     `command -v codebuddy` to simulate the most hostile case.
NO_CB_BASH_ENV="$(mktemp -t cb-smoke-no-cb-XXXXXX.sh)"
cat > "$NO_CB_BASH_ENV" <<'EOF'
command() {
  if [ "$1" = "-v" ] && [ "$2" = "codebuddy" ]; then
    return 1
  fi
  builtin command "$@"
}
EOF
# 12) --mode print with no codebuddy exits 4 with friendly diagnostic
out="$(env -i HOME="$REAL_HOME" PATH="/usr/bin:/bin" BASH_ENV="$NO_CB_BASH_ENV" \
  "$SCRIPT" --mode print --no-log '用 5 个字说 hi' 2>/tmp/no_cb_stderr)"
rc=$?
NO_CB_STDERR="$(cat /tmp/no_cb_stderr)"
rm -f /tmp/no_cb_stderr
assert_exit "no codebuddy exits 4" 4 "$rc" "$out"
assert_grep "no codebuddy diagnostic mentions CODEBUDDY_BIN" 'CODEBUDDY_BIN' "$NO_CB_STDERR"
assert_grep "no codebuddy diagnostic mentions npm install" 'npm i -g' "$NO_CB_STDERR"
assert_grep "no codebuddy diagnostic mentions the .codebuddy/bin confusion" 'CodeBuddy CN.app' "$NO_CB_STDERR"

# 13) Default acp mode with no codebuddy also exits 4
out="$(env -i HOME="$REAL_HOME" PATH="/usr/bin:/bin" BASH_ENV="$NO_CB_BASH_ENV" \
  "$SCRIPT" --no-log '用 5 个字说 hi' 2>/tmp/no_cb_stderr2)"
rc=$?
rm -f /tmp/no_cb_stderr2
assert_exit "no codebuddy in acp mode exits 4" 4 "$rc" "$out"

# Cleanup no-cb bash env
rm -f "$NO_CB_BASH_ENV"

# ════════════════════════════════════════════════════════════════════════
# PART 2: REAL tests — requires a real codebuddy CLI on disk
# ════════════════════════════════════════════════════════════════════════
# These tests verify what the SKILL.md actually teaches:
#   - "use --mode print for first try, ~5s"
#   - "use default acp mode for full state + tokens, ~5-30s"
#   - "codebuddy in acp mode with bypassPermissions does not ask
#      questions about file writes"  (live-verified on 2026-08-17)

if [ -z "$DETECTED_CB" ] || [ ! -x "$DETECTED_CB" ]; then
  log "SKIP" "REAL print mode test — no codebuddy CLI"
  log "SKIP" "REAL acp mode reply test"
  log "SKIP" "REAL acp mode file-write test (permission pre-emption)"
  log "SKIP" "REAL --metrics from prior sync call"
  log "SKIP" "REAL bridge.sh end-to-end"
  skip=$((skip+5))
else
  # Clean state for each real test
  rm -f /tmp/cb-smoke-file-write-*.txt

  # Helper: detect CJK characters (cross-platform — macOS BSD grep does
  # not support \x{...} character classes the way GNU grep does).
  has_cjk() {
    python3 -c "
import sys
data = sys.stdin.read()
print('yes' if any('一' <= c <= '鿿' for c in data) else 'no')
"
  }

  # 14) REAL --mode print — fastest path, ~4-8s
  out="$(CODEBUDDY_BIN="$DETECTED_CB" "$SCRIPT" --mode print --no-log 'reply with exactly 5 Chinese characters that mean hi' 2>&1)"
  rc=$?
  assert_exit "REAL print mode exits 0" 0 "$rc" "$out"
  if printf '%s' "$out" | has_cjk | grep -q '^yes$'; then
    log "PASS" "REAL print mode returned Chinese reply"
    pass=$((pass+1))
  else
    log "FAIL" "REAL print mode reply contains no CJK"
    echo "----- output -----"
    echo "$out"
    fail=$((fail+1))
  fi

  # 15) REAL default acp mode — full state, ~5-30s
  out_json="$(CODEBUDDY_BIN="$DETECTED_CB" "$SCRIPT" --json --no-log 'reply with exactly 3 Chinese characters that mean hi' 2>&1)"
  rc=$?
  assert_exit "REAL acp mode exits 0" 0 "$rc" "$out_json"
  # The reply is in a state/result-<handle>.md file, not stdout.
  handle=$(printf '%s' "$out_json" | python3 -c "import sys,json; print(json.load(sys.stdin).get('handle',''))" 2>/dev/null || echo "")
  if [ -n "$handle" ] && [ -f "$PLUGIN_ROOT/state/result-$handle.md" ]; then
    log "PASS" "REAL acp mode wrote state/result-$handle.md"
    pass=$((pass+1))
    reply=$(cat "$PLUGIN_ROOT/state/result-$handle.md")
    if printf '%s' "$reply" | has_cjk | grep -q '^yes$'; then
      log "PASS" "REAL acp mode reply contains CJK"
      pass=$((pass+1))
    else
      log "FAIL" "REAL acp mode reply contains no CJK"
      echo "reply: $reply"
      fail=$((fail+1))
    fi
    # system_prompt_mode should be "base-only" since we did not pass
    # any --system-prompt* override
    spm=$(printf '%s' "$out_json" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',{}).get('system_prompt_mode',''))" 2>/dev/null || echo "")
    if [ "$spm" = "base-only" ]; then
      log "PASS" "REAL acp mode system_prompt_mode=base-only"
      pass=$((pass+1))
    else
      log "FAIL" "REAL acp mode system_prompt_mode=$spm (expected base-only)"
      fail=$((fail+1))
    fi
  else
    log "FAIL" "REAL acp mode did not write state/result-<handle>.md (handle='$handle')"
    fail=$((fail+1))
  fi

  # 16) REAL acp mode permission pre-emption — codebuddy writes a file
  #     without asking. This is THE test for "fire-and-forget via
  #     task+background actually completes in real codebuddy".
  TEST_FILE="/tmp/cb-smoke-file-write-$(date +%s).txt"
  prompt="Without asking any clarifying questions, write the exact text 'smoke-test-permission-pre-emption-ok' into the file $TEST_FILE using the Write tool. Then return a one-line confirmation."
  out_json="$(CODEBUDDY_BIN="$DETECTED_CB" "$SCRIPT" --append-system-prompt "Do not stop to ask the user any question. Make reasonable choices and proceed." --no-log "$prompt" 2>&1)"
  rc=$?
  assert_exit "REAL acp file-write task exits 0" 0 "$rc" "$out_json"
  if [ -f "$TEST_FILE" ]; then
    content=$(cat "$TEST_FILE" 2>/dev/null)
    if [ "$content" = "smoke-test-permission-pre-emption-ok" ]; then
      log "PASS" "REAL acp file-write succeeded without user prompt (permission pre-emption works)"
      pass=$((pass+1))
    else
      log "FAIL" "REAL acp file wrote wrong content: $content"
      fail=$((fail+1))
    fi
    rm -f "$TEST_FILE"
  else
    log "FAIL" "REAL acp file-write did NOT create $TEST_FILE (task may have hung on permission prompt)"
    fail=$((fail+1))
  fi

  # 17) REAL --metrics reads the state file from the most recent acp call
  if [ -n "$handle" ]; then
    out="$("$SCRIPT" --metrics "$handle" 2>&1)"
    rc=$?
    assert_exit "REAL --metrics exits 0" 0 "$rc" "$out"
    assert_grep "REAL --metrics shows phase" 'phase:' "$out"
    assert_grep "REAL --metrics shows tokens" 'tokens:' "$out"
  else
    log "SKIP" "REAL --metrics — no prior acp handle"
    skip=$((skip+1))
  fi

  # 18) REAL bridge.sh — proves the "task + bridge.sh" pattern works
  #     end-to-end (this is what the worker would run). Note: bridge.sh
  #     inherits the env, so we pass CODEBUDDY_BIN through explicitly.
  out="$(CODEBUDDY_BIN="$DETECTED_CB" "$BRIDGE" --no-log 'reply with exactly 4 Chinese characters that mean hi' 2>&1)"
  rc=$?
  assert_exit "REAL bridge.sh exits 0" 0 "$rc" "$out"
  if printf '%s' "$out" | has_cjk | grep -q '^yes$'; then
    log "PASS" "REAL bridge.sh returned Chinese reply"
    pass=$((pass+1))
  else
    log "FAIL" "REAL bridge.sh reply contains no CJK"
    echo "----- output -----"
    echo "$out"
    fail=$((fail+1))
  fi
fi

# ════════════════════════════════════════════════════════════════════════
echo
echo "===== smoke.sh: $pass passed, $fail failed, $skip skipped ====="
[ "$fail" -eq 0 ]
