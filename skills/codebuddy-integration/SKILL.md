---
name: codebuddy-integration
description: "Delegate a text-reasoning subtask to codebuddy (a peer CLI) via the `invoke-codebuddy` command. Use this Skill when the user asks for a second opinion, a longer-context summary, a translation, a fresh implementation draft, brainstorming, or a design review — and burning codebuddy credits is preferable to spending mcode tokens. Do NOT use for file edits, git operations, or shell commands (mcode already has those tools). Triggers on phrases like '用 codebuddy', '让 codebuddy', 'summarize with codebuddy', 'review with codebuddy', 'ask codebuddy'."
license: MIT
compatibility: Requires MiniMax Code with Agent Plugins 1.0 support, the `codebuddy` CLI on $PATH (or `CODEBUDDY_BIN` env var pointing at it), and (for `--mode tui` only, otherwise optional) the `orca-ide` CLI.
metadata:
  author: weekbin
  version: "0.1.8"
---

# codebuddy-integration — codebuddy as a subagent

The plugin ships a `bin/invoke-codebuddy` script (symlink it to a directory on `$PATH` to expose
the command). It spawns `codebuddy` and returns the result. **Codebuddy calls spend the user's
codebuddy credits, not mcode tokens.**

## Quick reference

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

# async (ACP, default) — fire and forget, mcode keeps going, full state via ACP
invoke-codebuddy --background "long task"        # returns handle + events/status/result file paths
invoke-codebuddy --await <handle>                # blocks bash (0 mcode LLM tokens) until done
invoke-codebuddy --result-file <handle>          # just prints the result file path
invoke-codebuddy --metrics <handle>              # rich ACP state: phase / tokens / trace / by_category
invoke-codebuddy --events <handle>               # full event stream (JSONL, all phase + thought + message + usage)

# async (TUI, when codebuddy needs to do real work in the worktree)
#   - requires `orca-ide` on PATH; without it the script silently falls back
#     to --mode print and prints a warning to stderr
invoke-codebuddy --mode tui --background "edit this file"
```

### Mode cheat sheet

| Mode | `--background`? | Needs `codebuddy`? | Needs `orca-ide`? | Use when |
|------|-----------------|--------------------|-------------------|----------|
| `print` (default for one-shot) | n/a | yes | **no** | first try, pure text, no worktree context |
| `acp` (default) | n/a | yes | **no** | full ACP state + tokens + events; works without orca |
| `acp` (default) + `--background` | yes | yes | **no** (uses `systemd-run` on Linux / `setsid` on macOS) | long task, fire-and-forget |
| `tui` | n/a | yes | **yes** (with auto-fall-back to `print` if missing) | codebuddy needs to read/edit worktree files via orca-ide terminal |
| `tui` + `--background` | yes | yes | **yes** (with auto-fall-back) | long task in worktree context |

## Installation

After installing this plugin, three things must be true:

1. The `invoke-codebuddy` script is on `$PATH` (or called by absolute path).
2. The `codebuddy` CLI is on `$PATH` (or `CODEBUDDY_BIN` points to it).
3. The `orca-ide` CLI is on `$PATH` **only if you want `--mode tui`** — otherwise it is optional (the script will warn and fall back to `--mode print`).

```bash
# 1) expose the script
PLUGIN_ROOT="$(plugin-root)"   # e.g. ~/.minimax/plugins/codebuddy-integration
ln -s "$PLUGIN_ROOT/bin/invoke-codebuddy" "$HOME/bin/invoke-codebuddy"

# 2) expose codebuddy (pick one)
#    a) if it's already installed, just find it and put it on PATH:
find ~/.nvm ~/.local /opt -name codebuddy -type l 2>/dev/null | head -3
export CODEBUDDY_BIN="$(find ~/.nvm ~/.local -name codebuddy -type l 2>/dev/null | head -1)"
#       (add the export to ~/.zshenv / ~/.bashrc to persist)
#    b) or symlink it to a dir on PATH:
ln -sf "$(command -v codebuddy 2>/dev/null || echo /path/to/codebuddy)" "$HOME/bin/codebuddy"
#    c) or install fresh:
#       npm i -g @tencent-ai/codebuddy-code

