---
name: codebuddy-integration
description: "Delegate a text-reasoning subtask to codebuddy (a peer LLM) via 5 MCP tools. Use for translate / summarize / review / brainstorm / second opinion. Burns codebuddy credits, not mcode tokens. Triggers: '用 codebuddy', '让 codebuddy', 'ask codebuddy'."
license: MIT
compatibility: Requires MiniMax Code with Agent Plugins 1.0.0+ support, the `codebuddy` CLI on $PATH (or `CODEBUDDY_BIN` env var), Python 3.10+ with the `mcp>=2.0.0,<3` package (v1.x is EOL and the wrapper targets v2's `Server(name, on_list_tools=fn, on_call_tool=fn)` constructor-callback API).
metadata:
  author: weekbin
  version: "0.3.8"
---

# codebuddy-integration

5 MCP tools over a long-lived `codebuddy --acp` subprocess, auto-loaded by mcode at session start via `<plugin>/mcp.json`.

## Tools

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
- **Default pattern — worker → mcp (asynchronous from main agent)**: dispatch a `task(run_in_background=true, agent_name="worker", prompt="...call mcp__codebuddy__prompt...")` subagent for **every** codebuddy call, not just long ones. The worker is its own mcode session; mcode's MCP server pool may share the same `codebuddy-mcp-server.py` wrapper across workers, and codebuddy calls **serialize at the model layer** (one reader thread + one `codebuddy --acp` subprocess). Each worker gets its own `acp_session_id`. **The main agent's wall clock is not blocked** — keep doing independent work in the parent session and retrieve the result with `task_output(task_id)` when ready. Sync `mcp__codebuddy__prompt(...)` is a deliberate fallback only when the parent has nothing else to do and the answer is needed inline before the next step. The 2026-08-18 verification on `deepseek-v4-flash`: three calls in one session ran parent-side in parallel with bash / read / other work, and the underlying `codebuddy --acp` subprocess monotonically warmed its prefix cache 2.2% → 52.8% → 99.6% across the three calls (same `codebuddy_pid` throughout — that warm cache is the whole point of the long-lived subprocess).
- **`model=` switch is dynamic**: changing `model=` mid-session uses `session/set_config_option` (preserves `sessionId`, cache, and turn history). No subprocess restart. Call `list_models()` first to confirm the id is supported.
- **`append_system_prompt` change** respawns the subprocess and drops cache to cold. Set it once per session, not per call.
- **`include_thinking=true`** exposes the model's reasoning trace. Off by default — a long task can produce hundreds of thought chunks. Set per-call, not per-session.
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
