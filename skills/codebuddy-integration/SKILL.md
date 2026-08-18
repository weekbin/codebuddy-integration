---
name: codebuddy-integration
description: "Delegate a text-reasoning subtask to codebuddy (a peer LLM) via 9 MCP tools over a long-lived `codebuddy --acp` subprocess. DEFAULT pattern: dispatch a `task(run_in_background=true, agent_name='worker')` so the main agent's wall clock is not blocked; the worker calls `mcp__codebuddy__run(...)` (single call, millisecond-scale submit + short-poll loop). Use for translate / summarize / review / brainstorm / second opinion. Burns codebuddy credits, not mcode tokens. Triggers: '用 codebuddy', '让 codebuddy', 'ask codebuddy'."
license: MIT
compatibility: "Requires MiniMax Code with Agent Plugins 1.0.0+ support, the `codebuddy` CLI on $PATH (or `CODEBUDDY_BIN` env var), Python 3.10+ with the `mcp>=2.0.0,<3` package installed into the **same** `python3` the wrapper resolves at runtime (its shebang `#!/usr/bin/env python3`); otherwise startup fails with `ModuleNotFoundError: No module named 'mcp'` and the plugin loads zero tools."
metadata:
  author: weekbin
  version: "0.4.2"
---

# codebuddy-integration

## Pattern (primary — default for every call)
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

## Pattern (alternative — main agent must inline the result)
```python
# Same outcome, but the main agent calls MCP directly. Use this only when
# the result must be inline before the next reasoning step (no parallel
# work to do while waiting). The MCP request is still millisecond-scale on
# the submit side; wait_timeout_s caps how long get_result blocks.
sub = mcp__codebuddy__submit_prompt(text="<task>", model="deepseek-v4-flash")
res = mcp__codebuddy__get_result(sub["task_id"], wait_timeout_s=3600)  # default 1h
# res is {task_id, status: "done"|"error"|"stale"|"unknown", result?, error?}
```

## Tools
| Tool | When |
|---|---|
| `submit_prompt(text, model?, append_system_prompt?, include_thinking?)` | Dispatch a codebuddy call; return immediately with `{task_id, status, submitted_at}`. |
| `submit_continue(text, model?, append_system_prompt?, include_thinking?)` | Same as `submit_prompt` but reuses existing `sessionId` (continuation). |
| `get_result(task_id, wait_timeout_s=3600, mode="blocking"\|"poll")` | Wait/poll for a submitted task. default wait_timeout_s = 1h. |
| `run(text, model?, append_system_prompt?, include_thinking?, wait_timeout_s=3600)` | Convenience: submit + internal short-poll loop (≤30s). Returns the result if the call finishes within the window, or `{status: 'running', task_id, ...}` if not — caller then uses `get_result` to keep polling. Use this from a worker. |
| `cancel_task(task_id)` | Cancel an in-flight or recent task. Use this when a codebuddy call is hung (model API hang, codebuddy CLI bug, network) and the wrapper is stuck on a single in-flight task. Frees the wrapper to accept a new submit. |
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

## Defaults
- All timeouts default to **1h (3600s)**: `ACPSession.timeout` (codebuddy subprocess wait), `run` `wait_timeout_s`, and the wrapper is the same codebuddy sub-process instance for the entire session.
- `get_result` is **poll-only** (millisecond-scale MCP request). It does NOT block waiting for the result — that would be killed by the MCP client's per-request timeout. Caller must poll repeatedly. `run` is the convenience wrapper for "submit + bounded wait (≤30s)".
- `run` does submit + short-poll loop (every 2s) bounded to **30 seconds** (or `wait_timeout_s` if smaller). If the call finishes within the window, returns the result. If not, returns `{status: "running", task_id, ...}` — caller should then use `get_result` to keep polling. **The MCP request lifetime is bounded to 30s**, so the `run` tool survives the MCP client's per-request timeout.
- `submit_prompt` returns in milliseconds. The actual codebuddy call runs in a background thread and can take as long as the codebuddy call needs (bounded by `ACPSession.timeout` = 1h).
- `model="deepseek-v4-flash"` to avoid the free-tier `hy3` 429 rate limit. Burn `0.08 credits` per call.
- `append_system_prompt` change respawns the codebuddy subprocess (drops cache to cold). Set it once per session, not per call.
- `include_thinking=true` exposes the reasoning trace. Off by default — a long task can produce hundreds of thought chunks. Set per-call.
- `cancel_task(task_id)` is the recovery path when a codebuddy call hangs. It marks the task as cancelled (the daemon thread continues but its result is discarded) and frees the wrapper for a new submit.
- `continue` semantics: codebuddy keeps server-side history by `sessionId`, so a follow-up `run` is a continuation. No respawn.
