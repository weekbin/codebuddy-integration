# codebuddy-integration

> 给 mcode 增加一个调用 codebuddy 的入口，处理翻译、长文摘要、寻求不同意见、
> 换思路重写代码这类纯文字任务。通过 stdio MCP 维持一个长生命周期的
> `codebuddy --acp` 子进程，首次调用预热后缓存命中率稳定 99% 左右。
> 版本 0.4.2。

## MCP 工具

mcode 启动时通过 `<plugin>/mcp.json` 自动 spawn `bin/codebuddy-mcp-server.py`，
wrapper 维持一个长生命周期的 `codebuddy --acp` 子进程，暴露 9 个 tool
（`run` 是便利 wrapper，把 `submit_prompt` + 短轮询 get_result 合在一起）：

| Tool | 用途 | 关键参数 |
|------|------|----------|
| `submit_prompt(text, ...)` | 提交 codebuddy 调用，立即返回 `{task_id, status, submitted_at}`；实际调用在后台线程跑 | `model?` 选模型；`append_system_prompt?` 拼业务规则（触发子进程 respawn）；`include_thinking?` 暴露推理痕迹 |
| `submit_continue(text, ...)` | 同一 codebuddy 会话追问（复用 `sessionId`），立即返回 task_id | 同 `submit_prompt` 参数 |
| `get_result(task_id)` | 取已提交 task 的当前状态。**仅 poll 模式**（毫秒级返回，调用方自行循环 poll）。`mode="blocking"` 会抛 ValueError | `wait_timeout_s?` ignored（保留向后兼容，默认 0） |
| `run(text, ...)` | 便利：submit + 内部短轮询循环（≤30s），单调用拿结果。worker 模板首选 | `model?` 同上；`wait_timeout_s?` 默认 3600（MCP 请求本身 ≤30s） |
| `cancel_task(task_id, force?)` | 取消 in-flight 或最近 task。codebuddy 卡住时用它解锁 wrapper。`force=true` 还会 SIGKILL codebuddy 子进程（最保底） | `task_id` 必填；`force?` 默认 false |
| `kill_codebuddy()` | **保底**：无条件 SIGKILL codebuddy 子进程（不需 task_id）。最暴力的恢复手段，下次 call 自动 respawn | 无 |
| `status()` | wrapper + codebuddy 状态（pid、acp_session_id、model、uptime、call_count、最后 cache_ratio、累计 token、`inflight_task_id`、`inflight_model`） | 无 |
| `list_tasks(limit?)` | 最近 N 次调用的元数据（最新优先）+ 当前 in-flight task | `limit` 默认 10，clamp 到 50 |
| `list_models()` | 列出可用 model id + credits / maxInputTokens / supportsReasoning | 无 |

`run` / `get_result`（done 状态）返回格式：

```
<reply text>

--- tools (N) ---            (当 codebuddy 自己用 Read/Write/Bash 时)
  <title> [<status>]

[codebuddy: pid=..., model=..., dur=...s, stop=...]
[tokens: prompt=..., completion=..., cache_read=..., cache_ratio=...%]
```

`include_thinking=true` 时多一段 `--- thinking (N chars) ---` 在 tools 之前。

## 调用方式

**默认：派 worker 走 `mcp__codebuddy__run`**（主 agent 异步）。详细 Pattern 段和
worker 模板见 [`SKILL.md`](./skills/codebuddy-integration/SKILL.md) § Pattern (primary)。
worker 是独立的 mcode session，**主 agent 的 wall clock 不被 codebuddy 延迟阻塞**。
worker 模板（直接复制改 task 内容）：

