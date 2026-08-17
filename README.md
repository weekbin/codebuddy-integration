# codebuddy-integration

> Skill + CLI 包装:给 mcode 增加一个调用 codebuddy 的入口,处理翻译、长文摘要、寻求不同意见、
> 换思路重写代码这类纯文字任务。

## Try it

```text
用 codebuddy 帮我 review 这段 Python 的线程安全 LRU cache 设计，列出 3 个潜在的 race condition。
```

```text
summarize this 50k-token spec with codebuddy in 5 bullets, in Chinese.
```

```text
ask codebuddy to translate this README to English, keep code samples intact: $(cat README.md)
```

For long tasks (>10s), prefer the mavis subagent pattern so the main agent can
keep working in parallel:

```text
# Main agent calls the task tool (not bash), worker uses the bridge script:
task(
  agent_name="worker",
  run_in_background=true,
  prompt="""\
Run exactly this and print stdout as your final answer:

  invoke-codebuddy-bridge.sh 'review this 50k-token spec for breaking API changes, list 5'
"""
)
# Main agent continues. Later: task_output(task_id) returns the result immediately.
```

## How it works

This is a **Skill-only Plugin** containing one Skill plus a CLI binary:

- `codebuddy-integration` Skill — instructions for when MiniMax Code should call
  `invoke-codebuddy`, with the full command surface, trigger examples, and a hard
  "do not use for file edits / shell / git" rule. The Skill never writes to orca
  worktree state and never touches the user's own mcode session.
- `bin/invoke-codebuddy` — a portable bash wrapper that:
  - defaults to **ACP mode** (spawns `codebuddy --acp` over JSON-RPC 2.0, giving
    you full `session/update` events, a status JSON with phase / tokens / trace_id,
    and a result file);
  - falls back to **TUI mode** when codebuddy needs to see or edit files in the
    current orca worktree (uses `orca-ide terminal` under the hood);
  - supports a **print mode** for a quick 5-8s, no-agent-overhead subprocess call
    (`codebuddy --print`).
- `bin/invoke-codebuddy-acp-worker.py` — the Python JSON-RPC 2.0 client used by
  ACP mode to stream every event to disk (events JSONL, status JSON, result file,
  done marker). Knows how to read the mcode base system prompt and inject it via
  `--append-system-prompt` (per the "base + 业务" strategy below).
- `bin/invoke-codebuddy-bridge.sh` — a one-line sync wrapper for the mavis
  worker's bash tool. The worker calls it once, gets codebuddy's reply on
  stdout, copies that into its final answer. Worker LLM usage is near zero.
  Combined with `task(run_in_background=true)` and mcode's
  `<background-task-finished>` wake-up, this is the canonical "fire-and-
  forget" path — see "Async pattern" in `skills/codebuddy-integration/SKILL.md`.
- `bin/install.sh` — first-time setup. Writes `$HOME/.config/invoke-codebuddy/install-path`
  and `ln -sfn` the script to `~/bin/invoke-codebuddy`. Re-run after editing the
  plugin in place.
- `assets/mcode-base-system-prompt.md` — the **固化** mcode(Mavis) base system
  prompt. Injected into every acp call by default so codebuddy knows it's a
  Mavis/MiniMax Code subagent, not a free-standing assistant. Callers can
  override it (`--system-prompt` / `--system-prompt-file`) or append business
  rules after it (`--append-system-prompt`).

The plugin never carries `codebuddy` credentials, never opens network sockets of
its own, and never spawns anything that mcode's own tools could not have spawned
themselves. The only new behavior is **policy** ("when to delegate") and **ergonomics**
(oneshot / keep / follow / metrics / events in one command, plus a sync bridge for
`task(run_in_background=true)` workflows).

## Setup (one-time)

1. Install the **codebuddy** CLI and make sure it is on `$PATH`:
   `npm i -g @tencent-ai/codebuddy-code && command -v codebuddy`
