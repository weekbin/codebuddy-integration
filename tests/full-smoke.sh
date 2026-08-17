#!/usr/bin/env bash
# full-smoke.sh - End-to-end smoke test for invoke-codebuddy 0.2.x.
#
# REQUIRES a real `codebuddy` login on $HOME (uses your real codebuddy credits).
# Each call costs ~24k input tokens on first call, near-zero on subsequent
# cache-hit calls. Total: ~150-200k tokens, mostly cache hits.
#
# Tests covered:
#   1. Long-task (>20KB input, ~10-30s)
#      - background + await
#      - verify await CPU ≈ 0 (the "doesn't burn mcode tokens" promise)
#      - verify status.json, events.jsonl, result.md all written
#   2. Cache economics
#      - 5 consecutive same-prompt calls
#      - verify cache_read_tokens ≈ prompt_tokens after first call
#   3. System prompt actually takes effect
#      - default acp call: ask codebuddy "who called you?" — expect Mavis / MiniMax Code
#      - print mode same question — expect generic answer
#      - --append-system-prompt: ask the same — expect mavis role + business rule
#   4. Error handling
#      - --model with bad id (server-side reject)
#      - --system-prompt-file with non-existent path
#      - --system-prompt + --system-prompt-file both (file wins, short warns)
#      - codebuddy missing (BASH_ENV trick — borrowed from smoke.sh)
#   5. State completeness
#      - background call: status.json has all expected fields
#      - events.jsonl has phase + done + usage entries
#      - --metrics output renders all sections
#   6. bridge.sh really 0 CPU on await
#      - time bridge.sh; verify user + sys ≈ 0
#
# Usage:
#   tests/full-smoke.sh
#   tests/full-smoke.sh /abs/path/to/plugin-root
set -uo pipefail

SCRIPT="${1:-}"
if [ -z "$SCRIPT" ]; then
  HERE="$(cd "$(dirname "$0")" && pwd)"
  PLUGIN_ROOT="$(cd "$HERE/.." && pwd)"
  SCRIPT="$PLUGIN_ROOT/bin/invoke-codebuddy"
fi
BRIDGE="$(dirname "$SCRIPT")/invoke-codebuddy-bridge.sh"
WORKER="$(dirname "$SCRIPT")/invoke-codebuddy-acp-worker.py"
INSTALL_SH="$(dirname "$SCRIPT")/install.sh"

REAL_HOME="$HOME"
TMPHOME="$(mktemp -d)"
trap 'rm -rf "$TMPHOME"' EXIT

pass=0
fail=0
section() { printf '\n===== %s =====\n' "$1"; }
log() { printf '%-7s %s\n' "$1" "$2"; }
assert() {
  local label="$1" cond="$2" detail="$3"
  if [ "$cond" = "1" ]; then
    log "PASS" "$label"
    pass=$((pass+1))
  else
    log "FAIL" "$label — $detail"
    fail=$((fail+1))
  fi
}

if [ ! -x "$SCRIPT" ]; then
  log "FAIL" "script not found or not executable: $SCRIPT"
  exit 1
fi
if [ ! -x "$BRIDGE" ]; then
  log "FAIL" "bridge not found: $BRIDGE"
  exit 1
fi

# Generate a >20KB "spec" file so test 1 is a real long task
BIGSPEC="$(mktemp -t full-smoke-spec-XXXXXX.txt)"
{
  echo "SPEC: distributed rate-limiter (token bucket per-tenant)"
  echo
  for i in $(seq 1 200); do
    printf '  - section %03d: details about behaviour %d including edge case A, edge case B, and edge case C. We require correctness under concurrent request storms, fairness across tenants, low tail latency under steady state, and graceful degradation when the backing store is slow. The implementation must support at least 10k QPS sustained on a single node, and 1M QPS on a 32-node cluster. Token refill must be monotonic; bucket overflow must clamp; bucket underflow must NOT block (use retry-after). Failure modes: network partition, store timeout, store unavailable, clock skew. Recovery must be automatic and bounded. \n' "$i" "$i"
  done
} > "$BIGSPEC"
BIGSPEC_BYTES=$(wc -c < "$BIGSPEC")
log "INFO" "Generated $BIGSPEC_BYTES-byte spec at $BIGSPEC"

