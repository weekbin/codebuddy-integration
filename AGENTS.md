# AGENTS.md — project context for AI agents

This file is for AI agents (mcode, codebuddy, you-name-it) working
in this repo. **Not** for end users — see `README.md` for that.

## What this repo is

`codebuddy-integration` is a **plugin** for mcode (and any other
[Agent Plugins 1.0.0](https://agent-plugins.org/specification)
client) that exposes `codebuddy` as 5 MCP tools. mcode loads
`mcp.json` on session start; the wrapper keeps one
`codebuddy --acp` subprocess alive for the session.

Version: 0.3.11. Status: stable, in production use.

## File layout (what matters)

| File | Role | Touch when... |
|------|------|---------------|
| `plugin.json` | spec manifest (10 top-level fields, closed schema) | Bumping version, changing name/description/keywords |
| `mcp.json` | spec MCP config (fixed at plugin root per spec §7.2.1) | Changing server name, command, env, or cwd |
| `skills/codebuddy-integration/SKILL.md` | Agent Skills spec file (loaded by mcode as a skill) | Changing when/why mcode should call codebuddy; tool description, trigger phrases, decision tree |
| `bin/codebuddy-mcp-server.py` | The stdio MCP wrapper (~751 lines, one long-lived subprocess) | Adding tools, changing ACP protocol handling, changing log format |
| `assets/mcode-base-system-prompt.md` | Base system prompt injected into every codebuddy call | Changing mcode's identity/role/boundary for codebuddy |
| `tests/mcp-poc-test.py` | 5-call cold→warm cache smoke | After changing cache behavior |
| `tests/mcp-features-test.py` | 5-tool end-to-end (status, list_tasks, list_models, prompt, continue, model, append respawn, thinking) | After adding/changing tools |
| `tests/mcp-long-prompt-test.py` | Long-reply regression for reply concatenation (auto-skips on 429) | After changing reply concatenation |
| `tests/test_mcp_wrapper_unit.py` | 34 unit tests (no subprocess) | After changing wrapper internals |
| `CHANGELOG.md` | Keep-a-Changelog format | Every release |

`state/` and `logs/` are gitignored runtime output. `__pycache__/`
and `.DS_Store` too. `.minimax-plugin/plugin.json` is regenerated
by `mcode install` — don't hand-edit it.

## Workflow

1. **Edit the source files** (wrapper / SKILL.md / mcp.json / plugin.json)
2. **Run the test suite** (all four; all should pass or skip cleanly before commit):
   ```bash
   python3 -m unittest tests.test_mcp_wrapper_unit   # 34 tests, ~220ms
   python3 tests/mcp-poc-test.py                    # needs real codebuddy
   python3 tests/mcp-features-test.py              # needs real codebuddy
   python3 tests/mcp-long-prompt-test.py           # needs real codebuddy; skips on 429
   ```
3. **Update CHANGELOG.md** under a new `## [X.Y.Z] - DATE` heading.
   Use [Keep a Changelog](https://keepachangelog.com/) format.
4. **Commit** with a `X.Y.Z: short imperative summary` subject line
   matching the historical style (see `git log --oneline`).

## Spec compliance (must-read)

This plugin implements [Agent Plugins 1.0.0](https://agent-plugins.org/specification).
Key non-obvious rules:

- `mcp.json` MUST be at the plugin root (spec §7.2.1). Not in
  `~/.minimax/mcp/`, not in plugin subdirs.
- `mcp.json` server `command` MUST be a single token, either a bare
  name or a plugin-relative path starting with `./`. Absolute paths
  and shell strings are rejected by conformant clients.
- `mcp.json` server `env` keys MUST NOT be `PLUGIN_ROOT` or
  `PLUGIN_DATA` — those are reserved for the client to inject.
  Use `${PLUGIN_ROOT}` / `${PLUGIN_DATA}` only as values inside
  `args` / `env` / `cwd`.
- `cwd` MUST be one of: `./xxx`, `${PLUGIN_ROOT}/...`, `${PLUGIN_DATA}/...`.
- `plugin.json` is a closed schema: only `$schema`, `name`,
  `version`, `description`, `author`, `homepage`, `repository`,
  `license`, `keywords`, `extensions` are valid top-level keys.
- Skills: each subdirectory of `skills/` containing a `SKILL.md`
  file is one skill. Don't recurse deeper.

A full local index of spec facts is in agent memory
(`mavis` agent, "Agent Plugins 1.0.0 spec index" entry).

## Style preferences (for AI agents writing in this repo)

- **Commit messages**: `X.Y.Z: short summary` + 1-3 short paragraphs
  of context, matching the style in `git log`. Bullets are fine for
  listing what changed. No emoji, no marketing language.
- **CHANGELOG entries**: Keep-a-Changelog format. One section per
  release. Group changes under `### Added` / `### Changed` /
  `### Removed` / `### Fixed` / `### Kept` as appropriate.
- **SKILL.md frontmatter**: `name` (lowercase, alphanumeric + `-.`,
  no `--` or `..`) and `description` (single sentence, ends with
  trigger phrases) are required by Agent Skills spec. Keep
  `description` under ~1024 chars.
- **Wrapper code**: Python 3.10+ compatible, no type-annotation
  syntax that requires 3.12+. Use `pathlib.Path`, not `os.path`.
  Thread-safe where it matters (the `ACPSession` reads from a
  background reader thread).
- **MCP server log line format**: `<iso-ts> | <event-name> | k=v | k=v ...`
  (pipe-separated, one line per event). Daily rotation by filename.

## Things explicitly NOT to do

- Don't hand-edit `.minimax-plugin/plugin.json` — mcode regenerates
  it on `mcode install`.
- Don't write to `~/.minimax/mcp/mcp.json` — spec 1.0.0 puts MCP
  config in the plugin's own `mcp.json`, not a global location.
- No install hook, no symlink, no PATH mutation, no global config files. mcode auto-loads plugins per Agent Plugins 1.0.0 spec.
- Don't commit `state/`, `logs/`, `__pycache__/`, or `.DS_Store` —
  they're in `.gitignore` for a reason.

## Future work (0.4.0 ideas, not committed)

- **Streamable-HTTP transport in `mcp.json`** for remote codebuddy
  deployments (currently stdio-only). This is the only path to real
  concurrent codebuddy calls from mcode task workers (see memory
  "mcode task workers 共享 MCP server wrapper, codebuddy 不真并发").
- **Add a `health` tool** that reports `codebuddy --acp` reachability
  separately from process liveness.
- **Per-tool usage breakdown** in `state/mcp-YYYY-MM-DD.log` (already
  partially captured in `list_tasks`; just needs an aggregator).
- **Update this AGENTS.md** when the project structure changes.
