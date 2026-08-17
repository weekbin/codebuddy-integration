# Changelog

All notable changes to this plugin are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.7] - 2026-08-17

### Fixed (from live feedback during 0.1.6 install)

- **`orca-ide` is now a true optional dependency** (was: silent hard
  requirement for the `tui` mode path). Three concrete changes in
  `bin/invoke-codebuddy`:
  - `ORCA` default value is now `$(command -v orca-ide 2>/dev/null || true)`,
    so the default is empty string when `orca-ide` is missing — no
    hardcoded dependency left in the script header.
  - When the caller explicitly passes `--mode tui` and `orca-ide` is
    missing, the script **silently falls back to `--mode print`** with
    a single warning line on stderr. The result is identical to
    running `--mode print` directly; the call no longer exits with
    `code 4` just because `orca-ide` is not installed.
  - The unconditional `command -v "$ORCA"` check at the top of the
    TUI block (which used to fail-fast with exit 4) is now redundant
    and was removed; the fall-back gate at the top of the script
    short-circuits the TUI block before the check.

### Documented

- `SKILL.md` "Modes" table now has a "Requires `orca-ide`?" column
  that makes the optionality visible at a glance (`acp` and `print`
  = No, `tui` = Yes with auto-fall-back).
- `SKILL.md` "Troubleshooting" table reclassifies the `orca-ide not in
  PATH` symptom from "error" to "warning + auto-fall-back" with
  recovery "install orca-ide only if you actually need worktree-shared
  codebuddy".
- `README.md` Requirements section notes the auto-fall-back explicitly
  so plugin-level failure is never caused by a missing `orca-ide`.

### Changed

- Bumped `plugin.json`, `SKILL.md` metadata to `0.1.7`.

## [0.1.6] - 2026-08-17

### Documented (no code change)

- **The free wake-up that already works**: in 0.1.5 we shipped the
  `mavis cron once` self-poke pattern. After live verification
  (a `run_in_background=true` smoke test that completed in ~5 s and
  was automatically surfaced via `<background-task-finished>` on
  the next turn), it turns out the **default mcode wake-up already
  covers the common case** without any extra machinery. SKILL.md
  now leads with a new section "The free wake-up you already have:
  `<background-task-finished>`" that documents this as the
  **default and recommended** pattern.
- The `mavis cron once` self-poke section has been demoted to
  "subagent session only" with a hard callout: the `mavis` CLI tool
  is **not exposed to the root session** (mcode hides it from the
  primary Mavis agent by design — `agent list` defaults to
  `include_primary=false`). The cron self-poke is useful for
  subagent sessions (e.g. inside a `task` worker), but the root
  session does not need it.

### Changed

- Bumped `plugin.json`, `SKILL.md` metadata to `0.1.6`.
- Re-ordered SKILL.md "Subagent integration" so the
  `<background-task-finished>` wake-up is described first, and
  the `mavis cron once` self-poke is described second with a
  callout about session scope.

## [0.1.5] - 2026-08-17

### Documented (no code change)

- Added a new SKILL.md section **"Wake-up pattern: `mavis cron once`
  self-poke"** — the cheapest available path in mcode for
  "派完 codebuddy，主 agent 干别的事，过 N 分钟自动回来拿结果":
  spawn a background `task`, then immediately call
  `mavis({ command: "cron once", args: { after: "5m", prompt: "<self-check>",
  session: { mode: "sessionId", session_id: "me" } } })`. The cron fires
  into the *current* session as a user-role turn; the new turn reads
  the prompt, calls `task_query` / `task_output` (or re-arms another
  cron if still running), and surfaces the result. No in-band push
  is needed because the wake-up *is* a turn.
- Documented the hard rules and a safety budget for re-arming, so
  this pattern doesn't accidentally turn into a runaway cron loop.
- Decision (verified by 5-min static investigation of the mcode
  binary and the MCP TypeScript SDK in 0.1.4): **do not build an MCP
  streamable-HTTP bridge** as a side-channel for this. mcode's MCP
  client does not surface `notifications/message` to the LLM tool
  surface, so the bridge would only re-implement `task` / `task_query`
  in MCP form. The mavis `cron once` self-poke is the cheaper path.

### Changed

- Bumped `plugin.json`, `SKILL.md` metadata to `0.1.5`.

