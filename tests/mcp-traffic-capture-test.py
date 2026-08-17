#!/usr/bin/env python3
"""mcp-traffic-capture-test.py - capture full JSON-RPC traffic to see what
events codebuddy actually emits. Use CODEBUDDY_MCP_DEBUG_LOG to dump every
line the wrapper reads from codebuddy --acp stdout, then classify the
notification kinds + sample content.

Run: python3 tests/mcp-traffic-capture-test.py
"""
import asyncio, json, os, sys, tempfile
from pathlib import Path
from collections import Counter
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = PLUGIN_ROOT / "bin" / "codebuddy-mcp-server.py"

# Long prompt that forces the model to think + produce a multi-chunk reply
# (so we see all event kinds).
PROMPT = (
    "用 Python 写一个 LRU cache, 50 行内, 含 type hints 和 1 个 pytest 用例。"
    "先想清楚再写。"
)


async def main() -> int:
    if not WRAPPER.exists():
        print(f"FAIL: wrapper not found: {WRAPPER}"); return 1
    debug_log = Path(tempfile.gettempdir()) / "codebuddy-mcp-debug.log"
    if debug_log.exists():
        debug_log.unlink()
    server_params = StdioServerParameters(
        command=sys.executable, args=[str(WRAPPER)],
        env={**os.environ, "CODEBUDDY_MCP_CWD": str(PLUGIN_ROOT),
             "MCODE_BASE_PROMPT_FILE": str(PLUGIN_ROOT / "assets" / "mcode-base-system-prompt.md"),
             "CODEBUDDY_MCP_DEBUG_LOG": str(debug_log)},
    )
    print(f"→ spawning wrapper, debug log: {debug_log}")
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            # Call 1: warm up cache, no special model
            print("→ call 1: warm up")
            r = await session.call_tool("prompt", {"text": "用 3 个字说 hi"})
            print(f"  reply: {r.content[0].text.splitlines()[0]!r}")
            # Call 2: send the long prompt
            print("→ call 2: long prompt (LRU cache)")
            r = await session.call_tool("prompt", {"text": PROMPT, "timeout": 180})
            body = r.content[0].text.split("\n\n[codebuddy:")[0]
            print(f"  reply length: {len(body)} chars")
            # Now read the debug log and classify events
            if not debug_log.exists():
                print(f"✗ debug log not created at {debug_log}")
                return 1
            lines = debug_log.read_text(encoding="utf-8", errors="replace").splitlines()
            print(f"  captured {len(lines)} JSON lines from codebuddy --acp stdout")
    # Classify
    print("\n=== Event classification ===")
    kinds = Counter()
    response_sizes = []
    for ln in lines:
        try:
            msg = json.loads(ln)
        except Exception:
            continue
        if "id" in msg and msg.get("id") is not None:
            # JSON-RPC response (not a notification)
            result = msg.get("result", {}) or {}
            if isinstance(result, dict):
                keys = sorted(result.keys())
                kinds[f"RESP keys={keys}"] += 1
            continue
        upd = msg.get("params", {}).get("update", {}) or {}
        kind = upd.get("sessionUpdate", "??")
        kinds[f"NOTIF {kind}"] += 1
    for k, v in sorted(kinds.items(), key=lambda x: -x[1]):
        print(f"  {v:>4d} × {k}")
    # Dump first 3 example notifications of each kind
    print("\n=== Sample notifications per kind (first 200 chars each) ===")
    samples = {}
    for ln in lines:
        try: msg = json.loads(ln)
        except Exception: continue
        if "id" in msg and msg.get("id") is not None:
            continue
        upd = msg.get("params", {}).get("update", {}) or {}
        kind = upd.get("sessionUpdate", "??")
        if kind not in samples:
            samples[kind] = json.dumps(msg, ensure_ascii=False)[:300]
    for kind, sample in sorted(samples.items()):
        print(f"\n[{kind}]\n{sample}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
