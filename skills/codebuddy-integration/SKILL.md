---
name: codebuddy-integration
description: "Delegate a text-reasoning subtask to codebuddy (a peer LLM) via 4 MCP tools. Use for translate / summarize / review / brainstorm / second opinion. Burns codebuddy credits, not mcode tokens. Triggers: '用 codebuddy', '让 codebuddy', 'ask codebuddy'."
license: MIT
compatibility: Requires MiniMax Code with Agent Plugins 1.0.0+ support, the `codebuddy` CLI on $PATH (or `CODEBUDDY_BIN` env var), Python 3.10+ with the `mcp` package.
metadata:
  author: weekbin
  version: "0.3.1"
---

# codebuddy-integration

4 MCP tools over a long-lived `codebuddy --acp` subprocess, auto-loaded by mcode at session start via `<plugin>/mcp.json`.

## Tools

| Tool | When |
|---|---|
| `prompt(text, model?, append_system_prompt?, timeout?)` | One-shot text prompt. |
| `continue(text, ...)` | Follow-up in the same codebuddy session (reuses `sessionId`, no respawn). |
| `status()` | Wrapper health: pid, model, uptime, call_count, cache_ratio. |
| `list_tasks(limit?)` | Last N call metadata, most recent first. |

```python
# name="prompt", arguments={"text": "..."}
# → "<reply text>\n\n[codebuddy: pid=..., model=..., dur=...s, stop=...]\n[tokens: prompt=..., cache_read=..., cache_ratio=...%]"
```

## Rules

- **Cold / warm**: first call in a new session is cold (~1.4% cache, ~4s); calls 2+ warm to ~99% cache, ~1-2s. Same `codebuddy_pid` across calls in one session.
- **Multi-turn**: use `continue`, not `prompt` (keeps the warm cache, reuses `sessionId`).
- **Long prompt / >30s / 50k+ tokens**: spawn a `task(run_in_background=true)` worker and have it call `prompt` (or `continue`). Each worker has its own subprocess and cache — not shared with the parent session.
- **`append_system_prompt` change** triggers subprocess respawn and drops cache to cold. Set it once per session, not per call.
- **`model=`** is sticky. Omit for the server default; pass `model="deepseek-v4-pro"` / `"glm-5.2"` / etc. to force. Changing it resets turn history via `session/new`.
