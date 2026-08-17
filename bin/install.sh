#!/usr/bin/env bash
# install.sh - 把 invoke-codebuddy 安装到 ~/bin,固定 plugin 路径
#
# 这个脚本做的事:
#   1. 探测当前 plugin 的根目录(从本脚本位置反推)
#   2. 把 plugin 根写到 $HOME/.config/invoke-codebuddy/install-path
#      (这样不管 ~/bin/invoke-codebuddy 的 symlink 指向哪个版本,运行时
#       state/ 和 logs/ 都写在这个 plugin 目录里)
#   3. ln -sfn <plugin-root>/bin/invoke-codebuddy ~/bin/invoke-codebuddy
#   4. 探测 codebuddy CLI 位置,写 $HOME/.config/invoke-codebuddy/env
#      (主脚本会 source 它,免去每次 export CODEBUDDY_BIN)
#   5. 自动 export PATH 到 ~/.zshrc 和 ~/.bashrc (用 marker 块,只写一次)
#   6. 验证: invoke-codebuddy --help 跑得动
#
# 跑法: 在 plugin 根目录下 ./bin/install.sh
#       或在任何地方: /path/to/plugin/bin/install.sh
#       重装/同步: 直接再跑一次(覆盖式,marker 块会先删后写)
#
# 卸载:  rm ~/bin/invoke-codebuddy
#        rm -rf $HOME/.config/invoke-codebuddy
#        从 ~/.zshrc 和 ~/.bashrc 删掉 invoke-codebuddy install marker 块
set -eo pipefail