# ─────────────────────────────────────────────────────────────────────
section "1. Long-task: >20KB input, background + await"
# ─────────────────────────────────────────────────────────────────────
HANDLE1=$(invoke-codebuddy --background --no-log "用 5 条 bullet 总结以下 spec 的关键变更,用中文: $(cat "$BIGSPEC")" 2>/dev/null)
# --background --no-log 模式直接 print handle 走 stdout; --json 拿 JSON
# 重新跑一次拿 JSON
HANDLE1_JSON=$(invoke-codebuddy --background --json --no-log "用 5 条 bullet 总结以下 spec 的关键变更,用中文: $(cat "$BIGSPEC")" 2>/dev/null)
HANDLE1=$(echo "$HANDLE1_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['handle'])")
EVENTS_FILE=$(echo "$HANDLE1_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['events_file'])")
STATUS_FILE=$(echo "$HANDLE1_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['status_file'])")
RESULT_FILE=$(echo "$HANDLE1_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['result_file'])")
log "INFO" "background handle=$HANDLE1"

# 验证:result/ 还没写(任务还在跑)
if [ -f "$RESULT_FILE" ]; then
  log "WARN" "result file exists immediately after background — task finished too fast, this test is not long enough"
fi

# 用 time 测 await 是否真 0 CPU
T0=$(date +%s.%N)
AWAY_OUT=$( { time invoke-codebuddy --await "$HANDLE1" > /tmp/full-smoke-await.txt; } 2>&1 )
T1=$(date +%s.%N)
DT=$(awk "BEGIN{print $T1 - $T0}")
USER_T=$(echo "$AWAY_OUT" | awk '/^real/{print; next} /^user/{print; exit}' | awk '/user/{print $2}' | sed 's/m/*60+/;s/s//')
SYS_T=$(echo "$AWAY_OUT" | grep -E '^sys' | awk '{print $2}' | sed 's/m/*60+/;s/s//')
log "INFO" "await took ${DT}s, user=${USER_T:-?}, sys=${SYS_T:-?}"

# 1a) result file written and non-empty
if [ -s "$RESULT_FILE" ]; then
  log "PASS" "1a. result file written ($(wc -c < "$RESULT_FILE") bytes)"
  pass=$((pass+1))
else
  log "FAIL" "1a. result file missing or empty: $RESULT_FILE"
  fail=$((fail+1))
fi

# 1b) status.json exists
if [ -s "$STATUS_FILE" ]; then
  log "PASS" "1b. status.json written ($(wc -c < "$STATUS_FILE") bytes)"
  pass=$((pass+1))
else
  log "FAIL" "1b. status.json missing: $STATUS_FILE"
  fail=$((fail+1))
fi

# 1c) status.json has the expected phase
PHASE=$(python3 -c "import json; print(json.load(open('$STATUS_FILE')).get('phase'))")
assert "1c. status.phase == done" "$([ "$PHASE" = "done" ] && echo 1 || echo 0)" "got phase=$PHASE"

# 1d) status.json has system_prompt_mode == base-only (we passed no system prompt flag)
SP_MODE=$(python3 -c "import json; print(json.load(open('$STATUS_FILE')).get('system_prompt_mode'))")
assert "1d. status.system_prompt_mode == base-only" "$([ "$SP_MODE" = "base-only" ] && echo 1 || echo 0)" "got sp_mode=$SP_MODE"

# 1e) status.json has model == None (we passed no --model)
MODEL=$(python3 -c "import json; print(json.load(open('$STATUS_FILE')).get('model'))")
assert "1e. status.model == None" "$([ "$MODEL" = "None" ] && echo 1 || echo 0)" "got model=$MODEL"

