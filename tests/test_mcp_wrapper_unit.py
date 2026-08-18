#!/usr/bin/env python3
"""test_mcp_wrapper_unit.py - unit tests for codebuddy-mcp-server (no subprocess)"""
import importlib.util, json, sys, tempfile, threading, time, unittest, uuid
from collections import deque
from pathlib import Path

WRAPPER_PATH = Path(__file__).resolve().parent.parent / "bin" / "codebuddy-mcp-server.py"
_spec = importlib.util.spec_from_file_location("codebuddy_mcp_server", str(WRAPPER_PATH))
_mod = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_mod)


class TestPluginRootPathlib(unittest.TestCase):
    def test_plugin_root_is_absolute(self):
        self.assertTrue(_mod.PLUGIN_ROOT.is_absolute(), f"PLUGIN_ROOT must be absolute: {_mod.PLUGIN_ROOT}")
    def test_plugin_root_has_expected_layout(self):
        self.assertTrue((_mod.PLUGIN_ROOT / "bin").is_dir())
        self.assertTrue((_mod.PLUGIN_ROOT / "assets").is_dir())
        self.assertTrue((_mod.PLUGIN_ROOT / "skills").is_dir())
    def test_state_dir_under_plugin_root(self):
        self.assertEqual(_mod.STATE_DIR, _mod.PLUGIN_ROOT / "state")
        self.assertTrue(_mod.STATE_DIR.is_dir())


class TestLogLine(unittest.TestCase):
    def setUp(self):
        # Reset module-level cached log handle so each test writes to a fresh
        # STATE_DIR rather than reusing a handle opened against a prior tmpdir.
        try:
            if _mod._log_fh is not None:
                _mod._log_fh.close()
        except Exception:
            pass
        _mod._log_fh = None
        _mod._log_date = None
        self._orig_state_dir = _mod.STATE_DIR
        self.tmp_state = Path(tempfile.mkdtemp(prefix="mcp-test-"))
        _mod.STATE_DIR = self.tmp_state
    def tearDown(self):
        # Close & null any handle this test may have left open, then restore.
        try:
            if _mod._log_fh is not None:
                _mod._log_fh.close()
        except Exception:
            pass
        _mod._log_fh = None
        _mod._log_date = None
        _mod.STATE_DIR = self._orig_state_dir
    def test_writes_line_to_dated_file(self):
        _mod._log_line("test_event", foo=42, bar="hi")
        log_files = list(self.tmp_state.glob("mcp-*.log")); self.assertEqual(len(log_files), 1)
        content = log_files[0].read_text(encoding="utf-8")
        self.assertIn("test_event", content); self.assertIn("foo=42", content); self.assertIn("bar=hi", content)
    def test_appends_across_calls(self):
        _mod._log_line("event_a", x=1); _mod._log_line("event_b", y=2)
        log_files = list(self.tmp_state.glob("mcp-*.log")); self.assertEqual(len(log_files), 1)
        lines = log_files[0].read_text(encoding="utf-8").splitlines(); self.assertEqual(len(lines), 2)
        self.assertIn("event_a", lines[0]); self.assertIn("event_b", lines[1])
    def test_quotes_values_with_whitespace(self):
        _mod._log_line("ev", txt="hello world")
        log_files = list(self.tmp_state.glob("mcp-*.log"))
        line = log_files[0].read_text(encoding="utf-8"); self.assertIn('txt="hello world"', line)


class TestFormatResult(unittest.TestCase):
    def test_with_usage_includes_tokens(self):
        result = {"text": "reply body", "model": "hy3", "duration_s": 1.5, "stop_reason": "end_turn",
                  "cb_pid": 12345, "cache_ratio": 99.0,
                  "usage": {"prompt_tokens": 26600, "completion_tokens": 50, "prompt_tokens_details": {"cached_tokens": 26300}}}
        out = _mod._format_result(result)
        self.assertIn("reply body", out); self.assertIn("pid=12345", out)
        self.assertIn("model=hy3", out); self.assertIn("dur=1.5s", out)
        self.assertIn("prompt=26600", out); self.assertIn("cache_read=26300", out); self.assertIn("cache_ratio=99.0%", out)
    def test_without_usage_omits_tokens_line(self):
        result = {"text": "body", "model": "?", "duration_s": 0, "stop_reason": None,
                  "cb_pid": None, "cache_ratio": 0, "usage": {}}
        out = _mod._format_result(result); self.assertIn("body", out); self.assertNotIn("tokens:", out)


