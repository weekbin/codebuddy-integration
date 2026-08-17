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

## Quick reference (legacy CLI)

> **Removed in 0.4.0.** The 0.2.1 `invoke-codebuddy` CLI is preserved
> in 0.3.0 as a fallback for clients that drive text reasoning
> through the bash tool, but it is deprecated. New code should call
> the MCP tools above.

```bash
# RECOMMENDED FIRST TRY: print mode (no orca-ide needed, no worktree context, ~4s)
invoke-codebuddy --mode print "translate to English: 你好世界"

# oneshot in default acp mode (needs codebuddy on PATH; ~5-8s; richer state)
invoke-codebuddy "translate to English: 你好世界"

# structured output
invoke-codebuddy --json "write a Python LRU cache"

# pure text path (alias of the recommended first try)
invoke-codebuddy --mode print "summarize: $(cat spec.md)"

# multi-turn with conversation context
invoke-codebuddy --keep "explain this code in Chinese"
invoke-codebuddy --follow "now translate to English"

# async (>30s task) — wrap in mcode `task` tool with run_in_background=true
#   worker 调 invoke-codebuddy-bridge.sh "<prompt>",bridge 内部用 sync 模式等结果
#   mcode 用 <background-task-finished> 系统提醒自动 wake-up,不用 cron 轮询
#   详细见下面 "Async pattern — task tool + bridge.sh"

# model selection (default: let codebuddy server pick; e.g. try glm-5.2 / deepseek-v4-pro)
invoke-codebuddy --model glm-5.2 "review this code for race conditions"

# keep the mcode base prompt, append business rules
invoke-codebuddy --append-system-prompt "You are auditing for PCI-DSS. List every secret." \
  "review this diff"

# completely replace the mcode base prompt (rare; usually --append is enough)
invoke-codebuddy --system-prompt-file ./strict-reviewer.md "review this PR"

# read ACP state from a previous sync call's state files
#   (sync 模式也写 events-/status- 文件; --metrics 打印人类可读 summary)
invoke-codebuddy --metrics <handle>              # phase / tokens / trace / by_category / system_prompt_mode
invoke-codebuddy --events <handle>               # full JSONL event stream
```

### Mode cheat sheet

| Mode | Needs `codebuddy`? | Needs `orca-ide`? | Injects mcode base system prompt? | Use when |
|------|--------------------|-------------------|-----------------------------------|----------|
| `print` | yes | **no** | **no** (lighter, faster) | first try, pure text, no worktree context |
| `acp` (default) | yes | **no** | **yes** (default) | full ACP state + tokens + events; works without orca |
| `tui` | yes | **yes** (auto-fall-back to `print` if missing) | no (TUI is interactive) | codebuddy needs to read/edit worktree files via orca-ide terminal |

## Installation

`bin/install.sh` does **everything** for you (跨平台 — macOS / Linux 都跑同一脚本):

1. Resolves the plugin root and writes `$HOME/.config/invoke-codebuddy/install-path` (anchors `state/` and `logs/`).
2. `ln -sfn` the script to `~/bin/invoke-codebuddy` (or the first `~/X/bin` on `$PATH`).
3. Detects the `codebuddy` CLI under `~/.nvm/`, `~/.local/`, `/opt/homebrew/bin/`, `/usr/local/bin/`, `/usr/bin/` and writes `$HOME/.config/invoke-codebuddy/env` (the main script sources it automatically, so no `export CODEBUDDY_BIN=` is needed).
4. Auto-appends a `PATH` block (with marker) to `~/.zshrc` and `~/.bashrc` so new shells find `invoke-codebuddy`.
5. Smoke-tests `invoke-codebuddy --help`.

```bash
"$PLUGIN_ROOT/bin/install.sh"   # 重装/同步: 直接再跑(覆盖式)
```

If `install.sh` could not auto-detect the `codebuddy` CLI, install it and re-run:

```bash
# pick one
npm i -g @tencent-ai/codebuddy-code        # fresh install
ln -s /abs/path/to/codebuddy ~/bin/codebuddy   # already installed, just expose
# then re-run:  "$PLUGIN_ROOT/bin/install.sh"
```

If you want `--mode tui` (codebuddy reading/editing files in the current orca worktree), you also need `orca-ide` on `$PATH`. **Without it the script silently falls back to `--mode print`** — see the Mode cheat sheet above.

```bash
# macOS
brew install --cask orca-ide
# Linux / other
# see https://github.com/hetaoBackend/orca
```

> **`~/.codebuddy/bin/` is a directory created by the CodeBuddy CN.app macOS
> bundle — it is NOT where the `codebuddy` CLI lives.** The CLI is an npm
> package (`@tencent-ai/codebuddy-code`) and lands under your node version
> manager (`~/.nvm/versions/node/<v>/bin/`, `~/.local/bin/`, etc.). If
> `command -v codebuddy` is empty but `ls ~/.codebuddy/bin/` shows a
> `buddycn` symlink, you are looking at the wrong directory.

`$PLUGIN_ROOT` is the directory containing this plugin (e.g. `~/.minimax/plugins/codebuddy-integration`).
The script resolves its own plugin root in this priority order, so `state/handle` and
`logs/invocations.log` always live inside the installed plugin — even if `~/bin/invoke-codebuddy`
is a symlink pointing somewhere else:

1. `$CODEBUDDY_PLUGIN_DIR` env var (mavis / mcode can inject this when it installs the plugin),
2. `$HOME/.config/invoke-codebuddy/install-path` (written by `bin/install.sh`),
3. `os.path.realpath` of the script itself via `python3` (跨平台 — macOS BSD `readlink` 没有 `-f`,所以用 python)。

## When to invoke

Use `invoke-codebuddy` when ALL of these hold:

- The task is mostly text reasoning (write, explain, translate, summarize, brainstorm,
  refactor-plan, code-review-of-a-snippet).
- The user explicitly wants another model's perspective, OR mcode's own output would cost
  too many tokens (e.g., summarizing a >50k-token blob).
- The task can be expressed as a single prompt; for multi-step tool use, use `--keep` +
  `--follow` to maintain a running session.

Do NOT use `invoke-codebuddy` for:

- Reading, editing, or creating files — mcode's `read` / `write` / `edit` tools are direct
  and don't burn credits.
- Running shell commands, git operations, or tests — mcode's `bash` tool is direct.
- Fine-grained tool iteration where the user can see each step — codebuddy's iteration is
  opaque; for "fix the failing test in this file" use mcode's own tools.

### Choose the right execution path (REQUIRED first step)

**Before calling `invoke-codebuddy`**, the agent must **detect the current environment** and
pick the right path. There are exactly two paths:

| Environment detection | Path to use |
|-----------------------|-------------|
| `command -v orca-ide` succeeds AND we are inside an orca worktree (we need codebuddy to read/edit files in the worktree) | `--mode tui` (opens an orca-ide terminal; codebuddy can read/write files in the worktree; multi-turn via `--keep` / `--follow`) |
| Anything else (no orca-ide, or codebuddy only needs to do text reasoning) | **mcode `task(run_in_background=true)` + worker calls `invoke-codebuddy-bridge.sh "<prompt>"`** (sync mode, ~5-30s; mcode wakes up via `<background-task-finished>`) |

**Why this matters**: the two paths are not equivalent.

- `--mode tui` requires `orca-ide` and a running orca worktree. It gives codebuddy the
  ability to read and edit files in the worktree, which is what makes it "share the
  worktree with the user". If you don't have that, this path silently falls back to
  `--mode print` and the user is left wondering why file edits didn't happen.

