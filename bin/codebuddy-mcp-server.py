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
import dataclasses
import json
import os
import re
import signal
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

# ── Task persistence (Agent Plugins 1.0.0 async submit/poll model) ──
# Each in-flight or completed codebuddy call is a TaskRecord persisted to
# ${PLUGIN_DATA}/tasks/<task_id>.json. Wrapper restart GC marks any task still
# in "running" state as "stale" so get_result returns a deterministic error
# instead of pretending the call will complete. With the 0.4.0 async API,
# the MCP request lifetime is millisecond-scale; the task lifetime is the
# codebuddy call lifetime (potentially hours).

TASKS_DIR = STATE_DIR / "tasks"
# TASK_LIFETIME_S: how long completed task records stay on disk before they
# are eligible for GC. Long enough that get_result after a wrapper restart
# can still answer, short enough that the dir doesn't grow unbounded.
TASK_LIFETIME_S = 86400  # 24h


@dataclasses.dataclass
class TaskRecord:
    task_id: str
    status: str  # "running" | "done" | "error" | "stale"
    submitted_at: str
    text_preview: str = ""
    model: Optional[str] = None
    completed_at: Optional[str] = None
    result: Optional[dict] = None
    error: Optional[str] = None
    duration_s: Optional[float] = None


def _task_to_dict(rec: TaskRecord) -> dict:
    d = dataclasses.asdict(rec)
    return d


def _dict_to_task(d: dict) -> Optional[TaskRecord]:
    try:
        return TaskRecord(
            task_id=d["task_id"],
            status=d["status"],
            submitted_at=d["submitted_at"],
            text_preview=d.get("text_preview", ""),
            model=d.get("model"),
            completed_at=d.get("completed_at"),
            result=d.get("result"),
            error=d.get("error"),
            duration_s=d.get("duration_s"),
        )
    except (KeyError, TypeError):
        return None


def _save_task(rec: TaskRecord) -> bool:
    """Atomic write: tmp + rename. Returns True on success.

    mkdir is best-effort; a read-only install silently drops the record.
    Caller treats False as "task is in-memory only, will be lost on restart".
    """
    try:
        TASKS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        return False
    path = TASKS_DIR / f"{rec.task_id}.json"
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_task_to_dict(rec), f, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass
        return False


def _load_task(task_id: str) -> Optional[TaskRecord]:
    """Load task from disk. Returns None if file missing, malformed, or
    task_id contains anything outside [A-Za-z0-9_-] (defense against path
    traversal via crafted task_id from a malicious caller).
    """
    if not task_id or not all(c.isalnum() or c in "-_." for c in task_id):
        return None
    if len(task_id) > 64:
        return None
    path = TASKS_DIR / f"{task_id}.json"
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return _dict_to_task(d)