## [0.1.4] - 2026-08-17

### Documented (no code change)

- **`task` tool has no built-in timeout** — verified by reading
  `mcode`'s binary: the `task` tool's Zod schema exposes only
  `agent_name`, `prompt`, `model_config_id`, and `run_in_background`.
  There is no `timeout` / `deadline` / `max_runtime` parameter. A
  worker that gets stuck (codebuddy waiting on a permission prompt,
  or a worker LLM looping on a retry) holds the task in `running`
  indefinitely; the bridge's internal 300 s `await` only fires if
  codebuddy itself returns, it does not unblock a hung worker turn.
  Live reproduction: the 0.1.2 second review ran ~9 minutes before
  being aborted externally.
- Added a new SKILL.md section **"Background variant + watchdog"**
  that documents the only pattern that can survive a stuck worker:
  `run_in_background=true` + `task_query(task_id)` on a later turn +
  `task_stop(task_id)` if the task has been `running` longer than
  your chosen budget. There is no live "main agent wakes up when
  worker finishes" event; the check is pull-based, on a per-turn
  basis.
- Added a new SKILL.md section **"Subagent question handling — why
  we pre-empt, not respond"** that explains why the 0.1.3 `-y`-style
  flags are the *only* way to deal with codebuddy questions: there
  is no in-flight response channel, so the only mitigation is to
  prevent the question from being asked in the first place (via the
  subagent permission mode and a tight worker prompt).

### Changed

- Bumped `plugin.json`, `SKILL.md` metadata to `0.1.4`.

## [0.1.3] - 2026-08-17

### Fixed (from live codebuddy run in 0.1.2)

- **codebuddy subagent permission hang**: every codebuddy launch (both ACP
  worker and TUI terminal create) now passes
  `--dangerously-skip-permissions --permission-mode bypassPermissions
  --subagent-permission-mode bypassPermissions`. The third flag was
  missing in 0.1.0-0.1.2 and caused codebuddy's own teammate/subagent
  system to enter `waiting_for_permission` and hang until the bridge's
  300 s `await` timeout. Live reproduction: during the 0.1.2 review
  run, codebuddy tried to copy the plugin to `/tmp` for a sandbox test
  and the permission prompt blocked the entire task.

### Documented (rewrote "Subagent integration" section honestly)

- The previous "Subagent integration" section claimed `run_in_background=true`
  lets the main agent continue working while the worker runs. That was
  wishful. Reality in 0.1.2: the `task` tool's foreground variant blocks
  the current turn until the worker returns; `run_in_background=true`
  ends the turn but the main agent still has no in-turn push notification
  for completion. The section now describes what *actually happens* and
  what the user-visible behavior is.

### Changed

- Bumped `plugin.json`, `SKILL.md` metadata to `0.1.3`.
- README "Requirements" lists `jq` as a soft requirement (needed only for
  the mavis-subagent bridge path, not for direct `invoke-codebuddy` calls).

## [0.1.2] - 2026-08-17

### Fixed (from codebuddy review of 0.1.1)

- **1.2 acp --background 双开 TUI 终端** (`invoke-codebuddy:293-320`) — The
  default `acp` + `--background` path used to enter the TUI terminal-creation
  block before the ACP-background block, so every async call spawned both a
  real orca terminal (with `--dangerously-skip-permissions`) AND an ACP
  worker, double-billing codebuddy credits and leaking a TUI terminal. The
  TUI block is now gated on `MODE=tui`.
- **1.5 bridge 抓 handle 是多行** (`bin/invoke-codebuddy-bridge.sh:61`) —
  The bridge used `HANDLE=$("$INVOKE" --background "$PROMPT")`, but
  `--background` prints 6 lines (or a JSON object), so the captured HANDLE
  was multi-line garbage and `--await` immediately timed out. The bridge now
  calls `invoke-codebuddy --json --background ... | jq -r .handle`.
- **1.1 orca-ide 强制要求** (`invoke-codebuddy:215-216`) — The unconditional
  `command -v orca-ide` / `command -v codebuddy` checks ran before mode
  dispatch, blocking `print` and `acp` modes (and pure local subcommands
  like `--log`, `--metrics`) on systems without orca installed. Moved both
  checks inside the TUI branch.