- The task + background path is the default and works everywhere. It does **not** share
  the worktree — codebuddy only sees the prompt and returns a text reply. The agent
  does file edits in subsequent turns using mcode's own `read` / `write` / `edit` tools.

**Decision flow**:

```text
1. Is this a text-reasoning task? (translate, summarize, review, design, brainstorm)
   ├─ No  → use mcode's own tools, not codebuddy
   └─ Yes ↓
2. Does codebuddy need to read/edit files in the user's worktree?
   ├─ No  → task(run_in_background=true) + invoke-codebuddy-bridge.sh
   └─ Yes ↓
3. Do we have orca-ide AND an orca worktree?
   ├─ Yes → invoke-codebuddy --mode tui (or --keep / --follow for multi-turn)
   └─ No  → fall back to task + bridge.sh; tell the user file edits are not possible
```

### Trigger examples

If the user says any of the following, invoke `invoke-codebuddy`:

- "用 codebuddy 帮我看看这段代码有没有问题"
- "让 codebuddy 写一个 README 草稿"
- "ask codebuddy to translate this to English"
- "让 codebuddy review 一下这个 PR 的设计"
- "summarize this with codebuddy" (when input is > 10k chars)

If unsure, ask the user — the decision is theirs because it costs their credits.

### Permission pre-emption — preventing the question that kills progress

Codebuddy's default behavior is to ask the user for permission before destructive actions
(write a file, run a shell command, etc.). In a subagent context (no human in the loop),
this **hangs the task indefinitely**. Live-verified: the worker keeps waiting, the
`codebuddy` process never returns, and `<background-task-finished>` never fires.

**Two defenses, applied in this order**:

1. **Always pass `--dangerously-skip-permissions` (alias `-y`)** + `--permission-mode
   bypassPermissions` + `--subagent-permission-mode bypassPermissions` to `codebuddy`.
   The plugin's `bin/invoke-codebuddy-acp-worker.py` already does this in acp mode. If
   you are calling codebuddy directly (e.g. `codebuddy --print ...` in print mode), add
   the same flags yourself.

2. **In the worker's prompt, explicitly say "do not ask the user any question"**. The
   `bypassPermissions` flag bypasses codebuddy's permission gate, but other "clarify"
   moments (e.g. ambiguous scope, missing parameter) can still pause. Tell codebuddy
   in the prompt: "Make a reasonable choice and proceed; do not stop to ask the
   user. Burning a few extra seconds of inference is better than hanging the task."

If both defenses are applied, codebuddy in acp mode does NOT block on questions
(live-verified on 2026-08-17: a 7-second end-to-end call to write a file in `/tmp`
completed without any interactive prompt).

### What if acp `--dangerously-skip-permissions` is NOT enough?

The mcp-bridge + session-tick fallback pattern (acp over mcp + cron-driven `task_query`
on the current session) was considered as a workaround for tasks that genuinely need
bidirectional question/answer with codebuddy. **Do not implement it** unless a real
case is observed where `-y` is not enough. The `--y` flag is sufficient for
fire-and-forget text-reasoning subtasks, which is what this plugin is for.

## Modes

`invoke-codebuddy` has three execution modes:

| Mode | Default | Mechanism | Requires `orca-ide`? | Worktree access | Stream events | Use when |
|------|---------|-----------|----------------------|-----------------|---------------|----------|
| `acp` | **yes** | `codebuddy --acp` over JSON-RPC 2.0 | **No** | cwd only (the bash's cwd) | full (`session/update` events) | text reasoning tasks where you want rich state (phase, tokens, trace) |
| `tui` | no | `codebuddy` inside an orca-ide terminal | **Yes** (optional; auto-falls-back to `print` if missing) | yes (shares the worktree) | none (TUI only) | codebuddy needs to read/edit files or run commands |
| `print` | no | `codebuddy --print --output-format json` | **No** | no (fresh subprocess) | none | simplest, fastest (5-8s), no install/agent setup |

The **default is `acp`**, which works on **any system with `codebuddy` installed** —
`orca-ide` is **not required**. Pass `--mode tui` only when codebuddy needs to see/edit files in
the current worktree AND you have `orca-ide` installed; if you pass `--mode tui` and `orca-ide`
is missing, the script prints a warning to stderr and silently falls back to `--mode print`,
so you still get a result. Pass `--mode print` for the cheapest, fastest, no-side-effects call.

## Command surface

| Flag | Default | Behavior |
|------|---------|----------|
| `--json` | false | output structured JSON with `ok`, `mode`, `duration_s`, `result` |
| `--keep` | false | leave the codebuddy terminal alive after the task; saves handle to `state/handle` |
| `--follow` | false | reuse the handle from `state/handle`; implies `--keep` |
| `--new-session` | false | force a fresh terminal (close any existing one in `state/handle` first) |
| `--mode tui\|print` | `acp` (sugar: `tui` or `print` accepted) | `tui` = orca-ide terminal (worktree context); `print` = `codebuddy --print` (cleaner, no worktree). `tui` auto-falls-back to `print` if `orca-ide` is not in PATH. |
| `--timeout <sec>` | 300 | max wait time for the response |
| `--no-log` | false | skip writing to `logs/invocations.log` |
| `--log [N]` | 20 | print last N invocations and exit |
| `--status <handle>` | — | print current tail of the TUI terminal (worktree context) |
| `--kill [handle]` | — | close the TUI terminal; handle defaults to `state/handle` |
| `--events <handle>` | — | dump acp-mode event stream (JSONL) for a prior sync call — phase / thought / message / usage |
| `--metrics <handle>` | — | pretty-print acp-mode status (phase, prompt_tokens, trace_id, system_prompt_mode, etc.) for a prior sync call |
| `--model <id>` | server default (let codebuddy pick) | pin a codebuddy model id; use `--metrics <handle>` on any prior call to see `available_models` |
| `--system-prompt <text>` | mcode base prompt | completely replace the mcode base system prompt with this text |
| `--system-prompt-file <path>` | mcode base prompt | completely replace the mcode base system prompt with file contents |
| `--append-system-prompt <text>` | _(none)_ | append business rules **after** the mcode base system prompt (text) |
| `--help` | — | show usage |

> **No `--background` flag.** Long tasks (>30s) belong to mcode's `task` tool with
> `run_in_background=true`; the worker calls `invoke-codebuddy-bridge.sh` (sync)
> and mcode wakes up via `<background-task-finished>`. See "Async pattern" below.

### What comes back

- Plain text mode: the assistant message on stdout.
- `--json` mode: `{"ok": true, "mode": "acp|tui|print", "handle": "...", "duration_s": N, "result": "..."}`.
- For very long answers in TUI mode, the terminal may fold the result; the script captures
  the first `● ...` segment. If the user needs the full text, use `--keep` + `--status`,
  or fall back to `--mode print`.

## Cost and safety

- **First call** in a session costs ~24k codebuddy input tokens (mcode base system
  prompt + codebuddy's tool catalog).
- **Cache hit rate is unstable and server-driven** — not something this plugin
  can control. Empirically (20-call sample, same prompt):
  - 75% of calls hit 11% (server-side public cache, stable baseline)
  - 20% of calls hit 99–100% (rare alignment with a populated cache slot)
  - average ≈ 23%
  So a 100-call session will cost roughly 70–80× the per-call number, not 1×.
  Use `--metrics <handle>` to see the actual `cache_hit` / `prompt` ratio.
- `--mode print` skips the mcode base system prompt, so it is slightly cheaper
  on the first call too (no base-prompt cache slot to populate). Use `--mode
  print` for one-shot smoke tests; use `--mode acp` (default) for production
  calls where the role/boundary guarantee matters.
- The script manages terminal lifecycle through `state/handle` (configurable via
  `INVOKE_CODEBUDDY_HANDLE_FILE`). It **never** closes terminals by title matching — only
  handles it created. The user's own mcode sessions are untouched.
- `codebuddy` is started with `--dangerously-skip-permissions` so it won't prompt for
  every bash/edit; this is acceptable inside the user's own worktree.
- If you call `invoke-codebuddy` many times in one mcode turn, accumulated codebuddy spend
  can be significant. Prefer one well-formed prompt over many retries.

## System prompt

The plugin ships a **固化** mcode (Mavis/MiniMax Code) base system prompt at
`assets/mcode-base-system-prompt.md`. Every `acp`-mode call injects it by default
(via `codebuddy`'s `--append-system-prompt` flag), so codebuddy always knows it's
a Mavis subagent, what its role is, and what boundaries it has.

| Caller intent                                    | Flag                                | What codebuddy sees                           |
|--------------------------------------------------|-------------------------------------|------------------------------------------------|
| Use base only, no business rules                 | _(no flag)_                         | just the mcode base                            |
| Use base + business rules                        | `--append-system-prompt "rule"`     | mcode base + rule                              |
| Completely replace base (e.g. raw translation)   | `--system-prompt "..."` or `--system-prompt-file <path>` | just the caller's prompt (base skipped) |

`--mode print` deliberately does NOT inject the base (to stay lightweight). Use
`--mode acp` (the default) when you want the base.

## Model selection

`--model` is **caller-controlled** — the plugin does not pick a default. If you
omit it, codebuddy picks its own server-side default (currently `hy3` as of
writing, but check `--metrics` for the live `available_models` list). To pin
one: `--model glm-5.2`, `--model deepseek-v4-pro`, etc. You can also set
`CODEBUDDY_MODEL` env var for a sticky default in your shell.

## Examples

```bash
# Translate / rephrase
invoke-codebuddy "把这段文档翻成英文，保留代码示例不动：$(cat doc.md)"

# Get a second opinion on a design
invoke-codebuddy "Review this API design and list 3 weaknesses: $(cat design.md)"

# Summarize a long spec
invoke-codebuddy "用 5 条 bullet 总结以下 spec 的关键变更：$(cat spec.md)"

# Generate a fresh implementation draft
invoke-codebuddy "用 Python 写一个 LRU cache，30 行以内，含单元测试"

# Long-context question
invoke-codebuddy --mode print "$(cat huge.log) — 上面日志里有几次 ERROR？"
```

## Async pattern — `task(run_in_background=true)` + `bridge.sh`

**The plugin does NOT provide a `--background` flag.** Background work
belongs to mcode's `task` tool, not to a script trying to daemonize itself
across the bash tool's session boundary. Concretely:

```text
# From the main mcode agent (use the `task` tool, NOT bash):
# This returns a task_id immediately. Your current turn ends.
task(
  description="codebuddy review",
  agent_name="worker",
  run_in_background=true,
  prompt="""\
Run this single command via your bash tool and return its stdout
as your final answer, verbatim:

  invoke-codebuddy-bridge.sh 'review this 50k-token spec for breaking API changes, list 5 in priority order'

If the command exits non-zero, return whatever it printed verbatim
so I can see the failure. Do not call any other tool.
"""
)
```

`invoke-codebuddy-bridge.sh` is a **sync wrapper**: it does one
`invoke-codebuddy --json "$@"` (default acp mode, ~5-30s) and prints
codebuddy's reply on stdout. The worker LLM literally copies that
stdout into its final answer — almost no LLM tokens burned on the
worker side.

### The free wake-up you already have: `<background-task-finished>`

mcode's runtime implements a turn-boundary wake-up: when a background
`task` flips to a terminal state, mcode injects a
`<background-task-finished>` system reminder into the current session
on the next turn. **No cron, no polling, no manual check needed.** The
owning conversation resumes automatically.

1. **Turn N (this turn)**: spawn the worker with `run_in_background=true`.
   The tool returns a `task_id` immediately; your current turn ends.
2. **Time passes.** Your LLM does other things, the user sends other
   messages, or the session sits idle. None of this requires the
   worker to have finished.
3. **Turn N+1 (or whenever the user re-engages)**: mcode injects a
   `<background-task-finished>` system reminder naming the `task_id`
   and its final status. You (the main LLM) read the reminder, call
   `task_output(task_id)` (instant, returns the worker's final
   answer), and surface the result.

This is the **default and recommended pattern** for any codebuddy task
that takes longer than ~30 s. Live-verified on mcode 0.1.2+ with this
plugin 0.2.1+: a `run_in_background=true` worker running
`invoke-codebuddy-bridge.sh '用 5 个字说 hi'` completed in ~5 s, and
the main LLM was woken on the next turn with the result already
buffered in `task_output`.

### When to use direct sync instead

- A short prompt (< 10 s round-trip): call `invoke-codebuddy` directly
  from your own bash tool. Wrapping it in `task` adds Agent-team
  overhead for no benefit.
- A task that needs the result mid-reasoning in the same tool call: same
  as above — direct call, no `task` wrapper.
- Multiple parallel codebuddy reviews: spawn N `task(run_in_background=true)`
  calls in one turn. mcode wakes you once for each
  `<background-task-finished>`.

### Watchdog for hung tasks (rare but possible)

The `task` tool has no built-in timeout. A worker that gets stuck —
e.g. the worker LLM itself looping, or codebuddy hung on a network
call past `--timeout` — will hold the task in `running` forever.

Standard pattern: spawn with `run_in_background=true`, then on later
turns if `task_query(task_id).status == "running"` and
`started_at + N_minutes` has passed, call `task_stop(task_id)`. Pick
N by task type (5-10 min for a code review, 1-2 min for a
translate/summarize, 15 min for a multi-file implementation draft).

```text
# On a later turn, when checking a long-running task:
status = task_query(task_id)   # returns instantly
if status == "succeeded":
    answer = task_output(task_id)
elif status == "failed":
    report_failure(task_id)
elif status == "running" and too_long_running(task_id):
    task_stop(task_id)        # force-kill; no further output will arrive
```

### Permission pre-emption

Every codebuddy launch (`bin/invoke-codebuddy-acp-worker.py` line 269)
starts with `--dangerously-skip-permissions --permission-mode
bypassPermissions --subagent-permission-mode bypassPermissions`. The
third flag is critical: **codebuddy's own teammates run their own
permission system**, and without the subagent flag they will hit
`waiting_for_permission` and hang until `--timeout` fires. With it,
codebuddy will not actually ask the user any permission question; if
a "question" would otherwise come up, codebuddy answers it as the
default-allow user and proceeds.

If you do want codebuddy to be more conservative (e.g. refuse to
edit plugin source files), put that constraint in the worker's
prompt — the main agent never needs to "respond" to a codebuddy
question in real time, because codebuddy will not ask.

### Hard rules

- **Always use `run_in_background=true`** for the `task` call when
  delegating codebuddy. Foreground defeats the whole point and blocks
  the current turn.
- **Never wrap `invoke-codebuddy --background`** (the flag no longer
  exists). The script does not manage detached processes; that is the
  `task` tool's job. If a worker needs codebuddy, it calls
  `invoke-codebuddy-bridge.sh` (sync) and returns the result.
- **Worker prompt should be one command**: `invoke-codebuddy-bridge.sh
  '<prompt>'`. Anything more burns worker LLM tokens for no benefit.

## Troubleshooting / failure recovery

| Symptom | Cause | Recovery |
|---------|-------|----------|
| `invoke-codebuddy: codebuddy CLI not found (looked for: 'codebuddy')` followed by 3 fix hints | codebuddy CLI not on `$PATH` (and `CODEBUDDY_BIN` not set) | `export CODEBUDDY_BIN=/abs/path/to/codebuddy` (e.g. `~/.nvm/versions/node/v24.12.0/bin/codebuddy`), or symlink it to `~/bin/codebuddy`, or `npm i -g @tencent-ai/codebuddy-code` |
| `invoke-codebuddy: 'orca-ide' not in PATH; TUI unavailable, falling back to --mode print` | `orca-ide` not installed; TUI mode was requested | **This is a warning, not an error.** The script automatically fell back to `--mode print` and returned a result. Install `orca-ide` and pass `--mode tui` explicitly if you actually need worktree-shared codebuddy. |
| `(error: subprocess failed)` from print mode | codebuddy subprocess exited non-zero or its JSON output was unparseable | run `codebuddy --print --output-format json --dangerously-skip-permissions --no-session-persistence "your prompt"` directly to see the real error; usually a network/auth issue |
| `(无可见回复 - 可能是长答案被 TUI 折叠)` | codebuddy's TUI truncated the response | re-run with `--keep` and read full output with `--status`; or use `--mode print` |
| `--follow` says "I don't have a previous answer" | codebuddy's TUI was reset between turns | check the handle is still alive: `invoke-codebuddy --status $(cat state/handle)`; if not, use `--new-session` |
| `invoke-codebuddy: need handle (arg or .../state/handle)` | `--status` / `--kill` with no handle and no stored handle | pass the handle explicitly, or use `--keep` / `--follow` first to create one |
| `orca terminal close` hangs or errors | orca-ide is not running | `orca-ide status --json`; if `runtime.reachable=false`, start with `orca-ide open` |
| `invoke-codebuddy: failed to create codebuddy terminal` | orca worktree context lost or auth expired | `orca-ide worktree current --json`; re-auth codebuddy if needed |
| Codebuddy times out (>5 min) | long task or rate limit | split into smaller prompts; or use `task(run_in_background=true)` + `invoke-codebuddy-bridge.sh` and let the worker run with no perceived mcode cost |
| `unknown flag: --background` | the flag was removed in 0.2.1 (was unreliable across macOS/Linux); use `task(run_in_background=true)` + `invoke-codebuddy-bridge.sh` instead | see "Async pattern" above |

### Hard rules

- **NEVER** call `pkill -f codebuddy` — that pattern matches the calling bash too and
  kills the wrong process. The script uses `orca-ide terminal close` exclusively.
- **NEVER** close a codebuddy terminal by title match (e.g. `title contains "✳"`); the
  user's own mcode session can match that filter. Only close handles recorded in
  `state/handle`.
- **ALWAYS** set `invoke-codebuddy` to a sane `--timeout` (default 300s); codebuddy can
  hang on slow API calls and `tui-idle` waits up to that.
- **ALWAYS** prefer one well-formed prompt over multiple retries; each acp-mode call
  costs ~24k codebuddy input tokens (mcode base system prompt + tool catalog). The
  prompt itself is negligible on top of that. First call of a session is ~24k;
  subsequent calls are mostly cache-hit (often 95%+ cache_read_tokens).

## File layout (after install)

```text
<plugin-root>/
├── plugin.json
├── README.md
├── LICENSE
├── skills/
│   └── codebuddy-integration/
│       └── SKILL.md             # this file (loaded by mcode on demand)
├── bin/
│   ├── invoke-codebuddy         # the only CLI (canonical)
│   └── invoke-codebuddy-acp-worker.py
├── CHANGELOG.md
├── docs/
│   └── architecture.md
└── tests/
    └── smoke.sh                 # end-to-end smoke test
```

The script uses `readlink -f "$0"` to resolve its real location, so a symlink like
`~/bin/invoke-codebuddy` does not break path lookups for `state/handle` and
`logs/invocations.log` (both are kept next to the script under `<plugin-root>/state/`
and `<plugin-root>/logs/`).