def _gc_orphan_tasks() -> int:
    """Mark any status='running' task as 'stale' (wrapper-restart recovery).
    Also drop completed task files older than TASK_LIFETIME_S to keep the
    dir bounded. Returns the number of tasks touched.
    """
    if not TASKS_DIR.exists():
        return 0
    touched = 0
    now = time.time()
    cutoff = now - TASK_LIFETIME_S
    for path in list(TASKS_DIR.glob("*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            rec = _dict_to_task(d)
            if rec is None:
                continue
            changed = False
            if rec.status == "running":
                rec.status = "stale"
                rec.error = "wrapper restarted while this task was in-flight"
                rec.completed_at = datetime.now(timezone.utc).isoformat()
                changed = True
                touched += 1
            elif rec.status in ("done", "error", "stale") and rec.completed_at:
                try:
                    completed_ts = datetime.fromisoformat(rec.completed_at).timestamp()
                    if completed_ts < cutoff:
                        path.unlink()
                        touched += 1
                        continue
                except ValueError:
                    pass
            if changed:
                _save_task(rec)
        except (OSError, json.JSONDecodeError):
            continue
    return touched


def _safe_elapsed(submitted_at: str) -> Optional[float]:
    """Compute elapsed seconds since an ISO-8601 timestamp; return None
    if the timestamp can't be parsed (defensive — bad data on disk should
    not crash get_result)."""
    try:
        ts = datetime.fromisoformat(submitted_at).timestamp()
        return round(time.time() - ts, 2)
    except (ValueError, TypeError):
        return None


def _collect_response_artifacts(r: dict, notifications: list,
                                include_thinking: bool,
                                fallback_model: Optional[str]) -> dict:
    """Pure function: turn (immediate response + drained notifications) into
    the final result dict. Extracted from the old sync `prompt()` so the
    background thread and the unit tests can both use it without going
    through the whole codebuddy subprocess.

    `duration_s` and `cb_pid` are zero/Nones here; the caller fills them
    in (the thread knows wall time and pid; tests know their fixtures).
    """
    message = r.get("text") or r.get("message") or "" if isinstance(r, dict) else ""
    thinking = ""
    tool_calls: list[dict] = []
    usage = None
    used_model: Optional[str] = None
    for n in notifications:
        upd = n.get("params", {}).get("update", {})
        kind = upd.get("sessionUpdate")
        # Concatenate every agent_message_chunk (the 0.3.1 truncation bug
        # fix carried forward).
        if kind == "agent_message_chunk":
            message += upd.get("content", {}).get("text", "")
        elif kind == "agent_thought_chunk":
            thinking += upd.get("content", {}).get("text", "")
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
        # NOTE: 0.4.4 removed the previously-dead branch
        #   `elif kind in ("agent_thought_chunk", "agent_message_chunk")`
        # which tried to extract `codebuddy.ai/responseModelId` from
        # streaming chunks. The if/elif above already handles those kinds
        # for message/thinking accumulation, so the later branch was
        # unreachable. If a future codebuddy build starts sending the
        # response model id only on chunks (not on session_info_update),
        # re-introduce the extraction here — outside the elif chain so it
        # doesn't get masked by the early branches.
    if not usage and isinstance(r, dict):
        meta = r.get("_meta", {})
        usage = meta.get("usage") or meta.get("codebuddy.ai/usage") or r.get("usage") or {}
    if used_model:
        fallback_model = used_model
    pt = (usage or {}).get("prompt_tokens", 0)
    ct = (usage or {}).get("completion_tokens", 0)
    cached = (usage or {}).get("prompt_tokens_details", {}).get("cached_tokens", 0)
    cache_pct = round(100 * cached / pt, 1) if pt else 0.0
    result = {
        "text": message or "(no message received from codebuddy)",
        "usage": usage or {},
        "model": fallback_model,
        "duration_s": 0.0,  # caller overrides
        "stop_reason": r.get("stopReason") if isinstance(r, dict) else None,
        "cb_pid": None,      # caller overrides
        "cache_ratio": cache_pct,
        "tool_calls": tool_calls,
    }
    if include_thinking and thinking:
        result["thinking"] = thinking
        result["thinking_chars"] = len(thinking)
    return result


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


# ── ACP error model ───────────────────────────────────────────────
# The codebuddy CLI surfaces 429 (rate limit) and other application
# errors via the standard JSON-RPC `error` field. We carry the original
# error dict into Python-land so the recovery path in
# `_run_prompt_in_thread` can distinguish 429 from generic errors and
# decide whether retrying is safe (it isn't for 429 — the prompt may
# have been billed already).
class ACPError(RuntimeError):
    """Raised when codebuddy returns a JSON-RPC error response.

    Carries the original `{"code", "message", ...}` dict so callers can
    branch on the code (e.g. distinguish 429 from protocol errors) without
    parsing the human-readable string.
    """
    def __init__(self, error: dict, method: str = ""):
        self.error = error or {}
        self.method = method
        self.code = self.error.get("code")
        self.message = self.error.get("message", "")
        super().__init__(
            f"ACP error from {method!r}: code={self.code} message={self.message!r}"
        )


class ACPRateLimitError(ACPError):
    """codebuddy returned 429 (rate limited).

    The prompt may or may not have been processed by the model API before
    the rate-limit kicked in. Auto-retry is NOT safe — at best it's a no-op,
    at worst it double-bills. Callers should either switch to a different
    model (`model="deepseek-v4-flash"` instead of the `hy3` free-tier) or
    wait for the rate-limit window to reset. The wrapper surfaces the error
    to the caller and writes a structured log line; it does NOT retry.
    """
    def __init__(self, error: dict, method: str = ""):
        super().__init__(error, method)
        # Hint to callers: do not auto-retry. If a caller ignores this and
        # retries, the second attempt is their responsibility.
        self.retryable = False


# ── ACP session (one long-lived codebuddy subprocess) ─────────────
class ACPSession:
    def __init__(self, codebuddy_bin: str, cwd: str,
                 mcode_base_prompt_file: Optional[str] = None,
                 timeout: int = 3600):
        self.codebuddy_bin = codebuddy_bin
        self.cwd = cwd
        self.mcode_base_prompt_file = mcode_base_prompt_file
        self.timeout = timeout
        self._id = 0
        self._id_lock = threading.Lock()
        # _lock MUST be reentrant: _run_prompt_in_thread (line ~639) holds it
        # across `self.call("session/prompt", ...)`, and call() → _wait_id()
        # re-acquires it via `with self._resp_cv:`. A plain Lock self-deadlocks
        # on every prompt call — symptom is a worker thread stuck in
        # futex_do_wait with call_count never incrementing. Bug present from
        # 0.3.0..0.4.2; only list_models (which reads cached session catalog
        # and never calls call()) appeared to work. Fixed in 0.4.3.
        self._lock = threading.RLock()
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
        # 0.4.0 async submit/poll state: in-flight task + completed-task ring
        # + wakeup event for blocking get_result. The single in-flight
        # constraint (one codebuddy call at a time per session) is by design
        # — codebuddy is a single subprocess with a single sessionId, so
        # concurrent calls would serialize at the JSON-RPC layer anyway.
        self._inflight: Optional[TaskRecord] = None
        self._tasks_done: deque[TaskRecord] = deque(maxlen=50)
        self._task_event: threading.Event = threading.Event()
        self._task_lock: threading.Lock = threading.Lock()
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
            err = resp["error"] or {}
            # Detect 429 (rate limit) and raise a structured subclass so
            # `_run_prompt_in_thread` can branch on it. We match on either
            # `code == 429` (standard) or the substring "rate limit" in the
            # message (defensive — codebuddy CLI has been seen using
            # application-defined codes like -32001 with rate-limit text).
            code = err.get("code")
            msg = (err.get("message") or "").lower()
            if code == 429 or "rate limit" in msg or "rate-limit" in msg:
                raise ACPRateLimitError(err, method=method)
            raise ACPError(err, method=method)
        return resp.get("result", {})

    def _drain_notifications(self) -> list[dict]:
        with self._notif_lock:
            notifs = self._notifications[:]
            self._notifications.clear()
        return notifs

    def _initialize(self):
        self.call("initialize", {
            "protocolVersion": 1,
            "clientInfo": {"name": "codebuddy-mcp-server", "version": "0.3.13"},
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

    # ── 0.4.0 async submit/poll API ───────────────────────────────────
    def submit_prompt_async(self, text, model=None, append_system_prompt=None,
                            include_thinking=False) -> dict:
        """Submit a codebuddy call; return immediately with a task record.

        The actual codebuddy subprocess work happens in a daemon thread.
        Caller fetches the result later via get_result(task_id). This is
        what makes every MCP request millisecond-scale: even a 30-minute
        codebuddy call returns from submit_prompt_async in <50ms.

        Single in-flight: if another task is already running on this
        session, return a "busy" record (caller can wait for the in-flight
        task via get_result on its own task_id, then submit again).
        """
        with self._task_lock:
            if self._inflight is not None:
                return {"status": "busy",
                        "error": f"another task is in-flight ({self._inflight.task_id}); "
                                 f"call get_result on it first"}
            task_id = "tsk_" + uuid.uuid4().hex[:12]
            now = datetime.now(timezone.utc).isoformat()
            rec = TaskRecord(
                task_id=task_id, status="running",
                submitted_at=now,
                text_preview=text[:80] + ("..." if len(text) > 80 else ""),
                model=model or self.last_model,
            )
            self._inflight = rec
        # Persist + spawn thread OUTSIDE the lock so _run_prompt_in_thread
        # can re-acquire it without deadlocking on a slow codebuddy call.
        _save_task(rec)
        self._task_event.clear()
        thread = threading.Thread(
            target=self._run_prompt_in_thread,
            args=(task_id, text, model, append_system_prompt, include_thinking),
            daemon=True, name=f"cb-task-{task_id}",
        )
        thread.start()
        return {"task_id": task_id, "status": "running",
                "submitted_at": rec.submitted_at,
                "model": rec.model}

    def _run_prompt_in_thread(self, task_id, text, model, append_system_prompt,
                              include_thinking) -> None:
        """Background worker: runs the actual codebuddy session/prompt,
        collects streaming chunks, persists the result. Mutates self state
        under _task_lock and the codebuddy JSON-RPC state under _lock.

        Cancellation: cancel_task(task_id) clears _inflight and marks the
        record as cancelled. When this thread finishes, it checks whether
        it's still the active in-flight task; if not, it persists the
        result to disk with status='cancelled' (NOT 'done') and does NOT
        touch _inflight or _tasks_done — a new in-flight task is already
        in flight and must not be overwritten.
        """
        # Snapshot our own record at the start. If we get cancelled, the
        # wrapper's _inflight will be replaced by a new task (or None);
        # we never touch fields we don't own.
        with self._task_lock:
            rec = self._inflight
            if rec is None or rec.task_id != task_id:
                return
        t0 = time.time()
        try:
            # Serialize codebuddy I/O behind the existing _lock (the
            # JSON-RPC socket and any model-switch subprocess work both
            # need exclusive access).
            with self._lock:
                if append_system_prompt and append_system_prompt != self._appended_text:
                    self._respawn(append_text=append_system_prompt)
                    self._appended_text = append_system_prompt
                if model and (not self.last_model or model != self.last_model):
                    self._switch_model(model)
                try:
                    r = self.call("session/prompt", {
                        "sessionId": self.session_id,
                        "prompt": [{"type": "text", "text": text}],
                    }, timeout=self.timeout)
                except ACPRateLimitError as e:
                    # 429 from codebuddy / model API. The prompt may have
                    # been processed and billed before the rate-limit kicked
                    # in — auto-retry would double-bill. Surface structured
                    # info to the caller; do NOT spin up a fresh session and
                    # resend. Caller is expected to switch model or wait.
                    _log_line("prompt_rate_limited", task_id=task_id,
                              model=model or self.last_model,
                              code=e.code, msg=e.message)
                    raise
                except ACPError:
                    # Non-429 ACP error (model API 4xx/5xx, invalid params,
                    # etc.). The prompt was almost certainly delivered and
                    # the model API processed it. Auto-retry would either
                    # be a duplicate (double-billed) or hit the same error.
                    # Surface to the caller; do not retry.
                    raise
                except Exception:
                    # Connection-level / unexpected failure. The retry
                    # decision is: did the subprocess actually die? If yes,
                    # the prompt could not have been processed (the proc
                    # is what holds the connection to the model API), so
                    # a fresh session on a fresh subprocess is safe. If the
                    # proc is alive but the call failed (timeout, broken
                    # pipe recovery that swallowed, etc.), the prompt may
                    # have been sent — surface as error, do NOT retry.
                    proc_dead = (self.proc is not None
                                 and self.proc.poll() is not None)
                    if proc_dead:
                        _log_line("prompt_retry_after_subprocess_death",
                                  task_id=task_id)
                        # Subprocess died mid-call. Spin up a fresh
                        # ACP session inside a fresh subprocess.
                        self._respawn(append_text=self._appended_text,
                                      model=self.last_model)
                        r = self.call("session/prompt", {
                            "sessionId": self.session_id,
                            "prompt": [{"type": "text", "text": text}],
                        }, timeout=self.timeout)
                    else:
                        # Subprocess alive; the call may have delivered the
                        # prompt. Don't risk double-billing — surface.
                        _log_line("prompt_error_no_retry_proc_alive",
                                  task_id=task_id)
                        raise
            duration = time.time() - t0
            notifications = self._drain_notifications()
            result = _collect_response_artifacts(
                r, notifications, include_thinking, self.last_model,
            )
            result["duration_s"] = round(duration, 2)
            result["cb_pid"] = self.pid
            # Update session-level stats and finalize record under _task_lock.
            with self._task_lock:
                # Cancellation check: are we still the active in-flight task?
                was_cancelled = (self._inflight is not rec)
                used = result.get("model") or self.last_model
                if used and not was_cancelled:
                    self.last_model = used
                self.call_count += 1
                self.last_call_at = t0
                cache_pct = result["cache_ratio"]
                if not was_cancelled:
                    self.last_cache_ratio = cache_pct
                pt = (result["usage"] or {}).get("prompt_tokens", 0)
                ct = (result["usage"] or {}).get("completion_tokens", 0)
                cached = (result["usage"] or {}).get("prompt_tokens_details", {}).get("cached_tokens", 0)
                self.totals["prompt_tokens"] += pt
                self.totals["completion_tokens"] += ct
                self.totals["cached_tokens"] += cached
                # Finalize the record.
                if was_cancelled:
                    # Cancelled: persist result with cancelled status, do
                    # NOT touch _inflight or _tasks_done. The user already
                    # accepted that this call is being thrown away.
                    rec.status = "cancelled"
                    rec.completed_at = datetime.now(timezone.utc).isoformat()
                    rec.result = result
                    rec.duration_s = result["duration_s"]
                    rec.model = used or rec.model
                else:
                    rec.status = "done"
                    rec.completed_at = datetime.now(timezone.utc).isoformat()
                    rec.result = result
                    rec.duration_s = result["duration_s"]
                    rec.model = self.last_model
                    # Audit-log entry (also visible via list_tasks)
                    self._tasks.append({
                        "idx": self.call_count, "ts": t0,
                        "text_preview": rec.text_preview,
                        "model": self.last_model, "duration_s": result["duration_s"],
                        "prompt_tokens": pt, "completion_tokens": ct,
                        "cached_tokens": cached, "cache_ratio": cache_pct,
                        "stop_reason": result["stop_reason"],
                    })
                    # Move to done ring; clear inflight
                    self._tasks_done.append(rec)
                    self._inflight = None
            # Persist + log + wake AFTER releasing the lock (don't hold
            # _task_lock across IO or event set).
            _save_task(rec)
            if was_cancelled:
                _log_line("prompt_cancelled", task_id=task_id, dur_s=round(duration, 2),
                          model=used)
            else:
                _log_line("prompt", task_id=task_id, call_id=self.call_count,
                          model=self.last_model, dur_s=result["duration_s"],
                          pt=pt, ct=ct, cached=cached, cache_pct=cache_pct,
                          stop=result["stop_reason"])
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            with self._task_lock:
                was_cancelled = (self._inflight is not rec)
                rec.status = "cancelled" if was_cancelled else "error"
                rec.error = err
                rec.completed_at = datetime.now(timezone.utc).isoformat()
                rec.duration_s = round(time.time() - t0, 2)
                if not was_cancelled:
                    self._tasks_done.append(rec)
                    self._inflight = None
            _save_task(rec)
            _log_line("prompt_error" if not was_cancelled else "prompt_cancelled",
                      task_id=task_id, err=err)
        finally:
            # Wake any get_result waiter. The event is sticky (set until
            # cleared on next submit) so callers that re-check after the
            # first set see "done" rather than blocking forever.
            self._task_event.set()

    def cancel_task(self, task_id: str, force: bool = False) -> dict:
        """Cancel an in-flight or recent task. Frees the wrapper to accept
        a new submit. The actual codebuddy subprocess call (if in-flight)
        continues running but its result is discarded when the daemon
        thread finishes — it gets persisted with status='cancelled' and
        does not enter _tasks_done. This avoids the race where a freshly-
        spawned in-flight task gets overwritten by a late result from the
        cancelled one.

        `force=True` (0.4.2+): when the cancelled task was in-flight, ALSO
        kill the codebuddy subprocess (SIGKILL). Use this as the
        last-resort recovery when the daemon thread is truly stuck (e.g.,
        the model API itself is hung and a normal cancel can't recover
        because the daemon thread is blocked waiting for codebuddy's
        JSON-RPC response). The kill unblocks the daemon thread via a
        broken-pipe error; the next call to get_session() detects
        `proc.poll() is not None` and respawns a fresh codebuddy
        subprocess (this is the existing _session-recovery code path).
        Loses the in-flight task and codebuddy's sessionId; the
        conversation history is gone.

        Idempotent: cancelling an already-cancelled/done task is a no-op
        (returns the existing record).
        """
        was_inflight = False
        with self._task_lock:
            # Check in-memory first
            inflight = self._inflight
            if inflight is not None and inflight.task_id == task_id:
                # Currently in-flight. Mark cancelled, clear _inflight.
                inflight.status = "cancelled"
                inflight.completed_at = datetime.now(timezone.utc).isoformat()
                inflight.error = ("cancelled by user (force: codebuddy killed)"
                                   if force else "cancelled by user")
                cancelled = inflight
                was_inflight = True
                self._inflight = None
            else:
                # Maybe in _tasks_done (recent)
                cancelled = None
                for d in self._tasks_done:
                    if d.task_id == task_id:
                        d.status = "cancelled"
                        d.completed_at = datetime.now(timezone.utc).isoformat()
                        d.error = "cancelled by user"
                        cancelled = d
                        break
        if cancelled is None:
            # Check disk (wrapper restart case or never-seen task_id)
            disk = _load_task(task_id)
            if disk is None:
                return {"task_id": task_id, "status": "unknown",
                        "error": "no such task_id"}
            disk.status = "cancelled"
            disk.completed_at = datetime.now(timezone.utc).isoformat()
            disk.error = "cancelled by user"
            _save_task(disk)
            _log_line("task_cancelled", task_id=task_id, was_inflight=False, force=force)
            return {"task_id": task_id, "status": "cancelled",
                    "completed_at": disk.completed_at}
        # If force=True and we actually cleared the in-flight task, also
        # kill the codebuddy subprocess. The daemon thread, currently
        # blocked in self.call("session/prompt", ...), will get a
        # broken-pipe error; the except clause in _run_prompt_in_thread
        # catches it and writes the task to disk as cancelled/errored.
        force_killed = False
        if force and was_inflight:
            try:
                if self.proc and self.proc.poll() is None:
                    self.proc.kill()
                    force_killed = True
            except Exception as e:
                _log_line("codebuddy_force_kill_failed", task_id=task_id, err=str(e))
            else:
                if force_killed:
                    _log_line("codebuddy_force_killed", task_id=task_id)
        # Persist + log
        _save_task(cancelled)
        _log_line("task_cancelled", task_id=task_id, was_inflight=was_inflight,
                  force=force, force_killed=force_killed)
        # Wake any get_result pollster
        self._task_event.set()
        return {"task_id": task_id, "status": "cancelled",
                "completed_at": cancelled.completed_at,
                "force_killed": force_killed}

    def kill_codebuddy(self) -> dict:
        """Force-kill the codebuddy subprocess unconditionally. Use this
        as the absolute last-resort recovery when the wrapper is stuck
        and even cancel_task(force=True) doesn't help (e.g., the
        subprocess is in a bad state but no in-flight task to cancel).

        The next call to get_session() detects `proc.poll() is not None`
        and respawns a fresh codebuddy subprocess. The current
        `sessionId` and conversation history are lost.

        If there is an in-flight task, it's marked as cancelled (with
        a "codebuddy killed" error message) so the daemon thread can
        clean up when its call() gets the broken-pipe error.
        """
        killed = False
        with self._task_lock:
            inflight = self._inflight
            if inflight is not None:
                # Mark cancelled so the daemon thread's finalize path
                # writes the right status to disk when it gets the
                # broken-pipe error.
                inflight.status = "cancelled"
                inflight.completed_at = datetime.now(timezone.utc).isoformat()
                inflight.error = "codebuddy subprocess killed (kill_codebuddy)"
                self._inflight = None
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.kill()
                killed = True
        except Exception as e:
            _log_line("codebuddy_kill_failed", err=str(e))
            return {"status": "error", "error": f"kill failed: {e}"}
        if killed:
            _log_line("codebuddy_killed", reason="kill_codebuddy tool",
                      had_inflight=(inflight is not None))
            if inflight is not None:
                _save_task(inflight)
        return {"status": "killed", "codebuddy_was_running": killed,
                "had_inflight_task": inflight is not None,
                "respawn_on_next_call": True}

    def get_result(self, task_id: str, wait_timeout_s: int = 0,
                   mode: str = "poll") -> dict:
        """Fetch the result of a previously submitted task. ALWAYS POLL:
        returns the current state immediately, never blocks the MCP
        request. Caller should poll repeatedly (every few seconds) until
        the status is 'done' / 'error' / 'cancelled' / 'stale' / 'unknown'.

        Why no blocking mode: mcode's MCP client enforces a per-request
        timeout on every MCP call. A blocking get_result would hold the
        MCP request for the codebuddy call duration and be killed by the
        client timeout — same failure mode as the legacy sync `prompt`
        tool. The `run` tool handles the wait+retrieve case via an
        internal short-poll loop bounded to ~30s; for longer calls, poll
        get_result separately from the caller.

        `wait_timeout_s` and `mode` are accepted for API compatibility
        but ignored; passing 'blocking' raises ValueError so callers that
        relied on the old behavior fail loudly.
        """
        if mode not in ("poll", "blocking"):
            raise ValueError(f"get_result: invalid mode {mode!r}; only 'poll' is supported")
        if mode == "blocking":
            raise ValueError(
                "get_result blocking mode was removed in 0.4.1 — it held the MCP "
                "request and hit the client's per-request timeout, the same failure "
                "mode as the legacy sync `prompt` tool. Use mode='poll' and call "
                "get_result repeatedly, or use the `run` tool which does an internal "
                "short-poll loop bounded to ~30s. The `run` tool's MCP request lifetime "
                "is bounded so it survives the client's per-request timeout."
            )
        # 1. Look in done ring (in-memory)
        with self._task_lock:
            for d in reversed(self._tasks_done):
                if d.task_id == task_id:
                    return {"task_id": task_id, "status": d.status,
                            "result": d.result, "error": d.error,
                            "completed_at": d.completed_at}
            # 2. Look at in-flight
            inflight = self._inflight
        if inflight is not None and inflight.task_id == task_id:
            return {"task_id": task_id, "status": "running",
                    "submitted_at": inflight.submitted_at,
                    "model": inflight.model,
                    "elapsed_s": _safe_elapsed(inflight.submitted_at)}
        # 3. Not in-memory: try disk (covers wrapper-restart recovery)
        disk = _load_task(task_id)
        if disk is not None:
            return {"task_id": task_id, "status": disk.status,
                    "result": disk.result, "error": disk.error,
                    "completed_at": disk.completed_at}
        return {"task_id": task_id, "status": "unknown",
                "error": "no such task_id (may have been GC'd or never existed)"}

    # ── 0.4.0: legacy sync `prompt()` method deleted. ──
    # Callers must use submit_prompt_async + get_result (or the `run`
    # convenience tool which does both). Each MCP request is now
    # millisecond-scale; per-request client timeouts no longer matter.

    def status(self) -> dict:
        alive = self.proc.poll() is None
        with self._task_lock:
            inflight = self._inflight
        # Gap 4 (0.4.1): when no completed call has populated last_model
        # yet but an in-flight task is running, expose the in-flight
        # task's model as a fallback. The wrapper's "primary" model
        # surface is now: last completed model, else in-flight model,
        # else None.
        effective_model = self.last_model or (inflight.model if inflight else None)
        out = {
            "alive": alive, "codebuddy_pid": self.pid if alive else None,
            "acp_session_id": self.session_id, "model": effective_model,
            "started_at": self.started_at, "uptime_s": round(time.time() - self.started_at, 1),
            "call_count": self.call_count, "last_call_at": self.last_call_at,
            "last_cache_ratio": self.last_cache_ratio, "totals": dict(self.totals),
        }
        # 0.4.0 in-flight surface: the async API uses these for polling.
        if inflight is not None:
            out["inflight_task_id"] = inflight.task_id
            out["inflight_submitted_at"] = inflight.submitted_at
            out["inflight_model"] = inflight.model
            out["inflight_elapsed_s"] = _safe_elapsed(inflight.submitted_at)
        return out

    def list_tasks(self, limit: int = 10) -> list[dict]:
        if limit <= 0: return []
        # Input schema says "max 50" — the deque's maxlen=50 also enforces
        # this implicitly, but the explicit clamp here makes the doc claim
        # match the code (NOTE #7).
        if limit > 50: limit = 50
        # 0.4.0: merge the in-memory audit-log deque (most recent
        # completed) with the in-flight task (if any). Disk-persisted
        # tasks from previous wrapper processes are not surfaced here —
        # callers wanting history use get_result with a known task_id.
        items = list(self._tasks)[-limit:]
        items.reverse()
        with self._task_lock:
            inflight = self._inflight
        if inflight is not None:
            inflight_entry = {
                "idx": None,  # not yet counted in call_count
                "task_id": inflight.task_id,
                "ts": inflight.submitted_at,
                "text_preview": inflight.text_preview,
                "model": inflight.model,
                "status": "running",
                "duration_s": _safe_elapsed(inflight.submitted_at),
            }
            items = [inflight_entry] + items
            items = items[:limit]
        return items

    def close(self):
        """Cleanly shut down the codebuddy subprocess. Idempotent: safe to
        call from both a signal handler (e.g. SIGTERM) and the main
        `finally` block. The first call terminates + waits; subsequent
        calls are no-ops. Logs the outcome for diagnostics.
        """
        if getattr(self, "_closed", False):
            return
        self._closed = True
        reason = "ok"
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # Graceful terminate didn't take. Force kill.
                try:
                    self.proc.kill()
                except Exception:
                    pass
                reason = "terminate_timeout_force_kill"
        except Exception as e:
            try:
                self.proc.kill()
            except Exception:
                pass
            reason = f"terminate_failed:{type(e).__name__}"
        try:
            _log_line("wrapper_shutdown", pid=self.pid, reason=reason)
        except Exception:
            pass


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
    global _session, _models_cache
    with _session_lock:
        if _session is None or _session.proc.poll() is not None:
            cwd = os.environ.get("CODEBUDDY_MCP_CWD") or os.getcwd()
            cb_bin = os.environ.get("CODEBUDDY_BIN") or "codebuddy"
            base = os.environ.get("MCODE_BASE_PROMPT_FILE")
            # Fail-fast: verify reachability before spawning the long-lived
            # subprocess. This is the only time we pay the 0.5-1s cost.
            _health_check_codebuddy(cb_bin)
            _session = ACPSession(codebuddy_bin=cb_bin, cwd=cwd, mcode_base_prompt_file=base)
            # Gap 3 (0.4.1): a fresh session may have a richer
            # `models.availableModels` catalog than the cached
            # `codebuddy --help` parse. Invalidate so the next list_models
            # call picks up the live session data.
            with _models_cache_lock:
                _models_cache = None
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
        "timeout": {"type": "integer", "description": "Per-call timeout in seconds (default 3600). Leave unset for any task that could plausibly take more than a minute."},
    }


def _submit_props() -> dict:
    """inputSchema for the submit-only tools. No `timeout` here: submit
    returns in milliseconds; the wait happens on get_result."""
    return {
        "text": {"type": "string", "description": "The prompt / task description to send to codebuddy."},
        "model": {"type": "string",
                  "description": "Optional codebuddy model id (e.g. 'hy3', 'deepseek-v4-flash'). Use `list_models` to enumerate valid ids. Switching models is dynamic via `session/set_config_option` (preserves session_id, cache, and turn history) — no subprocess restart in the normal path."},
        "append_system_prompt": {"type": "string",
                                 "description": "Optional business rules / context appended to the mcode base system prompt. First call applies; subsequent changes trigger a subprocess respawn so the new append takes effect."},
        "include_thinking": {"type": "boolean",
                             "description": "If true, include the model's reasoning trace (`agent_thought_chunk` stream) in the response. Off by default — a long task can produce hundreds of thought chunks. Default false."},
    }


# 0.4.0 async submit/poll API — 7 tools total. The submit tools
# return immediately with a task_id; get_result fetches the result.
# `run` is the convenience wrapper for the common case (submit + blocking
# get_result, in one call).

TOOL_SUBMIT_PROMPT = Tool(
    name="submit_prompt",
    description=("Submit a codebuddy call and return immediately with a task_id (milliseconds). The actual codebuddy subprocess work runs in a background thread; fetch the result later via `get_result`. Use this when you want to dispatch a long-running codebuddy call without holding the MCP request open (which is what the 0.4.0 async API is for: the MCP client has a per-request timeout, so a synchronous codebuddy call that runs longer than the client's limit is silently dropped)."),
    inputSchema={"type": "object", "properties": _submit_props(), "required": ["text"]},
)
TOOL_SUBMIT_CONTINUE = Tool(
    name="submit_continue",
    description=("Continue a previously submitted codebuddy conversation with a follow-up message. Functionally identical to `submit_prompt` — codebuddy keeps server-side history by sessionId so subsequent calls are auto-continued — but exposed as a distinct tool so the caller's intent is explicit in the MCP trace. Returns a task_id immediately."),
    inputSchema={"type": "object", "properties": _submit_props(), "required": ["text"]},
)
TOOL_GET_RESULT = Tool(
    name="get_result",
    description=("Fetch the result of a previously submitted task by task_id. Always returns immediately (millisecond-scale MCP request). Caller should poll repeatedly (every 2-3s) until the returned status is 'done' / 'error' / 'cancelled' / 'stale' / 'unknown'. The `mode` parameter is accepted for API compatibility but only 'poll' is supported — passing 'blocking' raises an error. For a single-call submit+wait flow that respects the MCP client's per-request timeout, use the `run` tool which does an internal short-poll loop (≤30s) instead."),
    inputSchema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The task_id returned by submit_prompt or submit_continue."},
            "wait_timeout_s": {"type": "integer", "default": 0,
                               "description": "DEPRECATED: ignored. get_result is now always poll-mode (instant return)."},
            "mode": {"type": "string", "default": "poll", "enum": ["poll"],
                     "description": "Only 'poll' is supported. Passing 'blocking' raises an error."},
        },
        "required": ["task_id"],
    },
)
TOOL_RUN = Tool(
    name="run",
    description=("Convenience tool: submit a codebuddy call and wait up to 30 seconds (or wait_timeout_s, whichever is smaller) for it to finish, polling every 2s. If the call finishes within the window, returns the formatted result. If still running, returns {status: 'running', task_id, submitted_at, model, elapsed_s} — the caller should then call get_result(task_id) to keep polling. Use this from a worker. The MCP request lifetime is bounded to 30s (or wait_timeout_s), so it survives the client's per-request timeout. For calls expected to take >30s, use submit_prompt + repeated get_result calls instead."),
    inputSchema={
        "type": "object",
        "properties": {
            **{
                k: v for k, v in _submit_props().items()
                # submit-only fields (no `timeout` on submit; get_result owns waiting)
            },
            "wait_timeout_s": {"type": "integer", "default": 3600,
                               "description": "Upper bound on how long the internal poll loop will wait. The actual MCP request lifetime is min(wait_timeout_s, 30). default 3600 (1h) — the wrapper is consistent with the 1h-default plugin convention, but the run tool itself only blocks the MCP request for ≤30s."},
        },
        "required": ["text"],
    },
)
TOOL_STATUS = Tool(
    name="status",
    description=("Return the current wrapper + codebuddy subprocess state: liveness, pid, ACP session id, model, uptime, call_count, last_call timestamp, last cache_ratio, cumulative token totals, in-flight task_id (if any). No side effects. Use for diagnostics or to confirm a long-running wrapper is still healthy before the next call."),
    inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
)
TOOL_LIST_TASKS = Tool(
    name="list_tasks",
    description=("Return the most recent N call metadata records (most recent first), plus the current in-flight task (if any). Each record has: idx, ts, text_preview, model, duration_s, prompt_tokens, completion_tokens, cached_tokens, cache_ratio, stop_reason. Use to inspect what the wrapper has done in this mcode session without grepping the log file."),
    inputSchema={"type": "object", "properties": {"limit": {"type": "integer", "description": "Max records to return (default 10, max 50)."}}, "additionalProperties": False},
)
TOOL_LIST_MODELS = Tool(
    name="list_models",
    description=("List codebuddy's supported model IDs. Reads from the live ACP session's `models.availableModels` (rich: per-model credits / maxInputTokens / supportsReasoning); falls back to parsing `codebuddy --help` only if no session is live yet. Use this before passing a `model=` argument to verify the model id is valid (e.g. `deepseek-v4-flash`, `hy3`). Returns `{ok, models: [id, ...], count, source}` plus a `rich` array with per-model metadata on success; `{ok: false, error, ...}` on failure. Cached per process."),
    inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
)
TOOL_CANCEL_TASK = Tool(
    name="cancel_task",
    description=("Cancel an in-flight or recent task. Use this when a codebuddy call is hung (model API issue, codebuddy CLI bug, network) and the wrapper is stuck on a single in-flight task. Marks the task as cancelled and frees the wrapper to accept a new submit. The actual codebuddy subprocess call continues running but its result is discarded when the daemon thread finishes — it gets persisted with status='cancelled' and does not enter list_tasks / get_result's in-memory results. Idempotent: cancelling an already-cancelled/done task returns the existing record. **Set `force=true` to ALSO kill the codebuddy subprocess** (SIGKILL): use this when the daemon thread is truly stuck on a hung model API. The next call respawns a fresh codebuddy subprocess (loses the current sessionId and conversation history)."),
    inputSchema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The task_id returned by submit_prompt / submit_continue, or observed in status / list_tasks as the in-flight task."},
            "force": {"type": "boolean", "default": False,
                      "description": "If true, also SIGKILL the codebuddy subprocess in addition to marking the task as cancelled. Use this as the last-resort recovery when the daemon thread is stuck. The next call respawns a fresh codebuddy subprocess. Loses the current sessionId and conversation history."},
        },
        "required": ["task_id"],
    },
)
TOOL_KILL_CODEBUDDY = Tool(
    name="kill_codebuddy",
    description=("Force-kill the codebuddy subprocess unconditionally. Absolute last-resort recovery when the wrapper is stuck and even cancel_task(force=true) doesn't help (e.g., subprocess is in a bad state but there's no specific in-flight task to cancel, or the daemon thread is stuck on a non-cancel-related wait). The next call to get_session() detects the dead proc and respawns a fresh codebuddy subprocess. Loses the current sessionId and conversation history. If there is an in-flight task, it's marked as cancelled with a 'codebuddy killed' error message."),
    inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
)