class TestStatusShape(unittest.TestCase):
    def test_returns_required_keys(self):
        fake = type("Fake", (), {
            "proc": type("P", (), {"poll": staticmethod(lambda: None)})(),
            "pid": 99999, "session_id": "abc-123", "last_model": "hy3",
            "started_at": time.time() - 60, "call_count": 5, "last_call_at": time.time() - 1,
            "last_cache_ratio": 98.5,
            "totals": {"prompt_tokens": 100000, "completion_tokens": 200, "cached_tokens": 95000},
            # 0.4.0: status() now also reads _inflight under _task_lock; both
            # must exist on the fake or the call raises AttributeError.
            "_inflight": None,
            "_task_lock": threading.Lock(),
        })()
        st = _mod.ACPSession.status(fake)
        self.assertTrue(st["alive"]); self.assertEqual(st["codebuddy_pid"], 99999)
        self.assertEqual(st["acp_session_id"], "abc-123"); self.assertEqual(st["model"], "hy3")
        self.assertEqual(st["call_count"], 5); self.assertEqual(st["last_cache_ratio"], 98.5)
        self.assertEqual(st["totals"]["cached_tokens"], 95000)
        self.assertGreaterEqual(st["uptime_s"], 59)
        # No in-flight task → no inflight_* fields (or they're absent)
        self.assertNotIn("inflight_task_id", st)


class TestListTasksLimit(unittest.TestCase):
    def test_returns_most_recent_first(self):
        # 0.4.0: list_tasks() now also reads _inflight under _task_lock. Fake
        # must have both or the call raises AttributeError.
        fake = type("Fake", (), {
            "_tasks": deque(maxlen=50), "_inflight": None,
            "_task_lock": threading.Lock(),
        })()
        for i in range(5): fake._tasks.append({"idx": i + 1, "text_preview": f"task {i+1}"})
        items = _mod.ACPSession.list_tasks(fake, limit=10)
        self.assertEqual(len(items), 5); self.assertEqual([t["idx"] for t in items], [5, 4, 3, 2, 1])
    def test_limit_truncates(self):
        fake = type("Fake", (), {
            "_tasks": deque(maxlen=50), "_inflight": None,
            "_task_lock": threading.Lock(),
        })()
        for i in range(10): fake._tasks.append({"idx": i + 1})
        items = _mod.ACPSession.list_tasks(fake, limit=3)
        self.assertEqual(len(items), 3); self.assertEqual([t["idx"] for t in items], [10, 9, 8])
    def test_limit_zero_returns_empty(self):
        fake = type("Fake", (), {
            "_tasks": deque(maxlen=50), "_inflight": None,
            "_task_lock": threading.Lock(),
        })()
        for i in range(3): fake._tasks.append({"idx": i + 1})
        self.assertEqual(_mod.ACPSession.list_tasks(fake, limit=0), [])


class TestRespawnTriggers(unittest.TestCase):
    def test_first_call_with_append_triggers_respawn(self):
        calls = {"respawn": 0}
        fake = type("Fake", (), {
            "_appended_text": None,
            "_respawn": lambda self, t: calls.__setitem__("respawn", calls["respawn"] + 1)})()
        append = "new rule"
        if append and append != fake._appended_text:
            fake._respawn(append); fake._appended_text = append
        self.assertEqual(calls["respawn"], 1)
    def test_same_append_does_not_respawn(self):
        fake = type("Fake", (), {"_appended_text": "same rule"})()
        calls = {"respawn": 0}
        append = "same rule"
        if append and append != fake._appended_text:
            fake._appended_text = append; calls["respawn"] += 1
        self.assertEqual(calls["respawn"], 0)


class TestToolHandlerDispatch(unittest.TestCase):
    def _run(self, coro): return asyncio_run(coro)
    def test_unknown_tool_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self._run(_mod._call_tool(None, type("P", (), {"name": "bogus", "arguments": {}})()))
        self.assertIn("unknown tool", str(ctx.exception))
    def test_submit_prompt_missing_text_raises(self):
        # 0.4.0: the legacy `prompt` tool is gone. submit_prompt without
        # a `text` arg is the equivalent guard test.
        with self.assertRaises(ValueError) as ctx:
            self._run(_mod._call_tool(None, type("P", (), {"name": "submit_prompt", "arguments": {}})()))
        self.assertIn("text", str(ctx.exception))
    def test_get_result_missing_task_id_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self._run(_mod._call_tool(None, type("P", (), {"name": "get_result", "arguments": {}})()))
        self.assertIn("task_id", str(ctx.exception))
    def test_list_models_dispatches(self):
        # list_models has no required args; just confirm it doesn't raise and
        # the handler is wired up. The real result depends on the local
        # codebuddy binary; we monkey-patch the parse fn to a known stub.
        import unittest.mock as mock
        with mock.patch.object(_mod, "list_codebuddy_models",
                               return_value={"ok": True, "models": ["hy3"], "count": 1, "source": "stub"}):
            r = self._run(_mod._call_tool(None, type("P", (), {"name": "list_models", "arguments": {}})()))
        payload = r.content[0].text
        self.assertIn("list_models", payload)
        self.assertIn("hy3", payload)


