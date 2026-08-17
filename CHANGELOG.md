# Changelog

All notable changes to this plugin are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-08-18

Spec-aligned refactor. 0.2.2 was a PoC bump that was never released
(the release got retracted after mcode rejected the non-spec install
path); this 0.3.0 entry covers both the 0.2.2 PoC work and the spec
refactor in a single release.

### Added (Agent Plugins 1.0.0 spec compliance)

- **`mcp.json`** at the plugin root (spec §7.2.1 fixed location).
  Declares a single stdio MCP server named `codebuddy` that points at
  `bin/codebuddy-mcp-server.py`. mcode (or any spec-compliant client)
  auto-loads it at session start — no global install hook needed.
- **`bin/codebuddy-mcp-server.py`** (new): a long-lived stdio MCP
  wrapper that keeps one `codebuddy --acp` subprocess alive for the
  mcode session lifetime. Exposes 4 tools: `prompt`, `continue`,
  `status`, `list_tasks`. The first call warms server-side cache; calls
  2+ within the same mcode session hit 98-99% cache, dropping
  per-call cost from ~24k base tokens (0.2.1) to a few hundred
  conversation tokens.
- **`tests/mcp-poc-test.py`**: 5-call PoC that confirms a single
  codebuddy PID serves all calls and that cache ratios transition
  cold → warm.
- **`tests/mcp-features-test.py`**: end-to-end coverage of all 4 tools
  (status, list_tasks, prompt, continue, model, append_system_prompt,
  respawn triggers, cache accounting).
- **`tests/test_mcp_wrapper_unit.py`**: unit tests that don't need a
  running `codebuddy` binary (plugin-root path resolution, log line
  format, `_format_result` shape, `status` / `list_tasks` behavior,
  respawn decision, tool-handler dispatch).
- **`MCODE_BASE_PROMPT_FILE`** env hook: mcp.json's `env` block
  resolves `${PLUGIN_ROOT}/assets/mcode-base-system-prompt.md` so the
  wrapper loads the mcode base system prompt on startup.

### Changed

- `plugin.json` `version`: 0.2.1 → 0.3.0. `description` rewritten to
  mention the MCP path. `keywords` extended with `mcp` and
  `agent-plugins-spec-1.0.0`.
- `skills/codebuddy-integration/SKILL.md`: frontmatter `description`
  updated to mention the `codebuddy` MCP tool; a new "Why MCP over CLI"
  / "Quick reference (MCP — preferred)" section is added at the top;
  the 0.2.1 mode / `--keep` / `--status` / `--metrics` legacy CLI
  content was removed (the binaries it documented are removed in
  the same release; see Removed below). File shrunk from 573 to
  84 lines. `metadata.version` is bumped to 0.3.0.
- `README.md`: rewritten end-to-end for the 0.3.0 MCP-only entry
  point. 0.2.1 CLI sections are gone. File shrunk from 257 to
  120 lines.

### Removed (0.2.1 legacy CLI, no longer needed under spec 1.0.0)

- `bin/invoke-codebuddy` — 0.2.1 sync CLI. The spec 1.0.0 mcp.json
  entry replaces every call path this script supported.
- `bin/invoke-codebuddy-acp-worker.py` — 0.2.1 Python ACP client.
  Functionality lives in `bin/codebuddy-mcp-server.py` now.
- `bin/invoke-codebuddy-bridge.sh` — 0.2.1 sync bridge for
  `task(run_in_background=true)`. With the wrapper exposing
  long-lived MCP, the worker just calls the MCP tool directly —
  no bridge needed.
- `bin/install.sh` — 0.2.1 PATH symlink / `install-path` writer.
  Spec 1.0.0 loads the plugin from its declared location; no
  global install hook required.
- `tests/smoke.sh` / `tests/full-smoke.sh` — 0.2.1 CLI smoke
  tests. Exercised flags (`--mode acp --background`, etc.) that
  no longer exist. Replaced by `tests/mcp-poc-test.py` +
  `tests/mcp-features-test.py` + `tests/test_mcp_wrapper_unit.py`.
- `docs/architecture.md` — described 0.2.1's `systemd-run` /
  `--background` design that was removed in 0.2.1 itself.
  0.3.0 architecture is "one long-lived subprocess per mcode
  session" and is described inline in README + SKILL.md.