# 3) (only if you need --mode tui) install orca-ide
#    brew install --cask orca-ide   # macOS
#    see https://github.com/hetaoBackend/orca for Linux / other
```

> **`~/.codebuddy/bin/` is a directory created by the CodeBuddy CN.app macOS
> bundle — it is NOT where the `codebuddy` CLI lives.** The CLI is an npm
> package (`@tencent-ai/codebuddy-code`) and lands under your node version
> manager (`~/.nvm/versions/node/<v>/bin/`, `~/.local/bin/`, etc.). If
> `command -v codebuddy` is empty but `ls ~/.codebuddy/bin/` shows a
> `buddycn` symlink, you are looking at the wrong directory.

`$(plugin-root)` is the directory containing this plugin (e.g. `~/.minimax/plugins/weekbin/codebuddy-integration`
or wherever MiniMax Code unzips the plugin). The script uses `readlink -f "$0"` to resolve its
real location, so the symlink does not break internal path lookups for `state/handle` and
`logs/invocations.log`.

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

### Trigger examples

If the user says any of the following, invoke `invoke-codebuddy`:

- "用 codebuddy 帮我看看这段代码有没有问题"
- "让 codebuddy 写一个 README 草稿"
- "ask codebuddy to translate this to English"
- "让 codebuddy review 一下这个 PR 的设计"
- "summarize this with codebuddy" (when input is > 10k chars)

If unsure, ask the user — the decision is theirs because it costs their credits.

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
| `--background` | false | do not wait for response; print handle and exit |
| `--new-session` | false | force a fresh terminal (close any existing one in `state/handle` first) |
| `--mode tui\|print` | `acp` (sugar: `tui` or `print` accepted) | `tui` = orca-ide terminal (worktree context); `print` = `codebuddy --print` (cleaner, no worktree). `tui` auto-falls-back to `print` if `orca-ide` is not in PATH. |
| `--timeout <sec>` | 300 | max wait time for the response |
| `--no-log` | false | skip writing to `logs/invocations.log` |
| `--log [N]` | 20 | print last N invocations and exit |
| `--status <handle>` | — | print current tail of the codebuddy terminal |
| `--await <handle>` | — | **block** until the background task writes its result file, then print the result. Uses `inotifywait` if available, else polls every 1s. Burns 0 mcode LLM tokens. |
| `--result-file <handle>` | — | print the absolute path of the result file (so mcode can `inotifywait` / `tail -f` it itself) |
| `--kill [handle]` | — | close the codebuddy terminal; handle defaults to `state/handle` |
| `--help` | — | show usage |

### What comes back

- Plain text mode: the assistant message on stdout.
- `--json` mode: `{"ok": true, "mode": "acp|tui|print", "handle": "...", "duration_s": N, "result": "..."}`.
- For very long answers in TUI mode, the terminal may fold the result; the script captures
  the first `● ...` segment. If the user needs the full text, use `--keep` + `--status`,
  or fall back to `--mode print`.

## Cost and safety

- Each `invoke-codebuddy` call costs ~28k codebuddy input tokens (system prompt + tool
  catalog) plus the actual prompt. Output is typically 100-300 tokens. Even tiny prompts
  burn ~30k credits.
- The script manages terminal lifecycle through `state/handle` (configurable via
  `INVOKE_CODEBUDDY_HANDLE_FILE`). It **never** closes terminals by title matching — only
  handles it created. The user's own mcode sessions are untouched.
- `codebuddy` is started with `--dangerously-skip-permissions` so it won't prompt for
  every bash/edit; this is acceptable inside the user's own worktree.
- If you call `invoke-codebuddy` many times in one mcode turn, accumulated codebuddy spend
  can be significant. Prefer one well-formed prompt over many retries.

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

### Async pattern — fire and forget, mcode keeps working

```bash
# 1. mcode 启动后台任务（~5s 启动时间），返回 handle + result-file
HANDLE=$(invoke-codebuddy --background "用 Python 写一个完整的 HTTP server")
RESULT_FILE=$(invoke-codebuddy --result-file "$HANDLE")
echo "task started, will write to: $RESULT_FILE"

