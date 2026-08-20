#!/usr/bin/env python3
"""mcp-features-test.py - end-to-end test for the 7 MCP tools (0.4.0)

0.4.0 changes from the prior version:
- Tools renamed: prompt → run, continue → run (same tool, codebuddy keeps
  history by sessionId so a follow-up `run` is a continuation).
- The sync prompt tool is gone; `run` internally does submit_prompt +
  blocking get_result. Each MCP request is millisecond-scale (submit) plus
  the wait_timeout_s window for the result.
- 7 tools total: submit_prompt / submit_continue / get_result / run /
  status / list_tasks / list_models.
- status() now also exposes inflight_task_id if a codebuddy call is in
  flight. list_tasks() now merges inflight + completed ring.
"""
import asyncio, json, os, re, sys
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = PLUGIN_ROOT / "bin" / "codebuddy-mcp-server.py"


def parse_pid_and_dur(text: str) -> tuple:
    m = re.search(r"pid=(\d+)", text); pid = int(m.group(1)) if m else None
    m = re.search(r"model=([^,]+)", text); model = m.group(1).strip() if m else None
    m = re.search(r"dur=([\d.]+)s", text); dur = float(m.group(1)) if m else None
    return pid, model, dur


def parse_task_id_from_submit(payload: str) -> str:
    """submit_prompt returns a JSON object: {task_id, status, submitted_at, model}."""
    return json.loads(payload)["task_id"]


async def call_run(session, text, **kwargs) -> str:
    """Convenience: call `run` with default wait_timeout_s=3600."""
    return (await session.call_tool("run",
                                    {"text": text, **kwargs})).content[0].text


