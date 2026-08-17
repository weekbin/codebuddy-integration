#!/usr/bin/env python3
"""
codebuddy-mcp-server.py - MCP server wrapper for codebuddy

Long-lived stdio MCP server. Exposes 4 tools (prompt, continue, status,
list_tasks) over a single codebuddy --acp subprocess that the wrapper
keeps alive for its own process lifetime. The wrapper itself lives as
long as the mcode session that loaded it (mcode manages MCP server
lifecycle, not us).

Lives at <plugin-root>/bin/codebuddy-mcp-server.py. Spawned by clients
that load the plugin and read mcp.json. Path is resolved against
PLUGIN_ROOT at runtime; the wrapper itself uses
Path(__file__).resolve().parent.parent to find its own plugin root,
so it works regardless of where the plugin lives on disk.
"""
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent, ListToolsResult, CallToolResult

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = PLUGIN_ROOT / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)

# ── structured logger ─────────────────────
_log_lock = threading.Lock()
_log_date: Optional[str] = None
_log_fh = None  # cached file handle; reopened only on date change


def _log_line(event: str, **fields) -> None:
    global _log_date, _log_fh
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    ts = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    parts = [ts, event] + [f"{k}={_fmt(v)}" for k, v in fields.items()]
    line = " | ".join(parts) + "\n"
    with _log_lock:
        if _log_date != date_str:
            # Date rollover: close old handle, open new one.
            try:
                if _log_fh is not None:
                    _log_fh.close()
            except Exception:
                pass
            try:
                # buffering=1 (line buffering) so each '\n'-terminated line
                # is flushed to the OS immediately, while still reusing
                # the file handle across calls to avoid per-call open().
                _log_fh = (STATE_DIR / f"mcp-{date_str}.log").open(
                    "a", encoding="utf-8", buffering=1,
                )
            except Exception:
                _log_fh = None
            _log_date = date_str
        if _log_fh is not None:
            try:
                _log_fh.write(line)
            except Exception:
                pass


def _fmt(v) -> str:
    if v is None:
        return "-"
    s = str(v)
    if any(c in s for c in " |\n"):
        s = '"' + s.replace('"', '\\"') + '"'
    return s