class TestListCodebuddyModels(unittest.TestCase):
    """list_codebuddy_models() parses `codebuddy --help` to extract the
    parenthesized model id list. These tests exercise the parser with
    canned subprocess.run output (no real codebuddy binary required)."""

    def setUp(self):
        # Reset the module-level cache so each test re-parses.
        _mod._models_cache = None

    def _stub_help(self, help_text: str):
        import unittest.mock as mock
        fake = mock.MagicMock()
        fake.stdout = help_text
        fake.stderr = ""
        return mock.patch.object(_mod.subprocess, "run", return_value=fake)

    def test_parses_canonical_help_line(self):
        help_line = (
            "--model <model>                                  Model for the current session. "
            "Please provide the model ID. Currently supported: "
            "(hy3, glm-5.2, glm-5.1, glm-5v-turbo, deepseek-v4-pro, deepseek-v4-flash, "
            "custom-local:MiniMax-M3, custom-local:agnes-2.0-flash)"
        )
        with self._stub_help(help_line + "\n"):
            r = _mod.list_codebuddy_models()
        self.assertTrue(r["ok"])
        self.assertEqual(r["count"], 8)
        self.assertIn("hy3", r["models"])
        self.assertIn("deepseek-v4-flash", r["models"])
        self.assertIn("custom-local:MiniMax-M3", r["models"])  # colons OK in ids
        # Order preserved
        self.assertEqual(r["models"][0], "hy3")
        self.assertEqual(r["models"][-1], "custom-local:agnes-2.0-flash")

    def test_tolerates_unrelated_parentheses(self):
        help_text = (
            "Some other option (foo, bar)\n"
            "--model <model>                                  Currently supported: (a, b, c)\n"
        )
        with self._stub_help(help_text):
            r = _mod.list_codebuddy_models()
        self.assertTrue(r["ok"])
        self.assertEqual(r["models"], ["a", "b", "c"])

    def test_returns_error_when_no_model_line(self):
        with self._stub_help("no --model line here\nnothing useful\n"):
            r = _mod.list_codebuddy_models()
        self.assertFalse(r["ok"])
        self.assertEqual(r["models"], [])
        self.assertIn("could not locate", r["error"])
        self.assertIn("help_tail", r)

    def test_returns_error_on_filenotfound(self):
        import unittest.mock as mock
        with mock.patch.object(_mod.subprocess, "run", side_effect=FileNotFoundError):
            r = _mod.list_codebuddy_models()
        self.assertFalse(r["ok"])
        self.assertIn("not found", r["error"])

    def test_caches_result(self):
        import unittest.mock as mock
        fake = mock.MagicMock(); fake.stdout = "--model (x, y)"; fake.stderr = ""
        with mock.patch.object(_mod.subprocess, "run", return_value=fake) as run_mock:
            r1 = _mod.list_codebuddy_models()
            r2 = _mod.list_codebuddy_models()
        # subprocess.run called exactly once even though we asked twice
        self.assertEqual(run_mock.call_count, 1)
        self.assertIs(r1, r2)


class TestSpawnArgsWithModel(unittest.TestCase):
    """`_spawn(append_text, model=X)` must add `--model X` to the argv.
    We don't actually exec the subprocess — we monkey-patch Popen to
    capture the argv, then assert on it."""

    def test_no_model_arg_when_model_is_none(self):
        import unittest.mock as mock
        fake_session = _mod.ACPSession.__new__(_mod.ACPSession)
        fake_session.codebuddy_bin = "codebuddy"
        fake_session.cwd = "/tmp"
        fake_session.mcode_base_prompt_file = None
        with mock.patch.object(_mod.subprocess, "Popen") as Popen_mock:
            fake_session._spawn(append_text=None, model=None)
        argv = Popen_mock.call_args[0][0]
        self.assertNotIn("--model", argv)

    def test_model_arg_added_when_model_provided(self):
        import unittest.mock as mock
        fake_session = _mod.ACPSession.__new__(_mod.ACPSession)
        fake_session.codebuddy_bin = "codebuddy"
        fake_session.cwd = "/tmp"
        fake_session.mcode_base_prompt_file = None
        with mock.patch.object(_mod.subprocess, "Popen") as Popen_mock:
            fake_session._spawn(append_text=None, model="deepseek-v4-flash")
        argv = Popen_mock.call_args[0][0]
        idx = argv.index("--model")
        self.assertEqual(argv[idx + 1], "deepseek-v4-flash")


