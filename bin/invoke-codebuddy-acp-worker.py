#!/usr/bin/env python3
"""
invoke-codebuddy-acp-worker.py - codebuddy ACP client worker

Spawns `codebuddy --acp` as a subprocess, runs an Agent Client Protocol
(JSON-RPC 2.0) conversation, and streams every server-pushed event to disk:

  $EVENTS_FILE   - one JSON event per line (full event history)
  $STATUS_FILE   - latest snapshot (phase, tokens, trace, last_thought, last_message)
  $RESULT_FILE   - final result (assistant text)
  $DONE_FILE     - marker created on completion (success or failure)

Usage (called by invoke-codebuddy --mode acp --background):
  invoke-codebuddy-acp-worker.py \\
    --task "write a hello world" \\
    --model hy3 \\
    --events-file /path/events.jsonl \\
    --status-file /path/status.json \\
    --result-file /path/result.md \\
    --done-file /path/done \\
    --timeout 300

Exit code:
  0  - success
  2  - bad args
  3  - codebuddy process failed to start
  4  - initialize / session/new failed
  5  - prompt timed out
  6  - codebuddy reported error
"""
import argparse
import json
import os
import subprocess
import sys
import time
import threading
from pathlib import Path

# ── I/O helpers ─────────────────────────────────────────
def append_event(events_file: Path, ev: dict) -> None:
    """Append one event to the JSONL log; crash-safe (single-line write + flush)."""
    ev = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **ev}
    line = json.dumps(ev, ensure_ascii=False) + "\n"
    with events_file.open("a", encoding="utf-8") as f:
        f.write(line)
        f.flush()

def write_status(status_file: Path, status: dict) -> None:
    """Write a full snapshot of current state. Atomic via tmp + replace."""
    tmp = status_file.with_suffix(status_file.suffix + ".tmp")
    tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, status_file)

