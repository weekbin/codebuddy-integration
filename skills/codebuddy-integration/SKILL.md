---
name: codebuddy-integration
description: "Delegate a text-reasoning subtask to codebuddy (a peer LLM) via the `codebuddy` MCP tool (preferred — long-lived, cache-friendly) or the legacy `invoke-codebuddy` CLI. Use this Skill when the user asks for a second opinion, a longer-context summary, a translation, a fresh implementation draft, brainstorming, or a design review — and burning codebuddy credits is preferable to spending mcode tokens. Do NOT use for file edits, git operations, or shell commands (mcode already has those tools). Triggers on phrases like '用 codebuddy', '让 codebuddy', 'summarize with codebuddy', 'review with codebuddy', 'ask codebuddy', or 'use the codebuddy MCP tool'."
license: MIT
compatibility: Requires MiniMax Code with Agent Plugins 1.0.0+ support (loads via mcp.json at the plugin root), the `codebuddy` CLI on $PATH (or `CODEBUDDY_BIN` env var pointing at it), Python 3.10+ with the `mcp` package, and (for the legacy `--mode tui` only) the `orca-ide` CLI.
metadata:
  author: weekbin
  version: "0.3.0"
---

# codebuddy-integration — codebuddy as a subagent

This plugin ships an MCP server (in `mcp.json`) that exposes four tools
over a single long-lived `codebuddy --acp` subprocess, plus the
historical `invoke-codebuddy` CLI for clients that prefer to drive
text reasoning through the bash tool. The plugin conforms to the
[Agent Plugins 1.0.0](https://agent-plugins.org/specification)
specification: mcode discovers the MCP server via `<plugin>/mcp.json`
at session start, spawns the wrapper as a stdio subprocess, and the
wrapper keeps `codebuddy --acp` alive for the session lifetime.

## Why MCP over CLI

The legacy 0.2.1 `invoke-codebuddy` CLI spawned a fresh
`codebuddy --acp` subprocess per call. The system prompt + tool
catalog cache paid ~24k base tokens on every call. The 0.3.0 MCP
wrapper keeps one subprocess alive across the whole mcode session:
first call warms the cache (1.4% hit on a brand-new session); calls
2-N within the same mcode session hit 98-99% cache, dropping the
per-call cost to a few hundred conversation tokens.

> **Codebuddy calls spend the user's codebuddy credits, not mcode tokens.**

## Quick reference (MCP — preferred, Agent Plugins 1.0.0)

The wrapper exposes **4 tools** to mcode over a single long-lived
`codebuddy --acp` subprocess:

| Tool | When |
|------|------|
| `prompt(text, model?, append_system_prompt?, timeout?)` | Send a one-shot text prompt. |
| `continue(text, model?, append_system_prompt?, timeout?)` | Follow-up in the same codebuddy session (reuses `sessionId`, no respawn). |
| `status()` | Wrapper state: liveness, codebuddy PID, ACP session id, model, uptime, call_count, last_cache_ratio, cumulative token totals. No side effects. |
| `list_tasks(limit?)` | Most-recent-first list of recent call metadata (default 10, max 50). |

```python
# Tool call shape: name="prompt", arguments={"text": "..."}
#   response.content[0].text = "<reply>\n\n[codebuddy: pid=..., model=..., dur=...s, stop=...]\n[tokens: prompt=..., cache_ratio=...%]"
```

### Decision flow (MCP)

```text
1. Is this a text-reasoning task? (translate / summarize / review / design / brainstorm)
   ├─ No  → use mcode's own tools, not codebuddy
   └─ Yes ↓
2. Is this a follow-up in an existing codebuddy conversation?
   ├─ Yes → call `continue` (same session_id, no respawn)
   └─ No ↓
3. Will the result fit in this turn (short prompt, < 10s expected)?
   ├─ Yes → call `prompt` directly (sync; result in same turn)
   └─ No (long prompt, > 30s, 50k+ tokens) ↓
4. Spawn `task(run_in_background=true)` worker, prompt the worker to call
   `prompt` (or `continue` if mid-session), return its reply verbatim.
   mcode wakes up via `<background-task-finished>` when done.
```

### Quick diagnostics

When something feels off, call these in order — no side effects:

1. `status()` — is the wrapper alive, what model, how many calls so far?
2. `list_tasks(limit=5)` — what did the last 5 calls actually do (durations, cache ratios, model)?
3. `cat <plugin>/state/mcp-$(date +%F).log` — full audit trail (one line per event).

### Cross-session boundary

Each mcode session spawns its own wrapper, with its own
`codebuddy --acp` subprocess and its own cache. Cross-session calls
do NOT share cache. This is correct behavior (one wrapper per
worker is the right granularity for `task` workers), but be aware
that the first call of a brand-new session is server-side cold
(1.4% cache); calls 2+ warm to 98-99%.

**Verify**: `status()` returns the same `codebuddy_pid` across all
calls within one mcode session; expect a new pid across distinct
`task()` worker sessions. The wrapper's log line
`subprocess_respawn | pid=<new>` confirms the boundary was crossed.