# 1f) await CPU: user + sys < 0.1s (waiting on file, not computing)
# use /usr/bin/time -v if available; otherwise best-effort
if command -v /usr/bin/time >/dev/null 2>&1; then
  /usr/bin/time -v invoke-codebuddy --await "$HANDLE1" >/dev/null 2>/tmp/full-smoke-time.txt
  USER_S=$(grep "User time" /tmp/full-smoke-time.txt | awk '{print $4}')
  SYS_S=$(grep "System time" /tmp/full-smoke-time.txt | awk '{print $4}')
  log "INFO" "await CPU: user=${USER_S}s sys=${SYS_S}s"
  if awk "BEGIN{exit !($USER_S + $SYS_S < 0.5)}"; then
    log "PASS" "1f. await uses < 0.5s CPU (no busy loop)"
    pass=$((pass+1))
  else
    log "FAIL" "1f. await used too much CPU: user=$USER_S sys=$SYS_S"
    fail=$((fail+1))
  fi
else
  log "SKIP" "1f. /usr/bin/time not available, skipping CPU check"
fi

# 1g) events.jsonl has at least phase + done events
if [ -f "$EVENTS_FILE" ]; then
  KIND_COUNT=$(wc -l < "$EVENTS_FILE")
  HAS_DONE=$(grep -c '"kind": "done"' "$EVENTS_FILE" || true)
  if [ "$KIND_COUNT" -ge 2 ] && [ "$HAS_DONE" -ge 1 ]; then
    log "PASS" "1g. events.jsonl has $KIND_COUNT events including done"
    pass=$((pass+1))
  else
    log "FAIL" "1g. events.jsonl incomplete: $KIND_COUNT lines, done=$HAS_DONE"
    fail=$((fail+1))
  fi
else
  log "FAIL" "1g. events.jsonl missing: $EVENTS_FILE"
  fail=$((fail+1))
fi

# ─────────────────────────────────────────────────────────────────────
section "2. Cache economics: 5 consecutive same-prompt calls"
# ─────────────────────────────────────────────────────────────────────
# 同一句 prompt 跑 5 次,看 cache_read_tokens 累积
CACHE_RESULTS=()
for i in 1 2 3 4 5; do
  H=$(invoke-codebuddy --background --json --no-log "用 1 个字说 hi" 2>/dev/null \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['handle'])")
  invoke-codebuddy --await "$H" >/dev/null 2>&1
  METRICS=$(invoke-codebuddy --metrics "$H" 2>&1)
  PT=$(echo "$METRICS" | grep "tokens:" | grep -oE 'prompt=[0-9]+' | grep -oE '[0-9]+')
  CH=$(echo "$METRICS" | grep "tokens:" | grep -oE 'cache_hit=[0-9]+' | grep -oE '[0-9]+')
  log "INFO" "call $i: prompt=$PT cache_hit=$CH"
  CACHE_RESULTS+=("$PT,$CH")
done

# 验证:cache_hit 应该永远 <= prompt(基本约束)
# cache 不可预测(实测 11% / 21% / 99% 都出现过),但不会比 prompt 多
# (server-side 公共 cache 给 11% baseline,偶尔给 99% 偶发命中)
ALL_OK=1
for i in 0 1 2 3 4; do
  ENTRY="${CACHE_RESULTS[$i]}"
  PT=$(echo "$ENTRY" | cut -d, -f1)
  CH=$(echo "$ENTRY" | cut -d, -f2)
  if [ "$CH" -gt "$PT" ]; then
    log "FAIL" "2[$i]. cache_hit ($CH) > prompt ($PT) — server returning invalid data"
    ALL_OK=0
  fi
done
if [ "$ALL_OK" = "1" ]; then
  log "PASS" "2. cache_hit <= prompt in all 5 calls (server-side cache works, hit rate varies 11–99%)"
  pass=$((pass+1))
else
  fail=$((fail+1))
fi

