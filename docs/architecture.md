# Architecture

## Goals

- Give MiniMax Code a one-command way to delegate a text-reasoning task to a
  separate codebuddy subagent.
- Spend the user's codebuddy credits, not mcode LLM tokens, for that delegation.
- Be safe to call from inside an orca worktree: never close terminals the script
  did not create, never collide with the user's own mcode session.
- Be observable: every call writes a line to `logs/invocations.log`; every
  background call produces a status JSON, an events JSONL, and a result file
  the caller can `inotifywait` on.

## Components

```
┌─────────────────────────────────────────────────────────────┐
│ MiniMax Code agent (mcode)                                  │
│  - reads skills/codebuddy-integration/SKILL.md              │
│  - decides when to call invoke-codebuddy                    │
│  - calls via its bash tool                                  │
└──────────────────┬───────────────────────────────────────────┘
                   │ bash $ invoke-codebuddy "..."
                   ▼
┌─────────────────────────────────────────────────────────────┐
│ bin/invoke-codebuddy  (bash)                                │
│  - resolves <plugin-root> via readlink -f $0                │
│  - state/handle file tracks the live codebuddy terminal     │
│  - state/events-*.jsonl, state/status-*.json: ACP state    │
│  - state/result-*.md: final assistant text                  │
│  - logs/invocations.log: one tab-separated line per call    │
└────────────┬──────────────────────────┬─────────────────────┘
             │                          │
   --mode tui│                          │ --mode acp (default)
             ▼                          ▼
   orca-ide terminal            systemd-run --user transient unit
   (title = "invoke-codebuddy") │
             │                  bin/invoke-codebuddy-acp-worker.py
             ▼                          │ spawns
   codebuddy (interactive TUI)          ▼
             │                  codebuddy --acp (JSON-RPC 2.0)
             ▼                          │
        result                            ▼
                          events JSONL → state/events-*.jsonl
                          status JSON  → state/status-*.json
                          final text   → state/result-*.md
                          done marker  → state/done-*
```

## Why three modes

| Mode    | Speed | Worktree access | Stream events | Best for |
|---------|-------|-----------------|---------------|----------|
| `print` | 5-8 s | no              | none          | tiny prompts, no agent setup, fastest |
| `acp`   | 5-15 s| cwd only        | full          | long-context or anything where you want phase / tokens / trace_id |
| `tui`   | 10-25 s| yes (orca)     | none          | codebuddy needs to read/edit files in the worktree |

`acp` is the default because it gives the most signal for the least ceremony:
no worktree setup, no terminal idle wait, full event history, and a status JSON
the caller can `tail -f` instead of polling the TUI.

## Why `state/handle` and not title matching

The user's own mcode session may run a codebuddy agent whose window title
contains the same characters the script would naively use to identify its own
terminals. The script therefore never searches by title; it records the handle
that `orca-ide terminal create` returns and only closes handles from
`state/handle`. The handle is also written to disk so a later `--follow` call
from a different bash invocation can pick up the same conversation.

## Why ACP background mode is wrapped in `systemd-run --user`

The bash tool in mcode kills its child processes when the bash returns, so a
plain backgrounded Python process would die before the codebuddy call finishes.
`systemd-run --user --unit=NAME` creates a transient service unit that survives
the bash's session cleanup and is automatically garbage-collected when it
exits. The script transparently falls back to `setsid` + double-fork on systems
without systemd.

## Why `inotifywait` is preferred for `--await`

A 1-second poll loop is fine for tasks ≥ 5 s, but the bash tool's
`inotifywait` integration gives a 0-CPU event-driven wait that releases
immediately when the result file appears. The script detects `inotifywait` and
uses it automatically; otherwise it falls back to polling.

## Why we ship a single CLI and a single Python worker

- One CLI keeps the user's muscle memory simple: one command, three modes,
  fifteen flags.
- One Python worker keeps the ACP client implementation in one place. The bash
  wrapper spawns the worker in both `--background` and synchronous paths; the
  synchronous path just blocks on the same files the async path would
  eventually write.
- No `node`, no `npm install`, no native binaries — the plugin is two files plus
  docs, and the only runtime dependencies are `bash`, `python3`, and (already
  on the user's machine) `codebuddy`.

## What the plugin does NOT do

- It does not manage codebuddy auth. The user must `codebuddy` login themselves.
- It does not aggregate cost across calls. The user can `tail
  logs/invocations.log` and reason about it manually; a future plugin could
  post-process that log.
- It does not run as an MCP server. The CLI is invoked by the agent's bash
  tool, not exposed as an MCP tool, because each call is a single text
  reasoning request and the agent's bash tool already gives the user
  full visibility into the command being run.