2. Install this plugin through your MiniMax Code plugin manager.
3. Run `bin/install.sh` (from the plugin root or any path):
   ```bash
   /path/to/plugin/bin/install.sh
   ```
   This will:
   - write `$HOME/.config/invoke-codebuddy/install-path` (so the script knows
     where its plugin root is, even if `~/bin/invoke-codebuddy` is a symlink
     pointing somewhere else),
   - `ln -sfn <plugin-root>/bin/invoke-codebuddy ~/bin/invoke-codebuddy`,
   - smoke-test that `--help` runs.
   Re-run after editing the plugin in place to refresh the symlink.
4. (Optional, only for `--mode tui` to share the current orca worktree) make sure
   `orca-ide` is on `$PATH`: `command -v orca-ide`.

ACP and print modes do **not** require `orca-ide`.

### How plugin root is resolved

When the script runs, it figures out where its plugin root is in this order:

1. `CODEBUDDY_PLUGIN_DIR` env var (mavis / mcode can inject this when it
   installs the plugin),
2. `$HOME/.config/invoke-codebuddy/install-path` (written by `bin/install.sh`),
3. `readlink -f "$0"` of the script itself (fallback).

This means **no matter which `~/bin/invoke-codebuddy` symlink is in effect, the
`state/` and `logs/` directories always live inside the plugin that was
installed.**

## Data and network

- This Plugin itself **makes no network requests** of its own. It only spawns the
  user's already-installed `codebuddy` and (optionally) `orca-ide` CLIs.
- `codebuddy` is started with `--dangerously-skip-permissions` so it does not
  prompt on every bash/edit. This is acceptable inside the user's own worktree
  because the only caller is MiniMax Code acting on the user's behalf.
- Each call's `task` prompt is appended (first 200 chars) to
  `<plugin-root>/logs/invocations.log`. If you do not want a task recorded, pass
  `--no-log`. Rotate or delete the log as you see fit; it lives next to the
  installed script.
- Per-call artifacts (`state/result-<handle>.md`, `state/events-<handle>.jsonl`,
  `state/status-<handle>.json`) live next to the script and are not uploaded
  anywhere. Delete the `state/` directory to clear them.
- No credentials, no private endpoints, no telemetry, no hidden install steps.

## Requirements

- MiniMax Code with Agent Plugins 1.0 support.
- `codebuddy` CLI on `$PATH` (any recent build of `@tencent-ai/codebuddy-code`).
  If `command -v codebuddy` is empty, set `CODEBUDDY_BIN=/abs/path/to/codebuddy`
  instead of symlinking. The error message printed on a missing CLI lists the
  three common fix paths.
- `python3` on `$PATH` (used by the ACP worker and by JSON parsing helpers).
- The plugin does **not** need `systemd-run` or `setsid`; background work
  belongs to mcode's `task` tool with `run_in_background=true`, and the
  worker calls `invoke-codebuddy-bridge.sh` (sync). This works identically
  on macOS and Linux (no platform-specific daemonization).
- (`--mode tui` only) `orca-ide` on `$PATH` and a running orca worktree context.
  **If you pass `--mode tui` and `orca-ide` is not installed, the script falls
  back to `--mode print` automatically and prints a warning to stderr — so
  plugin-level failure is never caused by a missing `orca-ide`.**
- `jq` is **not** required.

> **`~/.codebuddy/bin/` is the CodeBuddy CN.app (GUI) install dir on macOS,
> not the codebuddy CLI.** The CLI is an npm package
> (`@tencent-ai/codebuddy-code`) and lands under your node version manager
> (`~/.nvm/versions/node/<v>/bin/`, `~/.local/bin/`, etc.). If
> `command -v codebuddy` is empty but `ls ~/.codebuddy/bin/` shows a
> `buddycn` symlink, you are looking at the wrong directory.

## Examples

```bash
# oneshot translation, plain text reply
invoke-codebuddy "translate to English: 你好世界"

# structured JSON
invoke-codebuddy --json "用 5 个字说 hi"

# long task: use mcode `task` tool with run_in_background=true;
# the worker calls invoke-codebuddy-bridge.sh (sync) and returns the reply.
# mcode wakes you via <background-task-finished>. See SKILL.md "Async pattern".

# inspect token usage after a sync call
invoke-codebuddy --metrics "$HANDLE"

# pick a model (default: let codebuddy server choose)
invoke-codebuddy --model glm-5.2 "review this 200-line function for race conditions"

# completely replace the mcode base system prompt
invoke-codebuddy --system-prompt-file ./my-strict-reviewer.md "review this PR"

# keep the mcode base, append business rules
invoke-codebuddy --append-system-prompt "You are reviewing for a fintech PCI-DSS audit." \
  "list all hardcoded secrets in this diff"
```

