import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from support import git, make_config, make_origin
from bot.deploy import DeployResult
from bot.gitops import Git
from bot.roundtrip import Cancelled
from bot.service import DEPLOY_NOTE, HELP, Bot, Progress
from bot.state import State


class LocalGit(Git):
    """Clones from a local bare repository; remote-name verification is covered by the Git tests."""

    def ensure_clone(self, url, dest, owner, name):
        if dest.exists():
            return False
        self.run("clone", url, str(dest))
        return True


class FakeTelegram:
    def __init__(self):
        self.sent, self.edits = [], []

    def send_message(self, chat_id, text):
        self.sent.append(text)
        return len(self.sent)

    def send_long(self, chat_id, text):
        self.sent.append(text)

    def edit_message(self, chat_id, message_id, text):
        self.edits.append(text)


class FakeGitHub:
    login = "me"

    def __init__(self, existing, tmp):
        self.repos, self.created, self._tmp = dict(existing), [], tmp

    def get_repo(self, owner, name):
        return self.repos.get(f"{owner}/{name}")

    def create_repo(self, owner, name, private=True):
        self.created.append((f"{owner}/{name}", private))
        origin = self._tmp / (name + ".git")
        git("init", "--bare", "-b", "main", str(origin))
        repo = {"clone_url": str(origin), "default_branch": "main"}
        self.repos[f"{owner}/{name}"] = repo
        return repo


class FakeRoundTrip:
    """Edits the checkout the way an applied Copilot result would."""

    def __init__(self, behavior="change"):
        self.behavior, self.calls = behavior, []

    def run(self, repo_dir, request, model, on_status, cancel, show_browser=None, temporary_chat=None):
        self.calls.append((Path(repo_dir), request, model, show_browser, temporary_chat))
        on_status("browser", "Opening Copilot")
        if self.behavior == "cancel":
            raise Cancelled()
        if self.behavior == "change":
            (Path(repo_dir) / "Makefile").write_text("run:\n\techo hi\n")
        return {"chat_response": "Added it.", "diff_stat": " Makefile | 2 ++\n 1 file changed, 2 insertions(+)\n"}