def load_status(status_file: Path) -> dict:
    if status_file.exists():
        try:
            return json.loads(status_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

# ── JSON-RPC 2.0 client ──────────────────────────────────
class ACPClient:
    def __init__(self, proc: subprocess.Popen, log=None):
        self.p = proc
        self._id = 0
        self._lock = threading.Lock()
        self._log = log or (lambda *a, **k: None)
        # For tracking server-pushed notifications vs our responses
        self._recv_buf: list[dict] = []
        self._recv_lock = threading.Lock()
        # Spawn reader thread
        self._reader = threading.Thread(target=self._reader_loop, daemon=True, name="acp-reader")
        self._reader.start()

    def _reader_loop(self):
        """Continuously read JSON-RPC lines from codebuddy stdout."""
        for line in self.p.stdout:
            line = line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception as e:
                self._log(f"  [reader] bad JSON: {e}: {line[:100]}")
                continue
            with self._recv_lock:
                self._recv_buf.append(msg)
        self._log("  [reader] stdout closed")

    def _send(self, method: str, params: dict) -> int:
        with self._lock:
            self._id += 1
            my_id = self._id
        req = {"jsonrpc": "2.0", "id": my_id, "method": method, "params": params}
        self.p.stdin.write((json.dumps(req) + "\n").encode("utf-8"))
        self.p.stdin.flush()
        return my_id

    def _wait_id(self, want_id: int, timeout: float = 60) -> dict | None:
        """Block until a JSON-RPC message with this id appears. Polls at 20ms."""
        end = time.time() + timeout
        while time.time() < end:
            with self._recv_lock:
                # Look for matching id
                for i, msg in enumerate(self._recv_buf):
                    if msg.get("id") == want_id:
                        del self._recv_buf[i]
                        # Drain any pending notifications first
                        notifications = [m for m in self._recv_buf if m.get("id") is None]
                        self._recv_buf.clear()
                        for n in notifications:
                            self._on_notification(n)
                        return msg
                # No match yet — capture remaining notifications
                notifications = [m for m in self._recv_buf if m.get("id") is None]
                self._recv_buf.clear()
            # Yield notifications to caller
            for n in notifications:
                self._on_notification(n)
            time.sleep(0.02)
        return None

    def _on_notification(self, msg: dict) -> None:
        """Hook for subclasses to handle session/update etc. Default: log."""
        self._log(f"  [notify] {msg.get('method')}: {json.dumps(msg.get('params', {}))[:200]}")

    def call(self, method: str, params: dict, timeout: float = 60) -> dict:
        my_id = self._send(method, params)
        resp = self._wait_id(my_id, timeout)
        if resp is None:
            raise TimeoutError(f"ACP call {method} timed out after {timeout}s")
        if "error" in resp:
            raise RuntimeError(f"ACP error: {resp['error']}")
        return resp.get("result", {})

# ── Worker that streams state to disk ─────────────────────
class Worker(ACPClient):
    def __init__(self, proc, events_file, status_file, task):
        self.events_file = events_file
        self.status_file = status_file
        self.task = task
        self.status = {
            "task": task[:200],
            "phase": "starting",
            "model": None,
            "session_id": None,
            "trace_id": None,
            "outcome": None,
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
                "cache_read_tokens": 0,
            },
            "usage_by_category": {
                "systemPrompt": 0, "conversation": 0, "tools": 0,
                "mcp": 0, "skills": 0,
            },
            "last_thought": "",
            "last_message": "",
            "message_buf": "",
            "thought_buf": "",
            "started_at": time.time(),
            "finished_at": None,
        }
        super().__init__(proc, log=self._log)

    def _log(self, msg):
        # Tee to stderr + events file
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()
        append_event(self.events_file, {"kind": "log", "msg": msg})

    def _on_notification(self, msg: dict) -> None:
        method = msg.get("method")
        params = msg.get("params", {})
        upd = params.get("update", {})
        kind = upd.get("sessionUpdate")

        if method == "session/update":
            if kind == "agent_thought_chunk":
                # HIGH-FREQUENCY: every token codebuddy streams. Do NOT
                # append to events-*.jsonl (would balloon to MB per call).
                # Only keep the last 500 chars in status for inspection.
                txt = upd.get("content", {}).get("text", "")
                self.status["thought_buf"] += txt
                self.status["last_thought"] = self.status["thought_buf"][-500:]
                # Throttled persist: only flush to disk every Nth chunk
                # so a long thought doesn't generate hundreds of writes.
                self.status["_thought_chunk_count"] = self.status.get("_thought_chunk_count", 0) + 1
                if self.status["_thought_chunk_count"] % 32 == 0:
                    self._persist()
            elif kind == "agent_message_chunk":
                # HIGH-FREQUENCY: same reasoning as thought_chunk. We only
                # need the FINAL message for the result file; the live
                # transcript is rebuilt from message_buf at done() time.
                txt = upd.get("content", {}).get("text", "")
                self.status["message_buf"] += txt
                self.status["last_message"] = self.status["message_buf"][-500:]
                self.status["_msg_chunk_count"] = self.status.get("_msg_chunk_count", 0) + 1
                if self.status["_msg_chunk_count"] % 32 == 0:
                    self._persist()
            elif kind == "usage_update":
                used = upd.get("used", 0)
                size = upd.get("size", 0)
                self.status["context_window"] = {"used": used, "size": size,
                                                  "used_pct": round(100*used/size, 1) if size else 0}
                meta = upd.get("_meta", {})
                cat = meta.get("codebuddy.ai/usageByCategory", {})
                if cat:
                    self.status["usage_by_category"] = cat
                # Per-request token usage lives at _meta.usage (not at top level)
                u = meta.get("usage") or {}
                if u:
                    self.status["usage"] = {
                        "prompt_tokens": u.get("prompt_tokens", 0),
                        "completion_tokens": u.get("completion_tokens", 0),
                        "reasoning_tokens": u.get("completion_tokens_details", {}).get("reasoning_tokens", 0),
                        "cache_read_tokens": u.get("prompt_tokens_details", {}).get("cached_tokens", 0),
                    }
                # LOW-FREQUENCY: usage only arrives a few times per call.
                # Safe to append, useful for diagnostics.
                append_event(self.events_file, {
                    "kind": "context_window",
                    "used": used, "size": size,
                    "usage_by_category": cat,
                    "usage": self.status.get("usage"),
                })
                self._persist()
            elif kind == "session_info_update":
                # LOW-FREQUENCY: phase transitions are exactly what we want
                # to record for observability.
                phase = upd.get("_meta", {}).get("codebuddy.ai/agentPhase", {}).get("phase")
                if phase:
                    self.status["phase"] = phase
                if upd.get("title"):
                    self.status["task_title"] = upd["title"]
                append_event(self.events_file, {"kind": "phase", "phase": self.status["phase"]})
                self._persist()
            elif kind == "available_commands_update":
                # LOW-FREQUENCY: arrives once at session start.
                cmds = [c.get("name") for c in upd.get("availableCommands", [])]
                self.status["available_commands"] = cmds
                append_event(self.events_file, {"kind": "available_commands", "cmds": cmds})
                self._persist()
            # NOTE: deliberately NOT appending unknown sessionUpdate kinds —
            # we'd rather drop unknown noise than bloat the events file.
        # NOTE: deliberately NOT appending _codebuddy.ai/* internal pings or
        # other generic notifications. They are low-signal for our use case
        # (we only need: phase transitions, usage, terminal done). The status
        # JSON already reflects every state change that matters.

    def _persist(self) -> None:
        write_status(self.status_file, self.status)