## Model selection

`--model` is fully caller-controlled. If you don't pass it, codebuddy picks
its own server-side default (currently `hy3` as of writing). Use
`--model <id>` to pin one — `available_models` from any prior call's
`--metrics` output lists what's reachable (e.g. `hy3`, `glm-5.2`,
`deepseek-v4-pro`, `kimi-k3-1`, ...). You can also set `CODEBUDDY_MODEL` env
var to make it a sticky default for your shell.

## System prompt strategy

The plugin ships a **固化** mcode base system prompt at
`assets/mcode-base-system-prompt.md`. Every acp-mode call injects it by
default, so codebuddy always knows it's a Mavis subagent (and what that
means for roles, boundaries, output style).

Callers have three options:

| Caller intent                                    | Flag                                | codebuddy gets                         |
|--------------------------------------------------|-------------------------------------|----------------------------------------|
| Use base only, no business rules                 | _(no flag)_                         | base via `--append-system-prompt`      |
| Use base + business rules                        | `--append-system-prompt "rule"`     | `base + rule` via `--append-system-prompt` |
| Completely replace base (e.g. raw translation)   | `--system-prompt "..."` or `--system-prompt-file <path>` | just the caller's prompt (base skipped) |

`--mode print` deliberately does **not** inject the base (to stay lightweight).
Use `--mode acp` (the default) when you want the base.

> **Why not just concatenate into `--system-prompt`?** Because codebuddy's
> `append-system-prompt` lets the caller-supplied rules land *after* the
> plugin's base, so the base is always present and the caller's text always
> wins on conflict — matches the "基础 + 业务拼接" mental model.

## Limitations

- Each call costs ~24k codebuddy input tokens on first invocation (system prompt
  + tool catalog).
- **Cache hit rate is unstable and server-driven** — it is NOT something this
  plugin can control or predict. Empirically (20-call sample, same prompt):
  - **75% of calls hit 11%** (server-side public cache, stable baseline)
  - **5% of calls hit 21%**
  - **20% of calls hit 99–100%** (rare alignment with a populated server cache slot)
  - average ≈ 23%
  In other words: a 100-call session will cost **roughly 70–80× the per-call
  number, not 1×** (which is what a "subsequent calls are nearly free" reading
  would imply). Use `--metrics <handle>` on any prior call to see the actual
  `cache_hit` / `prompt` ratio for your workload.
- TUI mode captures the first `● ...` segment of codebuddy's reply; very long
  answers are folded. Use `--keep` + `--status` or fall back to `--mode print`
  when you need the full text.
- The plugin is designed for a single-user, single-worktree flow. Sharing
  `state/handle` across multiple concurrent mcode sessions on the same
  worktree is not safe.

## Manual test evidence

The bundled `tests/smoke.sh` exercises the script's pure-local logic (argument
parsing, help text, error paths) without requiring a live `codebuddy` login.
Observed during development (2026-08-17, in a live orca worktree against
`@tencent-ai/codebuddy-code` 0.x):

```text
$ invoke-codebuddy --json "用 5 个字说 hi"
{"ok": true, "mode": "acp", "handle": "acp-sync-2251348-1786941750", "duration_s": 4.6, "rc": 0,
 "result": "你好，世界！", "status": {...}}

$ invoke-codebuddy --json "用 Python 写一个 hello world 程序，10 行以内"
{"ok": true, "mode": "acp", "handle": "acp-sync-2156048-1786939680", "duration_s": 4.1, "rc": 0,
 "result": "```python\nprint(\"Hello, world!\")\n```", "status": {...}}
```

Observed runtimes (print mode ≈ 5-8 s, ACP sync ≈ 5-10 s for short prompts,
TUI ≈ 10-25 s including terminal idle wait). Background + await pattern was
verified to release mcode LLM tokens during the wait — bash blocks at the
kernel level on the result file.

## License

MIT. See [LICENSE](./LICENSE).