### Kept (unchanged from 0.2.1, still in use)

- `assets/mcode-base-system-prompt.md` — content unchanged, only its
  path is referenced by `mcp.json` and loaded by the wrapper on
  startup.
- `.minimax-plugin/plugin.json` — the mcode client-extension file
  (mcode regenerates this on `mcode install`; not authored as
  part of the spec-compliant plugin manifest).

## [0.2.1] - 2026-08-17

Two parallel fixes were landed on 0.2.1 — pre-check/model_warning/cache
doc and the root-cause async-path rewrite — and merged here.

### Changed (root-cause rewrite of the "async" path)

The 0.2.0 plugin shipped a `--background` flag on `bin/invoke-codebuddy` that
tried to daemonize the codebuddy worker via `systemd-run` (Linux) or
`setsid` (macOS). This was unreliable in mcode's non-interactive `bash`
tool — on macOS the worker was killed when the bash session exited, so
`--await` blocked for the full 300s timeout and returned "no result".

The root cause was that **agent scheduling is not a script's job** —
mcode's `task` tool with `run_in_background=true` already does this, with
a `<background-task-finished>` system reminder that wakes the agent on
completion. The script's `--background` path was reinventing that
wheel, badly.

This release replaces the script-side background machinery with the
**proper `task` + `bridge.sh` pattern**. Concretely:

- **Removed** `--background` / `--bg` / `--await` / `--result-file`
  flags and the corresponding `systemd-run` / `setsid` /
  `disown` / `nohup` fallback in `bin/invoke-codebuddy`. The
  `--background` flag now errors with `unknown flag`, locking in
  the dead-code removal.
- **`bin/invoke-codebuddy-bridge.sh`** is now a thin sync wrapper
  (`exec invoke-codebuddy --json "$@"`), intended to be called by a
  worker LLM that was spawned via mcode's `task(run_in_background=true)`.
  The bridge is sync — it does one `--json` call and prints the reply
  on stdout for the worker to copy into its final answer.
- **SKILL.md "Choose the right execution path"** (new section, replaces
  the obsolete "Subagent integration — REAL behavior, not wishful"
  section): a decision flow that picks `--mode tui` only when
  `command -v orca-ide` succeeds AND the agent is in an orca worktree
  AND codebuddy needs to read/write files there. Otherwise, default to
  `task(run_in_background=true) + invoke-codebuddy-bridge.sh`.
- **SKILL.md "Permission pre-emption"** (new section): documents that
  `codebuddy --acp` with `--dangerously-skip-permissions` +
  `--permission-mode bypassPermissions` +
  `--subagent-permission-mode bypassPermissions` does NOT pause for
  permission prompts in practice. Live-verified 2026-08-17: a 7-second
  end-to-end call that writes a real file in `/tmp` completed without
  any interactive prompt. The "mcp-bridge + session-tick" fallback for
  genuine question/answer is documented as a future option but
  **explicitly NOT implemented** until a real case is observed.

### Fixed (cross-platform)

- `bin/invoke-codebuddy` and `bin/install.sh` used `readlink -f "$0"`
  to resolve the script's real path. **macOS BSD `readlink` does not
  support `-f`**, so the script crashed on macOS for any user whose
  `~/bin/invoke-codebuddy` is a symlink. Replaced with a
  `python3 -c "import os, sys; print(os.path.realpath(sys.argv[1]))"`
  helper (cross-platform, no `coreutils` dep).
- `bin/invoke-codebuddy-acp-worker.py` used PEP 604 union syntax
  (`dict | None`, `str | None`) in type hints. **Python 3.9 does not
  support it** (still ships as `/usr/bin/python3` on macOS). Replaced
  with `Optional[...]` for portability.
- `bin/invoke-codebuddy-acp-worker.py` hard-coded `"codebuddy"` as the
  first arg of `subprocess.Popen`, so a user with a non-PATH install
  (e.g. nvm v24 on macOS where the default shell is v22) got
  `FileNotFoundError` even after `export CODEBUDDY_BIN=...`. Now
  reads `os.environ.get("CODEBUDDY_BIN")` like the bash script does.