# ── Main flow ─────────────────────────────────────────
def run(task: str, model: str | None, events_file: Path, status_file: Path,
        result_file: Path, done_file: Path, timeout: int,
        system_prompt: str | None = None,
        system_prompt_file: str | None = None,
        append_system_prompt: str | None = None,
        mcode_base_prompt_file: str | None = None) -> int:
    # Spawn codebuddy --acp
    cb_args = [
        "codebuddy", "--acp",
        # --dangerously-skip-permissions: main session's tool permission gate
        # --permission-mode bypassPermissions: same as -y, but explicit
        # --subagent-permission-mode bypassPermissions: CRITICAL —
        #   codebuddy's own subagents/teammates run their own permission
        #   system; without this they ask the user (who is unavailable
        #   in this worker context) and the task hangs at
        #   `waiting_for_permission` until bridge times out.
        # --no-session-persistence: do not keep history across calls
        "--dangerously-skip-permissions",
        "--permission-mode", "bypassPermissions",
        "--subagent-permission-mode", "bypassPermissions",
        "--no-session-persistence",
    ]
    # system prompt 注入策略(对应 status.system_prompt_mode 字段):
    #   - 调用方传 --system-prompt-file → 完全覆盖,不用 base     ("caller-override-file")
    #   - 调用方传 --system-prompt      → 完全覆盖,不用 base     ("caller-override")
    #   - 调用方传 --append-system-prompt → base + 业务追加     ("caller-append")
    #   - 什么都没传                     → base 替换默认         ("base-only")
    # base 文件不在 --append-system-prompt-file(不存在),所以用 --append-system-prompt 传文本
    base_text = ""
    if mcode_base_prompt_file and Path(mcode_base_prompt_file).is_file():
        base_text = Path(mcode_base_prompt_file).read_text(encoding="utf-8").rstrip() + "\n\n"
    if system_prompt_file:
        cb_args += ["--system-prompt-file", system_prompt_file]
        mode = "caller-override-file"
    elif system_prompt:
        cb_args += ["--system-prompt", system_prompt]
        mode = "caller-override"
    elif append_system_prompt:
        cb_args += ["--append-system-prompt", base_text + append_system_prompt]
        mode = "caller-append"
    elif base_text:
        cb_args += ["--append-system-prompt", base_text.rstrip()]
        mode = "base-only"
    else:
        mode = "none"
    try:
        proc = subprocess.Popen(
            cb_args,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0,
        )
    except Exception as e:
        sys.stderr.write(f"failed to spawn codebuddy: {e}\n")
        return 3

    w = Worker(proc, events_file, status_file, task)
    w.status["model"] = model  # None = 让 codebuddy server 自选
    w.status["system_prompt_mode"] = mode

    # 1) initialize
    try:
        w.call("initialize", {
            "protocolVersion": 1,
            "clientInfo": {"name": "invoke-codebuddy", "version": "0.5"},
            "capabilities": {},
        }, timeout=15)
    except Exception as e:
        sys.stderr.write(f"initialize failed: {e}\n")
        proc.terminate()
        return 4

    append_event(events_file, {"kind": "initialized"})

    # 2) session/new — model 字段只有显式传了才带,否则让 codebuddy server 选
    session_new_params = {
        "cwd": os.getcwd(),
        "mcpServers": [],
    }
    if model is not None:
        session_new_params["model"] = model
    try:
        r = w.call("session/new", session_new_params, timeout=30)
    except Exception as e:
        sys.stderr.write(f"session/new failed: {e}\n")
        proc.terminate()
        return 4
    w.status["session_id"] = r.get("sessionId")
    w.status["available_models"] = [m.get("modelId") for m in r.get("models", {}).get("availableModels", [])]
    w.status["phase"] = "ready"
    w._persist()
    append_event(events_file, {"kind": "session_new", "session_id": w.status["session_id"]})

    # 3) session/prompt
    w.status["phase"] = "preparing"
    w._persist()
    try:
        r = w.call("session/prompt", {
            "sessionId": w.status["session_id"],
            "prompt": [{"type": "text", "text": task}],
        }, timeout=timeout)
    except Exception as e:
        sys.stderr.write(f"prompt failed: {e}\n")
        w.status["outcome"] = "error"
        w.status["phase"] = "error"
        w.status["finished_at"] = time.time()
        w._persist()
        proc.terminate()
        done_file.touch()
        return 5

    # 4) extract final state from response
    meta = r.get("_meta", {})
    w.status["trace_id"] = meta.get("codebuddy.ai/traceId")
    w.status["outcome"] = meta.get("codebuddy.ai/outcome")
    w.status["finish_reason"] = meta.get("codebuddy.ai/finishReason")
    w.status["stop_reason"] = r.get("stopReason")
    w.status["phase"] = "done"
    w.status["finished_at"] = time.time()
    w.status["duration_s"] = round(w.status["finished_at"] - w.status["started_at"], 2)
    # Per-request token usage comes in the final response's _meta
    # Format observed: meta may contain "usage" or similar; let's also try top-level
    final_usage = (
        meta.get("codebuddy.ai/usage") or
        meta.get("usage") or
        r.get("usage") or
        {}
    )
    if final_usage:
        w.status["usage"] = {
            "prompt_tokens": final_usage.get("prompt_tokens", 0),
            "completion_tokens": final_usage.get("completion_tokens", 0),
            "reasoning_tokens": final_usage.get("completion_tokens_details", {}).get("reasoning_tokens", 0),
            "cache_read_tokens": final_usage.get("prompt_tokens_details", {}).get("cached_tokens", 0),
        }
    w._persist()
    append_event(events_file, {
        "kind": "done",
        "stop_reason": r.get("stopReason"),
        "trace_id": w.status["trace_id"],
        "outcome": w.status["outcome"],
        "usage": w.status["usage"],
    })

    # 5) write result file
    final_answer = w.status["message_buf"] or "(no message received)"
    result_file.write_text(final_answer, encoding="utf-8")

    # 6) close
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    done_file.touch()

    if w.status["outcome"] != "SUCCESS":
        return 6
    return 0