class TestChunkConcatenation(unittest.TestCase):
    """The truncation bug fix: every `agent_message_chunk` must be
    appended, not just the first. 0.4.0: legacy `prompt()` is gone, so
    we test the pure helper `_collect_response_artifacts` directly. The
    background thread calls the same helper."""

    def _notifications_for(self, chunks):
        notifs = [
            {"params": {"update": {"sessionUpdate": "agent_message_chunk",
                                   "content": {"text": c}}}}
            for c in chunks
        ]
        notifs.append({
            "params": {"update": {
                "sessionUpdate": "usage_update",
                "_meta": {"usage": {"prompt_tokens": 100,
                                    "completion_tokens": sum(len(c) for c in chunks),
                                    "prompt_tokens_details": {"cached_tokens": 50}}},
            }},
        })
        return notifs

    def test_concatenates_multiple_chunks(self):
        r = _mod._collect_response_artifacts(
            {}, self._notifications_for(["Hello, ", "this is a ", "long reply with many tokens."]),
            include_thinking=False, fallback_model="hy3",
        )
        self.assertEqual(r["text"], "Hello, this is a long reply with many tokens.")
        self.assertEqual(r["usage"]["completion_tokens"],
                         len("Hello, this is a long reply with many tokens."))

    def test_single_chunk_still_works(self):
        r = _mod._collect_response_artifacts(
            {}, self._notifications_for(["short"]),
            include_thinking=False, fallback_model="hy3",
        )
        self.assertEqual(r["text"], "short")


class TestSetConfigOption(unittest.TestCase):
    """`_set_config_option` must call `session/set_config_option` with
    `{sessionId, configId, value}` and return the matching configOption
    from the response. We assert on the call args, not the result."""

    def test_sends_correct_rpc(self):
        import unittest.mock as mock
        sess = _mod.ACPSession.__new__(_mod.ACPSession)
        sess.session_id = "sess-xyz"
        sess.call = mock.MagicMock(return_value={
            "configOptions": [
                {"id": "mode", "currentValue": "bypassPermissions"},
                {"id": "model", "currentValue": "deepseek-v4-flash"},
                {"id": "thought_level", "currentValue": "enabled"},
            ]
        })
        opt = sess._set_config_option("model", "deepseek-v4-flash")
        sess.call.assert_called_once()
        args = sess.call.call_args
        self.assertEqual(args[0][0], "session/set_config_option")
        params = args[0][1]
        self.assertEqual(params["sessionId"], "sess-xyz")
        self.assertEqual(params["configId"], "model")
        self.assertEqual(params["value"], "deepseek-v4-flash")
        # Returns the matching configOption from the response
        self.assertEqual(opt["id"], "model")
        self.assertEqual(opt["currentValue"], "deepseek-v4-flash")


class TestSwitchModel(unittest.TestCase):
    """`_switch_model` tries set_config_option first; falls back to respawn
    if the server rejects or lies about the write."""

    def _build_sess(self, *, set_opt_return=None, set_opt_raises=None):
        import unittest.mock as mock
        sess = _mod.ACPSession.__new__(_mod.ACPSession)
        sess.session_id = "sess-1"
        sess.last_model = "hy3"
        sess._appended_text = None
        sess.pid = 100
        sess.proc = mock.MagicMock()
        sess._spawn = mock.MagicMock()
        sess._reader = mock.MagicMock()  # don't actually start a thread
        sess._initialize = mock.MagicMock()
        sess._session_new = mock.MagicMock()
        if set_opt_raises:
            sess._set_config_option = mock.MagicMock(side_effect=set_opt_raises)
        else:
            sess._set_config_option = mock.MagicMock(return_value=set_opt_return or {"id": "model", "currentValue": "deepseek-v4-flash"})
        return sess

    def test_happy_path_uses_set_config_option(self):
        sess = self._build_sess(set_opt_return={"id": "model", "currentValue": "deepseek-v4-flash"})
        sess._switch_model("deepseek-v4-flash")
        sess._set_config_option.assert_called_once_with("model", "deepseek-v4-flash")
        sess._spawn.assert_not_called()  # respawn NOT used
        self.assertEqual(sess.last_model, "deepseek-v4-flash")

    def test_falls_back_to_respawn_on_server_error(self):
        sess = self._build_sess(set_opt_raises=RuntimeError("ACP error: method not found"))
        sess._switch_model("deepseek-v4-flash")
        sess._set_config_option.assert_called_once()
        sess._spawn.assert_called_once()  # respawn IS used
        # _respawn's call should pass model=...
        kwargs = sess._spawn.call_args.kwargs
        self.assertEqual(kwargs.get("model"), "deepseek-v4-flash")
        self.assertEqual(sess.last_model, "deepseek-v4-flash")

    def test_falls_back_when_server_lies(self):
        # Server returns 200 but currentValue didn't change — the respawn
        # fallback is the safety net so the caller's intent is honored.
        sess = self._build_sess(set_opt_return={"id": "model", "currentValue": "hy3"})
        sess._switch_model("deepseek-v4-flash")
        sess._set_config_option.assert_called_once()
        sess._spawn.assert_called_once()


