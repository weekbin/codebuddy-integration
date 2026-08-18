#!/usr/bin/env python3
"""mcp-poc-test.py - PoC smoke test for codebuddy-mcp-server"""
import asyncio, os, sys
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = PLUGIN_ROOT / "bin" / "codebuddy-mcp-server.py"
CWD = str(PLUGIN_ROOT)


async def main():
    if not WRAPPER.exists():
        print(f"FAIL: wrapper not found: {WRAPPER}")
        return 1
    server_params = StdioServerParameters(
        command=sys.executable, args=[str(WRAPPER)],
        env={**os.environ, "CODEBUDDY_MCP_CWD": CWD,
             "MCODE_BASE_PROMPT_FILE": str(PLUGIN_ROOT / "assets" / "mcode-base-system-prompt.md")},
    )
    print("→ spawning codebuddy-mcp-server via stdio...")
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            si = init.server_info
            print(f"✓ initialized, server={si.name if si else '?'} v{si.version if si else '?'}")
            tools_resp = await session.list_tools()
            names = [t.name for t in tools_resp.tools]
            print(f"✓ tools/list: {names}")
            expected = {"submit_prompt", "submit_continue", "get_result",
                        "run", "status", "list_tasks", "list_models"}
            assert set(names) == expected, f"expected {expected}, got {set(names)}"
            results = []
            for i in range(5):
                prompt = f"用 5 个字说 hi，第 {i+1} 次调用 (PoC cache test)"
                print(f"→ call {i+1}: {prompt!r}")
                # 0.4.0: `run` is the convenience wrapper around submit_prompt
                # + blocking get_result. Each MCP request is millisecond-scale
                # on the submit side, then the wait_timeout_s window for the
                # codebuddy call to finish.
                resp = await session.call_tool("run", {"text": prompt,
                                                         "wait_timeout_s": 180})
                text = resp.content[0].text if resp.content else ""
                meta = {}
                for line in text.splitlines()[-3:]:
                    if line.startswith("[codebuddy:"): meta["header"] = line
                    elif line.startswith("[tokens:"): meta["tokens"] = line
                reply = text.splitlines()[0] if text.splitlines() else "(empty)"
                print(f"  reply: {reply}")
                print(f"  meta:  {meta.get('header', '')}")
                if "tokens" in meta: print(f"          {meta['tokens']}")
                results.append(meta)
            pids, durations, cache_ratios = [], [], []
            for m in results:
                hdr = m.get("header", ""); tok = m.get("tokens", "")
                if "pid=" in hdr: pids.append(int(hdr.split("pid=")[1].split(",")[0]))
                if "dur=" in hdr: durations.append(float(hdr.split("dur=")[1].split("s")[0]))
                if "cache_ratio=" in tok: cache_ratios.append(float(tok.split("cache_ratio=")[1].rstrip("%]")))
            print(f"\n→ codebuddy pids across 5 calls: {pids}")
            print(f"→ durations: {durations}")
            print(f"→ cache ratios: {cache_ratios}")
            if len(set(pids)) == 1 and pids[0] not in (0, None):
                print(f"✓ SINGLE codebuddy PID across 5 calls: {pids[0]} (cache reuse confirmed)")
            else:
                print(f"✗ PIDs varied: {set(pids)} (cache reuse NOT happening)"); return 1
            if (cache_ratios and cache_ratios[0] < 50 and all(r > 90 for r in cache_ratios[1:])):
                print(f"✓ cold→warm cache: 1st={cache_ratios[0]}%, calls 2-5={cache_ratios[1:]}% (long-lived subprocess, server cache hits — vs 0.2.1 which pays 24k EVERY call)")
            else:
                print(f"⚠ cache ratios unexpected: {cache_ratios}")
    print("\n✓ PoC PASSED: single codebuddy subprocess served 5 sequential MCP calls")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