```python
task(
  description="codebuddy: <one-line summary>",
  prompt="""Background codebuddy call. Do exactly this and return only the tool result.

Call:
  mcp__codebuddy__run(
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

**备选：主 agent 直接调 MCP**（不绕 worker），仅在答案需要 inline 拼到下一步推理
或主 agent 真的没别的事做时。详见 SKILL.md § Pattern (alternative)。

## 1h 默认

所有 timeout 默认 1 小时（3600s），不传参数就 1h：
- `ACPSession.timeout`（codebuddy 子进程等待上限）
- `get_result` / `run` 的 `wait_timeout_s`（默认 3600）
- `mcp.json` env 块含 `MCP_SERVER_REQUEST_TIMEOUT=3600000` /
  `MCP_REQUEST_TIMEOUT=3600` / `MCP_CONNECTION_TIMEOUT=3600` /
  `MCP_TIMEOUT=3600` / `MCP_MAX_REQUEST_TIMEOUT=3600000`（5 个常见 SDK 命名
  env var 全部 = 1h，覆盖宿主 mcode 任意一个名字）。如果 hard deadline 必要，
  设 `wait_timeout_s` ≥ 600s。

## 工作原理

`mcp.json`（plugin 根目录的固定文件，Agent Plugins 1.0.0 spec §7.2.1）声明了一个
stdio MCP server，mcode 启动时读取并 spawn wrapper。

- **异步 submit/poll 架构**：每条 MCP 请求毫秒级。`submit_prompt` 立即返回 task_id；
  codebuddy 实际调用在 wrapper 后台线程跑；`get_result` 取结果。`run` 把两者合一。
  **MCP 客户端的 per-request 时延上限无法丢失长 call 响应**。
- **长生命周期的 `codebuddy --acp` 子进程**：系统 prompt + tool catalog 一次性加载
  到内存，后续 `prompt_tokens` prefix 复用率稳定 99%。每个调用方（worker / 主 agent）
  拿到自己的 `acp_session_id`，互不污染。
- **mcp 2.x `Server` API**：wrapper 用 v2 的 `Server(name, on_list_tools=fn, on_call_tool=fn)`
  构造 callback 方式注册 handler。`mcp>=2.0.0,<3` 是硬依赖。
- **fail-fast 启动**：`get_session()` 第一次被调时先跑 `codebuddy --version`（5s 超时），
  缺二进制、退出非 0、或 hang 都给清晰报错。约多花 0.5-1s 启动延迟，换来错误信息明确。
- **state 写到 `${PLUGIN_DATA}/state`**：per-client 数据目录，state 跨 plugin 升级存活。
  兼容老的 `PLUGIN_ROOT/state`（fallback）。`STATE_DIR` 的 `mkdir` 是 lazy 的，
  read-only 安装也只丢 log，不会 crash wrapper（spec §7.2.2 rule 5）。
- **任务持久化到 `${PLUGIN_DATA}/tasks/<task_id>.json`**：wrapper 重启时 GC 把
  in-flight 任务标 stale，调用方能可靠拿到结果。`run` 调用的 task 完成后
  30min 内（默认 TASK_LIFETIME_S=24h 实际可调）`get_result` 仍能命中磁盘记录。
- **审计日志** `mcp-YYYY-MM-DD.log`：每次调用一行，pipe-separated kv 格式。
  设 `CODEBUDDY_MCP_DEBUG_LOG=/path` 可同时 dump 原始 ACP 帧（默认关闭，调试用）。

## 安装

1. 装 `codebuddy` CLI：`npm i -g @tencent-ai/codebuddy-code`
2. 装 `mcp` Python 包 v2.x：
   ```bash
   pip install --upgrade 'mcp>=2.0.0,<3'
   ```
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
# 单元测试，~220ms，无子进程（仅测 wrapper 内部逻辑，不调真 codebuddy）
python3 -m unittest tests.test_mcp_wrapper_unit

# 5 次调用 PoC：单一 codebuddy PID + 冷→热缓存（需要真 codebuddy + 烧 credits）
python3 tests/mcp-poc-test.py

# 端到端：7 tool + 缓存 + 模型切换 + append respawn + thinking + submit/get_result
# round-trip（需要真 codebuddy）
python3 tests/mcp-features-test.py

# 长文回归（防止回复被截断到第一个 agent_message_chunk，429 时自动 skip）
python3 tests/mcp-long-prompt-test.py
```

## 限制

- 多个 task worker 可能共享同一个 wrapper（mcode MCP server pool），codebuddy 调用在
  model 层**串行**。N worker 并发 wall clock ≈ Σ 单任务时长，不是 max。
- 首次调用（冷启服务端）~2% 缓存、~3-4s；同 session 内第 2 次起稳定 99%。
- 改 `append_system_prompt` 会触发子进程重启（约 1s，日志里记为 `subprocess_respawn`），
  重启后第一次调用回到冷缓存。
- 默认 `model` 是服务端默认（hy3，x0.00 credits 免费档），可能因账号频率限制 429
  拒答。生产用建议显式传 `model="deepseek-v4-flash"`（0.08 credits）。
- MCP 协议是 stdio-only。

## 规范符合

- `plugin.json`：`https://agent-plugins.org/schemas/1.0.0/plugin.schema.json`
- `mcp.json`：`https://agent-plugins.org/schemas/1.0.0/mcp.schema.json`
- Skill：符合 [Agent Skills 规范](https://agentskills.io/specification)

完整便携式格式合约见 [Agent Plugins 1.0.0](https://agent-plugins.org/specification)。

## 许可

MIT。详见 [LICENSE](./LICENSE)。