# 跨平台 readlink 兼容(macOS BSD readlink 不支持 -f)
_realpath() { python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$1"; }
SCRIPT_PATH="$(_realpath "$0")"
SCRIPT_DIR="$(dirname "$SCRIPT_PATH")"
PLUGIN_ROOT="$(dirname "$SCRIPT_DIR")"

# 校验 plugin 根是不是长得对(有 assets/, skills/, plugin.json)
if [ ! -d "$PLUGIN_ROOT/assets" ] || [ ! -d "$PLUGIN_ROOT/skills" ] || [ ! -f "$PLUGIN_ROOT/plugin.json" ]; then
  echo "install.sh: ERROR - $PLUGIN_ROOT 不像是个完整的 codebuddy-integration plugin 目录" >&2
  echo "  (缺少 assets/、skills/、plugin.json 其中之一)" >&2
  exit 1
fi

# 决定 ~/bin 路径(优先 $HOME/bin,其次第一个在 PATH 上的 ~/X/bin,最后 fallback $HOME/.local/bin)
TARGET_BIN_DIR="$HOME/bin"
if [ ! -d "$TARGET_BIN_DIR" ]; then
  # PATH 上没有 ~/bin,选第一个匹配的
  for d in $(echo "$PATH" | tr ':' ' '); do
    case "$d" in
      "$HOME"/*) TARGET_BIN_DIR="$d"; break ;;
    esac
  done
fi
# 如果还是没找到,fallback 到 ~/.local/bin
[ -d "$TARGET_BIN_DIR" ] || TARGET_BIN_DIR="$HOME/.local/bin"
mkdir -p "$TARGET_BIN_DIR"

# 写 install-path 配置文件
CONFIG_DIR="$HOME/.config/invoke-codebuddy"
mkdir -p "$CONFIG_DIR"
echo "$PLUGIN_ROOT" > "$CONFIG_DIR/install-path"
echo "install.sh: wrote $CONFIG_DIR/install-path -> $PLUGIN_ROOT"

# ln -sfn 到 target bin
TARGET="$TARGET_BIN_DIR/invoke-codebuddy"
if [ -e "$TARGET" ] && [ ! -L "$TARGET" ]; then
  # 真实文件存在,先备份
  BACKUP="${TARGET}.bak.$(date +%s)"
  mv "$TARGET" "$BACKUP"
  echo "install.sh: backed up existing $TARGET to $BACKUP"
fi
ln -sfn "$PLUGIN_ROOT/bin/invoke-codebuddy" "$TARGET"
echo "install.sh: symlinked $TARGET -> $PLUGIN_ROOT/bin/invoke-codebuddy"

# ── 探测 codebuddy CLI 位置,写 env 配置文件 ───────────
# 跨平台探测顺序:
#   1. command -v codebuddy(已经在 PATH 就不用探测)
#   2. macOS 常见: ~/.nvm/versions/node/*/bin/codebuddy, /opt/homebrew/bin/codebuddy
#   3. Linux 常见: ~/.nvm/versions/node/*/bin/codebuddy, /usr/local/bin/codebuddy
#   4. ~/.local/bin/codebuddy (pipx / npm local)
ENV_FILE="$CONFIG_DIR/env"
if command -v codebuddy >/dev/null 2>&1; then
  DETECTED_CB="$(command -v codebuddy)"
  echo "install.sh: codebuddy already on PATH at $DETECTED_CB"
  # 不写 env(用户已经在 PATH)。主脚本 source env 文件时如果不存在就直接跳过。
  # 但保留一个空 marker 让 plugin root + env 关系显式存在
  [ -f "$ENV_FILE" ] || cat > "$ENV_FILE" <<EOF
# codebuddy CLI already on PATH (no CODEBUDDY_BIN override needed).
# Detected: $DETECTED_CB
EOF
else
  # 跨平台 find 路径
  DETECTED_CB=$(find \
    "$HOME/.nvm" "$HOME/.local" \
    /opt/homebrew/bin /usr/local/bin /usr/bin \
    -name codebuddy -type l 2>/dev/null | head -1 || true)
  if [ -n "$DETECTED_CB" ] && [ -x "$DETECTED_CB" ]; then
    echo "install.sh: detected codebuddy at $DETECTED_CB"
    printf 'export CODEBUDDY_BIN=%q\n' "$DETECTED_CB" > "$ENV_FILE"
    echo "install.sh: wrote $ENV_FILE (export CODEBUDDY_BIN=$DETECTED_CB)"
  else
    cat > "$ENV_FILE" <<'EOF'
# codebuddy CLI not auto-detected. Run one of:
#   npm i -g @tencent-ai/codebuddy-code
#   or: export CODEBUDDY_BIN=/abs/path/to/codebuddy
EOF
    echo "install.sh: codebuddy CLI not found; wrote hint to $ENV_FILE"
    echo "  → 用户需要装 codebuddy CLI 后重跑 install.sh" >&2
  fi
fi

# ── 自动 export PATH 到 ~/.zshrc 和 ~/.bashrc (用 marker 块) ───────────
# 跨平台 shell rc 探测:
#   - macOS 默认 zsh,写 ~/.zshrc
#   - Linux 默认 bash,写 ~/.bashrc
#   - 两边都写保险(marker 保证不重复)
PATH_MARKER_BEGIN="# >>> invoke-codebuddy install >>>"
PATH_MARKER_END="# <<< invoke-codebuddy install <<<"
PATH_BLOCK="${PATH_MARKER_BEGIN}
# Added by codebuddy-integration plugin (idempotent — re-running install.sh is safe)
[ -d \"$TARGET_BIN_DIR\" ] && export PATH=\"$TARGET_BIN_DIR:\$PATH\"
${PATH_MARKER_END}"

write_path_block() {
  local rc="$1"
  # 如果 marker 存在,先删旧块(避免重复 export)
  if [ -f "$rc" ] && grep -q "$PATH_MARKER_BEGIN" "$rc" 2>/dev/null; then
    # 用 awk 删 marker 块(begin/end 整段)
    local tmp
    tmp=$(mktemp)
    awk -v begin="$PATH_MARKER_BEGIN" -v end="$PATH_MARKER_END" '
      $0 == begin { skip = 1; next }
      $0 == end   { skip = 0; next }
      !skip
    ' "$rc" > "$tmp" && mv "$tmp" "$rc"
  fi
  # 追加新块
  printf '\n%s\n' "$PATH_BLOCK" >> "$rc"
  echo "install.sh: wrote PATH block to $rc"
}

PATH_RC_WRITTEN=0
[ -f "$HOME/.zshrc" ] && { write_path_block "$HOME/.zshrc"; PATH_RC_WRITTEN=1; }
[ -f "$HOME/.bashrc" ] && { write_path_block "$HOME/.bashrc"; PATH_RC_WRITTEN=1; }
# 如果 .zshrc / .bashrc 都不存在(zsh 用户没有 ~/.zshrc / bash 用户没有 ~/.bashrc),
# 兜底:创建 ~/.zshrc(因为 macOS 新版默认 zsh)。
if [ "$PATH_RC_WRITTEN" = 0 ]; then
  if [ -n "${ZSH_VERSION:-}" ] || [ "$(basename "${SHELL:-/bin/zsh}")" = "zsh" ]; then
    write_path_block "$HOME/.zshrc"
  else
    write_path_block "$HOME/.bashrc"
  fi
fi

# 验证可执行(用 source env + 调脚本,模拟新 shell 拿到 CODEBUDDY_BIN 的场景)
# 不用 set -e 包这个,因为我们想看到 warn 而不是 fail。
echo
echo "=== smoke test: invoke-codebuddy --help ==="
if [ -f "$ENV_FILE" ] && [ -s "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  ( set +e; . "$ENV_FILE" 2>/dev/null; "$TARGET" --help 2>&1 | head -20; echo "=== exit: $? ===" )
else
  ( set +e; "$TARGET" --help 2>&1 | head -20; echo "=== exit: $? ===" )
fi

echo
echo "安装完成。"
echo
echo "使用:"
echo "  invoke-codebuddy --mode print \"翻译成英文: 你好世界\"     # 推荐 first try"
echo "  invoke-codebuddy --model glm-5.2 \"review this code\"      # 指定 model"
echo "  invoke-codebuddy --append-system-prompt \"你是严格 reviewer\" \"review this code\"  # 业务 system prompt"
echo
echo "plugin 根(状态/日志写在这里): $PLUGIN_ROOT"
echo "codebuddy CLI: ${DETECTED_CB:-未检测到(见 $ENV_FILE 提示)}"
echo "卸载:"
echo "  rm $TARGET"
echo "  rm -rf $CONFIG_DIR"
echo "  从 ~/.zshrc / ~/.bashrc 删 marker 块(begin: '$PATH_MARKER_BEGIN')"