async def main() -> int:
    if not WRAPPER.exists():
        print(f"FAIL: wrapper not found: {WRAPPER}"); return 1
    server_params = StdioServerParameters(
        command=sys.executable, args=[str(WRAPPER)],
        env={**os.environ, "CODEBUDDY_MCP_CWD": str(PLUGIN_ROOT),
             "MCODE_BASE_PROMPT_FILE": str(PLUGIN_ROOT / "assets" / "mcode-base-system-prompt.md")},
    )
    failures: list[str] = []
    pids: list[int] = []
    print("→ spawning codebuddy-mcp-server via stdio...")
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            expected = sorted(["submit_prompt", "submit_continue", "get_result",
                               "run", "cancel_task", "kill_codebuddy",
                               "status", "list_tasks", "list_models"])
            assert names == expected, f"unexpected tools: {names} (expected {expected})"
            print(f"✓ tools/list: {names}")
            r = await session.call_tool("status", {})
            st = json.loads(r.content[0].text.split("\n", 1)[1])
            print(f"  status(before): {st}")
            assert st["alive"] is True; assert st["call_count"] == 0
            assert st["acp_session_id"]; assert st["codebuddy_pid"] is not None
            print(f"✓ status: pid={st['codebuddy_pid']}, acp_session={st['acp_session_id'][:8]}...")
            r = await session.call_tool("list_models", {})
            lm = json.loads(r.content[0].text.split("\n", 1)[1])
            assert lm["ok"] is True, f"list_models failed: {lm}"
            assert lm["count"] >= 3, f"expected >=3 models, got {lm['count']}: {lm['models']}"
            assert "hy3" in lm["models"], f"hy3 missing from {lm['models']}"
            assert "deepseek-v4-flash" in lm["models"], f"deepseek-v4-flash missing from {lm['models']}"
            print(f"✓ list_models: {lm['count']} models, includes hy3 + deepseek-v4-flash")
            # 0.4.0: each call is now `run` (internally submit + blocking
            # get_result). For 5-char Chinese prompts in real codebuddy,
            # 3600s is generous; the actual runtime is a few seconds.
            for i in range(3):
                txt = await call_run(session, text=f"用 5 个字说 hi, 第 {i+1} 次")
                pid, model, dur = parse_pid_and_dur(txt); pids.append(pid)
                print(f"  call {i+1}: pid={pid}, model={model}, dur={dur}s")
                # Environmental guard: if the default model is rate-limited
                # (429 from codebuddy → wrapper returns "(no message received
                # from codebuddy)" + stop=refusal + 0 tokens), skip the rest
                # so we don't false-fail the entire suite on a free-tier
                # quota. Verified with `codebuddy models` outside the wrapper.
                if i == 0 and "(no message received from codebuddy)" in txt:
                    print("⚠ SKIP: codebuddy default model returned empty (likely rate-limit 429).")
                    print("  Verify with `codebuddy models` outside the MCP wrapper to see 429 reset time.")
                    print("  Re-run after the rate limit clears (default model is the free-tier hy3).")
                    return 0
            assert len(set(pids)) == 1, f"pids should all be the same, got {pids}"
            print(f"✓ 3 calls share single codebuddy PID {pids[0]}")
            # `run` continues the same session by reusing the existing
            # codebuddy sessionId (server-side history is preserved).
            txt = await call_run(session, text="再用一个字说 bye")
            pid, _, _ = parse_pid_and_dur(txt)
            assert pid == pids[0], f"continue should not respawn; pid {pid} != {pids[0]}"
            print(f"✓ run (continuation) reuses same PID {pid} (no respawn)")
            r = await session.call_tool("status", {})
            st = json.loads(r.content[0].text.split("\n", 1)[1])
            assert st["call_count"] == 4; assert st["last_cache_ratio"] is not None
            assert st["totals"]["prompt_tokens"] > 0
            print(f"✓ status(after 4 calls): count={st['call_count']}, last_cache={st['last_cache_ratio']}%, totals.pt={st['totals']['prompt_tokens']}")
            r = await session.call_tool("list_tasks", {"limit": 10})
            items = json.loads(r.content[0].text.split("\n", 1)[1])
            assert len(items) == 4
            for i, item in enumerate(items):
                assert item["idx"] == 4 - i, f"item {i} idx should be {4 - i}, got {item['idx']}"
            print(f"✓ list_tasks: {len(items)} records, most recent first, latest idx={items[0]['idx']} model={items[0]['model']}")
            txt = await call_run(session, text="用 3 个字说 ok", model="hy3")
            pid_after_model, _, _ = parse_pid_and_dur(txt)
            assert pid_after_model == pids[0], f"same model should not respawn; got pid {pid_after_model}"
            print(f"✓ run with same model: pid unchanged ({pid_after_model})")
            # Switch to deepseek-v4-flash — uses `session/set_config_option`
            # so the subprocess PID must NOT change (no respawn), but the
            # subsequent run should report the new model id.
            txt = await call_run(session, text="用 5 个字说 switch", model="deepseek-v4-flash")
            pid_after_switch, model_after_switch, _ = parse_pid_and_dur(txt)
            assert pid_after_switch == pids[0], f"model change should NOT respawn (set_config_option path); pid {pid_after_switch} != {pids[0]}"
            assert model_after_switch == "deepseek-v4-flash", f"expected deepseek-v4-flash, got {model_after_switch}"
            print(f"✓ model switch via set_config_option (no respawn): pid stays {pid_after_switch}, model={model_after_switch}")
            txt = await call_run(session, text="现在回答 short",
                                 append_system_prompt="Always answer in exactly 3 words.")
            pid_after_append, model_after_append, _ = parse_pid_and_dur(txt)
            assert pid_after_append != pid_after_switch, f"append change should respawn; pid {pid_after_append} == {pid_after_switch} (no respawn)"
            assert model_after_append == "deepseek-v4-flash", f"append respawn should preserve model, got {model_after_append}"
            pids.append(pid_after_append)
            print(f"✓ append_system_prompt respawned AND preserved model deepseek-v4-flash: pid {pid_after_switch} → {pid_after_append}")
            txt = await call_run(session, text="用 5 个字说 ok", model="hy3")
            pid_back, model_back, _ = parse_pid_and_dur(txt)
            assert pid_back == pid_after_append, f"model change back to hy3 should NOT respawn; pid {pid_back} != {pid_after_append}"
            assert model_back == "hy3", f"expected hy3, got {model_back}"
            print(f"✓ model switch back to hy3 (no respawn): model={model_back}")
            # include_thinking should expose the reasoning trace when set,
            # and suppress it by default.
            txt = await call_run(session, text="用 3 个字说 t",
                                 include_thinking=True, wait_timeout_s=60)
            # When thinking is captured, the formatter emits a "--- thinking" header.
            assert "--- thinking" in txt, f"include_thinking=true should expose thinking; got: {txt[:200]!r}"
            print(f"✓ include_thinking=true exposes '--- thinking (...) ---' section")
            txt = await call_run(session, text="用 3 个字说 no", wait_timeout_s=60)
            assert "--- thinking" not in txt, f"include_thinking default false should NOT expose thinking; got: {txt[:200]!r}"
            print(f"✓ include_thinking default false omits thinking section")
            r = await session.call_tool("status", {})
            st = json.loads(r.content[0].text.split("\n", 1)[1])
            assert st["model"] in ("hy3", "deepseek-v4-flash"), f"unexpected model: {st['model']}"
            print(f"✓ status: call_count={st['call_count']}, pid={st['codebuddy_pid']}, model={st['model']}")
            r = await session.call_tool("list_tasks", {"limit": 3})
            items = json.loads(r.content[0].text.split("\n", 1)[1])
            assert len(items) == 3
            print(f"✓ list_tasks(limit=3): returned {len(items)} items")
            # ── 0.4.0 submit/get_result round-trip ──
            # submit_prompt returns immediately; get_result with
            # mode='poll' returns 'running' on a long task. For a quick
            # codebuddy call it usually finishes before we poll, but
            # mode='poll' is always safe to call.
            sub_r = await session.call_tool("submit_prompt",
                                            {"text": "用 5 个字说 submit", "model": "deepseek-v4-flash"})
            sub = json.loads(sub_r.content[0].text)
            assert sub["status"] == "running", f"submit_prompt should return running, got {sub}"
            assert sub["task_id"].startswith("tsk_"), f"bad task_id: {sub['task_id']}"
            print(f"✓ submit_prompt: {sub['task_id']} (status=running, returned immediately)")
            # Poll for the result; if it's already done we get done, else
            # we block up to wait_timeout_s. Default 3600s is fine here.
            gr = json.loads((await session.call_tool(
                "get_result", {"task_id": sub["task_id"], "wait_timeout_s": 600})).content[0].text)
            assert gr["status"] == "done", f"get_result should return done for a finished submit, got {gr}"
            assert "result" in gr and gr["result"].get("text")
            print(f"✓ get_result: done, text='{gr['result']['text'][:30]}...', cache={gr['result']['cache_ratio']}%")
            # Unknown task_id → unknown
            gr_unknown = json.loads((await session.call_tool(
                "get_result", {"task_id": "tsk_doesnotexist00", "wait_timeout_s": 0, "mode": "poll"})).content[0].text)
            assert gr_unknown["status"] == "unknown", f"unknown task_id should return unknown, got {gr_unknown}"
            print(f"✓ get_result(unknown task_id): status=unknown")
    if failures:
        print(f"\n✗ {len(failures)} FAILED:")
        for f in failures: print(f"  - {f}")
        return 1
    print("\n✓ ALL FEATURES PASSED: 7 tools / run / submit+get_result / dynamic-model / append / thinking / cache / status / list_tasks")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
