---
name: codebuddy-integration
description: "Delegate a text-reasoning subtask to codebuddy (a peer LLM) via 9 MCP tools over a long-lived `codebuddy --acp` subprocess. DEFAULT pattern: dispatch a `task(run_in_background=true, agent_name='worker')` so the main agent's wall clock is not blocked; the worker calls `mcp__codebuddy__run(...)` (single call, millisecond-scale submit + short-poll loop). Use for translate / summarize / review / brainstorm / second opinion. Burns codebuddy credits, not mcode tokens. Triggers: '用 codebuddy', '让 codebuddy', 'ask codebuddy'."
license: MIT
compatibility: "Requires MiniMax Code with Agent Plugins 1.0.0+ support, the `codebuddy` CLI on $PATH (or `CODEBUDDY_BIN` env var), Python 3.10+ with the `mcp>=2.0.0,<3` package installed into the **same** `python3` the wrapper resolves at runtime (its shebang `#!/usr/bin/env python3`); otherwise startup fails with `ModuleNotFoundError: No module named 'mcp'` and the plugin loads zero tools."
metadata:
  author: weekbin
  version: "0.4.4"
---

# codebuddy-integration

## Decision tree (read first)
```
Is the task likely to take more than a few seconds, or could you do
other useful work while waiting?
├── YES → Pattern A (worker subagent, default). The main agent stays free;
│        the worker holds the MCP request for the codebuddy call. The
│        worker is woken up via <background-task-finished> when done.
│        Use this for ~all real workloads (translate, review, brainstorm,
│        second opinion, summarize long text).
└── NO, the result must be inline before the next reasoning step
  AND the task is short and cheap
    → Pattern B (main agent calls mcp__codebuddy__run directly).
      `run` is submit + short-poll loop (≤30s) — fits the MCP client's
      per-request timeout. Returns the result inline.
```

**Do not** poll `get_result` at 1s cadence from the main agent. The MCP
client per-request timeout (typically 30-60s) will kill the request long
before the codebuddy call returns, and the rapid polling wastes CPU
and burns tokens on the mcode side. The wrapper exposes 9 tools; only
the worker subagent pattern survives long calls cleanly.

## Pattern A (primary — default for every call)
```python
# Main agent dispatches a worker; worker does one mcp call and returns
# the result verbatim. The main agent stays free to do other work in
# parallel; the worker holds the MCP request for the codebuddy call.
task_id = task(
  description="codebuddy: <one-line summary>",
  prompt="""Background codebuddy call. Do exactly this and return only the tool result.

Call:
  mcp__codebuddy__run(
    text="<the actual task — paste verbatim>",
    model="deepseek-v4-flash"  # avoid hy3 default (429 on free tier)
  )

Return the tool's full response (text + `[codebuddy: ...]` + `[tokens: ...]` lines) verbatim, nothing else. No preamble, no analysis, no other tool calls, no file edits. If the tool errors, return the error verbatim.""",
  agent_name="worker",
  run_in_background=True,
)
# Main agent now does other work; result arrives via task_output(task_id)
# or <background-task-finished> notification.
```

The worker's `mcp__codebuddy__run` call internally does submit + short-poll
(every 2s, bounded 30s). If the codebuddy call is still running after 30s,
`run` returns `{status: "running", task_id, ...}` and the worker should
sleep ~5s and call `run` again (or just call `get_result` once with a
long timeout — `get_result` is poll-only, so it returns immediately and
the worker can check status without blocking). **Do not poll `get_result`
at 1s cadence** from the worker either; ≥2s is fine, ≥5s is better.

## Pattern B (alternative — main agent must inline the result)
```python
# Same outcome, but the main agent calls MCP directly. Use this only
# when (a) the result must be inline before the next reasoning step
# AND (b) the task is short enough to finish within ~25s (so a single
# `run` call's 30s bounded-wait window is enough). If the task could
# take longer, prefer Pattern A — `run` will return `{status: running}`
# and you'd be stuck polling on the main thread.
out = mcp__codebuddy__run(
  text="<task>",
  model="deepseek-v4-flash",
  wait_timeout_s=30,  # cap on how long run blocks; default 30
)
# out is the formatted reply, or {status: "running", task_id, ...}
```