ALL_TOOLS = [TOOL_SUBMIT_PROMPT, TOOL_SUBMIT_CONTINUE, TOOL_GET_RESULT, TOOL_RUN,
             TOOL_CANCEL_TASK, TOOL_KILL_CODEBUDDY, TOOL_STATUS, TOOL_LIST_TASKS,
             TOOL_LIST_MODELS]


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
    # 0.4.0 async API: submit returns immediately, get_result polls,
    # run is the convenience wrapper. Each MCP request is millisecond-scale.
    if name in ("submit_prompt", "submit_continue"):
        text = arguments.get("text")
        if not text:
            raise ValueError(f"{name}: missing required arg: text")
        sess = get_session()
        rec = sess.submit_prompt_async(
            text=text, model=arguments.get("model"),
            append_system_prompt=arguments.get("append_system_prompt"),
            include_thinking=bool(arguments.get("include_thinking", False)),
        )
        return CallToolResult(content=[TextContent(type="text",
            text=json.dumps(rec, ensure_ascii=False))])
    if name == "get_result":
        task_id = arguments.get("task_id")
        if not task_id:
            raise ValueError("get_result: missing required arg: task_id")
        sess = get_session()
        wait = int(arguments.get("wait_timeout_s", 3600))
        mode = arguments.get("mode", "blocking")
        if mode not in ("blocking", "poll"):
            raise ValueError(f"get_result: invalid mode {mode!r}; must be 'blocking' or 'poll'")
        result = sess.get_result(task_id, wait_timeout_s=wait, mode=mode)
        return CallToolResult(content=[TextContent(type="text",
            text=json.dumps(result, ensure_ascii=False))])
    if name == "run":
        text = arguments.get("text")
        if not text:
            raise ValueError("run: missing required arg: text")
        sess = get_session()
        sub = sess.submit_prompt_async(
            text=text, model=arguments.get("model"),
            append_system_prompt=arguments.get("append_system_prompt"),
            include_thinking=bool(arguments.get("include_thinking", False)),
        )
        if sub.get("status") == "busy":
            return CallToolResult(content=[TextContent(type="text",
                text=json.dumps(sub, ensure_ascii=False))])
        task_id = sub["task_id"]
        # Gap 1 (0.4.1): run does submit + internal short-poll loop. The
        # MCP request lifetime is bounded to RUN_POLL_BUDGET_S so the
        # MCP client's per-request timeout doesn't kill us mid-call. Each
        # iteration does one millisecond-scale get_result (poll mode).
        # If the codebuddy call finishes within the budget, we return
        # the result; otherwise we return the current "running" state and
        # the caller can continue polling with get_result separately.
        RUN_POLL_BUDGET_S = 30
        wait = int(arguments.get("wait_timeout_s", 3600))
        budget = min(wait, RUN_POLL_BUDGET_S)
        deadline = time.monotonic() + budget
        result = None
        while True:
            result = sess.get_result(task_id)
            if result.get("status") != "running":
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(2.0)
        # Final result: either done/error/cancelled/stale/unknown (the
        # loop exited because the call completed) or running (budget
        # exhausted). Format like the old sync result for done.
        if result.get("status") == "done" and result.get("result"):
            return CallToolResult(content=[TextContent(type="text",
                text=_format_result(result["result"]))])
        # Anything else: return the structured result so the caller sees
        # what happened (or "running" with task_id so they can continue
        # polling with get_result).
        return CallToolResult(content=[TextContent(type="text",
            text=json.dumps(result, ensure_ascii=False))])
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
    if name == "cancel_task":
        task_id = arguments.get("task_id")
        if not task_id:
            raise ValueError("cancel_task: missing required arg: task_id")
        sess = get_session()
        force = bool(arguments.get("force", False))
        result = sess.cancel_task(task_id, force=force)
        return CallToolResult(content=[TextContent(type="text",
            text=json.dumps(result, ensure_ascii=False))])
    if name == "kill_codebuddy":
        sess = get_session()
        result = sess.kill_codebuddy()
        return CallToolResult(content=[TextContent(type="text",
            text=json.dumps(result, ensure_ascii=False))])
    raise ValueError(f"unknown tool: {name}")


