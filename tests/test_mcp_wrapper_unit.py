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
        self._orig_state_dir = _mod.STATE_DIR
        self.tmp_state = Path(tempfile.mkdtemp(prefix="mcp-test-"))
        _mod.STATE_DIR = self.tmp_state
    def tearDown(self):
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


def asyncio_run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


if __name__ == "__main__":
    unittest.main(verbosity=2)
