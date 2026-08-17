#!/usr/bin/env python3
"""mcp-long-prompt-test.py - one-off verification that long replies are
not truncated to the first agent_message_chunk (the 0.3.1 bug).

Generates a code-buddy prompt that is known to produce a multi-chunk
reply, then asserts the captured text has length > 200 chars and the
completion_tokens line reports a token count roughly proportional.

Run with: python3 tests/mcp-long-prompt-test.py
"""
import asyncio, json, os, re, sys
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = PLUGIN_ROOT / "bin" / "codebuddy-mcp-server.py"

PROMPT = (
    "用 Python 写一个完整的、生产可用的 LRU cache。要求:\n"
    "1. 泛型 `LRUCache[K, V]` (Python 3.10+ type hints)\n"
    "2. 线程安全 (threading.Lock)\n"
    "3. 命中统计 (hits / misses / hit_rate)\n"
    "4. TTL 可选 (每个 entry 单独设过期时间)\n"
    "5. O(1) get / put\n"
    "6. 完整 docstring + 3 个 pytest 用例 (hit / evict / expire)\n"
    "7. 给我完整的、可直接 cp 跑的文件内容,不要省略号"
)


async def main() -> int:
    if not WRAPPER.exists():
        print(f"FAIL: wrapper not found: {WRAPPER}"); return 1
    server_params = StdioServerParameters(
        command=sys.executable, args=[str(WRAPPER)],
        env={**os.environ, "CODEBUDDY_MCP_CWD": str(PLUGIN_ROOT),
             "MCODE_BASE_PROMPT_FILE": str(PLUGIN_ROOT / "assets" / "mcode-base-system-prompt.md")},
    )
    print("→ spawning codebuddy-mcp-server via stdio...")
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("→ sending long prompt (LRU cache, ~800-1500 expected tokens)...")
            r = await session.call_tool("prompt", {"text": PROMPT, "timeout": 180})
            text = r.content[0].text
            # Pull the metadata tail
            tok_m = re.search(r"prompt=(\d+), completion=(\d+)", text)
            ct = int(tok_m.group(2)) if tok_m else 0
            # Body is everything before the trailing metadata block
            body = text.split("\n\n[codebuddy:")[0]
            print(f"  completion_tokens: {ct}")
            print(f"  body length (chars): {len(body)}")
            print(f"  body head: {body[:120]!r}")
            print(f"  body tail: {body[-120:]!r}")
            # Buggy 0.3.1 wrapper returned ~3 chars for 2694 tokens.
            # Healthy reply: body length should be > 200 chars AND roughly
            # proportional to completion_tokens (chars ≈ 2-4 × tokens for
            # code+Chinese mix).
            failures = []
            if len(body) < 200:
                failures.append(f"body too short ({len(body)} chars) — likely truncated to first chunk")
            if ct < 100:
                failures.append(f"completion_tokens too low ({ct}) — model may have given up")
            if ct > 0 and len(body) < ct / 5:
                failures.append(f"body/ct ratio {len(body)/ct:.2f} < 0.2 — body is much shorter than tokens imply")
            if failures:
                print(f"\n✗ TRUNCATED:")
                for f in failures: print(f"  - {f}")
                return 1
            print(f"\n✓ LONG REPLY INTACT: {len(body)} chars / {ct} completion tokens (ratio {len(body)/ct:.2f})")
            return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