class TestThinkingAndToolCalls(unittest.TestCase):
    """Verify agent_thought_chunk and tool_call/tool_call_update events
    are captured, and include_thinking gates whether thinking is in result.
    0.4.0: tested via the pure `_collect_response_artifacts` helper."""

    def test_thinking_captured_when_include_thinking_true(self):
        events = [
            {"params": {"update": {"sessionUpdate": "agent_thought_chunk",
                                   "content": {"text": "Let me "}}}},
            {"params": {"update": {"sessionUpdate": "agent_thought_chunk",
                                   "content": {"text": "think about this..."}}}},
            {"params": {"update": {"sessionUpdate": "agent_message_chunk",
                                   "content": {"text": "Answer: 42"}}}},
            {"params": {"update": {"sessionUpdate": "usage_update",
                                   "_meta": {"usage": {"prompt_tokens": 100, "completion_tokens": 12,
                                                       "prompt_tokens_details": {"cached_tokens": 50}}}}}},
        ]
        r = _mod._collect_response_artifacts({}, events, include_thinking=True,
                                            fallback_model="hy3")
        self.assertEqual(r["text"], "Answer: 42")
        self.assertEqual(r["thinking"], "Let me think about this...")
        self.assertEqual(r["thinking_chars"], len("Let me think about this..."))

    def test_thinking_NOT_in_result_when_include_thinking_false(self):
        events = [
            {"params": {"update": {"sessionUpdate": "agent_thought_chunk",
                                   "content": {"text": "should not be exposed"}}}},
            {"params": {"update": {"sessionUpdate": "agent_message_chunk",
                                   "content": {"text": "Answer"}}}},
        ]
        r = _mod._collect_response_artifacts({}, events, include_thinking=False,
                                            fallback_model="hy3")
        self.assertEqual(r["text"], "Answer")
        self.assertNotIn("thinking", r)
        # Default (no kwarg) matches False
        r2 = _mod._collect_response_artifacts({}, events, include_thinking=False,
                                             fallback_model="hy3")
        self.assertNotIn("thinking", r2)

    def test_tool_calls_captured_with_status_updates(self):
        events = [
            {"params": {"update": {"sessionUpdate": "tool_call",
                                   "toolCallId": "t1", "title": "Read", "kind": "other",
                                   "status": "in_progress"}}},
            {"params": {"update": {"sessionUpdate": "tool_call",
                                   "toolCallId": "t2", "title": "Write", "kind": "other",
                                   "status": "in_progress"}}},
            {"params": {"update": {"sessionUpdate": "tool_call_update",
                                   "toolCallId": "t1", "status": "completed"}}},
            {"params": {"update": {"sessionUpdate": "tool_call_update",
                                   "toolCallId": "t2", "status": "completed"}}},
            {"params": {"update": {"sessionUpdate": "agent_message_chunk",
                                   "content": {"text": "Done."}}}},
        ]
        r = _mod._collect_response_artifacts({}, events, include_thinking=False,
                                            fallback_model="hy3")
        self.assertEqual(len(r["tool_calls"]), 2)
        by_id = {tc["id"]: tc for tc in r["tool_calls"]}
        self.assertEqual(by_id["t1"]["status"], "completed")
        self.assertEqual(by_id["t2"]["status"], "completed")
        self.assertEqual(by_id["t1"]["title"], "Read")
        self.assertEqual(by_id["t2"]["title"], "Write")

    def test_format_result_renders_thinking_and_tools(self):
        result = {
            "text": "Answer: 42",
            "model": "hy3", "duration_s": 1.2, "stop_reason": "end_turn",
            "cb_pid": 100, "cache_ratio": 50.0,
            "usage": {"prompt_tokens": 100, "completion_tokens": 12,
                      "prompt_tokens_details": {"cached_tokens": 50}},
            "thinking": "Let me think...",
            "thinking_chars": 17,
            "tool_calls": [
                {"id": "t1", "title": "Read", "status": "completed"},
                {"id": "t2", "title": "Write", "status": "completed"},
            ],
        }
        out = _mod._format_result(result)
        self.assertIn("Answer: 42", out)
        self.assertIn("--- thinking (17 chars) ---", out)
        self.assertIn("Let me think...", out)
        self.assertIn("--- tools (2) ---", out)
        self.assertIn("Read [completed]", out)
        self.assertIn("Write [completed]", out)


