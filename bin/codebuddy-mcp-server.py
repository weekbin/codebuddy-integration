#!/usr/bin/env python3
"""
codebuddy-mcp-server.py - MCP server wrapper for codebuddy

Long-lived stdio MCP server. Exposes 5 tools (prompt, continue, status,
list_tasks, list_models) over a single codebuddy --acp subprocess that
the wrapper keeps alive for its own process lifetime. The wrapper itself
lives as long as the mcode session that loaded it (mcode manages MCP
server lifecycle, not us).

Lives at <plugin-root>/bin/codebuddy-mcp-server.py. Spawned by clients
that load the plugin and read mcp.json. Path is resolved against
PLUGIN_ROOT at runtime; the wrapper itself uses
Path(__file__).resolve().parent.parent to find its own plugin root,
so it works regardless of where the plugin lives on disk.
"""
import asyncio
import json
import os
import re
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
# State dir priority (Agent Plugins 1.0.0 spec §9.1):
#   1. $MCP_STATE_DIR, which mcp.json sets from ${PLUGIN_DATA}/state — per-client
#      injected data dir, survives plugin updates, isolated per install
#   2. $PLUGIN_ROOT/state — fallback for clients that don't inject PLUGIN_DATA
#      (older mcode, third-party clients). State shares fate with the plugin code.
# The literal string "${PLUGIN_DATA}/..." (unexpanded) is treated as unset so a
# non-conforming client doesn't accidentally create a dir called "${PLUGIN_DATA}".
_env_state_dir = os.environ.get("MCP_STATE_DIR", "")
if _env_state_dir and not _env_state_dir.startswith("${"):
    STATE_DIR = Path(_env_state_dir)
else:
    STATE_DIR = PLUGIN_ROOT / "state"