# 2. mcode 在同一个 bash 调用里干别的活 (4-8s)
do_something_else_useful
sleep 3

# 3. mcode 等结果 - bash 阻塞，0 mcode LLM tokens
RESULT=$(invoke-codebuddy --await "$HANDLE")
echo "codebuddy 答: $RESULT"

# 4. (可选) 用 --kill 关 terminal，或留 --keep 等后续追问
invoke-codebuddy --kill "$HANDLE"
```

`invoke-codebuddy --background` returns immediately after spawning a detached watcher that
runs the actual codebuddy task. The watcher writes `state/result-<handle>.md` and
`state/done-<handle>` when finished. `--await` blocks on those files (using `inotifywait`
when available, else 1s polling). **No mcode LLM tokens are burned during the wait** —
mcode's bash tool just blocks at the kernel level.

If `inotifywait` is not installed, install it for zero-CPU waits: `apt install inotify-tools`
(macOS: `brew install inotify-tools`). The polling fallback is fine for tasks ≥ 5s.

The `--mode acp` (default) worker runs in a `systemd-run --user` transient service unit
so it survives the bash tool's session cleanup. Each invocation gets a unique handle
(`acp-<pid>-<ts>`), writes JSONL events + JSON status snapshot + final result, and
cleans up. `--metrics` reads the live status JSON, so mcode can poll for progress at
any time:

```bash
# Async with full ACP state monitoring
HANDLE=$(invoke-codebuddy --background "用 Python 写一个 5 行的 LRU cache")
echo "background: $HANDLE"

# mcode does other work for ~3s
sleep 3

# Check current state (no LLM cost)
invoke-codebuddy --metrics $HANDLE
#  handle:         acp-XXXXX-XXXXXXX
#  phase:          model_streaming
#  outcome:        None
#  duration:       Nones           (still running)
#  trace_id:       None
#  tokens:         prompt=0 completion=0 reasoning=0 cache_hit=0
#  context:        used=0/192000 (0.0%)
#  by_category:    {systemPrompt:0, conversation:0, tools:0, mcp:0, skills:0}

# Wait for final result
RESULT=$(invoke-codebuddy --await $HANDLE)
echo "codebuddy 答: $RESULT"

# Now --metrics shows final stats
invoke-codebuddy --metrics $HANDLE
#  phase:          done
#  outcome:        SUCCESS
#  duration:       7.5s
#  trace_id:       6ffe777f...
#  tokens:         prompt=28105 completion=19 reasoning=0 cache_hit=27776
#  context:        used=28105/192000 (14.6%)
#  by_category:    {systemPrompt:2060, conversation:4246, tools:21415, ...}
```

The `trace_id` correlates to codebuddy's internal log; the user can use it for support
requests. The token breakdown shows exactly how many tokens each subsystem ate — useful
for understanding where the codebuddy credit is going.

## Subagent integration — REAL behavior, not wishful

**Important — this section was wrong in 0.1.0/0.1.1 and was rewritten after
real usage in 0.1.2/0.1.3.** mcode's `task` tool runs an Agent-team-style
synchronous subagent dispatch. **There is no true "fire-and-forget +
completion callback" path** in the tools currently exposed to the main
agent. Read this section as a record of what *actually happens*, not as a
promise.

For tasks that take more than ~10 seconds (long-context summarization, deep
code review, multi-step implementation drafts), wrap `invoke-codebuddy` in
a `task` call so the worker LLM does the bash invocation and the bridge
script's `--await` wait. The main agent's current turn **will block** until
the worker finishes — that is the price of using this path. If you need to
do other work in parallel, you must let this turn end and start a new one.

### What you cannot do today (as of 0.1.3)

- "派完 task 立刻在同一 turn 继续干别的事" — the `task` tool's foreground
  variant blocks until the worker returns. Setting `run_in_background=true`
  does not change this in practice for the way the tool is currently wired
  up; the only fire-and-forget that actually returns immediately is a
  detached child that you re-check via `task_query` / `task_output` on a
  later turn.
- "codebuddy 完成时主 agent 立刻被 push 通知" — there is no push channel
  into the LLM. Subagent completion is observed at the start of a new
  turn, when the main agent calls `task_query` / `task_output` and sees the
  status has flipped to `succeeded`.
- "codebuddy 提问时主 agent 实时给具体响应" — same reason. The fix is to
  pre-empt questions (see "Permission pre-emption" below), not to handle
  them in-flight.

### The pattern that actually works

1. From the **main agent** (mcode), call the `task` tool. The default
   foreground variant is what you want here — it returns the worker's
   final answer in the same turn.

2. The worker subagent runs `invoke-codebuddy-bridge.sh "<prompt>"` from
   its own bash tool. The bridge does `invoke-codebuddy --json --background`
   (so the handle is a single line) then `invoke-codebuddy --await` (event-
   driven via `inotifywait` on `state/done-<handle>`), and prints
   codebuddy's reply on stdout. The worker then copies that stdout into its
   final answer.

3. The main agent's turn is over when the worker returns. The next turn can
   be a follow-up tool call, a `task_query` of a different background task,
   or a user message.

### Permission pre-emption (CRITICAL)

In 0.1.3, every codebuddy launch is started with
`--dangerously-skip-permissions --permission-mode bypassPermissions
--subagent-permission-mode bypassPermissions`. The third flag is the one
that 0.1.0/0.1.1 was missing: **codebuddy's own subagents/teammates run
their own permission system, and without the subagent flag they will hit
`waiting_for_permission` and hang until the bridge's 300 s timeout.**
If you see `phase=waiting_for_permission` in a status JSON, that is the
exact symptom.

If you do want codebuddy to ask before doing something destructive
(typically: editing your plugin source files), pass a stricter prompt:
"do not edit any file outside /tmp; refuse any edit to plugin sources".
The `-y` flags above just mean "if a question comes up, answer it as
the user would, don't block waiting for a human".

### Example main-agent prompt

```text
# In the main mcode agent (use the `task` tool, not bash):
#
# Expect: this turn blocks until the worker returns (~30 s-5 min).