- **state/events-*.jsonl 爆炸** (`bin/invoke-codebuddy-acp-worker.py`) — The
  worker was appending every `agent_thought_chunk` and `agent_message_chunk`
  to the events JSONL, producing 1-2 MB files per call. Chunks are now kept
  only in in-memory buffers (with a 32-chunk throttled flush to status JSON);
  events JSONL now records only phase transitions, usage updates, and the
  final done event.

### Changed

- Bumped `plugin.json`, `SKILL.md` metadata to `0.1.2` (previously drifted
  to `0.1.1` in CHANGELOG but not in the manifest). Fixed README typo
  "codebudbuddy" -> "codebuddy".
- `invoke-codebuddy` adds a small comment explaining the TUI mode guard
  near the orca/codebuddy dependency checks, for future readers.

## [0.1.1] - 2026-08-17

### Added

- `bin/invoke-codebuddy-bridge.sh` — one-line wrapper that runs
  `invoke-codebuddy --background "<prompt>"` followed by
  `invoke-codebuddy --await <HANDLE>`. Designed for the mavis worker's bash
  tool: the worker prompt becomes a single command and the worker's LLM
  usage is near zero. Resolves the sibling `invoke-codebuddy` script
  automatically; falls back to `$PATH` if the bridge lives elsewhere.
- `SKILL.md` — new "Subagent integration (mavis task pattern)" section with
  a copy-pasteable main-agent prompt (using the `task` tool with
  `run_in_background=true`) and a follow-up `task_output(task_id)` example.
  Documents when to prefer the subagent pattern over sync oneshot and
  over inline `--background` + `--await`.
- `README.md` — added a `task` tool example under "Try it" for long tasks
  and listed the bridge script under "How it works".

### Notes

- This is a documentation + thin wrapper addition. No changes to
  `invoke-codebuddy` or the ACP worker.
- Deliberately does not add a cron-based "wake me up" pattern yet; the
  mavis subagent pattern alone covers the common case (派任务 → 继续 →
  下一轮 task_output). Cron can be added later if real usage shows it
  is needed.

## [0.1.0] - 2026-08-17

### Added

- First public release of the `codebuddy-integration` plugin for MiniMax Code.
- Single Skill `codebuddy-integration` that teaches the agent when to delegate a
  text-reasoning task to a separate codebuddy subagent.
- `bin/invoke-codebuddy` — bash CLI that wraps `codebuddy` in three modes:
  - `acp` (default) — JSON-RPC 2.0 over `codebuddy --acp`, full event/status/result
    streaming, suitable for background + `inotifywait` waits that burn 0 mcode LLM
    tokens.
  - `tui` — runs `codebuddy` inside an orca-ide terminal so it can read and edit
    files in the current worktree.
  - `print` — single-shot `codebuddy --print --output-format json` call, 5-8 s
    round-trip with no agent overhead.
- `bin/invoke-codebuddy-acp-worker.py` — JSON-RPC 2.0 client worker that writes
  `state/events-<handle>.jsonl`, `state/status-<handle>.json`, and
  `state/result-<handle>.md`.
- Command surface: `--json`, `--keep`, `--follow`, `--background`, `--await`,
  `--result-file`, `--metrics`, `--events`, `--status`, `--kill`, `--new-session`,
  `--mode {acp,tui,print}`, `--timeout`, `--no-log`, `--log [N]`, `--help`.
- `tests/smoke.sh` — local-only end-to-end smoke test (help text, error prefixes,
  argument parsing, file resolution under a symlink).
- `docs/architecture.md` — design notes and the mode/handle/event-file lifecycle.

### Notes for reviewers

- The Skill content is loaded on demand by MiniMax Code; it never executes by itself
  and never opens a network socket. All network calls come from the user's already
  installed `codebuddy` and (optionally) `orca-ide` CLIs.
- The plugin ships **no** codebuddy credentials, **no** codebuddy installer, and
  **no** native binaries. The two bundled scripts are a bash wrapper and a Python
  JSON-RPC 2.0 client.
- The bundled script must be symlinked into `$PATH` by the user (`ln -sf
  "<plugin-root>/bin/invoke-codebuddy" "$HOME/bin/invoke-codebuddy"`); the script
  uses `readlink -f` to resolve its real location, so the symlink does not break
  its `state/handle` and `logs/invocations.log` lookups.