# NOTE: STATE_DIR.mkdir is intentionally NOT called at module level. A read-only
# PLUGIN_ROOT (application bundle, container image, system-wide install) would
# crash the wrapper at import time, taking down the whole plugin per spec §7.2.2
# rule 5. The mkdir happens lazily on the first log write (see _log_line) inside
# a try/except, so a read-only install silently drops log writes instead of
# refusing to start.

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
                # Lazy mkdir (do NOT move to module level — see STATE_DIR
                # comment). If STATE_DIR is on a read-only mount, the mkdir
                # or open will fail; we drop the line and keep the wrapper
                # running instead of crashing on import.
                STATE_DIR.mkdir(parents=True, exist_ok=True)
                # buffering=1 (line buffering) so each '\n'-terminated line
                # is flushed to the OS immediately, while still reusing
                # the file handle across calls to avoid per-call open().
                _log_fh = (STATE_DIR / f"mcp-{date_str}.log").open(
                    "a", encoding="utf-8", buffering=1,
                )
            except Exception as e:
                sys.stderr.write(f"[codebuddy-mcp] log open failed: {e}\n")
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
        self.available_models: list = []  # populated by _session_new from server's models.availableModels
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

    def _spawn(self, append_text: Optional[str], model: Optional[str] = None):
        args = [
            self.codebuddy_bin, "--acp",
            "--dangerously-skip-permissions",
            "--permission-mode", "bypassPermissions",
            "--subagent-permission-mode", "bypassPermissions",
            "--no-session-persistence",
        ]
        if model:
            args += ["--model", model]
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

    def _respawn(self, append_text: Optional[str] = None, model: Optional[str] = None):
        try:
            self.proc.terminate(); self.proc.wait(timeout=5)
        except Exception:
            try: self.proc.kill()
            except Exception: pass
        time.sleep(0.1)
        # When model changes we must pass it to the new subprocess at spawn
        # time; the codebuddy CLI has no runtime model-switch command and
        # session/new JSON-RPC silently ignores params.model on this server
        # (verified 2026-08-18: --model X on the CLI works, params.model in
        # session/new does not). Pass-through model on respawn even when only
        # the append changed, so a respawn-with-append preserves the current
        # model instead of resetting to the server default.
        effective_model = model if model is not None else self.last_model
        self._spawn(self._appended_text if append_text is None else append_text,
                    model=effective_model)
        self.pid = self.proc.pid
        self._reader = threading.Thread(target=self._read_loop, daemon=True, name="acp-reader")
        self._reader.start()
        self._initialize()
        self._session_new()
        _log_line("subprocess_respawn", pid=self.pid, append_len=len(append_text or ""), model=effective_model)

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
            "clientInfo": {"name": "codebuddy-mcp-server", "version": "0.3.3"},
            "capabilities": {},
        }, timeout=15)

    def _session_new(self):
        # Model is no longer accepted here. The CLI's --model X sets the
        # initial model; runtime switches go through session/set_config_option
        # (see _set_config_option). Passing params.model in this JSON-RPC call
        # is silently ignored by the server.
        params = {"cwd": self.cwd, "mcpServers": []}
        r = self.call("session/new", params, timeout=30)
        self.session_id = r.get("sessionId")
        if not self.session_id:
            raise RuntimeError(f"session/new returned no sessionId: {r}")
        # Capture the rich model catalog the server returns here (it includes
        # credits, maxInputTokens, supportsReasoning, etc. — much richer than
        # what `codebuddy --help` exposes). `list_models` reads this instead
        # of re-parsing the CLI help text. We accept both the dict form
        # `{"availableModels": [...]}` and the bare-list form for forward compat.
        models = r.get("models")
        if isinstance(models, dict) and "availableModels" in models:
            self.available_models = list(models["availableModels"])
        elif isinstance(models, list):
            self.available_models = models
        else:
            self.available_models = []
        # Initial model is whatever the CLI was spawned with; if no --model
        # was passed, this stays None and the server default (currently hy3)
        # is in effect. Don't assume here — let the first session_info_update
        # populate self.last_model.
        return r

    def _set_config_option(self, config_id: str, value, timeout: int = 15) -> dict:
        """Set a session-level config option at runtime. Verified methods
        on the codebuddy --acp server (2026-08-18):
        - `session/set_config_option` with `{sessionId, configId, value}`
          is the ACP-standard way; response is the full configOptions list
          with updated currentValue.
        - `session/set_model` with `{sessionId, modelId}` is a non-standard
          shortcut that the server also accepts.
        We use the standard one; falls back to respawn if the server rejects
        it (older server build).
        """
        r = self.call("session/set_config_option", {
            "sessionId": self.session_id,
            "configId": config_id,
            "value": value,
        }, timeout=timeout)
        # The response is a list of config options; find the one we just
        # set and confirm the server applied it. If the value didn't change
        # the caller (prompt()) raises to trigger the respawn fallback.
        for opt in (r.get("configOptions") or []):
            if opt.get("id") == config_id:
                return opt
        return r

    def _switch_model(self, new_model: str) -> None:
        """Switch the active model at runtime. Tries the ACP-standard
        `session/set_config_option` first (fast, preserves session_id,
        cache, and turn history); falls back to subprocess respawn only
        if the server rejects the config option (older codebuddy builds).

        Verified 2026-08-18 against the live server: set_config_option
        with configId="model" changes currentValue and the next prompt
        uses the new model — no respawn, no session_id change, no cache
        cold-start.
        """
        try:
            opt = self._set_config_option("model", new_model)
        except Exception as e:
            # Fallback: respawn the subprocess with --model X. The old
            # (0.3.2) path. Costs ~1-2s and a cold cache, but works on
            # any server build.
            _log_line("model_switch_fallback_respawn", err=str(e), model=new_model)
            self._respawn(model=new_model)
            self.last_model = new_model
            return
        # Server accepted the config option. Confirm it actually applied
        # (defense against servers that return 200 but ignore the write).
        applied = opt.get("currentValue") if isinstance(opt, dict) else None
        if applied != new_model:
            # Server lied or rejected silently. Fall back to respawn so
            # the caller's intent is honored.
            _log_line("model_switch_mismatch", sent=new_model, currentValue=applied)
            self._respawn(model=new_model)
        self.last_model = new_model

    def prompt(self, text, model=None, append_system_prompt=None,
               include_thinking=False, timeout=None) -> dict:
        timeout = timeout or self.timeout
        if append_system_prompt and append_system_prompt != self._appended_text:
            self._respawn(append_text=append_system_prompt)
            self._appended_text = append_system_prompt
        if model and (not self.last_model or model != self.last_model):
            # Model change: try the cheap ACP path first. If the server
            # is on an old build that doesn't support set_config_option,
            # _switch_model falls back to a subprocess respawn (the
            # 0.3.2 path). Both paths preserve the caller's intent;
            # neither needs the caller to know which happened.
            self._switch_model(model)
        t0 = time.time()
        try:
            r = self.call("session/prompt", {
                "sessionId": self.session_id,
                "prompt": [{"type": "text", "text": text}],
            }, timeout=timeout)
        except Exception:
            # Recovery path: the previous session/prompt call failed (e.g.
            # the subprocess died mid-call). Spin up a fresh ACP session
            # inside the same subprocess (model is set at spawn time, so no
            # need to pass it here).
            self._session_new()
            r = self.call("session/prompt", {
                "sessionId": self.session_id,
                "prompt": [{"type": "text", "text": text}],
            }, timeout=timeout)
        duration = time.time() - t0
        message = r.get("text") or r.get("message") or "" if isinstance(r, dict) else ""
        thinking = ""
        tool_calls: list[dict] = []  # internal tool execution (Write/Read/Bash/…)
        usage = None
        used_model = None
        for n in self._drain_notifications():
            upd = n.get("params", {}).get("update", {})
            kind = upd.get("sessionUpdate")
            # Concatenate every agent_message_chunk we see. The earlier
            # `and not message` guard only collected the first chunk, which
            # silently truncated any reply longer than a single streaming
            # segment (verified 2026-08-18: a 2694-token reply came back as
            # 3 chars). Keep the r.get("text") seed above so older server
            # builds that put the full body in the immediate response still
            # work — chunks then append, which is idempotent for the typical
            # "all-in-first-chunk" case.
            if kind == "agent_message_chunk":
                message += upd.get("content", {}).get("text", "")
            # Reasoning / "thinking" trace. Opt-in via include_thinking=True;
            # default is off because this can be hundreds of chunks per call
            # and bloats the response. We always capture to `thinking` (cheap,
            # just a string concat) and only include it in the result when
            # the caller asked.
            elif kind == "agent_thought_chunk":
                thinking += upd.get("content", {}).get("text", "")
            # Tool execution: codebuddy is itself a coding agent — it can
            # call Read/Write/Bash/etc. internally before answering. Capture
            # the title + status of each so callers can show "I wrote X
            # files" without seeing the raw I/O. Always included in result
            # (small, structured, high-signal for "what did the agent do").
            elif kind == "tool_call":
                tool_calls.append({
                    "id": upd.get("toolCallId"),
                    "title": upd.get("title"),
                    "kind": upd.get("kind"),
                    "status": upd.get("status"),
                })
            elif kind == "tool_call_update":
                tc_id = upd.get("toolCallId")
                for tc in tool_calls:
                    if tc.get("id") == tc_id:
                        new_status = upd.get("status")
                        if new_status: tc["status"] = new_status
                        break
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
            "tool_calls": tool_calls,
        }
        # Thinking is opt-in: a 79s long task can produce 600+ thought
        # chunks; always capturing (cheap) but only surfacing when asked.
        if include_thinking and thinking:
            result["thinking"] = thinking
            result["thinking_chars"] = len(thinking)
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
        # Input schema says "max 50" — the deque's maxlen=50 also enforces
        # this implicitly, but the explicit clamp here makes the doc claim
        # match the code (NOTE #7).
        if limit > 50: limit = 50
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