# ── CLI ──────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--model", default=None,
                    help="codebuddy model id; 不传 = 先看 CODEBUDDY_MODEL env, "
                         "再 fallback 到 None (让 server 自选)。")
    ap.add_argument("--system-prompt", default=None,
                    help="完全覆盖默认 system prompt(短文本)。")
    ap.add_argument("--system-prompt-file", default=None,
                    help="完全覆盖默认 system prompt(从文件读,长文本友好)。")
    ap.add_argument("--append-system-prompt", default=None,
                    help="在 plugin 内置 mcode base system prompt 之后追加(短文本)。")
    ap.add_argument("--mcode-base-prompt-file", default=None,
                    help="plugin 内置的 mcode base system prompt 文件路径。"
                         "worker 会自动读这个文件,在 caller-append 模式下拼到业务规则之前,"
                         "在 base-only 模式下作为唯一的 system prompt 内容。"
                         "对应 invoke-codebuddy 的 assets/mcode-base-system-prompt.md。")
    ap.add_argument("--events-file", required=True, type=Path)
    ap.add_argument("--status-file", required=True, type=Path)
    ap.add_argument("--result-file", required=True, type=Path)
    ap.add_argument("--done-file", required=True, type=Path)
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()
    # --model 解析顺序: CLI > env > None
    if args.model is None:
        args.model = os.environ.get("CODEBUDDY_MODEL")
    for p in [args.events_file, args.status_file, args.result_file, args.done_file]:
        p.parent.mkdir(parents=True, exist_ok=True)
    rc = run(args.task, args.model, args.events_file, args.status_file,
             args.result_file, args.done_file, args.timeout,
             system_prompt=args.system_prompt,
             system_prompt_file=args.system_prompt_file,
             append_system_prompt=args.append_system_prompt,
             mcode_base_prompt_file=args.mcode_base_prompt_file)
    sys.exit(rc)

if __name__ == "__main__":
    main()