task(
  agent_name="worker",
  prompt="""\
Run exactly this command via your bash tool and return its stdout as
your final answer, without modification:

  invoke-codebuddy-bridge.sh 'review this 50k-token spec for breaking API changes, list 5 in priority order'

If the command times out, return whatever it printed verbatim so I can
see the failure. Do not call any other tool.
"""
)
```

### When to use a smaller surface instead

- A short prompt (< 10 s round-trip): call `invoke-codebuddy` directly
  via your own bash tool. Wrapping it in `task` adds Agent-team overhead
  for no benefit.
- A task that needs the result mid-reasoning in the same tool call: same
  as above — direct call, no `task` wrapper.
- Multiple parallel codebuddy reviews: see "Background variant" below.

### Background variant (when you do not need this turn's result)

```text
# Returns a task_id immediately, current turn ends. The next turn can
# task_query(task_id) to check status; task_output(task_id) returns
# immediately once the task has flipped to "succeeded".
task(
  agent_name="worker",
  run_in_background=true,
  prompt="...",
)
```

In 0.1.3 this still ends your turn, but does not block the LLM on the
worker's actual completion. The user perceives the worker as running in
the background; the main agent's next turn sees the result.

### Background variant + watchdog (the pattern you should actually use)

The `task` tool has **no timeout parameter** (verified in 0.1.3: its
schema exposes only `agent_name`, `prompt`, `model_config_id`, and
`run_in_background`). A worker that gets stuck — e.g. codebuddy
waiting on a permission prompt, or the worker's LLM looping on a
retry — will hold the task in `running` forever (until the bridge's
internal 300 s `await` timeout fires, which only unblocks the worker
if codebuddy itself returns; it does not unblock a hung worker LLM
turn). Live reproduction: the 0.1.2 second review ran for ~9 minutes
before the task was eventually aborted externally.

The standard pattern is **background + watchdog**:

1. Spawn the worker with `run_in_background=true`. The current turn
   ends immediately. Save the returned `task_id`.
2. On a later turn, call `task_query(task_id)` — **returns instantly**
   with one of `queued | running | stopping | succeeded | failed |
   canceled | lost`.
3. If status is `running` and `started_at + N_minutes` has passed,
   call `task_stop(task_id)` to force-kill. There is no built-in
   timeout, so **N is your choice** — pick something like 5-10 min
   for a code review, 1-2 min for a translate/summarize, 15 min for
   a multi-file implementation draft.
4. If status is `succeeded` or `failed`, call `task_output(task_id)`
   for the worker's final answer (returns immediately, no blocking).

```text
# Turn 1: spawn and end the turn immediately
task_id = task(
  agent_name="worker",
  run_in_background=true,
  prompt="invoke-codebuddy-bridge.sh 'review plugin X'",
)