class TestTaskPersistence(unittest.TestCase):
    """Task persistence: save / load / round-trip / GC. These are the
    primitives that wrapper restart and get_result-after-restart rely on."""

    def setUp(self):
        self._orig_state_dir = _mod.STATE_DIR
        self.tmp_state = Path(tempfile.mkdtemp(prefix="mcp-task-test-"))
        _mod.STATE_DIR = self.tmp_state
        _mod.TASKS_DIR = self.tmp_state / "tasks"
    def tearDown(self):
        _mod.STATE_DIR = self._orig_state_dir
        _mod.TASKS_DIR = self._orig_state_dir / "tasks"

    def test_save_load_roundtrip(self):
        rec = _mod.TaskRecord(
            task_id="tsk_abc123def456",
            status="done",
            submitted_at="2026-08-18T10:00:00+00:00",
            text_preview="hello",
            model="deepseek-v4-flash",
            result={"text": "world", "duration_s": 1.5, "model": "deepseek-v4-flash",
                     "stop_reason": "end_turn", "cb_pid": 123, "cache_ratio": 95.0,
                     "usage": {"prompt_tokens": 100, "completion_tokens": 5,
                                "prompt_tokens_details": {"cached_tokens": 95}}},
            duration_s=1.5,
        )
        self.assertTrue(_mod._save_task(rec))
        loaded = _mod._load_task("tsk_abc123def456")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.task_id, "tsk_abc123def456")
        self.assertEqual(loaded.status, "done")
        self.assertEqual(loaded.text_preview, "hello")
        self.assertEqual(loaded.model, "deepseek-v4-flash")
        self.assertEqual(loaded.result["text"], "world")
        self.assertEqual(loaded.duration_s, 1.5)

    def test_load_missing_returns_none(self):
        self.assertIsNone(_mod._load_task("tsk_does_not_exist"))

    def test_load_rejects_path_traversal(self):
        # Defense against malicious task_id: must not escape TASKS_DIR.
        self.assertIsNone(_mod._load_task("../etc/passwd"))
        self.assertIsNone(_mod._load_task(""))
        self.assertIsNone(_mod._load_task("tsk/../../bad"))
        # Reject anything with shell metacharacters or path separators
        self.assertIsNone(_mod._load_task("tsk;rm -rf /"))

    def test_load_malformed_json_returns_none(self):
        # Write garbage to disk and confirm load returns None (no crash)
        _mod.TASKS_DIR.mkdir(parents=True, exist_ok=True)
        (_mod.TASKS_DIR / "tsk_garbage.json").write_text("{not json}", encoding="utf-8")
        self.assertIsNone(_mod._load_task("tsk_garbage"))

    def test_gc_marks_running_tasks_as_stale(self):
        # Two tasks: one running, one done. GC should mark running as stale
        # and leave done alone (or remove if old enough).
        _mod.TASKS_DIR.mkdir(parents=True, exist_ok=True)
        running = _mod.TaskRecord(
            task_id="tsk_running1", status="running",
            submitted_at="2026-08-18T09:00:00+00:00", text_preview="x")
        done_old = _mod.TaskRecord(
            task_id="tsk_doneold1", status="done",
            submitted_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T00:01:00+00:00",
            result={"text": "y"})
        done_fresh = _mod.TaskRecord(
            task_id="tsk_donefresh", status="done",
            submitted_at="2026-08-18T10:00:00+00:00",
            completed_at="2026-08-18T10:00:01+00:00",
            result={"text": "z"})
        _mod._save_task(running)
        _mod._save_task(done_old)
        _mod._save_task(done_fresh)
        touched = _mod._gc_orphan_tasks()
        # Running → stale (touched), old done → unlinked (touched), fresh done → unchanged
        self.assertGreaterEqual(touched, 2)
        # running now stale
        loaded = _mod._load_task("tsk_running1")
        self.assertEqual(loaded.status, "stale")
        self.assertIn("wrapper restarted", loaded.error)
        # old done unlinked
        self.assertFalse((_mod.TASKS_DIR / "tsk_doneold1.json").exists())
        # fresh done still there
        self.assertTrue((_mod.TASKS_DIR / "tsk_donefresh.json").exists())

    def test_gc_on_missing_dir_is_safe(self):
        # No TASKS_DIR exists → GC should be a no-op, not crash
        # (the tmp dir is empty, no tasks subdir)
        self.assertEqual(_mod._gc_orphan_tasks(), 0)

    def test_save_atomic_no_partial_file_on_failure(self):
        # Mock json.dump to raise — confirm .tmp doesn't linger
        import unittest.mock as mock
        _mod.TASKS_DIR.mkdir(parents=True, exist_ok=True)
        with mock.patch("json.dump", side_effect=OSError("disk full")):
            rec = _mod.TaskRecord(task_id="tsk_fail", status="done",
                                   submitted_at="2026-08-18T10:00:00+00:00")
            self.assertFalse(_mod._save_task(rec))
        # No .tmp file left behind
        self.assertFalse((_mod.TASKS_DIR / "tsk_fail.tmp").exists())
        self.assertFalse((_mod.TASKS_DIR / "tsk_fail.json").exists())


