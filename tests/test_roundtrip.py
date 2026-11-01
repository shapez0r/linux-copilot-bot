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
from bot.roundtrip import (Cancelled, RoundTrip, RoundTripError, format_prompt_status,
                           format_task_status, parse_activity_event, parse_live_event, parse_status,
                           parse_status_event)

FAKE_ENGINE = textwrap.dedent('''
    import json, sys
    mode = sys.argv[sys.argv.index("--") + 1]
    def status(stage, message, tasks=None):
        value = {"stage": stage, "message": message}
        if tasks is not None:
            value["tasks"] = tasks
        print("ROUNDTRIP_STATUS:" + json.dumps(value), file=sys.stderr, flush=True)
    status("browser", "Opening")
    status("task", 'Copilot task "Build app" is in progress; 3 plan items visible. 15s elapsed; no new output for 2s of 900s allowed.',
           [{"title": "Build app", "state": "In progress", "active": True, "steps": [
               {"title": "Inspect repository", "state": "completed"},
               {"title": "Implement changes", "state": "active"},
               {"title": "Run tests", "state": "pending"}]}])
    print("ROUNDTRIP_REASONING:" + json.dumps({"blocks": [{
        "title": "Reasoning", "active": True, "layout": "working", "steps": [
            {"title": "Inspecting files", "kind": "completed", "lines": []},
            {"title": "Coding and executing", "kind": "coding", "lines": ["secret_code()"]},
        ]}], "answers": [{"after": 1, "text": "Interim result."}]}), file=sys.stderr, flush=True)
    if mode == "ok":
        print(json.dumps({"chat_response": "done", "diff_stat": " 1 file changed"}))
    elif mode == "fail":
        print("No downloadable result", file=sys.stderr)
        sys.exit(1)
    elif mode == "env":
        import os
        print(json.dumps({"chat_response": ",".join(k for k in ("TELEGRAM_BOT_TOKEN", "GITHUB_TOKEN") if k in os.environ)}))
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
        self.assertEqual(self.statuses, [
            ("browser", "Opening"),
            ("task", "▶ Build app\nIn progress\n  ✓ Inspect repository\n"
                     "  ▶ Implement changes\n  ○ Run tests"),
            ("activity", "▶ Build app\nIn progress\n  ✓ Inspect repository\n"
                          "  ▶ Implement changes\n  ○ Run tests\n\n"
                          "▶ Working through the steps: Inspecting files\n\nInterim result."),
        ])
        self.assertNotIn("secret_code", str(self.statuses))
        self.assertNotIn("no new output", str(self.statuses))

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

    def test_task_status_parses_the_structured_plan_and_omits_execution_details(self):
        event = parse_status_event("ROUNDTRIP_STATUS:" + json.dumps({
            "stage": "task", "message": "internal code output", "tasks": [{
                "title": "  Build   app ", "state": "In progress", "steps": [
                    {"title": " Inspect files ", "state": "completed"},
                    {"title": " Coding and executing ", "state": "active", "output": "secret()"},
                    {"title": "Test", "state": "unknown"}, None,
                ],
            }],
        }))
        rendered = format_task_status(event["message"], event["tasks"])
        self.assertEqual(rendered, "▶ Build app\nIn progress\n"
                          "  ✓ Inspect files\n  ▶ Coding and executing\n  ○ Test")
        self.assertNotIn("secret", rendered)
        self.assertNotIn("internal code output", rendered)

    def test_unstructured_task_status_does_not_relay_generated_details(self):
        self.assertEqual(format_task_status("Coding and executing\nprint('internal')", None),
                         "Copilot is executing the prompt.")
        self.assertEqual(format_task_status("Copilot task card closed.", []),
                         "Copilot task card closed; waiting for the final response.")

    def test_activity_status_keeps_titles_and_omits_details_answers_and_code(self):
        line = "ROUNDTRIP_REASONING:" + json.dumps({
            "blocks": [{
                "title": "Creating files", "active": True, "layout": "working",
                "lines": ["private reasoning"],
                "steps": [
                    {"title": "Reviewing Python comments", "kind": "completed", "lines": []},
                    {"title": "Coding and executing", "kind": "coding",
                     "lines": ["print('secret')"], "total": 1},
                    {"title": "Testing", "lines": ["test output"]},
                ],
            }],
            "answers": [{"after": 1, "text": "generated reply"}],
        })
        blocks = parse_activity_event(line)
        self.assertEqual(blocks, [{
            "title": "Creating files", "active": True, "layout": "working", "steps": [
                {"title": "Reviewing Python comments", "kind": "completed"},
                {"title": "Coding and executing", "kind": "coding"},
                {"title": "Testing", "kind": "thought"},
            ],
        }])
        rendered = format_prompt_status("", [], blocks)
        self.assertEqual(rendered, "▶ Working through the steps: Reviewing Python comments")
        for hidden in ("private reasoning", "secret", "test output", "generated reply"):
            self.assertNotIn(hidden, rendered)

    def test_activity_parser_rejects_other_or_malformed_lines(self):
        for bad in ("ROUNDTRIP_REASONING:{", "ROUNDTRIP_REASONING:{}", "other"):
            self.assertIsNone(parse_activity_event(bad))

    def test_live_parser_matches_extension_order_and_omits_code_output(self):
        line = "ROUNDTRIP_REASONING:" + json.dumps({
            "blocks": [
                {"title": "Inspecting", "active": False, "layout": "working",
                 "steps": [{"title": "Read files", "kind": "completed", "lines": []}]},
                {"title": "Reasoning", "active": True, "layout": "section",
                 "steps": [
                     {"title": "Thought process", "kind": "thought", "lines": ["Comparing behavior"]},
                     {"title": "Coding and executing", "kind": "coding", "lines": ["token=secret"]},
                 ]},
            ],
            "answers": [{"after": 0, "text": "Starting."}, {"after": 1, "text": "Found it."}],
        })
        live = parse_live_event(line)
        self.assertEqual(live["answers"], [
            {"after": 0, "text": "Starting."}, {"after": 1, "text": "Found it."}])
        rendered = format_prompt_status("900s elapsed; no new output", [], live["blocks"], live["answers"])
        self.assertEqual(rendered,
                         "Starting.\n\n✓ Working through the steps: Read files\n\nFound it.\n\n"
                         "▶ Reasoning\n  • Thought process\n    Comparing behavior\n"
                         "  • Coding and executing")
        self.assertNotIn("token=secret", rendered)
        self.assertNotIn("no new output", rendered)

    def test_live_answer_without_activity_keeps_the_compact_stage(self):
        self.assertEqual(format_prompt_status("Waiting for Copilot...", [], [], [
            {"after": 0, "text": "I am checking the repository."},
        ]), "Waiting for Copilot...\n\nI am checking the repository.")


if __name__ == "__main__":
    unittest.main()
