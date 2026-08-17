#!/usr/bin/env bash
# install.sh - 把 invoke-codebuddy 安装到 ~/bin,固定 plugin 路径
#
# 这个脚本做的事:
#   1. 探测当前 plugin 的根目录(从本脚本位置反推)
#   2. 把 plugin 根写到 $HOME/.config/invoke-codebuddy/install-path
#      (这样不管 ~/bin/invoke-codebuddy 的 symlink 指向哪个版本,运行时
#       state/ 和 logs/ 都写在这个 plugin 目录里)
#   3. ln -sfn <plugin-root>/bin/invoke-codebuddy ~/bin/invoke-codebuddy
#   4. 验证: invoke-codebuddy --help 跑得动
#
# 跑法: 在 plugin 根目录下 ./bin/install.sh
#       或在任何地方: /path/to/plugin/bin/install.sh
#       重装/同步: 直接再跑一次(覆盖式)
#
# 卸载:  rm ~/bin/invoke-codebuddy
#        rm -rf $HOME/.config/invoke-codebuddy
set -eo pipefail

# 探测 plugin 根 (本脚本在 <root>/bin/install.sh)
SCRIPT_PATH="$(readlink -f "$0")"
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

# 验证可执行
if ! command -v invoke-codebuddy >/dev/null 2>&1; then
  echo "install.sh: WARN - $TARGET_BIN_DIR 不在 PATH,需要手动加:" >&2
  echo "  export PATH=\"\$HOME/bin:\$PATH\"   (加到 ~/.bashrc 或 ~/.zshrc)" >&2
fi

# 跑一下 --help 验证
echo
echo "=== smoke test: invoke-codebuddy --help ==="
invoke-codebuddy --help 2>&1 | head -20
RC=${PIPESTATUS[0]}
echo "=== exit: $RC ==="

if [ $RC -ne 0 ]; then
  echo "install.sh: ERROR - invoke-codebuddy --help 跑挂了" >&2
  exit 1
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
echo "卸载:"
echo "  rm $TARGET"
echo "  rm -rf $CONFIG_DIR"