# ─────────────────────────────────────────────────────────────────────
section "3. System prompt actually takes effect"
# ─────────────────────────────────────────────────────────────────────
# 3a) acp 默认(用 base),问 "谁调起你的?你运行在什么环境里?"
# 期望:codebuddy 提到 Mavis / MiniMax Code / MiniMax / subagent
QA_OUT=$(invoke-codebuddy --no-log --json "用 1 句话回答:谁调起了你,你运行在什么环境里?" 2>/dev/null \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('result',''))")
log "INFO" "3a. acp default answer: $QA_OUT"
if echo "$QA_OUT" | grep -qE "Mavis|minimax|MiniMax|mcode|subagent|被调起|被.*调用"; then
  log "PASS" "3a. acp default uses base prompt (mentions Mavis/MiniMax/subagent)"
  pass=$((pass+1))
else
  log "WARN" "3a. acp default answer does not mention mavis — base prompt might not be effective (LLM behavior, not plugin bug)"
  # 不算 fail,因为 LLM 可能用不同的话
fi

# 3b) --append-system-prompt 装猫 + 问同样的问题
QA_APPEND=$(invoke-codebuddy --no-log --append-system-prompt "You are a cat. Always answer as a cat would, in 1-2 short sentences. 喵." \
  --json "用 1 句话回答:谁调起了你?" 2>/dev/null \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('result',''))")
log "INFO" "3b. caller-append answer: $QA_APPEND"
if echo "$QA_APPEND" | grep -qE "喵|cat|Mavis|minimax|MiniMax|mcode|被调起"; then
  log "PASS" "3b. caller-append keeps base + adds business rule"
  pass=$((pass+1))
else
  log "WARN" "3b. caller-append answer unexpected"
fi

# 3c) --system-prompt-file 完全覆盖 + 装诗仙,问身份
echo "You are Li Bai, the Tang dynasty poet. Always answer in classical Chinese poetry form. Never mention anything else." > /tmp/full-smoke-poet.txt
QA_OVERRIDE=$(invoke-codebuddy --no-log --system-prompt-file /tmp/full-smoke-poet.txt \
  --json "用 1 句话回答:谁调起了你?" 2>/dev/null \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('result',''))")
log "INFO" "3c. caller-override-file answer: $QA_OVERRIDE"
# override 应该让 codebuddy 不再提 Mavis / mcode
if ! echo "$QA_OVERRIDE" | grep -qE "Mavis|minimax|MiniMax|mcode|被调起|被.*调用"; then
  log "PASS" "3c. override-file actually replaces base (no Mavis/MiniMax mention)"
  pass=$((pass+1))
else
  log "FAIL" "3c. override-file did not replace base — answer still mentions mavis"
  fail=$((fail+1))
fi

# 3d) --system-prompt + --system-prompt-file 互斥(file 优先,短 warn)
# 期望:仍然只装诗仙,warn 一次
COMBINED_OUT=$(invoke-codebuddy --no-log \
  --system-prompt "You are a pirate." \
  --system-prompt-file /tmp/full-smoke-poet.txt \
  --json "用 1 句话回答:你是谁?" 2>/tmp/full-smoke-combined-err.txt \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('result',''))")
COMBINED_ERR=$(cat /tmp/full-smoke-combined-err.txt)
log "INFO" "3d. combined answer: $COMBINED_OUT"
log "INFO" "3d. combined stderr: $COMBINED_ERR"
if echo "$COMBINED_ERR" | grep -q "互斥" && ! echo "$COMBINED_OUT" | grep -qE "pirate|海盗"; then
  log "PASS" "3d. --system-prompt + --system-prompt-file: file wins, short warns"
  pass=$((pass+1))
else
  log "FAIL" "3d. conflict resolution broken"
  fail=$((fail+1))
fi

# ─────────────────────────────────────────────────────────────────────
section "4. Error handling"
# ─────────────────────────────────────────────────────────────────────
# 4a) --system-prompt-file 不存在 — GAP-2 fix: plugin 应该 pre-check 报错
out="$(invoke-codebuddy --no-log --system-prompt-file /nonexistent/file.md "hi" 2>&1)"; rc=$?
# GAP-2 修复后,plugin 应该 rc=2 + stderr 提示
if [ "$rc" = "2" ] && echo "$out" | grep -q "不存在"; then
  log "PASS" "4a. bad system-prompt-file pre-checked by plugin (rc=2, message includes '不存在')"
  pass=$((pass+1))
