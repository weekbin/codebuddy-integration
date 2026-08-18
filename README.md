# codebuddy-integration

> 给 mcode 增加一个调用 codebuddy 的入口，处理翻译、长文摘要、寻求不同意见、
> 换思路重写代码这类纯文字任务。通过 stdio MCP 维持一个长生命周期的
> `codebuddy --acp` 子进程，首次调用预热后缓存命中率稳定 99% 左右。
> 版本 0.3.13。

## MCP 工具

mcode 启动时通过 `<plugin>/mcp.json` 自动 spawn `bin/codebuddy-mcp-server.py`，
wrapper 维持一个长生命周期的 `codebuddy --acp` 子进程，暴露 5 个 tool：

| Tool | 用途 | 关键参数 |
|------|------|----------|
| `prompt(text, ...)` | 一次性文本 prompt | `model?` 选模型；`append_system_prompt?` 拼业务规则（触发子进程 respawn）；`include_thinking?` 暴露推理痕迹；`timeout?` 默认 3600s（1h，真正长任务也不卡） |
| `continue(text, ...)` | 同一 codebuddy 会话追问，复用 `sessionId`，不重启子进程 | 同 `prompt` 参数 |
| `status()` | wrapper + codebuddy 状态（pid、acp_session_id、model、uptime、call_count、最后 cache_ratio、累计 token） | 无 |
| `list_tasks(limit?)` | 最近 N 次调用的元数据（最新优先），每条含 model/dur/prompt_tokens/completion_tokens/cached_tokens/cache_ratio/stop_reason | `limit` 默认 10，clamp 到 50 |
| `list_models()` | 列出可用 model id + credits / maxInputTokens / supportsReasoning | 无 |

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

### 默认：派 worker 走 `mcp__codebuddy__*`（主 agent 异步）

**每**一次 codebuddy 调用都建议走 `task(run_in_background=true, agent_name="worker", ...)`，
不只限于长任务。worker 是独立的 mcode session，**主 agent 的 wall clock 不被 codebuddy
延迟阻塞** —— 起完 task 立刻 free，可继续做独立工作（读文件 / 跑 shell / 派更多 task），
结果用 `task_output(task_id)` 拿，或者被 `<background-task-finished>` 通知唤起。

worker 的 prompt 模板（直接复制改 task 内容）：

```python
task(
  description="codebuddy: <one-line summary>",
  prompt="""Background codebuddy call. Do exactly this and return only the tool result.

Call:
  mcp__codebuddy__prompt(
    text="<the actual task — paste verbatim>",
    model="deepseek-v4-flash"  # avoid hy3 default (429 on free tier)
  )

Return the tool's full response (text + `[codebuddy: ...]` + `[tokens: ...]` lines)
verbatim, nothing else. No preamble, no analysis, no other tool calls, no file edits.
If the tool errors, return the error verbatim.""",
  agent_name="worker",
  run_in_background=True,
)
```

worker 上下文是**故意保持干净**的：codebuddy 完整 turn 历史留在 wrapper 的 acp session
里，不进 worker 也不进主 agent 上下文。

### 何时用同步

`mcp__codebuddy__prompt(...)` 同步直调是**故意**的 fallback，只在以下情况用：
- 主 agent 接下来**没别的事**做，硬等就行
- 答案需要 in-line 拼到下一步推理里（比如要"先看 codebuddy 的回复再决定下一步 prompt"）
- 调试时想实时看 token 流

### 关键约束（多 worker 共享 wrapper 的代价）

- mcode 可能让多个 task worker 共享同一个 `codebuddy-mcp-server.py` wrapper 实例
  （mcode MCP server pool 行为），wrapper 内部 1 个 reader thread + 1 个
  `codebuddy --acp` 子进程 → 多个 prompt 在 model 层**串行**
- 每个 worker 进 wrapper 拿到自己的 `acp_session_id`，prefix cache 互不影响
- N worker 并发的 wall clock ≈ Σ 单任务时长，**不是** max
- 想真并发得等 0.4.0 的 streamable-HTTP transport（一个 worker 一个 server）

## 快速试用

在 mcode 里直接说：

> 用 codebuddy 帮我 review 这段 Python 的线程安全 LRU cache 设计，列出 3 个潜在的 race condition。

> summarize this 50k-token spec with codebuddy in 5 bullets, in Chinese.

> ask codebuddy to translate this README to English, keep code samples intact: $(cat README.md)

## 工作原理

`mcp.json`（plugin 根目录的固定文件，Agent Plugins 1.0.0 spec §7.2.1）声明了一个 stdio
MCP server，mcode 启动时读取并 spawn wrapper。

- **长生命周期的 `codebuddy --acp` 子进程**：系统 prompt + tool catalog 一次性加载到内存，
  后续 `prompt_tokens` prefix 复用率稳定 99%（实测见上节）。每个调用方（worker / 主 agent）
  拿到自己的 `acp_session_id`，互不污染。
- **mcp 2.x `Server` API**：wrapper 用 v2 的 `Server(name, on_list_tools=fn, on_call_tool=fn)`
  构造 callback 方式注册 handler。`mcp>=2.0.0,<3` 是硬依赖。
- **fail-fast 启动**：`get_session()` 第一次被调时先跑 `codebuddy --version`（5s 超时），
  缺二进制、退出非 0、或 hang 都给清晰报错，不会延后到第一次 prompt 才发现。约多花
  0.5-1s 启动延迟，换来错误信息明确。
