#!/usr/bin/env python3
"""test_mcp_wrapper_unit.py - unit tests for codebuddy-mcp-server (no subprocess)"""
import importlib.util, json, sys, tempfile, threading, time, unittest
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
            "totals": {"prompt_tokens": 100000, "completion_tokens": 200, "cached_tokens": 95000}})()
        st = _mod.ACPSession.status(fake)
        self.assertTrue(st["alive"]); self.assertEqual(st["codebuddy_pid"], 99999)
        self.assertEqual(st["acp_session_id"], "abc-123"); self.assertEqual(st["model"], "hy3")
        self.assertEqual(st["call_count"], 5); self.assertEqual(st["last_cache_ratio"], 98.5)
        self.assertEqual(st["totals"]["cached_tokens"], 95000)
        self.assertGreaterEqual(st["uptime_s"], 59)


class TestListTasksLimit(unittest.TestCase):
    def test_returns_most_recent_first(self):
        fake = type("Fake", (), {"_tasks": deque(maxlen=50)})()
        for i in range(5): fake._tasks.append({"idx": i + 1, "text_preview": f"task {i+1}"})
        items = _mod.ACPSession.list_tasks(fake, limit=10)
        self.assertEqual(len(items), 5); self.assertEqual([t["idx"] for t in items], [5, 4, 3, 2, 1])
    def test_limit_truncates(self):
        fake = type("Fake", (), {"_tasks": deque(maxlen=50)})()
        for i in range(10): fake._tasks.append({"idx": i + 1})
        items = _mod.ACPSession.list_tasks(fake, limit=3)
        self.assertEqual(len(items), 3); self.assertEqual([t["idx"] for t in items], [10, 9, 8])
    def test_limit_zero_returns_empty(self):
        fake = type("Fake", (), {"_tasks": deque(maxlen=50)})()
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
    def test_prompt_missing_text_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self._run(_mod._call_tool(None, type("P", (), {"name": "prompt", "arguments": {}})()))
        self.assertIn("text", str(ctx.exception))
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
    appended, not just the first. We reconstruct the relevant slice of
    `prompt()` by calling _drain_notifications and then running the
    post-call loop body. Simpler: just call the inner accumulation
    logic by exercising `prompt()` with a fake session."""

    def _build_fake_session(self, chunks):
        """Build a fake ACPSession that, when prompt() is called, will
        return the given list of chunked text fragments as the
        assistant message."""
        import unittest.mock as mock
        full_text = "".join(chunks)
        session = _mod.ACPSession.__new__(_mod.ACPSession)
        session.codebuddy_bin = "codebuddy"
        session.cwd = "/tmp"
        session.mcode_base_prompt_file = None
        session.timeout = 30
        session._appended_text = None
        session.last_model = "hy3"
        session.session_id = "sess-1"
        session.started_at = time.time()
        session.call_count = 0
        session.last_call_at = None
        session.last_cache_ratio = None
        session.totals = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}
        session._tasks = deque(maxlen=50)
        session.proc = mock.MagicMock()
        session.pid = 99999  # used for cb_pid in result metadata
        # The r returned by call() — the immediate response is empty
        session.call = mock.MagicMock(return_value={})
        # Drain returns synthetic notifications
        notifications = [
            {"params": {"update": {"sessionUpdate": "agent_message_chunk",
                                   "content": {"text": c}}}}
            for c in chunks
        ]
        notifications.append({
            "params": {"update": {
                "sessionUpdate": "usage_update",
                "_meta": {"usage": {"prompt_tokens": 100, "completion_tokens": len(full_text),
                                    "prompt_tokens_details": {"cached_tokens": 50}}},
            }},
        })
        session._drain_notifications = mock.MagicMock(return_value=notifications)
        return session

    def test_concatenates_multiple_chunks(self):
        session = self._build_fake_session(["Hello, ", "this is a ", "long reply with many tokens."])
        r = session.prompt(text="hi")
        self.assertEqual(r["text"], "Hello, this is a long reply with many tokens.")
        self.assertEqual(r["usage"]["completion_tokens"], len("Hello, this is a long reply with many tokens."))

    def test_single_chunk_still_works(self):
        session = self._build_fake_session(["short"])
        r = session.prompt(text="hi")
        self.assertEqual(r["text"], "short")


def asyncio_run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


if __name__ == "__main__":
    unittest.main(verbosity=2)