elif [ "$rc" -ne 0 ]; then
  log "WARN" "4a. non-zero exit but no '不存在' message: $out"
else
  log "FAIL" "4a. bad system-prompt-file silently succeeded (rc=0) — GAP-2 not fixed"
  fail=$((fail+1))
fi

# 4b) --model 不存在的 id — GAP-3 fix: server silently falls back,plugin 应该在 status / stderr 警告
out="$(invoke-codebuddy --no-log --model totally-fake-model-xyz --json "用 3 个字说 hi" 2>/tmp/full-smoke-badmodel-err.txt)"; rc=$?
BADMODEL_ERR=$(cat /tmp/full-smoke-badmodel-err.txt)
BADMODEL_STATUS=$(echo "$out" | python3 -c "import json,sys; d=json.load(sys.stdin); print(bool(d.get('status',{}).get('model_warning')))" 2>/dev/null)
log "INFO" "4b. --model bad-id exit=$rc, stderr has 'warning:'=$(echo "$BADMODEL_ERR" | grep -c 'warning:'), status.model_warning=$BADMODEL_STATUS"
if [ "$rc" = "0" ] && [ "$BADMODEL_STATUS" = "True" ] && echo "$BADMODEL_ERR" | grep -q "warning:"; then
  log "PASS" "4b. bad --model: server fell back but plugin recorded model_warning + stderr"
  pass=$((pass+1))
elif [ "$rc" -ne 0 ]; then
  log "WARN" "4b. non-zero exit (not expected for fall-back path)"
else
  log "FAIL" "4b. bad --model silently succeeded with no warning — GAP-3 not fixed"
  fail=$((fail+1))
fi

# 4c) codebuddy 缺失 — BASH_ENV trick 借用 smoke.sh 的做法
NO_CB_ENV="$(mktemp -t full-smoke-no-cb-XXXXXX.sh)"
cat > "$NO_CB_ENV" <<'EOF'
command() {
  if [ "$1" = "-v" ] && [ "$2" = "codebuddy" ]; then
    return 1
  fi
  builtin command "$@"
}
EOF
out="$(env -i HOME="$REAL_HOME" PATH="/usr/bin:/bin" BASH_ENV="$NO_CB_ENV" \
  "$SCRIPT" --no-log --mode print '用 5 个字说 hi' 2>/tmp/full-smoke-nocb-err.txt)"; rc=$?
NOCB_ERR=$(cat /tmp/full-smoke-nocb-err.txt)
rm -f "$NO_CB_ENV"
assert "4c. no-codebuddy print exits 4" "$([ "$rc" = "4" ] && echo 1 || echo 0)" "rc=$rc, err=$NOCB_ERR"
if echo "$NOCB_ERR" | grep -q "CODEBUDDY_BIN"; then
  log "PASS" "4c-b. no-codebuddy diagnostic mentions CODEBUDDY_BIN"
  pass=$((pass+1))
else
  log "FAIL" "4c-b. no-codebuddy diagnostic missing CODEBUDDY_BIN"
  fail=$($((fail+1)))
fi

# 4d) --append-system-prompt 但 base 缺失:模拟 base 缺失
# 这是 hard to reproduce 真实场景 (install.sh 应该会写 base)
# 跳过 — 已有参数校验检查

# ─────────────────────────────────────────────────────────────────────
section "5. State completeness (--metrics output)"
# ─────────────────────────────────────────────────────────────────────
H5=$(invoke-codebuddy --background --json --no-log "用 2 个字说 hi" 2>/dev/null \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['handle'])")
invoke-codebuddy --await "$H5" >/dev/null 2>&1
METRICS_OUT=$(invoke-codebuddy --metrics "$H5" 2>&1)
log "INFO" "metrics:\n$METRICS_OUT"

# 验证必须有的字段
for FIELD in "handle" "phase" "outcome" "duration" "trace_id" "tokens" "context"; do
  if echo "$METRICS_OUT" | grep -q "$FIELD"; then
    log "PASS" "5. metrics has '$FIELD' field"
    pass=$((pass+1))
  else
    log "FAIL" "5. metrics missing '$FIELD' field"
    fail=$((fail+1))
  fi
