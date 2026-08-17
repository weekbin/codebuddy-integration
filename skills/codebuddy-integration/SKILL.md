---
name: codebuddy-integration
description: "Delegate a text-reasoning subtask to codebuddy (a peer LLM) via 5 MCP tools. Use for translate / summarize / review / brainstorm / second opinion. Burns codebuddy credits, not mcode tokens. Triggers: '用 codebuddy', '让 codebuddy', 'ask codebuddy'."
license: MIT
compatibility: Requires MiniMax Code with Agent Plugins 1.0.0+ support, the `codebuddy` CLI on $PATH (or `CODEBUDDY_BIN` env var), Python 3.10+ with the `mcp` package.
metadata:
  author: weekbin
  version: "0.3.3"
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
- **Long prompt / >30s / 50k+ tokens**: spawn a `task(run_in_background=true)` worker and have it call `prompt` (or `continue`). Each worker has its own subprocess and cache — not shared with the parent session.
- **`append_system_prompt` change** respawns the subprocess and drops cache to cold. Set it once per session, not per call.
- **`model=` switch is dynamic** (0.3.3+): changing `model=` mid-session uses `session/set_config_option` (preserves session_id, cache, and turn history). No subprocess restart in the normal path. The respawn fallback only fires if the server rejects the config option (old codebuddy build). Call `list_models()` first to confirm the id is supported.
- **Long replies are full-length**: every `agent_message_chunk` is concatenated, not just the first. A 4000-token reply comes back as 4000 tokens, not 3 chars.
- **`include_thinking=true`** exposes the model's reasoning trace. Off by default — a long task can produce hundreds of thought chunks and bloat the response. Set per-call, not per-session.
- **Tool calls are always summarized** in the response (a `--- tools (N) ---` section) so you can see "I wrote 3 files" without seeing the raw I/O.
