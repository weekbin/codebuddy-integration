# codebuddy-integration

> 给 mcode 增加一个调用 codebuddy 的入口，处理翻译、长文摘要、寻求不同意见、
> 换思路重写代码这类纯文字任务。0.3.0 通过 stdio MCP 实现，mcode 启动时
> 自动加载 `mcp.json`，长驻一个 `codebuddy --acp` 子进程，首次调用预热缓存后
> 后续调用命中率稳定在 98-99%。

## 快速试用

在 mcode 里直接说：

> 用 codebuddy 帮我 review 这段 Python 的线程安全 LRU cache 设计，列出 3 个潜在的 race condition。

> summarize this 50k-token spec with codebuddy in 5 bullets, in Chinese.

> ask codebuddy to translate this README to English, keep code samples intact: $(cat README.md)

长任务（预期 > 30s）走 `task(run_in_background=true)` 派 worker 调用同一个 MCP tool，详见 `skills/codebuddy-integration/SKILL.md` 里的决策树。

## 工作原理

`mcp.json`（plugin 根目录的固定文件，Agent Plugins 1.0.0 spec §7.2.1）声明了一个 stdio MCP server，mcode 启动时读取并 spawn `bin/codebuddy-mcp-server.py`。

wrapper 维持**一个** `codebuddy --acp` 子进程（system prompt + tool catalog 在内存里缓存），暴露 4 个 tool：

| Tool | 用途 |
|------|------|
| `prompt(text, model?, append_system_prompt?, timeout?)` | 一次性文本 prompt |
| `continue(text, ...)` | 同一 codebuddy 会话内的追问（复用 sessionId，不重启子进程） |
| `status()` | wrapper 状态：存活、codebuddy PID、ACP session id、model、运行时长、调用次数、最后一次缓存命中率、累计 token。无副作用 |
| `list_tasks(limit?)` | 最近 N 次调用的元数据（最新优先） |

审计日志写到 `<plugin>/state/mcp-YYYY-MM-DD.log`（每次调用一行）。设 `CODEBUDDY_MCP_DEBUG_LOG=/path` 可同时 dump 原始 ACP 帧，默认关闭。

## 安装

1. 装 `codebuddy` CLI：`npm i -g @tencent-ai/codebuddy-code`
2. 通过 mcode plugin manager 装本插件。mcode 启动时读 `mcp.json`，不需要全局 install hook。
3. （可选）装 `mcp` Python 包：`uv pip install --system --break-system-packages mcp`

没有 `install.sh` 要跑，没有 symlink 要建，没有 PATH 要改。插件符合 spec 1.0.0，mcode 从声明位置自动加载。

## 依赖

- mcode（支持 Agent Plugins 1.0.0）
- `codebuddy` CLI（`$PATH` 或 `CODEBUDDY_BIN` 环境变量）
- Python 3.10+ 及 `mcp` 包
- `python3` 在 PATH（wrapper 和 JSON 解析用）

## 数据与网络

- 本插件不主动发网络请求，只 spawn 用户的 `codebuddy` 子进程。
- `codebuddy` 启动时加 `--dangerously-skip-permissions` 和 `--subagent-permission-mode bypassPermissions`，避免每个 bash/edit 都弹询问。唯一调用方是 mcode 代用户操作。
- 每次调用的审计日志：`<plugin>/state/mcp-YYYY-MM-DD.log`。可自行轮转或删除。
- 无凭据、无私有 endpoint、无遥测、无隐藏安装步骤。

## 测试

三个测试文件，单元测试不需要 `codebuddy` 账号或网络：

```bash
# 16 个单元测试，~4ms，无子进程
python3 -m unittest tests.test_mcp_wrapper_unit

# 5 次调用 PoC：断言单一 codebuddy PID + 冷→热缓存
python3 tests/mcp-poc-test.py

# 4 个 tool 端到端：status, list_tasks, prompt, continue, model, append_system_prompt 重启
python3 tests/mcp-features-test.py
```

## 限制

- 每个 mcode session 各自一个 wrapper + 各自一个 `codebuddy --acp` 子进程，跨 session 不共享缓存。新 session 首次调用服务端冷启（约 1.4% 缓存），第 2 次起稳定 98-99%。
- 首次调用之后的成本主要由对话 token 决定（通常几百），不是 24k 的 system prompt——这正是长驻子进程的价值。
- 改 `append_system_prompt` 会触发子进程重启（约 1s，日志里记为 `subprocess_respawn`），重启后第一次调用回到冷缓存。

## 规范符合

- `plugin.json`：`https://agent-plugins.org/schemas/1.0.0/plugin.schema.json`
- `mcp.json`：`https://agent-plugins.org/schemas/1.0.0/mcp.schema.json`
- Skill：符合 [Agent Skills 规范](https://agentskills.io/specification)

完整便携式格式合约见 [Agent Plugins 1.0.0](https://agent-plugins.org/specification)。

## 许可

MIT。详见 [LICENSE](./LICENSE)。
