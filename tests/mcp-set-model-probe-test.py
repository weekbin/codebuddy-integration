#!/usr/bin/env python3
"""mcp-set-model-probe-test.py - probe ACP for runtime model-switch RPCs.

We bypass the MCP wrapper and talk JSON-RPC directly to `codebuddy --acp`
to discover which methods the server actually accepts.

Run: python3 tests/mcp-set-model-probe-test.py
"""
import json, os, subprocess, sys, time
from pathlib import Path

CB_BIN = os.environ.get("CODEBUDDY_BIN") or "codebuddy"
CWD = "/tmp"

CANDIDATE_METHODS = [
    # (method_name, params_template, description)
    ("session/set_config_option",
     {"sessionId": None, "configId": "model", "value": "deepseek-v4-flash"},
     "ACP-style: set_config_option"),
    ("session/set_model",
     {"sessionId": None, "modelId": "deepseek-v4-flash"},
     "Alt: set_model"),
    ("session/configure",
     {"sessionId": None, "config": {"model": "deepseek-v4-flash"}},
     "Alt: configure dict"),
    ("session/set",
     {"sessionId": None, "model": "deepseek-v4-flash"},
     "Alt: set with model field"),
    ("session/select_model",
     {"sessionId": None, "modelId": "deepseek-v4-flash"},
     "Alt: select_model"),
]


def rpc(proc, method, params, timeout=10):
    my_id = int(time.time() * 1e6) % 1_000_000
    req = {"jsonrpc": "2.0", "id": my_id, "method": method, "params": params}
    proc.stdin.write((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
    proc.stdin.flush()
    end = time.time() + timeout
    while time.time() < end:
        line = proc.stdout.readline().decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if msg.get("id") == my_id:
            return msg
    return {"error": "timeout"}


def main() -> int:
    print(f"→ spawning: {CB_BIN} --acp --dangerously-skip-permissions "
          f"--permission-mode bypassPermissions --no-session-persistence")
    proc = subprocess.Popen(
        [CB_BIN, "--acp", "--dangerously-skip-permissions",
         "--permission-mode", "bypassPermissions",
         "--subagent-permission-mode", "bypassPermissions",
         "--no-session-persistence"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, bufsize=0, cwd=CWD,
    )
    try:
        r = rpc(proc, "initialize", {
            "protocolVersion": 1,
            "clientInfo": {"name": "probe", "version": "0.0.1"},
            "capabilities": {},
        })
        print(f"  initialize OK, protocolVersion={r.get('result', {}).get('protocolVersion')}")
        r = rpc(proc, "session/new", {"cwd": CWD, "mcpServers": []})
        sess = r.get("result", {}).get("sessionId")
        models = r.get("result", {}).get("models", {})
        print(f"  session/new OK, sessionId={sess[:8] if sess else None}...")
        if isinstance(models, dict):
            avail = models.get("availableModels", [])
            print(f"  models.availableModels: {len(avail)} entries")
        # Try each candidate method
        print(f"\n→ probing {len(CANDIDATE_METHODS)} candidate RPCs for runtime model switch:")
        for method, params, desc in CANDIDATE_METHODS:
            params["sessionId"] = sess
            r = rpc(proc, method, params, timeout=5)
            ok = "error" not in r
            err = r.get("error", {})
            err_str = json.dumps(err, ensure_ascii=False)[:100] if isinstance(err, dict) else str(err)[:100]
            mark = "✓" if ok else "✗"
            print(f"  {mark} {method:40s} → {r.get('result', err_str)}")
        # Verify model is still hy3 by checking status
        r = rpc(proc, "session/status", {"sessionId": sess}, timeout=5)
        print(f"\n  session/status (post-probe): {json.dumps(r, ensure_ascii=False)[:300]}")
    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except Exception: proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