### Fixed (from full-smoke.sh gap report on 0.2.0)

- **Pre-check `--system-prompt-file` existence** — was: codebuddy silently
  fell back to its default prompt when the file path didn't exist, so callers
  thought their system prompt took effect when it didn't. Now: plugin rejects
  missing file with `rc=2` and a clear error message before the call.
- **`--model` unknown-id detection** — was: codebuddy silently fell back to
  its default model when the caller passed an unknown model id; `status.model`
  still showed the caller-requested value, so callers were deceived. Now:
  worker checks `available_models` after `session/new` and records
  `status.model_warning` + writes a `warning: ...` line to stderr + emits a
  `model_warning` event. Note this is a post-hoc detection (we cannot
  pre-check without a listModels RPC), so the bad call still happens once —
  but the warning is loud.
- **Stderr hygiene in `acp-worker.py`** — was: worker printed internal debug
  like `[reader] stdout closed` to stderr, mixed with the real warnings.
  Now: `_log(msg, level)` only writes `warn` / `error` to stderr; debug goes
  to the events file only. All `failed:` / `error:` prefixes standardized.
- **Removed `2>/dev/null` on acp-sync worker spawn** — was suppressing the
  model_warning (and any other future stderr). Now sync-mode stderr reaches
  the user.

### Added (cross-platform install)

- `bin/install.sh`:
  - **Auto-writes `~/.config/invoke-codebuddy/env`** with
    `export CODEBUDDY_BIN=<detected path>`. Cross-platform probe
    (`~/.nvm`, `~/.asdf`, `~/.volta`, `~/.local`, `/opt/homebrew/bin`,
    `/usr/local/bin`, `/usr/bin`). The main script sources this env
    file on startup, so users no longer need to remember
    `export CODEBUDDY_BIN=...` every shell.
  - **Auto-appends a `PATH` block** to `~/.zshrc` and `~/.bashrc` (with
    a marker line so re-runs are idempotent) so new shells find
    `invoke-codebuddy` without manual `export PATH=...`.
  - **Cross-platform readlink** (see Fixed above).

### Documented (gap report correction)

- **Cache hit rate is unstable and server-driven** — previous README claimed
  "subsequent calls almost entirely cache-hit (cache_read ≈ prompt)". This
  was misleading. 20-call sample shows: 75% of calls hit 11%, 5% hit 21%,
  20% hit 99-100%, average ≈ 23%. A 100-call session costs 70-80× per-call,
  not 1×. README and SKILL.md updated to reflect actual behavior. Use
  `--metrics <handle>` to see your own hit rate.

### Tests

- `tests/smoke.sh` rewritten to be a **real** end-to-end test, not a
  mock. The previous version mocked `codebuddy` at the bash level;
  passing it proved nothing about the real codebuddy runtime. The
  new version:
  - Detects a real `codebuddy` CLI on the test machine.
  - Runs **REAL** `--mode print`, **REAL** acp-mode with
    `system_prompt_mode=base-only`, **REAL** permission-pre-emption
    (asks codebuddy to write a file and verifies it was written
    without any interactive prompt), **REAL** `--metrics`, **REAL**
    `bridge.sh` end-to-end. If no real codebuddy is found, those tests
    SKIP (not pass-with-mock) so a CI box without codebuddy can still
    run the script-only logical tests.
  - Keeps the **logical** tests (`--help`, `--bogus-flag`, "no
    codebuddy" friendly diagnostic, plugin-root resolution under
    symlink, `bridge.sh` no-args usage) which do not require a real
    codebuddy.
  - Final result: **40 passed, 0 failed, 0 skipped** with a real
    codebuddy on disk; logical-only subset still runs to completion
    on a host without codebuddy.
