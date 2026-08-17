# codebuddy-integration

> Plugin that lets mcode delegate text-reasoning tasks (translate,
> summarize, design review, brainstorm, second opinion) to codebuddy
> via the Agent Plugins 1.0.0 MCP integration. One long-lived
> `codebuddy --acp` subprocess per mcode session; first call warms
> server-side cache; calls 2+ hit 98-99% cache.

## Try it

In mcode:

> 用 codebuddy 帮我 review 这段 Python 的线程安全 LRU cache 设计, 列出 3 个潜在的 race condition.

> summarize this 50k-token spec with codebuddy in 5 bullets, in Chinese.

> ask codebuddy to translate this README to English, keep code samples intact: $(cat README.md)

For long tasks (>30s expected), mcode should follow the decision flow
in `skills/codebuddy-integration/SKILL.md` (short tasks call the MCP
tool directly, long tasks spawn a `task(run_in_background=true)`
worker that calls the same MCP tool).

## How it works

The plugin declares a single stdio MCP server in `mcp.json` at the
plugin root (Agent Plugins 1.0.0 spec §7.2.1 fixed location). mcode
discovers the server on session start and spawns
`bin/codebuddy-mcp-server.py` over stdio.

The wrapper keeps **one** `codebuddy --acp` subprocess alive for the
entire mcode session (model-side cache for system prompt + tool
catalog stays hot). It exposes four tools:

| Tool | Purpose |
|------|---------|
| `prompt(text, model?, append_system_prompt?, timeout?)` | Send a one-shot prompt. |
| `continue(text, ...)` | Follow-up in the same codebuddy session (reuses sessionId, no respawn). |
| `status()` | Wrapper state: liveness, codebuddy PID, ACP session id, model, uptime, call_count, last cache_ratio, cumulative token totals. No side effects. |
| `list_tasks(limit?)` | Most-recent-first list of recent call metadata. |

The wrapper writes a per-day audit log to
`<plugin>/state/mcp-YYYY-MM-DD.log` (one line per call). Set
`CODEBUDDY_MCP_DEBUG_LOG=/path` to also dump every raw ACP frame
the wrapper receives — opt-in, off by default.

## Setup

1. Install the **codebuddy** CLI: `npm i -g @tencent-ai/codebuddy-code`
2. Install this plugin through your mcode plugin manager. mcode reads
   `mcp.json` at session start; no global install hook is needed.
3. (Optional) install the `mcp` Python package on the python that
   mcode uses to spawn wrappers: `uv pip install --system --break-system-packages mcp`.

There is no `install.sh` to run, no symlink to create, no PATH entry
to add. The plugin conforms to spec 1.0.0 and mcode loads it from
its declared location.

## Requirements

- mcode with Agent Plugins 1.0.0 support.
- `codebuddy` CLI on `$PATH` (or `CODEBUDDY_BIN` env var).
- Python 3.10+ with the `mcp` package.
- `python3` on PATH (used by the wrapper and by the JSON parser).

## Data and network

- The plugin makes no network requests of its own. It only spawns
  the user's `codebuddy` subprocess over stdio.
- `codebuddy` is started with `--dangerously-skip-permissions` and
  `--subagent-permission-mode bypassPermissions` so it does not
  prompt on every bash/edit. The only caller is mcode acting on the
  user's behalf.
- Per-call audit log: `<plugin>/state/mcp-YYYY-MM-DD.log`. Rotate or
  delete as you wish.
- No credentials, no private endpoints, no telemetry, no hidden
  install steps.

## Testing

Three test files, no `codebuddy` account or network required for unit
tests:

```bash
# 16 unit tests, ~4ms, no subprocess
python3 -m unittest tests.test_mcp_wrapper_unit

# 5-call PoC: asserts single codebuddy PID + cold→warm cache
python3 tests/mcp-poc-test.py

# 4-tool end-to-end: status, list_tasks, prompt, continue, model,
# append_system_prompt respawn
python3 tests/mcp-features-test.py
```

## Limitations

- Each mcode session gets its own wrapper + its own `codebuddy
  --acp` subprocess. Cross-session calls do NOT share cache. The
  first call of a brand-new session is server-side cold (~1.4%
  cache); calls 2+ warm to 98-99%.
- Per-call cost after the first is dominated by the conversation
  tokens (typically a few hundred), not the 24k base system prompt
  — that is the whole point of the long-lived subprocess.
- `append_system_prompt` change triggers a subprocess respawn
  (~1s, log shows as `subprocess_respawn`); the next call returns
  to a cold cache for one call.

## Conformance

- `plugin.json`: `https://agent-plugins.org/schemas/1.0.0/plugin.schema.json`
- `mcp.json`: `https://agent-plugins.org/schemas/1.0.0/mcp.schema.json`
- Skill: conforms to the [Agent Skills specification](https://agentskills.io/specification).

See [Agent Plugins 1.0.0](https://agent-plugins.org/specification) for
the portable format contract this plugin implements.

## License

MIT. See [LICENSE](./LICENSE).
