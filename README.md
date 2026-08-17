# codebuddy-integration

> 给 mcode 增加一个调用 codebuddy 的入口，处理翻译、长文摘要、寻求不同意见、
> 换思路重写代码这类纯文字任务。通过 stdio MCP 维持一个长生命周期的
> `codebuddy --acp` 子进程，首次调用预热后缓存命中率稳定 98-99%。

## MCP 工具

mcode 启动时通过 `<plugin>/mcp.json` 自动 spawn `bin/codebuddy-mcp-server.py`，
wrapper 维持一个长生命周期的 `codebuddy --acp` 子进程，暴露 5 个 tool：

| Tool | 用途 | 关键参数 |
|------|------|----------|
| `prompt(text, ...)` | 一次性文本 prompt | `model?` 选模型；`append_system_prompt?` 拼业务规则（触发子进程 respawn）；`include_thinking?` 暴露推理痕迹；`timeout?` 默认 300s |
| `continue(text, ...)` | 同一 codebuddy 会话追问，复用 `sessionId`，不重启子进程 | 同 `prompt` 参数 |
| `status()` | wrapper + codebuddy 状态（pid、acp_session_id、model、uptime、call_count、最后 cache_ratio、累计 token） | 无 |
| `list_tasks(limit?)` | 最近 N 次调用的元数据（最新优先），每条含 model/dur/prompt_tokens/completion_tokens/cached_tokens/cache_ratio/stop_reason | `limit` 默认 10 |
| `list_models()` | 列出可用 model id + credits / maxInputTokens / supportsReasoning，从活的 `session/new` 响应读 | 无 |

`prompt` / `continue` 返回格式：

```
<reply text>

--- tools (N) ---            (当 codebuddy 自己用 Read/Write/Bash 时)
  <title> [<status>]

[codebuddy: pid=..., model=..., dur=...s, stop=...]
[tokens: prompt=..., completion=..., cache_read=..., cache_ratio=...%]
```

`include_thinking=true` 时多一段 `--- thinking (N chars) ---` 在 tools 之前。

## 调用方式

### 方式 A：mcode `task` 派 worker 异步调用（推荐长任务）

长任务（>30s / 50k+ token / 不阻塞当前 session）走 `task(run_in_background=true)`。
worker session 启动后 agent 自己的 wall clock **不阻塞**——可以同时做别的事。

```python
# 在 mcode 里的 task brief：
# "用 mcp__codebuddy__prompt 跑 1 分钟长任务, model=deepseek-v4-flash,
#  text='...1500+ 字 prompt...', timeout=180。
#  调完调 mcp__codebuddy__status 拿状态。汇报 model/dur/cache_ratio/pid。"

# Agent 主线在 task 跑期间可以同时：
#  - read 其他文件
#  - run bash 独立命令
#  - 调其它 mcp 工具
# 然后用 task_output(task_id) 拿结果
```

**实测**：22s 内 agent 完成 10+ 个独立操作（git log / file reads / unit tests / fib），
跟一个 19.32s 后台 codebuddy call 完全并行。

**已知约束**（0.3.4 实证）：
- mcode 可能让多个 task worker 共享同一个 wrapper（mcode MCP server pool 行为）
- wrapper 内部 1 个 reader thread + 1 个 `codebuddy --acp` 子进程 → 多个 prompt 在 model 层**串行**
- N worker 并发 = wall clock ≈ Σ 单任务时长，不是 max

### 方式 B：直接 `mcp__codebuddy__prompt` 同步调用（短任务）

短任务、立刻要结果、不想开新 task 时直接调 wrapper：

```python
# 在 mcode 里：
mcp__codebuddy__prompt(text="翻译下面这段到日文: ...", model="deepseek-v4-flash")
# ↑ 同步等结果，agent 阻塞直到 reply 返回
```

适合：
- < 5s 短翻译 / 短摘要
- 立刻需要结果用于下一步
- 想看 codebuddy 推理过程（加 `include_thinking=true`）

### 选哪种

| 场景 | 推荐 |
|------|------|
| < 5s 短翻译 / 短摘要 | 方式 B（直接调） |
| > 30s 长文生成 | 方式 A（background task） |
| 多个独立任务并发编排（如翻译 5 个文档） | 方式 A × N（agent 等全部完成） |
| 想看 codebuddy 推理过程 | 方式 B + `include_thinking=true` |
| 验 model 切换是否生效 | 方式 B（一次调完立刻 `list_models` 比对） |

## 快速试用

在 mcode 里直接说：

> 用 codebuddy 帮我 review 这段 Python 的线程安全 LRU cache 设计，列出 3 个潜在的 race condition。

> summarize this 50k-token spec with codebuddy in 5 bullets, in Chinese.

> ask codebuddy to translate this README to English, keep code samples intact: $(cat README.md)

## 工作原理

`mcp.json`（plugin 根目录的固定文件，Agent Plugins 1.0.0 spec §7.2.1）声明了一个 stdio MCP server，mcode 启动时读取并 spawn wrapper。

`codebuddy --acp` 子进程启动时把 system prompt + tool catalog 加载到内存，后续对话的 `prompt_tokens` prefix 复用率稳定 99%。审计日志写到 `<plugin>/state/mcp-YYYY-MM-DD.log`，每次调用一行。设 `CODEBUDDY_MCP_DEBUG_LOG=/path` 可同时 dump 原始 ACP 帧（默认关闭，调试用）。

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

```bash
# 34 个单元测试，~220ms，无子进程
python3 -m unittest tests.test_mcp_wrapper_unit

# 5 次调用 PoC：单一 codebuddy PID + 冷→热缓存
python3 tests/mcp-poc-test.py

# 端到端：5 tool + 缓存 + 模型切换 + append respawn + thinking
python3 tests/mcp-features-test.py

# 长文回归（防止回复被截断到第一个 agent_message_chunk）
python3 tests/mcp-long-prompt-test.py
```

## 限制

- 多个 task worker 可能共享同一个 wrapper（mcode MCP server pool），codebuddy 调用在 model 层串行。wall clock ≈ Σ 单任务时长。
- 首次调用（冷启服务端）~1.4% 缓存、~4s；第 2 次起稳定 99%。
- 改 `append_system_prompt` 会触发子进程重启（约 1s，日志里记为 `subprocess_respawn`），重启后第一次调用回到冷缓存。
- 默认 `model` 是服务端默认（hy3，x0.00 credits 免费档），可能因账号频率限制 429 拒答。生产用建议显式传 `model="deepseek-v4-flash"`。
- MCP 协议是 stdio-only（spec 1.0.0 也支持 `streamable-http`，但本 plugin 0.4.0 才考虑）。

## 规范符合

- `plugin.json`：`https://agent-plugins.org/schemas/1.0.0/plugin.schema.json`
- `mcp.json`：`https://agent-plugins.org/schemas/1.0.0/mcp.schema.json`
- Skill：符合 [Agent Skills 规范](https://agentskills.io/specification)

完整便携式格式合约见 [Agent Plugins 1.0.0](https://agent-plugins.org/specification)。

## 许可

MIT。详见 [LICENSE](./LICENSE)。