- `tests/full-smoke.sh` (in the user's pre-merge 0.2.1) — long-task +
  cache + system-prompt + error + state + bridge + install-sh smoke
  tests, 28 cases. Requires a real `codebuddy` login (burns ~150-200k
  tokens, mostly cache hits).

## [0.2.0] - 2026-08-17

### Added

- **`--model <id>` flag** — caller controls codebuddy model. Default is now
  "let codebuddy server pick" (no longer hardcoded `hy3`). Either pass the
  flag or set `CODEBUDDY_MODEL` env var. `--metrics` on any prior call
  lists `available_models`.
- **`--system-prompt <text>` and `--system-prompt-file <path>`** —
  completely replace the mcode base system prompt with caller-supplied
  content. (acp mode; print mode already had it via codebuddy directly.)
- **`--append-system-prompt <text>`** — append business rules **after**
  the mcode base system prompt. Lets the caller keep mcode role/boundary
  guarantees while adding task-specific instructions.
- **`bin/install.sh`** — first-time setup script. Writes
  `$HOME/.config/invoke-codebuddy/install-path` (anchors plugin root for
  state/ and logs/ lookup), `ln -sfn` the script to `~/bin/`, and smoke-
  tests `--help`. Re-run after editing the plugin in place.
- **`assets/mcode-base-system-prompt.md`** — the **固化** mcode base
  system prompt. Injected into every acp-mode call by default via
  `codebuddy --append-system-prompt`. Aligned with mavis 2.x system
  prompt (2026-08); bump in CHANGELOG when mavis system prompt changes
  materially.
- **`status.system_prompt_mode`** in acp status JSON — reflects which
  of the four modes the call was in: `caller-override-file`,
  `caller-override`, `caller-append`, `base-only`, `none`.

### Changed

- Plugin root resolution now uses a **3-level fallback** (instead of pure
  `readlink -f "$0"`):
  1. `$CODEBUDDY_PLUGIN_DIR` env var (mavis / mcode can inject at install),
  2. `$HOME/.config/invoke-codebuddy/install-path` (written by install.sh),
  3. `readlink -f "$0"` (backward-compat fallback).
  This ensures `state/` and `logs/` always live inside the installed
  plugin, no matter which symlink is on `~/bin/`.
- `bin/invoke-codebuddy-acp-worker.py`: `--model` default is now `None`
  (was `hy3`); `--model` is only added to `session/new` JSON if set.
- `SKILL.md` / `README.md`: documented install.sh, model selection, system
  prompt strategy, and the **cache economics** (first call ~24k tokens,
  subsequent calls almost entirely cache-hit).

## [0.1.8] - 2026-08-17

### Fixed (from cross-machine gap report on 0.1.7 install)

- **`codebuddy` CLI missing is now an actionable error** (was: one-liner
  `'codebuddy' not in PATH` that left users unable to distinguish from
  orca-ide / network / timeout failures). In `bin/invoke-codebuddy`:
  - The upfront `command -v "$CB_BIN"` check now prints a **5-line
    diagnostic** listing the three common fix paths in priority order:
    `CODEBUDDY_BIN=/abs/path`, symlink to `~/bin/codebuddy`, or
    `npm i -g @tencent-ai/codebuddy-code`. It also points at the
    `find ~/.nvm ~/.local -name codebuddy -type l` lookup so users on
    a stock nvm install can discover the real path in one command.
  - The `~/.codebuddy/bin/` directory naming collision with
    CodeBuddy CN.app (GUI) is now called out in the error, the
    README Requirements, and the SKILL.md Installation section.

### Documented

- `SKILL.md` Quick reference **leads with `--mode print` as the
  recommended first try** for fresh installs (4s, no orca-ide, no
  worktree context). The default acp-mode line is now explicitly
  second and labeled as needing `codebuddy` on PATH.
- New `SKILL.md` "Mode cheat sheet" table shows the four
  (mode × background) combinations and which CLIs each requires.
- `SKILL.md` Installation section is now a 3-step checklist
  (script on PATH → codebuddy on PATH → orca-ide if you need TUI)
  with the `find ~/.nvm ~/.local -name codebuddy -type l` lookup
  and the CodeBuddy CN.app warning.
- `SKILL.md` Troubleshooting table adds rows for the new
  codebuddy-missing error and the macOS systemd gap.
- `SKILL.md` top-of-file `compatibility` line now mentions
  `CODEBUDDY_BIN` as an alternative to PATH exposure.
- `README.md` Requirements section adds the `CODEBUDDY_BIN` alt
  and the `~/.codebuddy/bin/` warning.

### Changed

- Bumped `plugin.json`, `SKILL.md` metadata to `0.1.8`.

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
