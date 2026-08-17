#!/usr/bin/env bash
# invoke-codebuddy-bridge.sh - sync wrapper for codebuddy subagent calls.
#
# USAGE
#   invoke-codebuddy-bridge.sh "<task prompt>" [extra invoke-codebuddy flags...]
#
# WHAT IT DOES
#   单一调用:`invoke-codebuddy --json "$@"` (默认 acp 同步模式,~6s,带完整 status)
#   把 codebuddy 的 reply 打到 stdout,worker 把它原样 copy 到自己的 final answer。
#
# WHY IT EXISTS
#   用来给 mcode 的 `task` 工具 + `run_in_background=true` 派出的 worker 用:
#   - worker 跑这个 bridge,bridge 内部用 sync 模式等结果(5-30s)
#   - mcode 用 `<background-task-finished>` wake-up,worker 一回话 mcode 拿到结果
#   - 跟 mcode session 共生命周期,不会像 systemd-run/setsid 那样在 bash 退出时被杀
#
#   注意:这个 bridge 是"fire-and-collect on a worker",不是"fire-and-forget on
#   mcode 自己的 bash tool" — 后者已经由 mcode task 工具 + run_in_background 解决。
#   脚本不提供 --background 路径(参见 SKILL.md "Async pattern" 章节)。
#
# EXIT CODES (同 invoke-codebuddy)
#   0  codebuddy 返回了 reply
#   2  bad arguments
#   3  orca-ide 不可用且 mode=tui
#   4  codebuddy CLI 找不到
#   5  acp prompt timeout
#   6  codebuddy 报告 error
set -eo pipefail

if [ $# -lt 1 ]; then
  cat >&2 <<'EOF'
usage: invoke-codebuddy-bridge.sh "<task prompt>" [extra invoke-codebuddy flags...]

Examples:
  invoke-codebuddy-bridge.sh "translate to English: 你好世界"
  invoke-codebuddy-bridge.sh "summarize this spec: $(cat spec.md)"
  invoke-codebuddy-bridge.sh "review this Python LRU cache for race conditions"
  invoke-codebuddy-bridge.sh --mode print "short reply"   # 5s no orca-ide
  invoke-codebuddy-bridge.sh --model glm-5.2 "review ..." # 指定 model

The bridge runs invoke-codebuddy once (sync, JSON output) and prints the
codebuddy reply on stdout. It appends one line to
<plugin-root>/logs/invocations.log. Pass --no-log to the underlying
invoke-codebuddy to skip the log line.
EOF
  exit 2
fi

# Resolve sibling invoke-codebuddy: <plugin-root>/bin/invoke-codebuddy
# Falls back to $PATH for users who symlink the bridge instead of placing
# it next to invoke-codebuddy.
# 跨平台:macOS BSD readlink 不支持 -f,用 python3。
_self_realpath() { python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$1"; }
SELF="$(_self_realpath "$0")"
SELF_DIR="$(dirname "$SELF")"
INVOKE="$SELF_DIR/invoke-codebuddy"
if [ ! -x "$INVOKE" ]; then
  INVOKE="invoke-codebuddy"
fi
command -v "$INVOKE" >/dev/null 2>&1 || {
  echo "invoke-codebuddy-bridge.sh: cannot find invoke-codebuddy (looked in $SELF_DIR and \$PATH)" >&2
  exit 4
}

# 单一 sync 调用。bridge 不需要 --json,但传 --json 让 stdout 更稳(避免 stdout
# 同时含 events 调试行时解析混乱); worker 复制 stdout 的最后一段(就是 result)
# 到自己的 final answer。
exec "$INVOKE" --json "$@"