# ... some time passes; main agent gets a new turn (user message, or
# a new tool call, or `update_goal` polling) ...

# Turn N: check and act
status = task_query(task_id)   # returns instantly
if status == "succeeded":
    answer = task_output(task_id)
elif status == "failed":
    report_failure(task_id)
elif status == "running" and too_long_running(task_id):
    task_stop(task_id)        # force-kill; no further output will arrive
```

This is the closest you get to "true fire-and-forget with timeout"
in current mcode. It costs an extra turn each time you check status,
but the main agent is never stuck on a hung worker.

### Subagent "question" handling — why we pre-empt, not respond

In 0.1.3, every codebuddy launch runs with
`--dangerously-skip-permissions --permission-mode bypassPermissions
--subagent-permission-mode bypassPermissions`. This means **codebuddy
will not actually ask the user any permission question**; if a
"question" would otherwise come up, codebuddy answers it as the
default-allow user and proceeds. The subagent-mode flag is the one
that was missing in 0.1.0-0.1.2 and caused the famous
`waiting_for_permission` hang.

If you want codebuddy to be more conservative (e.g. refuse to edit
plugin source files), put that constraint in the **worker's prompt**:

```text
"codebuddy should not edit any file outside /tmp; if it tries to edit
 plugin sources, refuse and continue with a textual suggestion only"
```

The main agent never needs to "respond" to a codebuddy question in
real time, because codebuddy will not ask. (If the worker LLM itself
gets stuck on something and the task hangs, the watchdog above is
the only way out — there is no live answer-back channel.)

### The free wake-up you already have: `<background-task-finished>`

This is the part that surprised us in 0.1.5. mcode's runtime already
implements a turn-boundary wake-up: when a background `task` flips to
a terminal state, mcode injects a `<background-task-finished>`
system reminder into the current session on the next turn. **You
do not need cron, polling, or any external mechanism to be notified
that a background codebuddy task finished.** The owning conversation
resumes automatically.

Concretely, the pattern that works on mcode 0.1.2+ with this plugin
0.1.4+ is:

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
that takes longer than ~30 s. Live-verified in 0.1.5: a
`run_in_background=true` smoke test with `agent_name="worker"` and
`prompt="invoke-codebuddy-bridge.sh '用 5 个字说 hi'"` completed in
~5 s, and the main LLM was automatically woken by
`<background-task-finished>` on the next turn with the result
"你好呀世界" already buffer in `task_output`. No cron, no polling,
no user prompt.

### Wake-up pattern: `mavis cron once` self-poke (subagent session only)

For long-running codebuddy work that you would like to come back to
**without the user having to ask**, combine the background `task` with
`mavis cron once` set to a future turn. This is the closest you get
in current mcode to "派完 codebuddy，主 agent 干别的事，过 N 分钟
自动回来拿结果" without building any new infrastructure. The cron
once fires into the same session as a user-role message; on the next
turn the main agent (or you, in this case) reads the cron prompt and
picks up where it left off.

```text
# Turn 1: spawn the worker in the background, then immediately
# schedule a self-poke. Both calls are non-blocking.

task_id = task(
  description="long codebuddy review",
  agent_name="worker",
  run_in_background=True,
  prompt="""\
Run: invoke-codebuddy-bridge.sh 'review this 50k-token spec for breaking API changes'

If the command exits 0, print its stdout verbatim.
If it exits non-zero, print whatever it printed verbatim so I can see
the failure.
Do not call any other tool.
"""
)