app = Server("codebuddy", on_list_tools=_list_tools, on_call_tool=_call_tool)


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


# Module-level flag used by _install_signal_handlers: a second signal
# during in-progress cleanup forces immediate os._exit so a stuck
# codebuddy terminate can't hang the wrapper indefinitely.
_signal_force_exit = False


def _install_signal_handlers():
    """Install SIGTERM / SIGHUP / SIGINT handlers that cleanly close the
    codebuddy subprocess before exiting. Without this, default signal
    behavior (immediate exit) leaves the long-lived `codebuddy --acp`
    subprocess orphaned whenever mcode shuts down, the controlling
    terminal closes, or the wrapper is `kill`-ed (graceful).

    Best-effort: `signal.signal` can fail in non-main threads or
    restricted environments. We log and continue without handlers in
    that case — the implicit stdio-EOF path in `main()` will eventually
    close the subprocess, just with a small delay.

    Idempotent: a second signal during close() forces immediate exit
    (`os._exit(1)`) so a stuck `codebuddy` terminate can't hang the
    wrapper forever.
    """
    def _handler(signum, _frame):
        global _signal_force_exit
        if _signal_force_exit:
            os._exit(1)
        _signal_force_exit = True
        try:
            sess = _session
            if sess is not None:
                sess.close()
        except Exception:
            pass
        # Skip Python atexit / finally cleanup; we've already closed
        # the subprocess and the idempotent guard prevents double-close.
        os._exit(0)

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError) as e:
            # Non-main thread or restricted env; log and continue.
            try:
                _log_line("signal_handler_install_failed", sig=int(sig), err=str(e))
            except Exception:
                pass


if __name__ == "__main__":
    # 0.4.0: at startup, mark any "running" task files as "stale" — they
    # were left in-flight by a previous wrapper process that died. GC also
    # drops completed task files older than TASK_LIFETIME_S to keep the
    # dir bounded. This is best-effort: a read-only install silently no-ops.
    try:
        _gc_orphan_tasks()
    except Exception:
        pass
    # 0.4.3: install SIGTERM/SIGHUP/SIGINT handlers BEFORE asyncio.run so
    # the long-lived `codebuddy --acp` subprocess is closed cleanly on
    # session exit. Without this, default signal action leaves the
    # subprocess orphaned. Idempotent: a second signal forces exit.
    _install_signal_handlers()
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
    finally:
        if _session: _session.close()