class FakeDeployer:
    def __init__(self, ok=True):
        self.ok, self.calls = ok, []

    def redeploy(self, app, repo_dir):
        self.calls.append((app, Path(repo_dir)))
        return DeployResult(self.ok, "`make run` is running (pid 1)." if self.ok else "`make run` exited with code 2.")

    def status(self, app, repo_dir):
        return "not running"


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = make_config(self.tmp)
        self.origin = make_origin(self.tmp)
        self.template = self.cfg.repos_dir / "eurofish"
        (self.template / "src").mkdir(parents=True)
        (self.template / "README.md").write_text("eurofish reference app\n")
        (self.template / "src" / "app.py").write_text("print('eurofish')\n")
        git("init", "-b", "main", str(self.template))
        git("add", "-A", cwd=self.template)
        git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-m", "reference",
            cwd=self.template)
        self.tg = FakeTelegram()
        self.gh = FakeGitHub({"me/app": {"clone_url": str(self.origin)}}, self.tmp)
        self.rt, self.deployer = FakeRoundTrip(), FakeDeployer()
        self.state = State(self.tmp / "state.json", "GPT/GPT-6")
        self.bot = Bot(self.cfg, self.tg, self.gh, LocalGit(self.cfg), self.rt, self.deployer, self.state)
        self.dest = self.cfg.repos_dir / "app"

    def say(self, text):
        self.bot.handle_text(1, text)
        if self.bot._worker is not None:
            self.bot._worker.join(timeout=30)

    def test_only_the_owner_in_a_private_chat_is_served(self):
        def update(sender, chat_type="private"):
            return {"message": {"from": {"id": sender}, "chat": {"id": 1, "type": chat_type}, "text": "/help"}}
        self.bot.handle_update(update(7))
        self.bot.handle_update(update(42, "group"))
        self.assertEqual(self.tg.sent, [])
        self.bot.handle_update(update(42))
        self.assertEqual(self.tg.sent, [HELP])

    def test_request_without_repository_asks_to_select_one(self):
        self.say("add a login page")
        self.assertIn("Select a repository first", self.tg.sent[-1])
        self.assertEqual(self.rt.calls, [])

    def test_prompt_progress_has_activity_and_last_model_output(self):
        progress = Progress(self.tg, 1, self.cfg)
        progress.MIN_INTERVAL = 0
        progress.begin_prompt()
        progress.update("me/app: waiting for Copilot")
        self.assertIn("Last model output: not received yet", self.tg.sent[-1])
        progress.update("me/app: Copilot is working", model_output=True)
        self.assertRegex(self.tg.edits[-1], r"^[\U0001f504\u23f3\u231b] ")
        self.assertRegex(self.tg.edits[-1], r"Last model output: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC$")
        progress.end_prompt()
        progress.flush()
        self.assertNotIn("Last model output:", self.tg.edits[-1])

    def test_repo_command_clones_and_persists_selection(self):
        self.say("/repo app")
        self.assertTrue((self.dest / ".git").exists())
        self.assertEqual(self.state.repo, "me/app")
        self.assertIn("cloned", self.tg.sent[-1])
        self.assertEqual(State(self.tmp / "state.json", "x").repo, "me/app")
        self.say("/repo")
        self.assertIn("me/app", self.tg.sent[-1])

    def test_missing_repository_is_created_through_github(self):
        self.say("/repo brand-new")
        self.assertEqual(self.gh.created, [("me/brand-new", True)])
        dest = self.cfg.repos_dir / "brand-new"
        self.assertTrue((dest / ".git").exists())
        self.assertEqual((dest / "README.md").read_text(), "eurofish reference app\n")
        self.assertEqual(git("rev-list", "--count", "HEAD", cwd=dest), "1")
        self.assertEqual(git("log", "-1", "--format=%s", cwd=dest), "Initial commit")
        self.assertNotIn(".git", git("ls-tree", "-r", "--name-only", "HEAD", cwd=dest).splitlines())
        self.assertEqual(self.state.repo, "me/brand-new")
        self.assertIn("created from eurofish", self.tg.sent[-1])

    def test_new_creates_selects_and_accepts_followup_request(self):
        self.say("/new fresh-app")
        self.assertEqual(self.gh.created, [("me/fresh-app", True)])
        self.assertEqual(self.state.repo, "me/fresh-app")
        self.assertIn("Send your next message", self.tg.sent[-1])
        self.say("build a starter app")
        self.assertEqual(len(self.rt.calls), 1)
        self.assertEqual(self.rt.calls[0][0], self.cfg.repos_dir / "fresh-app")

    def test_visibility_choice_for_new_and_repo(self):
        self.say("/new public open-app")
        self.assertEqual(self.gh.created, [("me/open-app", False)])
        self.say("/repo private closed-app")
        self.assertEqual(self.gh.created[-1], ("me/closed-app", True))
        self.say("/repo public app")
        self.assertEqual(len(self.gh.created), 2)  # Existing visibility is not changed.
        self.say("/new public")
        self.assertIn("Usage: /new", self.tg.sent[-1])

    def test_new_refuses_existing_repo_and_requires_name(self):
        self.say("/new")
        self.assertIn("Usage: /new", self.tg.sent[-1])
        self.say("/new app")
        self.assertIn("already exists", self.tg.sent[-1])
        self.assertEqual(self.gh.created, [])
        self.assertEqual(self.state.repo, "")

    def test_request_runs_commits_pushes_and_redeploys(self):
        self.say("/repo app")
        self.say("add a Makefile")
        self.assertEqual(self.rt.calls[0][1], "add a Makefile" + DEPLOY_NOTE)
        self.assertEqual(self.rt.calls[0][2], "GPT/GPT-6")
        self.assertEqual(self.deployer.calls, [("app", self.dest)])
        self.assertEqual(git("log", "-1", "--format=%s", cwd=self.origin), "Copilot: add a Makefile")
        report = self.tg.sent[-1]
        self.assertIn("pushed to main", report)
        self.assertIn("1 file changed", report)
        self.assertNotIn("Added it.", report)
        self.assertNotIn("Copilot:", report)

    def test_fix_selects_the_repository_and_runs(self):
        self.say("/fix app tidy the readme")
        self.assertEqual(self.state.repo, "me/app")
        self.assertEqual(len(self.rt.calls), 1)

    def test_fix_options_override_environment_defaults_for_active_repo(self):
        self.state.repo = "me/app"
        self.say("/fix browser tidy the readme")
        self.assertEqual(self.rt.calls[0][0], self.dest)
        self.assertEqual(self.rt.calls[0][1], "tidy the readme" + DEPLOY_NOTE)
        self.assertEqual(self.rt.calls[0][3:], (True, None))

        self.say("/fix notempchat add tests")
        self.assertEqual(self.rt.calls[1][3:], (None, False))

        self.say("/fix tempchat nobrowser update docs")
        self.assertEqual(self.rt.calls[2][3:], (False, True))

    def test_fix_option_requires_request_and_active_repo(self):
        self.say("/fix browser")
        self.assertIn("Usage", self.tg.sent[-1])
        self.say("/fix browser add tests")
        self.assertIn("Select a repository first", self.tg.sent[-1])
        self.assertEqual(self.rt.calls, [])

    def test_no_changes_skips_redeploy(self):
        self.rt.behavior = "none"
        self.say("/fix app nothing")
        self.assertEqual(self.deployer.calls, [])
        self.assertIn("no changes", self.tg.sent[-1])

    def test_failed_redeploy_is_reported_after_the_push(self):
        self.deployer.ok = False
        self.say("/fix app break it")
        self.assertIn("redeploy failed", self.tg.sent[-1])
        self.assertIn("exited with code 2", self.tg.sent[-1])

    def test_cancelled_run_reports_and_does_not_commit(self):
        self.rt.behavior = "cancel"
        self.say("/fix app anything")
        self.assertEqual(self.tg.sent[-1], "Cancelled.")
        self.assertEqual(self.deployer.calls, [])

    def test_cloudflare_requires_credentials_and_valid_arguments(self):
        for command in ("/cloudflare", "/cloudflare a 0", "/cloudflare a 65536",
                        "/cloudflare bad.name 8080", "/cloudflare A 8080", "/cloudflare a abc"):
            self.say(command)
            self.assertTrue("Usage:" in self.tg.sent[-1] or "must be" in self.tg.sent[-1])
        self.say("/cloudflare eurofish 8080")
        self.assertIn("Configure Cloudflare", self.tg.sent[-1])

    def test_cloudflare_list_command(self):
        from unittest.mock import patch
        self.say("/cloudflare list")
        self.assertIn("Configure Cloudflare", self.tg.sent[-1])
        self.bot.cfg = make_config(self.tmp, cloudflare_api_token="cf", cloudflare_account_id="a" * 32,
                                   cloudflare_zone_id="b" * 32)
        with patch("bot.service.CloudflareRoutes") as routes:
            routes.return_value.list_routes.return_value = "Routes on kurwalab-tunnel:\nhttps://app.kurwalab.fyi -> http://127.0.0.1:8080"
            self.say("/cloudflare list")
            routes.return_value.list_routes.assert_called_once_with()
            routes.return_value.prepare.assert_not_called()
        self.assertIn("https://app.kurwalab.fyi", self.tg.sent[-1])

    def test_cloudflare_command_routes_only_on_request(self):
        from unittest.mock import patch
        self.bot.cfg = make_config(self.tmp, cloudflare_api_token="cf", cloudflare_account_id="a" * 32,
                                   cloudflare_zone_id="b" * 32)
        with patch("bot.service.CloudflareRoutes") as routes:
            routes.return_value.prepare.return_value = "Tunnel configured."
            self.say("/cloudflare eurofish 8080")
            routes.return_value.prepare.assert_called_once_with("eurofish", 8080)
        self.assertIn("Tunnel configured", self.tg.sent[-1])

    def test_model_and_status_commands(self):
        self.say("/model Claude/Opus 5.5")
        self.assertEqual(self.state.model, "Claude/Opus 5.5")
        self.say("/model nonsense")
        self.assertIn("provider/model", self.tg.sent[-1])
        self.say("/status")
        self.assertIn("Model: Claude/Opus 5.5", self.tg.sent[-1])
        self.say("/unknown")
        self.assertIn("Unknown command", self.tg.sent[-1])
        self.say("/fix")
        self.assertIn("Usage", self.tg.sent[-1])

    def test_second_job_is_refused_while_one_runs(self):
        release = threading.Event()
        started = threading.Event()

        class Slow(FakeRoundTrip):
            def run(inner, *args):
                started.set()
                release.wait(10)
                return super().run(*args)

        self.bot.roundtrip = Slow()
        self.bot.handle_text(1, "/fix app first")
        self.assertTrue(started.wait(10))
        self.bot.handle_text(1, "/fix app second")
        self.assertIn("already running", self.tg.sent[-1])
        self.bot.handle_text(1, "/cancel")
        self.assertTrue(self.bot._cancel.is_set())
        release.set()
        self.bot._worker.join(timeout=30)

    def test_polling_loop_confirms_updates_and_survives_errors(self):
        from bot.telegram_api import TelegramError
        stop = threading.Event()
        offsets, batches = [], [
            TelegramError("network"),
            [{"update_id": 5, "message": {"from": {"id": 42}, "chat": {"id": 1, "type": "private"}, "text": "/help"}},
             {"update_id": 6}],
        ]

        def get_updates(offset, poll_seconds=30):
            offsets.append(offset)
            if not batches:
                stop.set()
                return []
            batch = batches.pop(0)
            if isinstance(batch, Exception):
                raise batch
            return batch

        self.tg.get_updates = get_updates
        stop.wait = lambda delay: None
        self.bot.run_forever(stop)
        self.assertEqual(offsets, [0, 0, 7])
        self.assertEqual(self.tg.sent, [HELP])

    def test_secrets_are_redacted_from_messages(self):
        self.bot.say(1, "token gh-secret and tg-secret")
        self.assertEqual(self.tg.sent[-1], "token *** and ***")


if __name__ == "__main__":
    unittest.main()