## Tools (9)
| Tool | When |
|---|---|
| `submit_prompt(text, model?, append_system_prompt?, include_thinking?)` | Dispatch a codebuddy call; return immediately with `{task_id, status, submitted_at}`. |
| `submit_continue(text, model?, append_system_prompt?, include_thinking?)` | Same as `submit_prompt` but reuses existing `sessionId` (continuation). |
| `get_result(task_id)` | Poll for a submitted task. **Poll-only** (millisecond-scale MCP request). Returns `{status: "running"\|"done"\|"error"\|"stale"\|"unknown", result?, error?}`. The deprecated `wait_timeout_s` and `mode` params were removed in 0.4.1. |
| `run(text, model?, append_system_prompt?, include_thinking?, wait_timeout_s=30)` | Convenience: submit + internal short-poll loop (every 2s) bounded to **30 seconds** (or `wait_timeout_s` if smaller). Returns the result if the call finishes within the window, or `{status: 'running', task_id, ...}` if not — caller then uses `get_result` to keep polling. Use this from a worker. |
| `cancel_task(task_id)` | Cancel an in-flight or recent task. Use this when a codebuddy call is hung (model API hang, codebuddy CLI bug, network) and the wrapper is stuck on a single in-flight task. Frees the wrapper to accept a new submit. |
| `kill_codebuddy()` | Absolute last-resort: SIGKILL the `codebuddy --acp` subprocess unconditionally. Use when `cancel_task(force=True)` doesn't recover (e.g. daemon thread stuck on a non-cancel-related wait). Loses the current sessionId and conversation history; the next call respawns a fresh subprocess. |
| `status()` | Wrapper health: pid, model, uptime, call_count, cache_ratio, totals, `inflight_task_id` (if any). |
| `list_tasks(limit?)` | Last N call metadata + the current in-flight task (if any). max 50. |
| `list_models()` | Enumerate valid model ids + credits / max-tokens / supports-reasoning. |

## Response format
```
<reply text>
--- tools (N) ---            (when codebuddy itself called Read/Write/Bash/...)
  <title> [<status>]
[codebuddy: pid=..., model=..., dur=...s, stop=...]
[tokens: prompt=..., completion=..., cache_read=..., cache_ratio=...%]
```
With `include_thinking=true`, a `--- thinking (N chars) ---` section appears before the tools block.

## 429 (rate limit) handling
The wrapper surfaces 429 as a structured `ACPRateLimitError` (error string
contains `ACPRateLimitError`). The full response text is preserved in the
task record's `error` field on disk.

**Do not auto-retry on 429.** The prompt may have been processed and billed
before the rate limit kicked in; auto-retry risks double-billing. Recovery:
- **Best**: switch model. `model="deepseek-v4-flash"` is the recommended
  default precisely because the free-tier `hy3` 429s frequently. Verify
  the model is still valid via `mcp__codebuddy__list_models` before retrying.
- **OK**: wait a few minutes and retry the same model. The codebuddy
  free-tier window is typically 5-10 minutes.
- **Wrong**: auto-retry from inside a worker loop. If you must retry, do
  it once with an explicit backoff (≥60s) and switch model on the second 429.

## Defaults
- All timeouts default to **1h (3600s)**: `ACPSession.timeout` (codebuddy subprocess wait). `run`'s `wait_timeout_s` defaults to 30s (not 1h) — see the table above. The wrapper is the same codebuddy sub-process instance for the entire session.
- `get_result` is **poll-only** (millisecond-scale MCP request). It does NOT block waiting for the result — that would be killed by the MCP client's per-request timeout. Caller must poll repeatedly. `run` is the convenience wrapper for "submit + bounded wait (≤30s)".
- `run` does submit + short-poll loop (every 2s) bounded to **30 seconds** (or `wait_timeout_s` if smaller). If the call finishes within the window, returns the result. If not, returns `{status: "running", task_id, ...}` — caller should then use `get_result` to keep polling. **The MCP request lifetime is bounded to 30s**, so the `run` tool survives the MCP client's per-request timeout.
- `submit_prompt` returns in milliseconds. The actual codebuddy call runs in a background thread and can take as long as the codebuddy call needs (bounded by `ACPSession.timeout` = 1h).
- `model="deepseek-v4-flash"` to avoid the free-tier `hy3` 429 rate limit. Burn `0.08 credits` per call.
- `append_system_prompt` change respawns the codebuddy subprocess (drops cache to cold). Set it once per session, not per call.
- `include_thinking=true` exposes the reasoning trace. Off by default — a long task can produce hundreds of thought chunks. Set per-call.
- `cancel_task(task_id)` is the recovery path when a codebuddy call hangs. It marks the task as cancelled (the daemon thread continues but its result is discarded) and frees the wrapper for a new submit.
- `kill_codebuddy()` is the absolute last-resort when `cancel_task(force=True)` can't recover. Loses the current sessionId.
- `continue` semantics: codebuddy keeps server-side history by `sessionId`, so a follow-up `run` is a continuation. No respawn.