# Self-poke: 5 minutes from now, inject a user-role turn into this
# same session. The new turn's prompt is the cron prompt; the LLM
# will execute it as a fresh turn.
mavis({
  command: "cron once",
  args: {
    cron_name: "codebuddy-self-check",
    after: "5m",
    prompt: """\
Check the background codebuddy review (task_id={task_id}):

  status = task_query({task_id})
  if status == "succeeded":
      answer = task_output({task_id})
      # ... summarize and surface to the user
  elif status == "running":
      # Re-schedule another self-poke in 60s, OR give up if too long
      mavis({{ command: "cron once", args: {{
        cron_name: "codebuddy-self-check",
        after: "60s",
        prompt: <this same prompt>,
        session: {{ mode: "sessionId", session_id: "me" }}
      }}})
  elif status in ("failed", "stopped", "canceled", "lost"):
      # ... report failure
""",
    session: { mode: "sessionId", session_id: "me" }
  }
})
# Both calls return immediately. Your current turn ends here.
```

Notes on this pattern:

- The self-poke prompt is the contract. Make it idempotent: re-running
  it should be safe (check status, don't double-act). Re-arming the
  cron is how you implement "wait longer" without holding a socket.
- Use `session_id: "me"` (the literal string) so the cron routes
  into the current session. `agent_name` is optional in this mode;
  the runtime derives the agent from the target session.
- `after` accepts durations like `"5m"`, `"60s"`, `"1h30m"`. The
  parser is shared with `cron self`'s `every` and the `task_output`
  timeout; millisecond-scale values are not documented.
- The cron-fired turn is a **fresh user-role turn**, not an in-band
  tool result. The LLM treats the cron prompt as a new request from
  the user. This is the closest you get to a wake-up without
  changes to mcode.
- If the task already finished before the cron fires, the cron turn
  will see `succeeded` and call `task_output` immediately. If the
  task is still running, the cron prompt can re-arm itself.
- Safety budget: pick a maximum number of re-arms (e.g. "after 6
  re-arms, `task_stop` the task"). Without a budget, a hung task
  will keep re-arming the cron indefinitely.
- **Important: the `mavis` tool is not exposed to the root session.**
  mcode hides the `mavis` CLI tool from the primary Mavis agent (by
  design — `agent list` defaults to `include_primary=false`). The
  `cron once` self-poke pattern only works in **subagent sessions**,
  e.g. from inside a `task` worker. For the root session's own
  "fire and forget a codebuddy review" use case, prefer the default
  `<background-task-finished>` wake-up above — it works without
  needing `mavis` at all.

### Hard rules when using any wake-up pattern

- Always use `run_in_background=True` on the `task` call. Foreground
  defeats the whole point and blocks the current turn.
- If using `mavis cron once` (subagent session only): set
  `session.mode: "sessionId"` and `session_id: "me"` on
  the cron. `mode: "new"` would create a *separate* session that
  the user is not looking at.
- Do not stack more than 1-2 re-arms in flight. Each re-arm uses a
  mavis cron slot; if you spawn 10 in 5 minutes you are creating
  more pressure than you are saving.
- Always end your current turn **immediately** after scheduling the
  cron. The whole point is to release the LLM to do other things; if
  you keep reasoning in the same turn, you block yourself.

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
| Codebuddy times out (>5 min) | long task or rate limit | re-run with `--background` and poll with `--status`; or split into smaller prompts |
| `--mode tui` works on Linux but hangs on macOS | macOS has no `systemd-run`; the script's fallback to `setsid` for `--background` is less robust than the systemd path | for macOS, prefer `--mode print` for one-shots, and `--mode tui` (foreground) without `--background` for in-worktree work; long-running macOS background tasks may need a manual `nohup … &` wrapper |

### Hard rules

- **NEVER** call `pkill -f codebuddy` — that pattern matches the calling bash too and
  kills the wrong process. The script uses `orca-ide terminal close` exclusively.
- **NEVER** close a codebuddy terminal by title match (e.g. `title contains "✳"`); the
  user's own mcode session can match that filter. Only close handles recorded in
  `state/handle`.
- **ALWAYS** set `invoke-codebuddy` to a sane `--timeout` (default 300s); codebuddy can
  hang on slow API calls and `tui-idle` waits up to that.
- **ALWAYS** prefer one well-formed prompt over multiple retries; each call costs ~28k
  tokens regardless of prompt length.

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