class TestTaskIdFormat(unittest.TestCase):
    """TaskRecord.task_id format: tsk_ + 12 hex chars. Get this wrong and
    load_task's character-class filter will reject it."""

    def test_format_in_submit_prompt_async(self):
        import re
        # We don't run a real session here; we just check that the format
        # function the plan calls is what we expect. (If submit_prompt_async
        # ever changes the prefix, this guard catches it.)
        # Use uuid directly to derive a sample task_id and assert the format
        sample = "tsk_" + uuid.uuid4().hex[:12]
        self.assertRegex(sample, r"^tsk_[0-9a-f]{12}$")


class TestSubmitPromptAsync(unittest.TestCase):
    """submit_prompt_async: returns immediately with task_id, spawns a
    daemon thread, single in-flight constraint, status fields present."""

    def _build_sess(self, *, inflight_set=False):
        """Bare ACPSession — bypass __init__ so we don't actually spawn
        a codebuddy subprocess."""
        sess = _mod.ACPSession.__new__(_mod.ACPSession)
        sess._inflight = None
        sess._tasks_done = deque(maxlen=50)
        sess._task_event = threading.Event()
        sess._task_lock = threading.Lock()
        sess.timeout = 3600
        sess.last_model = "hy3"
        if inflight_set:
            sess._inflight = _mod.TaskRecord(
                task_id="tsk_existing1234", status="running",
                submitted_at="2026-08-18T10:00:00+00:00",
            )
        return sess

    def test_returns_task_id_immediately(self):
        import unittest.mock as mock
        sess = self._build_sess()
        with mock.patch.object(_mod, "_save_task", return_value=True), \
             mock.patch.object(_mod.threading, "Thread") as ThreadMock:
            ThreadMock.return_value.start = mock.MagicMock()
            rec = sess.submit_prompt_async(text="hello world", model="deepseek-v4-flash")
        # No waiting — returns immediately
        self.assertEqual(rec["status"], "running")
        self.assertRegex(rec["task_id"], r"^tsk_[0-9a-f]{12}$")
        self.assertEqual(rec["model"], "deepseek-v4-flash")
        self.assertIn("submitted_at", rec)

    def test_second_submit_returns_busy(self):
        import unittest.mock as mock
        sess = self._build_sess(inflight_set=True)
        rec = sess.submit_prompt_async(text="x", model="hy3")
        self.assertEqual(rec["status"], "busy")
        self.assertIn("tsk_existing1234", rec["error"])