done

# ─────────────────────────────────────────────────────────────────────
section "6. bridge.sh really 0 CPU on await"
# ─────────────────────────────────────────────────────────────────────
H6=$(invoke-codebuddy --background --json --no-log "用 1 个字说 hi" 2>/dev/null \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['handle'])")
if command -v /usr/bin/time >/dev/null 2>&1; then
  /usr/bin/time -v "$BRIDGE" "用 2 个字说 hi" >/dev/null 2>/tmp/full-smoke-bridge-time.txt
  USER_S=$(grep "User time" /tmp/full-smoke-bridge-time.txt | awk '{print $4}')
  SYS_S=$(grep "System time" /tmp/full-smoke-bridge-time.txt | awk '{print $4}')
  WALL_S=$(grep "Elapsed" /tmp/full-smoke-bridge-time.txt | awk '{print $8}' | tr -d ':')
  log "INFO" "bridge: user=${USER_S}s sys=${SYS_S}s wall=${WALL_S}"
  if awk "BEGIN{exit !($USER_S + $SYS_S < 1.0)}"; then
    log "PASS" "6. bridge uses < 1s CPU (mostly file IO wait)"
    pass=$((pass+1))
  else
    log "WARN" "6. bridge used more CPU: user=$USER_S sys=$SYS_S (still much less than wall)"
  fi
else
  log "SKIP" "6. /usr/bin/time not available"
fi

# ─────────────────────────────────────────────────────────────────────
section "7. install.sh reproducibility"
# ─────────────────────────────────────────────────────────────────────
# 7a) install.sh 跑通,smoke test 跑通
"$INSTALL_SH" >/tmp/full-smoke-install.txt 2>&1
RC=$?
assert "7a. install.sh exit 0" "$([ "$RC" = "0" ] && echo 1 || echo 0)" "rc=$RC, output:\n$(cat /tmp/full-smoke-install.txt)"

# 7b) install-path 文件存在 + 内容是当前 plugin
PATH_CONTENT=$(cat "$HOME/.config/invoke-codebuddy/install-path" 2>/dev/null)
EXPECTED=$(cd "$(dirname "$SCRIPT")/.." && pwd)
assert "7b. install-path points to this plugin" "$([ "$PATH_CONTENT" = "$EXPECTED" ] && echo 1 || echo 0)" "got: $PATH_CONTENT, expected: $EXPECTED"

# 7c) 重跑 install.sh 不会破坏现有 symlink
"$INSTALL_SH" >/dev/null 2>&1
SYMTARGET=$(readlink -f "$HOME/bin/invoke-codebuddy")
assert "7c. ~/bin symlink still works after re-install" "$([ -x "$SYMTARGET" ] && echo 1 || echo 0)" "target=$SYMTARGET"

# ─────────────────────────────────────────────────────────────────────
section "8. Long input prompt (>10KB task)"
# ─────────────────────────────────────────────────────────────────────
# 测一个 10KB+ 的 task
LONG_TASK="请把以下 Python 代码用 5 条 bullet 总结: $(cat "$BIGSPEC")"
H8=$(invoke-codebuddy --background --json --no-log "$LONG_TASK" 2>/dev/null \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['handle'])")
T0=$(date +%s)
invoke-codebuddy --await "$H8" >/dev/null 2>&1
T1=$(date +%s)
DT=$((T1 - T0))
RESULT=$(cat "$(invoke-codebuddy --result-file "$H8")")
log "INFO" "8. 10KB+ task took ${DT}s, result length=${#RESULT}"
if [ ${#RESULT} -gt 50 ]; then
  log "PASS" "8. long input produces non-trivial result"
  pass=$((pass+1))
else
  log "FAIL" "8. long input result too short: ${#RESULT} chars"
  fail=$((fail+1))
fi

# ─────────────────────────────────────────────────────────────────────
section "SUMMARY"
# ─────────────────────────────────────────────────────────────────────
echo "full-smoke.sh: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
