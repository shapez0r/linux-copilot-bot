import json
import os
import shutil
import tempfile
import textwrap
import threading
import unittest
from unittest import mock
from pathlib import Path

from support import make_config
from bot.roundtrip import (Cancelled, RoundTrip, RoundTripError, format_reasoning_progress,
                           parse_reasoning, parse_status)

FAKE_ENGINE = textwrap.dedent('''
    import json, sys
    mode = sys.argv[sys.argv.index("--") + 1]
    def status(stage, message, tasks=None):
        value = {"stage": stage, "message": message}
        if tasks is not None:
            value["tasks"] = tasks
        print("ROUNDTRIP_STATUS:" + json.dumps(value), file=sys.stderr, flush=True)
    status("browser", "Opening")
    status("task", "Coding and executing\\nprint('internal')", [{
        "title": "Update progress display", "state": "In progress", "active": True,
        "steps": [{"title": "Inspect status relay", "state": "completed"},
                  {"title": "Add Telegram progress", "state": "active"}]
    }])
    print("ROUNDTRIP_REASONING:" + json.dumps({"blocks": [{
        "title": "Reasoning", "active": True,
        "steps": [{"title": "Reviewing output", "kind": "thought", "lines": ["Keep the status useful."]}]
    }]}), file=sys.stderr, flush=True)
    if mode == "ok":
        status("waiting", "Still waiting for Copilot prompt to finish. 30s elapsed; no new output for 15s "
               "(last output: 2026-11-01 00:50:02 UTC); inactivity limit: 900s.")
        print(json.dumps({"chat_response": "done", "diff_stat": " 1 file changed"}))
    elif mode == "fail":
        print("No downloadable result", file=sys.stderr)
        sys.exit(1)
    elif mode == "env":
        import os
        print(json.dumps({"chat_response": ",".join(k for k in ("TELEGRAM_BOT_TOKEN", "GITHUB_TOKEN") if k in os.environ)}))
    elif mode == "clear":
        print("ROUNDTRIP_REASONING:" + json.dumps({"blocks": [], "answers": []}), file=sys.stderr, flush=True)
        print(json.dumps({"chat_response": "done"}))
    elif mode == "wait":
        status("waiting", "Waiting")
        sys.stdin.readline()
        sys.exit(130)
''')


class RoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        engine = self.tmp / "engine.py"
        engine.write_text(FAKE_ENGINE)
        self.cfg = make_config(self.tmp, engine=engine)
        self.statuses = []

    def run_engine(self, request, cancel=None):
        return RoundTrip(self.cfg).run(self.tmp, request, "GPT/GPT-6", lambda s, m: self.statuses.append((s, m)),
                                       cancel or threading.Event())

    def test_command_passes_the_request_after_a_separator(self):
        cmd = RoundTrip(self.cfg).command(self.tmp, "-rm things", "Claude/Opus 5.5")
        self.assertEqual(cmd[-2:], ["--", "-rm things"])
        self.assertIn("--profile", cmd)
        self.assertNotIn("--show-browser", cmd)
        self.assertNotIn("--no-temporary-chat", cmd)
        cfg = make_config(self.tmp, show_browser=True, temporary_chat=False)
        configured = RoundTrip(cfg).command(self.tmp, "x", "A/B")
        self.assertIn("--show-browser", configured)
        self.assertIn("--no-temporary-chat", configured)
        overridden = RoundTrip(cfg).command(self.tmp, "x", "A/B", False, True)
        self.assertNotIn("--show-browser", overridden)
        self.assertNotIn("--no-temporary-chat", overridden)

    def test_success_returns_result_and_relays_status(self):
        result = self.run_engine("ok")
        self.assertEqual(result["chat_response"], "done")
        self.assertEqual(self.statuses[0], ("browser", "Opening"))
        self.assertEqual([stage for stage, _ in self.statuses[1:]], ["task", "reasoning", "waiting"])
        self.assertIn("Task: Update progress display", self.statuses[1][1])
        self.assertIn("\u2705 Inspect status relay", self.statuses[1][1])
        self.assertIn("\U0001f504 Add Telegram progress", self.statuses[1][1])
        self.assertIn("Current reasoning: Keep the status useful.", self.statuses[2][1])
        self.assertIn("Still waiting for Copilot prompt to finish.", self.statuses[3][1])
        self.assertIn("last output: 2026-11-01 00:50:02 UTC", self.statuses[3][1])
        self.assertIn("Task: Update progress display", self.statuses[3][1])
        self.assertIn("Current reasoning: Keep the status useful.", self.statuses[3][1])
        self.assertNotIn("print('internal')", "\n".join(message for _, message in self.statuses))

    def test_failure_reports_engine_message(self):
        with self.assertRaisesRegex(RoundTripError, "No downloadable result"):
            self.run_engine("fail")

    def test_cancel_sends_quit_to_the_engine(self):
        cancel = threading.Event()
        threading.Timer(1.0, cancel.set).start()
        with self.assertRaises(Cancelled):
            self.run_engine("wait", cancel)

    def test_engine_does_not_inherit_bot_secrets(self):
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t", "GITHUB_TOKEN": "g"}):
            self.assertEqual(self.run_engine("env")["chat_response"], "")

    def test_parse_status(self):
        self.assertEqual(parse_status('ROUNDTRIP_STATUS:' + json.dumps({"stage": "a", "message": "b"})), ("a", "b"))
        for bad in ("ROUNDTRIP_STATUS:{", "ROUNDTRIP_STATUS:{}", "other"):
            self.assertIsNone(parse_status(bad))

    def test_parse_reasoning(self):
        value = {"blocks": [], "answers": [{"text": "working"}]}
        self.assertEqual(parse_reasoning("ROUNDTRIP_REASONING:" + json.dumps(value)), value)
        for bad in ("ROUNDTRIP_REASONING:{", "ROUNDTRIP_REASONING:[]", "other"):
            self.assertIsNone(parse_reasoning(bad))

    def test_reasoning_progress_is_limited_to_500_characters(self):
        message = format_reasoning_progress({"answers": [{"text": "x" * 600}]})
        self.assertEqual(len(message.removeprefix("Latest response: ")), 500)
        self.assertTrue(message.endswith("\u2026"))

    def test_working_progress_uses_completed_step_and_visible_prose(self):
        payload = {"blocks": [{
            "title": "Reasoning", "active": True, "layout": "working",
            "steps": [
                {"title": "Inspecting files", "kind": "completed", "lines": []},
                {"title": "Testing", "kind": "thought", "lines": ["Running focused tests."]},
                {"title": "Coding and executing", "kind": "coding", "lines": ["secret_code()"]},
            ],
        }]}
        message = format_reasoning_progress(payload)
        self.assertIn("Working through the steps: Inspecting files", message)
        self.assertIn("Current reasoning: Running focused tests.", message)
        self.assertNotIn("secret_code", message)

    def test_empty_live_update_clears_stale_progress(self):
        self.run_engine("clear")
        self.assertEqual([stage for stage, _ in self.statuses[-2:]], ["reasoning", "reasoning"])
        self.assertIn("Current reasoning: Keep the status useful.", self.statuses[-2][1])
        self.assertIn("Task: Update progress display", self.statuses[-1][1])
        self.assertNotIn("Current reasoning:", self.statuses[-1][1])


if __name__ == "__main__":
    unittest.main()
