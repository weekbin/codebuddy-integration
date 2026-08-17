---
name: codebuddy-integration
description: "Delegate a text-reasoning subtask to codebuddy (a peer LLM) via 5 MCP tools. Use for translate / summarize / review / brainstorm / second opinion. Burns codebuddy credits, not mcode tokens. Triggers: '用 codebuddy', '让 codebuddy', 'ask codebuddy'."
license: MIT
compatibility: Requires MiniMax Code with Agent Plugins 1.0.0+ support, the `codebuddy` CLI on $PATH (or `CODEBUDDY_BIN` env var), Python 3.10+ with the `mcp` package.
metadata:
  author: weekbin
  version: "0.3.4"
---

# codebuddy-integration

5 MCP tools over a long-lived `codebuddy --acp` subprocess, auto-loaded by mcode at session start via `<plugin>/mcp.json`.

## Tools

| Tool | When |
|---|---|
| `prompt(text, model?, append_system_prompt?, include_thinking?, timeout?)` | One-shot text prompt. `include_thinking=true` exposes the reasoning trace; tool-call summary is always included. |
| `continue(text, ...)` | Follow-up in the same codebuddy session (reuses `sessionId`, no respawn). |
| `status()` | Wrapper health: pid, model, uptime, call_count, cache_ratio. |
| `list_tasks(limit?)` | Last N call metadata, most recent first. |
| `list_models()` | Enumerate valid model ids + credits / max-tokens / supports-reasoning. Backed by the live `session/new` response. |

```python
# name="prompt", arguments={"text": "..."}
# → "<reply text>\n\n--- tools (N) ---\n  ... \n\n[codebuddy: pid=..., model=..., dur=...s, stop=...]\n[tokens: prompt=..., cache_read=..., cache_ratio=...%]"
# with include_thinking=true, also a "--- thinking (N chars) ---" section before the tools.
```

## Rules

- **Cold / warm**: first call in a new session is cold (~1.4% cache, ~4s); calls 2+ warm to ~99% cache, ~1-2s. Same `codebuddy_pid` across calls in one session.
- **Multi-turn**: use `continue`, not `prompt` (keeps the warm cache, reuses `sessionId`).
- **Long prompt / >30s / 50k+ tokens**: spawn a `task(run_in_background=true)` worker and have it call `prompt` (or `continue`). The worker is its own mcode session, but mcode's MCP server pool may share the same `codebuddy-mcp-server.py` wrapper across workers; codebuddy calls **serialize at the model layer** (one reader thread + one `codebuddy --acp`), so total wall clock across N workers ≈ Σ individual call durations, not max. Each worker that enters the wrapper gets its own `acp_session_id` and a fresh `prompt_tokens` prefix cache.
- **Async from the agent's perspective**: `task(run_in_background=true)` returns immediately with a `task_id`. The agent's wall clock is **not blocked** by the codebuddy call — keep doing independent work in the parent session while the worker runs. Verified 2026-08-18: 10+ independent operations (git log, file reads, unit tests, fib) in 22s of parent time overlapped fully with a 19.32s background codebuddy call. Retrieve the result with `task_output(task_id)` whenever ready.
- **Failed prompts stay in the acp session history**: a `stop=refusal` (or any non-`end_turn`) does **not** clear the prompt. The next model you switch to will see the full history including refused prompts and may "catch up" by answering them in one reply.
- **`(no message received from codebuddy)` + `stop=refusal` + 0 tokens** = the model returned no `message` field on the wire. For the free-tier default `hy3` (x0.00 credits), this is typically a **rate limit 429** — check with `codebuddy models` outside the MCP wrapper to see the actual 429 reset time. Workaround: pass `model="deepseek-v4-flash"` (0.08 credits) or any other paid-tier model, which is unaffected.
- **`append_system_prompt` change** respawns the subprocess and drops cache to cold. Set it once per session, not per call.
- **`model=` switch is dynamic** (0.3.3+): changing `model=` mid-session uses `session/set_config_option` (preserves session_id, cache, and turn history). No subprocess restart in the normal path. The respawn fallback only fires if the server rejects the config option (old codebuddy build). Call `list_models()` first to confirm the id is supported.
- **Long replies are full-length**: every `agent_message_chunk` is concatenated, not just the first. A 4000-token reply comes back as 4000 tokens, not 3 chars.
- **`include_thinking=true`** exposes the model's reasoning trace. Off by default — a long task can produce hundreds of thought chunks and bloat the response. Set per-call, not per-session.
- **Tool calls are always summarized** in the response (a `--- tools (N) ---` section) so you can see "I wrote 3 files" without seeing the raw I/O.
