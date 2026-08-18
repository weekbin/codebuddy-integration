---
name: codebuddy-integration
description: "Delegate a text-reasoning subtask to codebuddy (a peer LLM). DEFAULT pattern: dispatch a `task(run_in_background=true, agent_name='worker')` so the main agent's wall clock is not blocked — sync `mcp__codebuddy__prompt` is a fallback only when the parent has nothing else to do. Use for translate / summarize / review / brainstorm / second opinion. Burns codebuddy credits, not mcode tokens. Triggers: '用 codebuddy', '让 codebuddy', 'ask codebuddy', 'second opinion'."
license: MIT
compatibility: "Requires MiniMax Code with Agent Plugins 1.0.0+ support, the `codebuddy` CLI on $PATH (or `CODEBUDDY_BIN` env var), Python 3.10+ with the `mcp>=2.0.0,<3` package. Install the `mcp` package into the **same** `python3` the wrapper resolves at runtime (its shebang `#!/usr/bin/env python3`) — otherwise startup fails with `ModuleNotFoundError: No module named 'mcp'` and the plugin loads zero tools."
metadata:
  author: weekbin
  version: "0.3.13"
---

# codebuddy-integration

5 MCP tools over a long-lived `codebuddy --acp` subprocess, auto-loaded by mcode at session start via `<plugin>/mcp.json`.

## Pattern (default — read this first)

For **every** codebuddy call, dispatch a `task(run_in_background=true, agent_name="worker", ...)` so the main agent's wall clock is not blocked. The worker has its own mcode session and is the one that calls `mcp__codebuddy__prompt` / `mcp__codebuddy__continue`. The parent then continues with independent work and retrieves the result with `task_output(task_id)`.

**Sync `mcp__codebuddy__prompt` is a fallback only** — when the parent has nothing else to do and the answer must be inline before the next step. Codebuddy calls serialize at the model layer across workers, so dispatching many workers gives you parallel orchestration (independent task IDs, independent `acp_session_id`s) but **not** parallel model inference — total wall clock is roughly the sum of per-call durations.

## Tools (called from inside a worker unless sync is justified)

| Tool | When |
|---|---|
| `prompt(text, model?, append_system_prompt?, include_thinking?, timeout?)` | One-shot text prompt. |
| `continue(text, model?, append_system_prompt?, include_thinking?, timeout?)` | Follow-up in the same codebuddy session (reuses `sessionId`, no respawn). |
| `status()` | Wrapper health: pid, model, uptime, call_count, cache_ratio, totals. |
| `list_tasks(limit?)` | Last N call metadata, most recent first. |
| `list_models()` | Enumerate valid model ids + credits / max-tokens / supports-reasoning. |

Response format:

```
<reply text>

--- tools (N) ---            (when codebuddy itself called Read/Write/Bash/...)
  <title> [<status>]

[codebuddy: pid=..., model=..., dur=...s, stop=...]
[tokens: prompt=..., completion=..., cache_read=..., cache_ratio=...%]
```

With `include_thinking=true`, also a `--- thinking (N chars) ---` section before the tools block.

## Rules

- **Cold / warm**: first call in a new session is cold (~1.4% cache, ~4s); calls 2+ warm to ~99% cache, ~1-2s. Same `codebuddy_pid` across calls in one session.
- **Multi-turn**: use `continue`, not `prompt` (keeps the warm cache, reuses `sessionId`).
- **`model=` switch is dynamic**: changing `model=` mid-session uses `session/set_config_option` (preserves `sessionId`, cache, and turn history). No subprocess restart. Call `list_models()` first to confirm the id is supported.
- **`append_system_prompt` change** respawns the subprocess and drops cache to cold. Set it once per session, not per call.
- **`include_thinking=true`** exposes the model's reasoning trace. Off by default — a long task can produce hundreds of thought chunks. Set per-call, not per-session.
- **Don't pass a short `timeout=` to `mcp__codebuddy__prompt` / `mcp__codebuddy__continue`**: a real long prompt (full-doc review, large code-context summary, ~100K+ input tokens) routinely runs 100-160s end-to-end, and 3600s is a more honest ceiling for the genuinely long ones (multi-thousand-line code-context review, large doc summary). Passing `timeout=120` or similar to "bound latency" only makes the mcp client give up *while codebuddy is still finishing*: the wrapper's `call_count` still goes up and credits are still charged, but the client receives `MCP error -32001: Request timed out` and the response is lost. Leave `timeout` unset (= wrapper's 3600s default) for any task that could plausibly take more than a minute; if you genuinely need a hard deadline, set it to ≥ 600s.
- **`(no message received from codebuddy)` + `stop=refusal` + 0 tokens** = the model returned no `message` field. For the free-tier default `hy3` (x0.00 credits), this is typically a rate limit 429; check `codebuddy models` outside the wrapper for the reset time. Workaround: pass `model="deepseek-v4-flash"` (0.08 credits) or any other paid-tier model.
- **Failed prompts stay in the acp session history**: a `stop=refusal` does not clear the prompt. The next model you switch to will see the full history including refused prompts.

## Worker prompt template (default)

```python
task(
  description="codebuddy: <one-line summary>",
  prompt="""Background codebuddy call. Do exactly this and return only the tool result.

Call:
  mcp__codebuddy__prompt(
    text="<the actual task — paste verbatim>",
    model="deepseek-v4-flash"  # or another model; avoid hy3 default (429 on free tier)
  )

Return the tool's full response (text + `[codebuddy: ...]` + `[tokens: ...]` lines) verbatim, nothing else. No preamble, no analysis, no other tool calls, no file edits. If the tool errors, return the error verbatim.""",
  agent_name="worker",
  run_in_background=True,
)
```

The parent then continues with independent work and retrieves the result with `task_output(task_id)` (or is woken by the `<background-task-finished>` notification). The worker's session is short-lived and context-free — that's a feature, not a bug: the model has nothing to confuse it, and the main agent's context isn't polluted by codebuddy's full turn history.
