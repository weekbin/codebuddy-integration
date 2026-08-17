# codebuddy-integration

> Skill and CLI wrapper that lets MiniMax Code delegate a text-reasoning task to a
> separate **codebuddy** subagent. Codebuddy calls spend the user's codebuddy credits
> instead of mcode tokens — useful when you want a second opinion, a translation, a
> long-context summary, or a fresh implementation draft.

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
  done marker).
- `bin/invoke-codebuddy-bridge.sh` — a one-line wrapper that does
  `--background` + `--await` for the mavis worker's bash tool, so the worker
  prompt can be one line and the worker's LLM usage is near zero. See
  "Subagent integration" in `skills/codebuddy-integration/SKILL.md`.

The plugin never carries `codebuddy` credentials, never opens network sockets of
its own, and never spawns anything that mcode's own tools could not have spawned
themselves. The only new behavior is **policy** ("when to delegate") and **ergonomics**
(oneshot / keep / follow / background / await / metrics all in one command).

## Setup (one-time)

1. Install the **codebuddy** CLI and make sure it is on `$PATH`:
   `npm i -g @tencent-ai/codebuddy-code && command -v codebuddy`
2. Install this plugin through your MiniMax Code plugin manager.
3. Symlink the bundled script so the command works in your shell:
   ```bash
   ln -sf "<plugin-root>/bin/invoke-codebuddy" "$HOME/bin/invoke-codebuddy"
   command -v invoke-codebuddy
   ```
   `<plugin-root>` is wherever MiniMax Code unzips the plugin (e.g.
   `~/.minimax/plugins/weekbin/codebuddy-integration`). The script uses
   `readlink -f` to resolve its real location, so a symlink does not break its
   `state/handle` and `logs/invocations.log` lookups.
4. (Optional, only for `--mode tui` to share the current orca worktree) make sure
   `orca-ide` is on `$PATH`: `command -v orca-ide`.

ACP and print modes do **not** require `orca-ide`.

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
- `python3` on `$PATH` (used by the ACP worker and by JSON parsing helpers).
- `systemd-run --user` for the ACP background mode (Linux only); the script
  transparently falls back to `setsid` on systems without systemd.
- (TUI mode only) `orca-ide` on `$PATH` and a running orca worktree context.
- (Optional, recommended) `inotifywait` for zero-CPU `--await` waits. Without it
  the script polls every 1 s, which is still fine for tasks ≥ 5 s.
- (TUI mode) `jq` is **not** required for TUI. (ACP / subagent worker only) `jq`
  is required because `bin/invoke-codebuddy-bridge.sh` uses `jq -r .handle` to
  extract the handle from the `--json --background` output.

## Examples

```bash
# oneshot translation, plain text reply
invoke-codebuddy "translate to English: 你好世界"

# structured JSON
invoke-codebuddy --json "用 5 个字说 hi"

# fire-and-forget long task, then await (no mcode LLM tokens burned)
HANDLE=$(invoke-codebuddy --background "用 Python 写一个 LRU cache")
sleep 30
invoke-codebuddy --await "$HANDLE"

# inspect token usage after a background task finishes
invoke-codebuddy --metrics "$HANDLE"
```

## Limitations

- Each call costs ~28k codebuddy input tokens (system prompt + tool catalog) plus
  the actual prompt. Tiny prompts still burn ~30k credits. Prefer one well-formed
  prompt over many retries.
- TUI mode captures the first `● ...` segment of codebuddy's reply; very long
  answers are folded. Use `--keep` + `--status` or fall back to `--mode print`
  when you need the full text.
- The plugin is designed for a single-user, single-worktree flow. Sharing
  `state/handle` across multiple concurrent mcode sessions on the same
  worktree is not safe.
- ACP background mode depends on `systemd-run --user` (Linux) or `setsid` as
  fallback. On platforms where neither is available, `--background` may not
  survive the bash tool's session cleanup.

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