- **state 写到 `${PLUGIN_DATA}/state`**：per-client 数据目录，state 跨 plugin 升级存活。
  兼容老的 `PLUGIN_ROOT/state`（fallback）。`STATE_DIR` 的 `mkdir` 是 lazy 的，
  read-only 安装也只丢 log，不会 crash wrapper（spec §7.2.2 rule 5）。
- **审计日志** `mcp-YYYY-MM-DD.log`：每次调用一行，pipe-separated kv 格式。
  设 `CODEBUDDY_MCP_DEBUG_LOG=/path` 可同时 dump 原始 ACP 帧（默认关闭，调试用）。

## 安装

1. 装 `codebuddy` CLI：`npm i -g @tencent-ai/codebuddy-code`
2. 装 `mcp` Python 包 v2.x：
   ```bash
   pip install --upgrade 'mcp>=2.0.0,<3'
   ```
   （需要 mcp v2.x）
3. **确认 `pip` 装到的是 wrapper 用的同一个 `python3`** — wrapper 的 shebang 是
   `#!/usr/bin/env python3`，由 PATH 第一个 `python3` 解析决定。多 python 共存时
   （macOS homebrew 装 3.14 + 系统自带的 3.9 / 3.10 等）很常见踩坑：装到 A python，
   wrapper 跑的是 B python → 启动直接 `ModuleNotFoundError: No module named 'mcp'`，
   整个 plugin 加载成 0 工具。检查：
   ```bash
   which python3                                  # PATH 第一个
   python3 -c "import mcp; print(mcp.__file__)"   # 它有 mcp 吗
   ```
   不匹配时显式装到目标 python：`/path/to/the/right/python3 -m pip install 'mcp>=2.0.0,<3'`。
4. 通过 mcode plugin manager 装本插件。mcode 启动时读 `mcp.json`，不需要全局 install hook。

没有 `install.sh` 要跑，没有 symlink 要建，没有 PATH 要改。插件符合 spec 1.0.0，
mcode 从声明位置自动加载。

## 依赖

- mcode（支持 Agent Plugins 1.0.0）
- `codebuddy` CLI（`$PATH` 或 `CODEBUDDY_BIN` 环境变量）
- Python 3.10+ 及 **`mcp>=2.0.0,<3`** Python 包
- `python3` 在 PATH（wrapper 和 JSON 解析用）

## 数据与网络

- 本插件不主动发网络请求，只 spawn 用户的 `codebuddy` 子进程。
- `codebuddy` 启动时加 `--dangerously-skip-permissions` 和 `--subagent-permission-mode
  bypassPermissions`，避免每个 bash/edit 都弹询问。唯一调用方是 mcode 代用户操作。
- 每次调用的审计日志：`<plugin>/state/mcp-YYYY-MM-DD.log`（或在
  `${PLUGIN_DATA}/state/mcp-YYYY-MM-DD.log`，看 mcp.json 的 `MCP_STATE_DIR` 设置）。
  可自行轮转或删除。
- 无凭据、无私有 endpoint、无遥测、无隐藏安装步骤。

## 测试

```bash
# 34 个单元测试，~220ms，无子进程（仅测 wrapper 内部逻辑，不调真 codebuddy）
python3 -m unittest tests.test_mcp_wrapper_unit

# 5 次调用 PoC：单一 codebuddy PID + 冷→热缓存（需要真 codebuddy + 烧 credits）
python3 tests/mcp-poc-test.py

# 端到端：5 tool + 缓存 + 模型切换 + append respawn + thinking（需要真 codebuddy）
python3 tests/mcp-features-test.py

# 长文回归（防止回复被截断到第一个 agent_message_chunk，429 时自动 skip）
python3 tests/mcp-long-prompt-test.py
```

## 限制

- 多个 task worker 可能共享同一个 wrapper（mcode MCP server pool），codebuddy 调用在
  model 层**串行**。N worker 并发 wall clock ≈ Σ 单任务时长，不是 max。真并发得等
  streamable-HTTP（0.4.0 路线）。
- 首次调用（冷启服务端）~2% 缓存、~3-4s；同 session 内第 2 次起稳定 99%。
- 改 `append_system_prompt` 会触发子进程重启（约 1s，日志里记为 `subprocess_respawn`），
  重启后第一次调用回到冷缓存。
- 默认 `model` 是服务端默认（hy3，x0.00 credits 免费档），可能因账号频率限制 429
  拒答。生产用建议显式传 `model="deepseek-v4-flash"`（0.08 credits）。
- MCP 协议是 stdio-only（spec 1.0.0 也支持 `streamable-http`，但本 plugin 0.4.0 才考虑）。
- 需要 mcp v2.x（`mcp>=2.0.0,<3`）。

## 规范符合

- `plugin.json`：`https://agent-plugins.org/schemas/1.0.0/plugin.schema.json`
- `mcp.json`：`https://agent-plugins.org/schemas/1.0.0/mcp.schema.json`
- Skill：符合 [Agent Skills 规范](https://agentskills.io/specification)

完整便携式格式合约见 [Agent Plugins 1.0.0](https://agent-plugins.org/specification)。

## 许可

MIT。详见 [LICENSE](./LICENSE)。