# ── ACP session (one long-lived codebuddy subprocess) ─────────────
class ACPSession:
    def __init__(self, codebuddy_bin: str, cwd: str,
                 mcode_base_prompt_file: Optional[str] = None,
                 timeout: int = 300):
        self.codebuddy_bin = codebuddy_bin
        self.cwd = cwd
        self.mcode_base_prompt_file = mcode_base_prompt_file
        self.timeout = timeout
        self._id = 0
        self._id_lock = threading.Lock()
        self._lock = threading.Lock()
        self._resp_buf: dict[int, dict] = {}
        self._resp_cv = threading.Condition(self._lock)
        self._notifications: list[dict] = []
        self._notif_lock = threading.Lock()
        self.session_id: Optional[str] = None
        self.last_model: Optional[str] = None
        self._appended_text: Optional[str] = None
        self.started_at = time.time()
        self.call_count = 0
        self.last_call_at: Optional[float] = None
        self.last_cache_ratio: Optional[float] = None
        self.totals = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}
        self._tasks: deque = deque(maxlen=50)
        self._spawn(self._appended_text)
        self.pid = self.proc.pid
        self._reader = threading.Thread(target=self._read_loop, daemon=True, name="acp-reader")
        self._reader.start()
        self._initialize()
        self._session_new()
        _log_line("session_new", pid=self.pid, cwd=self.cwd)

    def _spawn(self, append_text: Optional[str]):
        args = [
            self.codebuddy_bin, "--acp",
            "--dangerously-skip-permissions",
            "--permission-mode", "bypassPermissions",
            "--subagent-permission-mode", "bypassPermissions",
            "--no-session-persistence",
        ]
        if self.mcode_base_prompt_file and Path(self.mcode_base_prompt_file).is_file():
            base = Path(self.mcode_base_prompt_file).read_text(encoding="utf-8").rstrip()
        else:
            base = ""
        combined = base
        if append_text:
            combined = (combined + "\n\n" if combined else "") + append_text
        if combined:
            args += ["--append-system-prompt", combined]
        try:
            self.proc = subprocess.Popen(
                args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0, cwd=self.cwd,
            )
        except Exception as e:
            raise RuntimeError(f"failed to spawn {self.codebuddy_bin}: {e}")

    def _respawn(self, append_text: Optional[str]):
        try:
            self.proc.terminate(); self.proc.wait(timeout=5)
        except Exception:
            try: self.proc.kill()
            except Exception: pass
        time.sleep(0.1)
        self._spawn(append_text)
        self.pid = self.proc.pid
        self._reader = threading.Thread(target=self._read_loop, daemon=True, name="acp-reader")
        self._reader.start()
        self._initialize()
        self._session_new()
        _log_line("subprocess_respawn", pid=self.pid, append_len=len(append_text or ""))

    def _read_loop(self):
        try:
            for line in self.proc.stdout:
                line = line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                dbg = os.environ.get("CODEBUDDY_MCP_DEBUG_LOG")
                if dbg:
                    try:
                        with open(dbg, "a", encoding="utf-8") as f:
                            f.write(line + "\n")
                    except Exception: pass
                try: msg = json.loads(line)
                except Exception: continue
                if isinstance(msg, dict) and "id" in msg and msg.get("id") is not None:
                    with self._resp_cv:
                        self._resp_buf[msg["id"]] = msg
                        self._resp_cv.notify_all()
                else:
                    with self._notif_lock:
                        self._notifications.append(msg)
        except Exception: pass

    def _next_id(self) -> int:
        with self._id_lock:
            self._id += 1
            return self._id

    def _send(self, method, params):
        my_id = self._next_id()
        req = {"jsonrpc": "2.0", "id": my_id, "method": method, "params": params}
        self.proc.stdin.write((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
        self.proc.stdin.flush()
        return my_id

    def _wait_id(self, want_id, timeout):
        end = time.time() + timeout
        with self._resp_cv:
            while time.time() < end:
                if want_id in self._resp_buf:
                    return self._resp_buf.pop(want_id)
                remaining = end - time.time()
                if remaining <= 0: break
                self._resp_cv.wait(timeout=remaining)
        raise TimeoutError(f"ACP call id={want_id} timeout after {timeout}s")

    def call(self, method, params, timeout=None):
        timeout = timeout or self.timeout
        my_id = self._send(method, params)
        resp = self._wait_id(my_id, timeout)
        if "error" in resp:
            raise RuntimeError(f"ACP error: {resp['error']}")
        return resp.get("result", {})

    def _drain_notifications(self) -> list[dict]:
        with self._notif_lock:
            notifs = self._notifications[:]
            self._notifications.clear()
        return notifs

    def _initialize(self):
        self.call("initialize", {
            "protocolVersion": 1,
            "clientInfo": {"name": "codebuddy-mcp-server", "version": "0.3.0"},
            "capabilities": {},
        }, timeout=15)

    def _session_new(self, model=None):
        params = {"cwd": self.cwd, "mcpServers": []}
        if model:
            params["model"] = model
        r = self.call("session/new", params, timeout=30)
        self.session_id = r.get("sessionId")
        if not self.session_id:
            raise RuntimeError(f"session/new returned no sessionId: {r}")
        return r

    def prompt(self, text, model=None, append_system_prompt=None, timeout=None) -> dict:
        timeout = timeout or self.timeout
        if append_system_prompt and append_system_prompt != self._appended_text:
            self._respawn(append_system_prompt)
            self._appended_text = append_system_prompt
        if model and self.last_model and model != self.last_model:
            self._session_new(model=model)
            self.last_model = model
        elif model and not self.last_model:
            self._session_new(model=model)
            self.last_model = model
        t0 = time.time()
        try:
            r = self.call("session/prompt", {
                "sessionId": self.session_id,
                "prompt": [{"type": "text", "text": text}],
            }, timeout=timeout)
        except Exception:
            self._session_new(model=self.last_model)
            r = self.call("session/prompt", {
                "sessionId": self.session_id,
                "prompt": [{"type": "text", "text": text}],
            }, timeout=timeout)
        duration = time.time() - t0
        message = r.get("text") or r.get("message") or "" if isinstance(r, dict) else ""
        usage = None
        used_model = None
        for n in self._drain_notifications():
            upd = n.get("params", {}).get("update", {})
            kind = upd.get("sessionUpdate")
            if kind == "agent_message_chunk" and not message:
                message += upd.get("content", {}).get("text", "")
            elif kind == "usage_update":
                u = upd.get("_meta", {}).get("usage")
                if u: usage = u
            elif kind == "session_info_update":
                m = upd.get("_meta", {}).get("codebuddy.ai/requestModelId")
                if m: used_model = m
            elif kind in ("agent_thought_chunk", "agent_message_chunk"):
                m = upd.get("_meta", {}).get("codebuddy.ai/responseModelId")
                if m: used_model = m
        if not usage and isinstance(r, dict):
            meta = r.get("_meta", {})
            usage = meta.get("usage") or meta.get("codebuddy.ai/usage") or r.get("usage") or {}
        if used_model:
            self.last_model = used_model
        pt = (usage or {}).get("prompt_tokens", 0)
        ct = (usage or {}).get("completion_tokens", 0)
        cached = (usage or {}).get("prompt_tokens_details", {}).get("cached_tokens", 0)
        cache_pct = round(100 * cached / pt, 1) if pt else 0.0
        result = {
            "text": message or "(no message received from codebuddy)",
            "usage": usage or {},
            "model": self.last_model,
            "duration_s": round(duration, 2),
            "stop_reason": r.get("stopReason") if isinstance(r, dict) else None,
            "cb_pid": self.pid,
            "cache_ratio": cache_pct,
        }
        self.call_count += 1
        self.last_call_at = t0
        self.last_cache_ratio = cache_pct
        self.totals["prompt_tokens"] += pt
        self.totals["completion_tokens"] += ct
        self.totals["cached_tokens"] += cached
        self._tasks.append({
            "idx": self.call_count, "ts": t0,
            "text_preview": text[:80] + ("..." if len(text) > 80 else ""),
            "model": self.last_model, "duration_s": result["duration_s"],
            "prompt_tokens": pt, "completion_tokens": ct, "cached_tokens": cached,
            "cache_ratio": cache_pct, "stop_reason": result["stop_reason"],
        })
        _log_line("prompt", call_id=self.call_count, model=self.last_model,
                  dur_s=result["duration_s"], pt=pt, ct=ct, cached=cached,
                  cache_pct=cache_pct, stop=result["stop_reason"])
        return result

    def status(self) -> dict:
        alive = self.proc.poll() is None
        return {
            "alive": alive, "codebuddy_pid": self.pid if alive else None,
            "acp_session_id": self.session_id, "model": self.last_model,
            "started_at": self.started_at, "uptime_s": round(time.time() - self.started_at, 1),
            "call_count": self.call_count, "last_call_at": self.last_call_at,
            "last_cache_ratio": self.last_cache_ratio, "totals": dict(self.totals),
        }

    def list_tasks(self, limit: int = 10) -> list[dict]:
        if limit <= 0: return []
        items = list(self._tasks)[-limit:]
        items.reverse()
        return items

    def close(self):
        try: self.proc.terminate(); self.proc.wait(timeout=5)
        except Exception:
            try: self.proc.kill()
            except Exception: pass


_session: Optional[ACPSession] = None
_session_lock = threading.Lock()


def get_session() -> ACPSession:
    global _session
    with _session_lock:
        if _session is None or _session.proc.poll() is not None:
            cwd = os.environ.get("CODEBUDDY_MCP_CWD") or os.getcwd()
            cb_bin = os.environ.get("CODEBUDDY_BIN") or "codebuddy"
            base = os.environ.get("MCODE_BASE_PROMPT_FILE")
            _session = ACPSession(codebuddy_bin=cb_bin, cwd=cwd, mcode_base_prompt_file=base)
        return _session


def _prompt_props() -> dict:
    return {
        "text": {"type": "string", "description": "The prompt / task description to send to codebuddy."},
        "model": {"type": "string",
                  "description": "Optional codebuddy model id (e.g. 'glm-5.2', 'deepseek-v4-pro'). Omit = codebuddy server default."},
        "append_system_prompt": {"type": "string",
                                 "description": "Optional business rules / context appended to the mcode base system prompt. First call applies; subsequent changes trigger a subprocess respawn so the new append takes effect."},
        "timeout": {"type": "integer", "description": "Per-call timeout in seconds (default 300)."},
    }


TOOL_PROMPT = Tool(
    name="prompt",
    description=("Send a one-shot text prompt to codebuddy (a peer LLM). Use for translation, summarization, design review, brainstorming, second opinion, or fresh implementation drafts. The wrapper keeps a single codebuddy subprocess alive so cache stays hot; per-call cost after the first is ~150-300 conversation tokens. Returns the assistant message plus a metadata tail with pid / model / tokens / cache_ratio. Triggers: 'ask codebuddy', '用 codebuddy', '让 codebuddy', 'summarize/review/translate with codebuddy'."),
    inputSchema={"type": "object", "properties": _prompt_props(), "required": ["text"]},
)
TOOL_CONTINUE = Tool(
    name="continue",
    description=("Continue the same codebuddy conversation with a follow-up message. Functionally identical to `prompt` — codebuddy keeps server-side history by sessionId so subsequent calls are auto-continued — but exposed as a distinct tool so the caller's intent is explicit in the MCP trace. Triggers: 'ask codebuddy follow-up', '让 codebuddy 继续', 'codebuddy 然后'."),
    inputSchema={"type": "object", "properties": _prompt_props(), "required": ["text"]},
)
TOOL_STATUS = Tool(
    name="status",
    description=("Return the current wrapper + codebuddy subprocess state: liveness, pid, ACP session id, model, uptime, call_count, last_call timestamp, last cache_ratio, cumulative token totals. No side effects. Use for diagnostics or to confirm a long-running wrapper is still healthy before the next call."),
    inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
)
TOOL_LIST_TASKS = Tool(
    name="list_tasks",
    description=("Return the most recent N call metadata records (most recent first). Each record has: idx, ts, text_preview, model, duration_s, prompt_tokens, completion_tokens, cached_tokens, cache_ratio, stop_reason. Use to inspect what the wrapper has done in this mcode session without grepping the log file."),
    inputSchema={"type": "object", "properties": {"limit": {"type": "integer", "description": "Max records to return (default 10, max 50)."}}, "additionalProperties": False},
)

ALL_TOOLS = [TOOL_PROMPT, TOOL_CONTINUE, TOOL_STATUS, TOOL_LIST_TASKS]


async def _list_tools(ctx, params):
    return ListToolsResult(tools=ALL_TOOLS)


def _format_result(result: dict) -> str:
    body = result.get("text", "")
    meta = [f"[codebuddy: pid={result.get('cb_pid') or '?'}, model={result.get('model') or '?'}, dur={result.get('duration_s')}s, stop={result.get('stop_reason') or '?'}]"]
    usage = result.get("usage") or {}
    if usage:
        pt = usage.get("prompt_tokens", 0)
        ct = usage.get("completion_tokens", 0)
        cached = usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
        meta.append(f"[tokens: prompt={pt}, completion={ct}, cache_read={cached}, cache_ratio={result.get('cache_ratio', 0)}%]")
    return body + "\n\n" + "\n".join(meta)


async def _call_tool(ctx, params):
    name = getattr(params, "name", None) or (params.get("name") if isinstance(params, dict) else None)
    arguments = (getattr(params, "arguments", None) or (params.get("arguments") if isinstance(params, dict) else {}) or {})
    if name in ("prompt", "continue"):
        text = arguments.get("text")
        if not text:
            raise ValueError(f"{name}: missing required arg: text")
        sess = get_session()
        result = sess.prompt(
            text=text, model=arguments.get("model"),
            append_system_prompt=arguments.get("append_system_prompt"),
            timeout=arguments.get("timeout"),
        )
        return CallToolResult(content=[TextContent(type="text", text=_format_result(result))])
    if name == "status":
        sess = get_session()
        st = sess.status()
        return CallToolResult(content=[TextContent(type="text", text="[codebuddy status]\n" + json.dumps(st, ensure_ascii=False, indent=2))])
    if name == "list_tasks":
        sess = get_session()
        items = sess.list_tasks(limit=int(arguments.get("limit") or 10))
        return CallToolResult(content=[TextContent(type="text", text="[codebuddy list_tasks]\n" + json.dumps(items, ensure_ascii=False, indent=2))])
    raise ValueError(f"unknown tool: {name}")


app = Server("codebuddy", on_list_tools=_list_tools, on_call_tool=_call_tool)


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
    finally:
        if _session: _session.close()