def _health_check_codebuddy(cb_bin: str) -> None:
    """Verify the codebuddy binary is reachable BEFORE we start the
    long-lived subprocess. Raises RuntimeError with a clear, actionable
    message if the binary is missing, returns non-zero, or hangs.

    Adds ~0.5-1s latency to first call after a fresh session start (where
    the user already pays a model warmup cost). Pays for itself the first
    time someone has a typo in $PATH — they get "set CODEBUDDY_BIN or
    install codebuddy" instead of a confusing FileNotFoundError stack.
    """
    try:
        r = subprocess.run(
            [cb_bin, "--version"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()[:200]
            raise RuntimeError(
                f"codebuddy at {cb_bin!r} returned exit {r.returncode}: {err!r}\n"
                f"  → Set CODEBUDDY_BIN to the full path of a working codebuddy, or\n"
                f"  → Install with `npm i -g @tencent-ai/codebuddy-code`."
            )
    except FileNotFoundError:
        raise RuntimeError(
            f"codebuddy binary not found: {cb_bin!r}\n"
            f"  → Set CODEBUDDY_BIN env var to the full path, or\n"
            f"  → Install with `npm i -g @tencent-ai/codebuddy-code`, or\n"
            f"  → Symlink codebuddy onto $PATH."
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"codebuddy at {cb_bin!r} did not respond to --version within 5s.\n"
            f"  → Likely a broken install or a hung interactive prompt. "
            f"Try `codebuddy --version` manually."
        )


def get_session() -> ACPSession:
    global _session
    with _session_lock:
        if _session is None or _session.proc.poll() is not None:
            cwd = os.environ.get("CODEBUDDY_MCP_CWD") or os.getcwd()
            cb_bin = os.environ.get("CODEBUDDY_BIN") or "codebuddy"
            base = os.environ.get("MCODE_BASE_PROMPT_FILE")
            # Fail-fast: verify reachability before spawning the long-lived
            # subprocess. This is the only time we pay the 0.5-1s cost.
            _health_check_codebuddy(cb_bin)
            _session = ACPSession(codebuddy_bin=cb_bin, cwd=cwd, mcode_base_prompt_file=base)
        return _session


# ── Model catalog (parsed from `codebuddy --help`, cached per process) ──
_models_cache: Optional[dict] = None
_models_cache_lock = threading.Lock()


def list_codebuddy_models() -> dict:
    """Return codebuddy's supported model catalog.

    Strategy: the wrapper's `ACPSession._session_new` already captures
    `models.availableModels` from the `session/new` JSON-RPC response,
    which is a rich list with id, name, description, supportsImages,
    supportsReasoning, credits, maxInputTokens — much richer than
    what `codebuddy --help` exposes. The MCP-level list_models tool
    reads from the active session if one exists; if not, it falls back
    to a fresh subprocess + `codebuddy --help` parse. This function
    is the module-level entry point (used by both the tool handler and
    the test suite) and is safe to call before any session is created.
    """
    global _models_cache
    with _models_cache_lock:
        if _models_cache is not None:
            return _models_cache
        # If a session is already live, its catalog is fresher than a
        # codebuddy --help parse (the server can return models the CLI
        # help text doesn't list, e.g. custom-local:* names from a
        # user config). Copy it out under the lock.
        if _session is not None:
            try:
                rich = list(_session.available_models or [])
            except Exception:
                rich = []
            if rich:
                ids = [m.get("modelId") for m in rich if m.get("modelId")]
                _models_cache = {
                    "ok": True, "models": ids, "count": len(ids),
                    "rich": rich,  # full metadata for callers that want it
                    "source": "session/new models.availableModels",
                }
                return _models_cache
        # Fallback: parse codebuddy --help. The CLI documents its `--model`
        # flag with a parenthesized, comma-separated list of ids on a
        # single line, so a regex is enough.
        cb_bin = os.environ.get("CODEBUDDY_BIN") or "codebuddy"
        try:
            r = subprocess.run(
                [cb_bin, "--help"], capture_output=True, text=True, timeout=10,
            )
        except FileNotFoundError:
            _models_cache = {
                "ok": False, "models": [],
                "error": f"codebuddy binary not found: {cb_bin!r} "
                         f"(set CODEBUDDY_BIN or symlink codebuddy onto $PATH)",
            }
            return _models_cache
        except subprocess.TimeoutExpired:
            _models_cache = {
                "ok": False, "models": [],
                "error": f"codebuddy --help timed out after 10s",
            }
            return _models_cache
        out = (r.stdout or "") + "\n" + (r.stderr or "")
        models_line = None
        for line in out.splitlines():
            if "--model" not in line:
                continue
            paren = re.search(r"\(([^)]+)\)", line)
            if paren and "," in paren.group(1):
                models_line = line
                break
        if not models_line:
            _models_cache = {
                "ok": False, "models": [],
                "error": "could not locate `--model (...)` line in `codebuddy --help`; "
                         f"help output had {len(out.splitlines())} lines",
                "help_tail": "\n".join(out.splitlines()[-5:]),
            }
            return _models_cache
        paren = re.search(r"\(([^)]+)\)", models_line)
        ids = [s.strip() for s in paren.group(1).split(",") if s.strip()]
        _models_cache = {
            "ok": True, "models": ids, "count": len(ids),
            "source": "codebuddy --help",
        }
        return _models_cache


def _prompt_props() -> dict:
    return {
        "text": {"type": "string", "description": "The prompt / task description to send to codebuddy."},
        "model": {"type": "string",
                  "description": "Optional codebuddy model id (e.g. 'hy3', 'deepseek-v4-flash'). Use `list_models` to enumerate valid ids. Switching models is dynamic via `session/set_config_option` (preserves session_id, cache, and turn history) — no subprocess restart in the normal path."},
        "append_system_prompt": {"type": "string",
                                 "description": "Optional business rules / context appended to the mcode base system prompt. First call applies; subsequent changes trigger a subprocess respawn so the new append takes effect."},
        "include_thinking": {"type": "boolean",
                             "description": "If true, include the model's reasoning trace (`agent_thought_chunk` stream) in the response. Off by default — a long task can produce hundreds of thought chunks. Default false."},
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
TOOL_LIST_MODELS = Tool(
    name="list_models",
    description=("List codebuddy's supported model IDs by parsing `codebuddy --help`. Use this before passing a `model` argument to `prompt` / `continue` to verify the model id is valid (e.g. `deepseek-v4-flash`, `hy3`). Returns `{ok, models: [id, ...], count, source}` on success, `{ok: false, error, ...}` on failure. Cached per process."),
    inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
)

ALL_TOOLS = [TOOL_PROMPT, TOOL_CONTINUE, TOOL_STATUS, TOOL_LIST_TASKS, TOOL_LIST_MODELS]


async def _list_tools(ctx, params):
    return ListToolsResult(tools=ALL_TOOLS)


def _format_result(result: dict) -> str:
    parts: list[str] = [result.get("text", "")]
    # Opt-in thinking trace (request include_thinking=true on the prompt
    # tool). Format with a clear header so callers can grep for it.
    thinking = result.get("thinking")
    if thinking:
        parts.append(f"--- thinking ({result.get('thinking_chars', len(thinking))} chars) ---\n{thinking}")
    # Tool calls are always included (small, structured, high-signal).
    # Each line: "tool title [status]" — enough for "what did the agent do".
    tool_calls = result.get("tool_calls") or []
    if tool_calls:
        lines = [f"--- tools ({len(tool_calls)}) ---"]
        for tc in tool_calls:
            title = tc.get("title") or tc.get("kind") or "?"
            status = tc.get("status") or "?"
            lines.append(f"  {title} [{status}]")
        parts.append("\n".join(lines))
    meta = [f"[codebuddy: pid={result.get('cb_pid') or '?'}, model={result.get('model') or '?'}, dur={result.get('duration_s')}s, stop={result.get('stop_reason') or '?'}]"]
    usage = result.get("usage") or {}
    if usage:
        pt = usage.get("prompt_tokens", 0)
        ct = usage.get("completion_tokens", 0)
        cached = usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
        meta.append(f"[tokens: prompt={pt}, completion={ct}, cache_read={cached}, cache_ratio={result.get('cache_ratio', 0)}%]")
    parts.append("\n".join(meta))
    return "\n\n".join(parts)


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
            include_thinking=bool(arguments.get("include_thinking", False)),
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
    if name == "list_models":
        result = list_codebuddy_models()
        return CallToolResult(content=[TextContent(type="text", text="[codebuddy list_models]\n" + json.dumps(result, ensure_ascii=False, indent=2))])
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