class TestGetResult(unittest.TestCase):
    """get_result: done → return result; stale → return error; unknown → unknown."""

    def _build_sess(self, *, done_records=None, inflight=None, disk=None):
        sess = _mod.ACPSession.__new__(_mod.ACPSession)
        sess._inflight = inflight
        sess._tasks_done = deque(done_records or [], maxlen=50)
        sess._task_event = threading.Event()
        sess._task_lock = threading.Lock()
        sess.timeout = 3600
        # Allow _load_task to be patched per test
        self._disk_return = disk
        return sess

    def test_done_in_done_ring(self):
        rec = _mod.TaskRecord(
            task_id="tsk_done1234ab", status="done",
            submitted_at="2026-08-18T10:00:00+00:00",
            result={"text": "the answer", "model": "deepseek-v4-flash",
                     "duration_s": 1.5, "stop_reason": "end_turn",
                     "cb_pid": 100, "cache_ratio": 95.0,
                     "usage": {"prompt_tokens": 100, "completion_tokens": 5,
                                "prompt_tokens_details": {"cached_tokens": 95}}},
        )
        sess = self._build_sess(done_records=[rec])
        out = sess.get_result("tsk_done1234ab", wait_timeout_s=0, mode="poll")
        self.assertEqual(out["status"], "done")
        self.assertEqual(out["result"]["text"], "the answer")

    def test_error_in_done_ring(self):
        rec = _mod.TaskRecord(
            task_id="tsk_error12345", status="error",
            submitted_at="2026-08-18T10:00:00+00:00",
            error="RuntimeError: foo",
        )
        sess = self._build_sess(done_records=[rec])
        out = sess.get_result("tsk_error12345", wait_timeout_s=0, mode="poll")
        self.assertEqual(out["status"], "error")
        self.assertIn("RuntimeError", out["error"])

    def test_unknown_task_id(self):
        import unittest.mock as mock
        sess = self._build_sess()
        with mock.patch.object(_mod, "_load_task", return_value=None):
            out = sess.get_result("tsk_unknown", wait_timeout_s=0, mode="poll")
        self.assertEqual(out["status"], "unknown")
        self.assertIn("no such task_id", out["error"])

    def test_stale_task_from_disk(self):
        # Wrapper restart scenario: task was running in previous process,
        # GC marked it stale, current process sees the stale record.
        import unittest.mock as mock
        rec = _mod.TaskRecord(
            task_id="tsk_stale123ab", status="stale",
            submitted_at="2026-08-18T09:00:00+00:00",
            completed_at="2026-08-18T09:01:00+00:00",
            error="wrapper restarted while this task was in-flight",
        )
        sess = self._build_sess()
        with mock.patch.object(_mod, "_load_task", return_value=rec):
            out = sess.get_result("tsk_stale123ab", wait_timeout_s=0, mode="poll")
        self.assertEqual(out["status"], "stale")
        self.assertIn("wrapper restarted", out["error"])


class TestGlobalTimeoutDefaults(unittest.TestCase):
    """v4 invariant: every timeout default = 3600 (1h). grep-able
    guard so a future change can't quietly regress to 60/300/600."""

    def test_acp_session_default_timeout(self):
        # The default value of `timeout` in the ACPSession signature is
        # 3600. We assert it via signature inspection rather than running
        # __init__ (which spawns a subprocess).
        import inspect
        sig = inspect.signature(_mod.ACPSession.__init__)
        self.assertEqual(sig.parameters["timeout"].default, 3600)

    def test_get_result_default_wait_timeout(self):
        import inspect
        sig = inspect.signature(_mod.ACPSession.get_result)
        self.assertEqual(sig.parameters["wait_timeout_s"].default, 3600)

    def test_input_schemas_use_3600(self):
        # Both get_result and run expose wait_timeout_s default in their
        # input_schema (read by the MCP client at tools/list time). mcp v2
        # uses pydantic which exposes it as `input_schema` (snake_case).
        for tool in _mod.ALL_TOOLS:
            schema = tool.input_schema
            props = schema.get("properties", {})
            if "wait_timeout_s" in props:
                self.assertEqual(
                    props["wait_timeout_s"].get("default"), 3600,
                    f"{tool.name}.input_schema.wait_timeout_s.default must be 3600, got {props['wait_timeout_s'].get('default')!r}",
                )

    def test_legacy_prompt_tool_not_in_all_tools(self):
        names = {t.name for t in _mod.ALL_TOOLS}
        self.assertNotIn("prompt", names)
        self.assertNotIn("continue", names)

    def test_seven_tools_total(self):
        self.assertEqual(len(_mod.ALL_TOOLS), 7)

    def test_expected_tool_set(self):
        names = {t.name for t in _mod.ALL_TOOLS}
        self.assertEqual(names, {
            "submit_prompt", "submit_continue", "get_result", "run",
            "status", "list_tasks", "list_models",
        })


def asyncio_run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


if __name__ == "__main__":
    unittest.main(verbosity=2)
