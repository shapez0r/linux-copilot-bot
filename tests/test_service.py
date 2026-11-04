import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from support import git, make_config, make_origin
from bot.cloudflare import CloudflareError
from bot.deploy import DeployResult
from bot.github_api import GitHubError
from bot.gitops import Git, GitError
from bot.roundtrip import Cancelled, RoundTripError
from bot.service import (ADMIN_USER_VIEW_KEYBOARD, ADMIN_VIEW, ADMIN_VIEW_HINT, BOT_DEPLOY_NOTE, CHAT_MODE_KEYBOARD,
                         CONFIRM_DELETE, CREATE_APP, CUSTOM_MODEL, DELETE_APP, USER_VIEW,
                         DEPLOY_NOTE, FIX_APP, HELP, KNOWN_MODELS, LIST_APPS, MAIN_KEYBOARD, MODEL_KEYBOARD,
                         NEW_APP_PROMPT, PRIVATE_REPO, PUBLIC_REPO, VISIBILITY_KEYBOARD,
                         REGULAR_CHAT, TEMPORARY_CHAT, USER_HELP, USER_KEYBOARD, REGISTER_SERVICES,
                         START_APP, STOP_APP, Bot, Progress)
from bot.state import ADMIN_OWNER, State, telegram_owner


class LocalGit(Git):
    """Clones from a local bare repository; remote-name verification is covered by the Git tests."""

    def ensure_clone(self, url, dest, owner, name):
        if dest.exists():
            return False
        self.run("clone", url, str(dest))
        return True

    def require_checkout(self, dest, owner, name):
        if not (Path(dest) / ".git").is_dir():
            raise GitError(f"{dest} exists but is not a Git checkout.")


class FakeTelegram:
    def __init__(self):
        self.sent, self.edits, self.markups = [], [], []

    def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
        self.sent.append(text)
        self.markups.append(reply_markup)
        return len(self.sent)

    def send_long(self, chat_id, text, reply_markup=None):
        self.sent.append(text)
        self.markups.append(reply_markup)

    def edit_message(self, chat_id, message_id, text, parse_mode=None):
        self.edits.append(text)


class FakeGitHub:
    login = "me"

    def __init__(self, existing, tmp):
        self.repos, self.created, self.deleted, self._tmp = dict(existing), [], [], tmp
        self.delete_error = None

    def get_repo(self, owner, name):
        return self.repos.get(f"{owner}/{name}")

    def create_repo(self, owner, name, private=True):
        self.created.append((f"{owner}/{name}", private))
        origin = self._tmp / (name + ".git")
        git("init", "--bare", "-b", "main", str(origin))
        repo = {"clone_url": str(origin), "default_branch": "main"}
        self.repos[f"{owner}/{name}"] = repo
        return repo

    def delete_repo(self, owner, name):
        full = f"{owner}/{name}"
        self.deleted.append(full)
        if self.delete_error:
            raise self.delete_error
        return self.repos.pop(full, None) is not None


class FakeRoundTrip:
    """Edits the checkout the way an applied Copilot result would."""

    def __init__(self, behavior="change"):
        self.behavior, self.calls = behavior, []
        self.models, self.model_error, self.model_checks = ["Claude/Opus 9", "GPT/GPT-9 Pro"], None, 0

    def list_models(self, on_status, cancel):
        self.model_checks += 1
        if self.model_error:
            raise self.model_error
        return list(self.models)

    def run(self, repo_dir, request, model, on_status, cancel, show_browser=None, temporary_chat=None):
        self.calls.append((Path(repo_dir), request, model, show_browser, temporary_chat))
        on_status("browser", "Opening Copilot")
        if self.behavior == "cancel":
            raise Cancelled()
        if self.behavior == "change":
            (Path(repo_dir) / "Makefile").write_text("run:\n\techo hi\n")
        elif self.behavior == "append":
            (Path(repo_dir) / "notes.txt").write_text(f"run {len(self.calls)}\n")
        return {"chat_response": "Added it.", "diff_stat": " Makefile | 2 ++\n 1 file changed, 2 insertions(+)\n"}


class FakeDeployer:
    def __init__(self, ok=True):
        self.ok, self.calls, self.registered, self.running = ok, [], [], []
        self.starts, self.stops, self.unregistered, self.stop_result = [], [], [], False
        self.statuses = {}
        self.as_service, self.bot_restarts = True, 0

    def redeploy(self, app, repo_dir):
        self.calls.append((app, Path(repo_dir)))
        self.registered.append((app, Path(repo_dir)))
        return DeployResult(self.ok, f"Systemd service copilot-app-{app}.service is running (pid 1)."
                            if self.ok else f"Systemd service copilot-app-{app}.service failed.")

    def ensure_service(self, app, repo_dir):
        self.registered.append((app, Path(repo_dir)))
        return f"Service copilot-app-{app}.service is registered and enabled."

    def start(self, app, repo_dir):
        self.starts.append((app, Path(repo_dir)))
        return DeployResult(self.ok, f"Systemd service copilot-app-{app}.service is running (pid 1).")

    def status(self, app, repo_dir):
        return self.statuses.get(
            app, "running (pid 1)" if any(name == app for name, _ in self.running) else "stopped")

    def running_apps(self):
        return self.running

    def stop(self, app, repo_dir):
        self.stops.append((app, Path(repo_dir)))
        return self.stop_result

    def unregister(self, app, repo_dir):
        self.unregistered.append((app, Path(repo_dir)))
        return f"Service: disabled and removed copilot-app-{app}.service."

    def bot_status(self):
        return "running (pid 7)"

    def bot_runs_as_service(self):
        return self.as_service

    def restart_bot(self):
        self.bot_restarts += 1

    @staticmethod
    def service_name(app):
        return f"copilot-app-{app}.service"

    @staticmethod
    def is_bot_repo(path):
        return Path(path).name == "linux-copilot-bot"


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
        for worker in (self.bot._worker, self.bot._list_worker):
            if worker is not None:
                worker.join(timeout=30)

    def user_say(self, user_id, text):
        self.bot.handle_text(user_id, text, user_id)
        for worker in (self.bot._worker, self.bot._list_worker):
            if worker is not None:
                worker.join(timeout=30)

    def test_only_authorized_users_in_private_chats_are_served(self):
        def update(sender, chat_type="private"):
            return {"message": {"from": {"id": sender}, "chat": {"id": 1, "type": chat_type}, "text": "/help"}}
        self.bot.handle_update(update(7))
        self.bot.handle_update(update(42, "group"))
        self.assertEqual(self.tg.sent, [])
        self.bot.handle_update(update(42))
        self.assertEqual(self.tg.sent, [HELP])

        self.bot.cfg = make_config(self.tmp, owner_ids=frozenset({42}), user_ids=frozenset({77}))
        self.bot.handle_update(update(77))
        self.assertEqual(self.tg.sent, [HELP, USER_HELP])

    def test_start_shows_the_app_management_menu(self):
        self.say("/start")
        self.assertIn("Create and manage apps", self.tg.sent[-1])
        self.assertEqual(self.tg.markups[-1], MAIN_KEYBOARD)

    def test_regular_users_see_create_list_fix_and_own_app_controls(self):
        self.bot.cfg = make_config(self.tmp, user_ids=frozenset({77}))
        self.user_say(77, "/start")
        self.assertEqual(self.tg.markups[-1], USER_KEYBOARD)
        labels = {button["text"] for row in USER_KEYBOARD["keyboard"] for button in row}
        self.assertEqual(labels, {CREATE_APP, "📱 List apps", FIX_APP, START_APP, STOP_APP})
        self.user_say(77, "/help")
        self.assertEqual(self.tg.sent[-1], USER_HELP)

    def test_regular_users_cannot_invoke_admin_commands_or_buttons(self):
        self.bot.cfg = make_config(self.tmp, user_ids=frozenset({77}))
        for command in ("/delete app", "/repo app", "/cloudflare list", "/model GPT/GPT-6", "/status",
                        "/register"):
            with self.subTest(command=command):
                self.user_say(77, command)
                self.assertIn("only to kurwalab.fyi admins", self.tg.sent[-1])
        self.user_say(77, DELETE_APP)
        self.assertIn("only to kurwalab.fyi admins", self.tg.sent[-1])
        self.user_say(77, REGISTER_SERVICES)
        self.assertIn("only to kurwalab.fyi admins", self.tg.sent[-1])

    def test_admins_can_switch_between_admin_and_user_views(self):
        self.bot.cfg = make_config(self.tmp, owner_ids=frozenset({42}), user_ids=frozenset({77}))
        admin_labels = {button["text"] for row in MAIN_KEYBOARD["keyboard"] for button in row}
        self.assertIn(USER_VIEW, admin_labels)
        self.user_say(42, USER_VIEW)
        self.assertIn("User view enabled", self.tg.sent[-1])
        self.assertEqual(self.tg.markups[-1], ADMIN_USER_VIEW_KEYBOARD)
        user_labels = {button["text"] for row in ADMIN_USER_VIEW_KEYBOARD["keyboard"] for button in row}
        self.assertEqual(user_labels, {CREATE_APP, LIST_APPS, FIX_APP, START_APP, STOP_APP, ADMIN_VIEW})
        self.assertTrue(State(self.tmp / "state.json", "x").user_view(42))
        self.user_say(42, "/status")
        self.assertIn("only in admin view", self.tg.sent[-1])
        self.user_say(42, DELETE_APP)
        self.assertIn("only in admin view", self.tg.sent[-1])
        self.user_say(42, "/help")
        self.assertEqual(self.tg.sent[-1], USER_HELP + "\n\n" + ADMIN_VIEW_HINT)
        self.user_say(42, "/start")
        self.assertEqual(self.tg.markups[-1], ADMIN_USER_VIEW_KEYBOARD)
        self.user_say(42, ADMIN_VIEW)
        self.assertIn("Admin view enabled", self.tg.sent[-1])
        self.assertEqual(self.tg.markups[-1], MAIN_KEYBOARD)
        self.assertFalse(State(self.tmp / "state.json", "x").user_view(42))
        self.user_say(42, "/help")
        self.assertEqual(self.tg.sent[-1], HELP)

    def test_user_view_applies_regular_user_listing_and_ownership(self):
        self.bot.cfg = make_config(self.tmp, owner_ids=frozenset({42}), user_ids=frozenset({77}))
        (self.cfg.repos_dir / "theirs").mkdir(exist_ok=True)
        self.state.set_app_owner("me/theirs", telegram_owner(77))
        self.user_say(42, USER_VIEW)
        self.user_say(42, "/list")
        self.assertNotIn("folder:", self.tg.sent[-1])
        self.assertNotIn("port:", self.tg.sent[-1])
        self.user_say(42, "/fix theirs change it")
        self.assertIn("only fix apps you created", self.tg.sent[-1])
        self.user_say(42, "/app stop theirs")
        self.assertIn("only start or stop apps you created", self.tg.sent[-1])
        self.assertEqual(self.rt.calls, [])
        self.user_say(42, ADMIN_VIEW)
        self.user_say(42, "/list")
        self.assertIn("folder:", self.tg.sent[-1])

    def test_regular_users_cannot_switch_views(self):
        self.bot.cfg = make_config(self.tmp, owner_ids=frozenset({42}), user_ids=frozenset({77}))
        for label in (USER_VIEW, ADMIN_VIEW):
            with self.subTest(label=label):
                self.user_say(77, label)
                self.assertIn("only to kurwalab.fyi admins", self.tg.sent[-1])
        self.assertFalse(self.state.user_view(77))

    def test_regular_users_see_only_their_apps_in_the_fix_picker(self):
        self.bot.cfg = make_config(self.tmp, user_ids=frozenset({77, 88}))
        self.state.set_app_owner("me/mine", telegram_owner(77))
        self.state.set_app_owner("me/theirs", telegram_owner(88))
        self.state.set_app_owner("me/admin-app", ADMIN_OWNER)
        self.user_say(77, FIX_APP)
        labels = {button["text"] for row in self.tg.markups[-1]["keyboard"] for button in row}
        self.assertIn("me/mine", labels)
        self.assertNotIn("me/theirs", labels)
        self.assertNotIn("me/admin-app", labels)

    def test_create_app_menu_guides_name_model_template_prompt_and_returns_link(self):
        self.bot.cfg = make_config(self.tmp, cloudflare_api_token="cf", cloudflare_account_id="a" * 32,
                                   cloudflare_zone_id="b" * 32)
        with patch("bot.service.CloudflareRoutes") as routes:
            routes.return_value.app_routes.return_value = [
                {"app": "eurofish", "hostname": "eurofish.kurwalab.fyi",
                 "service": "http://127.0.0.1:8080"},
            ]
            routes.return_value.next_port.return_value = 8081
            routes.return_value.prepare.return_value = (
                "Tunnel: https://menu-app.kurwalab.fyi -> http://127.0.0.1:8081.")
            self.say(CREATE_APP)
            self.say("menu-app")
            self.assertEqual(self.rt.model_checks, 1)
            self.assertIn("available in Copilot now", self.tg.sent[-1])
            labels = [button["text"] for row in self.tg.markups[-1]["keyboard"] for button in row]
            self.assertEqual(labels[:2], ["Claude/Opus 9", "GPT/GPT-9 Pro"])
            self.assertNotIn(CUSTOM_MODEL, labels)
            self.say(KNOWN_MODELS[0])
            self.assertIn("Choose one of the listed models", self.tg.sent[-1])
            self.say("Claude/Opus 9")
            self.say("eurofish")
            self.assertIn("private by default", self.tg.sent[-1])
            self.assertEqual(self.tg.markups[-1], VISIBILITY_KEYBOARD)
            self.say("eurofish")
            self.assertIn("Choose a private or public repository", self.tg.sent[-1])
            self.say(PRIVATE_REPO)
            self.assertIn("Choose the Copilot chat type", self.tg.sent[-1])
            self.assertEqual(self.tg.markups[-1], CHAT_MODE_KEYBOARD)
            self.say("eurofish")
            self.assertIn("Choose a temporary or regular chat", self.tg.sent[-1])
            self.say(REGULAR_CHAT)
            self.assertIn("styling, architecture and stack", self.tg.sent[-1])
            self.say("Build a task board with a different color palette")
        self.assertEqual(self.gh.created, [("me/menu-app", True)])
        self.assertEqual(self.rt.calls[0][2], "Claude/Opus 9")
        self.assertEqual(self.rt.calls[0][1],
                         NEW_APP_PROMPT.format(port=8081) +
                         "Build a task board with a different color palette" + DEPLOY_NOTE)
        self.assertEqual(self.rt.calls[0][4], False)
        self.assertTrue(any("App: https://menu-app.kurwalab.fyi" in message for message in self.tg.sent))
        self.assertEqual(self.tg.markups[-1], MAIN_KEYBOARD)

    def test_create_app_menu_falls_back_to_defaults_and_a_custom_model_when_the_check_fails(self):
        self.rt.model_error = RoundTripError("The Copilot model check failed: picker missing")
        self.bot.cfg = make_config(self.tmp, cloudflare_api_token="cf", cloudflare_account_id="a" * 32,
                                   cloudflare_zone_id="b" * 32)
        with patch("bot.service.CloudflareRoutes") as routes:
            routes.return_value.app_routes.return_value = [
                {"app": "eurofish", "hostname": "eurofish.kurwalab.fyi",
                 "service": "http://127.0.0.1:8080"},
            ]
            routes.return_value.next_port.return_value = 8081
            routes.return_value.prepare.return_value = "Tunnel configured."
            self.say(CREATE_APP)
            self.say("custom-model-app")
            self.assertIn("Could not check the available models", self.tg.sent[-1])
            self.assertIn("picker missing", self.tg.sent[-1])
            self.assertEqual(self.tg.markups[-1], MODEL_KEYBOARD)
            self.say(CUSTOM_MODEL)
            self.say("Other/Model 1")
            self.say("eurofish")
            self.say(PUBLIC_REPO)
            self.say(TEMPORARY_CHAT)
            self.say("Build it")
        self.assertEqual(self.gh.created, [("me/custom-model-app", False)])
        self.assertEqual(self.rt.calls[0][4], True)
        self.assertEqual(self.rt.calls[0][2], "Other/Model 1")

    def test_regular_user_create_app_menu_skips_visibility_and_creates_a_public_repository(self):
        self.bot.cfg = make_config(self.tmp, owner_ids=frozenset({42}), user_ids=frozenset({77}),
                                   cloudflare_api_token="cf", cloudflare_account_id="a" * 32,
                                   cloudflare_zone_id="b" * 32)
        with patch("bot.service.CloudflareRoutes") as routes:
            routes.return_value.app_routes.return_value = [
                {"app": "eurofish", "hostname": "eurofish.kurwalab.fyi",
                 "service": "http://127.0.0.1:8080"},
            ]
            routes.return_value.next_port.return_value = 8081
            routes.return_value.prepare.return_value = "Tunnel configured."
            self.user_say(77, CREATE_APP)
            self.user_say(77, "member-menu-app")
            self.user_say(77, "Claude/Opus 9")
            self.user_say(77, "eurofish")
            self.assertIn("styling, architecture and stack", self.tg.sent[-1])
            self.user_say(77, "Build it")
        self.assertEqual(self.gh.created, [("me/member-menu-app", False)])
        self.assertIsNone(self.rt.calls[0][4])
        self.assertFalse(any("chat type" in message for message in self.tg.sent))

    def test_fix_app_menu_selects_an_app_accepts_a_prompt_and_restores_the_menu(self):
        self.state.repo = "me/app"
        self.say(FIX_APP)
        self.say("me/app")
        self.assertEqual(self.rt.model_checks, 1)
        self.assertIn("will fix the app", self.tg.sent[-1])
        self.say("GPT/GPT-9 Pro")
        self.assertEqual(self.tg.markups[-1], CHAT_MODE_KEYBOARD)
        self.say(TEMPORARY_CHAT)
        self.assertIn("Describe what should be fixed", self.tg.sent[-1])
        self.say("Improve the empty state")
        self.assertEqual(self.rt.calls[0][4], True)
        self.assertEqual(self.rt.calls[0][0], self.dest)
        self.assertEqual(self.rt.calls[0][1], "Improve the empty state" + DEPLOY_NOTE)
        self.assertEqual(self.rt.calls[0][2], "GPT/GPT-9 Pro")
        self.assertEqual(self.state.model, "GPT/GPT-6")
        self.assertEqual(self.tg.markups[-1], MAIN_KEYBOARD)

    def test_fix_app_menu_offers_a_regular_chat_to_admins(self):
        self.say(FIX_APP)
        self.say("me/app")
        self.say("GPT/GPT-9 Pro")
        self.say(REGULAR_CHAT)
        self.say("Keep this chat")
        self.assertEqual(self.rt.calls[0][3:], (None, False))

    def test_regular_user_fix_app_menu_skips_the_chat_type(self):
        self.bot.cfg = make_config(self.tmp, user_ids=frozenset({77}))
        self.state.set_app_owner("me/app", telegram_owner(77))
        self.user_say(77, FIX_APP)
        self.user_say(77, "me/app")
        self.user_say(77, "GPT/GPT-9 Pro")
        self.assertIn("Describe what should be fixed", self.tg.sent[-1])
        self.assertNotEqual(self.tg.markups[-1], CHAT_MODE_KEYBOARD)
        self.user_say(77, "Improve it")
        self.assertIsNone(self.rt.calls[0][4])

    def test_cancel_during_the_model_check_stops_it_and_closes_the_dialog(self):
        started, release = threading.Event(), threading.Event()

        def slow_models(on_status, cancel):
            started.set()
            self.assertTrue(release.wait(10))
            if cancel.is_set():
                raise Cancelled()
            return ["GPT/GPT-9 Pro"]

        self.rt.list_models = slow_models
        self.bot.handle_text(1, FIX_APP)
        self.bot.handle_text(1, "me/app")
        self.assertTrue(started.wait(10))
        self.bot.handle_text(1, "Improve it")
        self.assertIn("Still checking the available models", self.tg.sent[-1])
        self.bot.handle_text(1, "/cancel")
        self.assertTrue(self.bot._cancel.is_set())
        release.set()
        self.bot._worker.join(timeout=30)
        self.assertNotIn(1, self.bot._dialogs)
        self.assertEqual(self.rt.calls, [])

    def test_delete_app_menu_requires_confirmation(self):
        self.say(DELETE_APP)
        self.say("app")
        self.assertEqual(self.gh.deleted, [])
        self.say(CONFIRM_DELETE)
        self.assertEqual(self.gh.deleted, ["me/app"])
        self.assertEqual(self.tg.markups[-1], MAIN_KEYBOARD)

    def test_progress_stages_have_a_current_header_and_timestamp(self):
        with patch("bot.roundtrip.format_timestamp", return_value="16:21:21"):
            progress = Progress(self.tg, 1, self.cfg)
            progress.update("Preparing repository...")
        self.assertEqual(self.tg.sent[-1],
                         "Currently doing: Preparing repository...\n\n"
                         "[16:21:21] Preparing repository...")

    def test_repository_context_does_not_displace_the_current_header(self):
        message = "Currently doing: Testing\n\n[16:21:21] ▶ Testing"
        self.assertEqual(self.bot._repo_progress("me/app", message),
                         "Currently doing: Testing\nRepository: https://github.com/me/app\n\n"
                         "[16:21:21] ▶ Testing")

    def test_request_without_repository_asks_to_select_one(self):
        self.say("add a login page")
        self.assertIn("Select a repository first", self.tg.sent[-1])
        self.assertEqual(self.rt.calls, [])

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

    def test_new_creates_builds_deploys_routes_and_reports_output(self):
        self.bot.cfg = make_config(self.tmp, cloudflare_api_token="cf", cloudflare_account_id="a" * 32,
                                   cloudflare_zone_id="b" * 32)
        with patch("bot.service.CloudflareRoutes") as routes:
            routes.return_value.next_port.return_value = 8081
            routes.return_value.prepare.return_value = (
                "Tunnel: https://fresh-app.kurwalab.fyi -> http://127.0.0.1:8081.")
            self.say("/new fresh-app build a starter app")
        self.assertEqual(self.gh.created, [("me/fresh-app", True)])
        self.assertEqual(self.state.repo, "me/fresh-app")
        self.assertEqual(len(self.rt.calls), 1)
        self.assertEqual(self.rt.calls[0][0], self.cfg.repos_dir / "fresh-app")
        self.assertEqual(self.rt.calls[0][1],
                         NEW_APP_PROMPT.format(port=8081) + "build a starter app" + DEPLOY_NOTE)
        self.assertEqual(self.rt.calls[0][2], "GPT/GPT-6")
        routes.return_value.next_port.assert_called_once_with()
        routes.return_value.prepare.assert_called_once_with("fresh-app", 8081)
        self.assertEqual(self.deployer.calls, [("fresh-app", self.cfg.repos_dir / "fresh-app")])
        self.assertEqual(self.deployer.registered, [("fresh-app", self.cfg.repos_dir / "fresh-app")])
        self.assertEqual(self.state.app_owner("me/fresh-app"), ADMIN_OWNER)
        report = self.tg.sent[-1]
        self.assertIn("https://fresh-app.kurwalab.fyi", report)
        self.assertIn("Copilot output:\nAdded it.", report)
        self.assertIn("Deploy: Systemd service copilot-app-fresh-app.service", report)
        self.assertIn("Tunnel: https://fresh-app.kurwalab.fyi -> http://127.0.0.1:8081.", report)

    def test_regular_user_creation_records_ownership_and_allows_only_owner_fixes(self):
        self.bot.cfg = make_config(self.tmp, owner_ids=frozenset({42}), user_ids=frozenset({77, 88}),
                                   cloudflare_api_token="cf", cloudflare_account_id="a" * 32,
                                   cloudflare_zone_id="b" * 32)
        with patch("bot.service.CloudflareRoutes") as routes:
            routes.return_value.next_port.return_value = 8085
            routes.return_value.prepare.return_value = "Tunnel configured."
            self.user_say(77, "/new member-app build it")
        self.assertEqual(self.gh.created, [("me/member-app", False)])
        report = self.tg.sent[-1]
        self.assertIn("App: https://member-app.kurwalab.fyi", report)
        self.assertIn("Copilot output:\nAdded it.", report)
        for hidden in ("Deploy:", "Tunnel", "systemd", "Systemd", "pid", "8085", str(self.cfg.repos_dir)):
            self.assertNotIn(hidden, report)
        for progress in self.tg.sent[:-1] + self.tg.edits:
            for hidden in ("systemd", "tunnel", "port", str(self.cfg.repos_dir)):
                self.assertNotIn(hidden, progress)
        self.assertEqual(self.state.app_owner("me/member-app"), telegram_owner(77))
        self.assertEqual(self.state.get_active_repo(77), "me/member-app")
        saved = State(self.tmp / "state.json", "fallback")
        self.assertEqual(saved.app_owner("me/member-app"), telegram_owner(77))
        with patch("bot.service.CloudflareRoutes") as routes:
            routes.return_value.app_routes.return_value = []
            self.user_say(77, "/list")
        self.assertIn("name: member-app", self.tg.sent[-1])
        self.assertNotIn("folder:", self.tg.sent[-1])
        self.assertNotIn("port:", self.tg.sent[-1])

        calls = len(self.rt.calls)
        self.rt.behavior = "append"
        self.user_say(88, "/fix member-app change the title")
        self.assertEqual(len(self.rt.calls), calls)
        self.assertIn("only fix apps you created", self.tg.sent[-1])

        self.user_say(77, "/fix member-app change the title")
        self.assertEqual(len(self.rt.calls), calls + 1)
        report = self.tg.sent[-1]
        self.assertIn("App: https://member-app.kurwalab.fyi", report)
        for hidden in ("Deploy:", "systemd", "Systemd", "pid"):
            self.assertNotIn(hidden, report)

    def test_regular_user_failures_hide_service_and_tunnel_details(self):
        self.bot.cfg = make_config(self.tmp, owner_ids=frozenset({42}), user_ids=frozenset({77}),
                                   cloudflare_api_token="cf", cloudflare_account_id="a" * 32,
                                   cloudflare_zone_id="b" * 32)
        with patch("bot.service.CloudflareRoutes") as routes:
            routes.return_value.next_port.return_value = 8085
            routes.return_value.prepare.side_effect = CloudflareError("ingress for kurwalab-tunnel rejected")
            self.user_say(77, "/new member-app build it")
        report = self.tg.sent[-1]
        self.assertIn("The app address could not be published", report)
        self.assertNotIn("kurwalab-tunnel", report)
        self.assertNotIn("Deploy:", report)
        self.deployer.ok = False
        self.rt.behavior = "append"
        self.user_say(77, "/fix member-app break it")
        self.assertIn("redeploy failed. Ask an admin", self.tg.sent[-1])
        self.assertNotIn("copilot-app-", self.tg.sent[-1])
        with patch("bot.service.CloudflareRoutes") as routes:
            routes.return_value.next_port.side_effect = CloudflareError("kurwalab-tunnel unavailable")
            self.user_say(77, "/new other-app build it")
        self.assertIn("app address could not be configured", self.tg.sent[-1])
        self.assertNotIn("kurwalab-tunnel", self.tg.sent[-1])

    def test_regular_users_cannot_create_private_repositories(self):
        self.bot.cfg = make_config(self.tmp, owner_ids=frozenset({42}), user_ids=frozenset({77}),
                                   cloudflare_api_token="cf", cloudflare_account_id="a" * 32,
                                   cloudflare_zone_id="b" * 32)
        self.user_say(77, "/new private member-app build it")
        self.assertIn("use public repositories", self.tg.sent[-1])
        self.assertEqual(self.gh.created, [])

    def test_admin_can_fix_a_regular_users_app_without_changing_its_owner(self):
        self.state.set_app_owner("me/app", telegram_owner(77))
        self.state.save()
        self.say("/fix app update it")
        self.assertEqual(len(self.rt.calls), 1)
        self.assertEqual(self.state.app_owner("me/app"), telegram_owner(77))

    def test_new_name_option_creates_and_publishes_the_named_app(self):
        self.bot.cfg = make_config(self.tmp, cloudflare_api_token="cf", cloudflare_account_id="a" * 32,
                                   cloudflare_zone_id="b" * 32)
        with patch("bot.service.CloudflareRoutes") as routes:
            routes.return_value.next_port.return_value = 8084
            routes.return_value.prepare.return_value = "Tunnel configured."
            self.say('/new name="org/named-app" public template="eurofish" '
                     'model="Claude/Opus 5.5" build a starter app')
        self.assertEqual(self.gh.created, [("org/named-app", False)])
        self.assertEqual(self.state.repo, "org/named-app")
        self.assertEqual(self.rt.calls[0][0], self.cfg.repos_dir / "named-app")
        self.assertEqual(self.rt.calls[0][1],
                         NEW_APP_PROMPT.format(port=8084) + "build a starter app" + DEPLOY_NOTE)
        self.assertEqual(self.rt.calls[0][2], "Claude/Opus 5.5")
        routes.return_value.prepare.assert_called_once_with("named-app", 8084)

    def test_new_name_option_parses_quotes_order_and_preserves_request(self):
        for options in ('name="fresh-app" public', "private name='fresh-app'",
                        'template=eurofish name=fresh-app model=GPT/GPT-6'):
            with self.subTest(options=options):
                remaining, private, temporary_chat, model, template, name, prompt, error = Bot._new_options(
                    options + ' build "a page" with name="Title"')
                self.assertEqual(name, "fresh-app")
                self.assertEqual(remaining, 'build "a page" with name="Title"')
                self.assertEqual(error, "")

    def test_new_prompt_option_parses_in_any_named_option_order(self):
        request = 'Build a page with name="Title", model=example and template=demo.'
        for options in (
                "name=fresh-app prompt='" + request + "' public template=eurofish",
                "prompt='" + request + "' model=GPT/GPT-6 name=fresh-app"):
            with self.subTest(options=options):
                remaining, private, temporary_chat, model, template, name, prompt, error = Bot._new_options(options)
                self.assertEqual((remaining, name, prompt, error), ("", "fresh-app", request, ""))

    def test_new_chat_type_option_is_parsed_in_any_order(self):
        for options, expected in (("tempchat name=fresh-app", True),
                                  ("name=fresh-app public notempchat", False),
                                  ("name=fresh-app", None)):
            with self.subTest(options=options):
                remaining, private, temporary_chat, model, template, name, prompt, error = Bot._new_options(
                    options + " build it")
                self.assertEqual((remaining, temporary_chat, name, error), ("build it", expected, "fresh-app", ""))

    def test_new_chat_type_option_is_passed_for_admins_and_refused_for_regular_users(self):
        self.bot.cfg = make_config(self.tmp, owner_ids=frozenset({42}), user_ids=frozenset({77}),
                                   cloudflare_api_token="cf", cloudflare_account_id="a" * 32,
                                   cloudflare_zone_id="b" * 32)
        self.user_say(77, "/new notempchat member-app build it")
        self.assertIn("Only kurwalab.fyi admins can choose the Copilot chat type", self.tg.sent[-1])
        self.assertEqual(self.gh.created, [])
        with patch("bot.service.CloudflareRoutes") as routes:
            routes.return_value.next_port.return_value = 8084
            routes.return_value.prepare.return_value = "Tunnel configured."
            self.say("/new notempchat saved-app build it")
        self.assertEqual(self.rt.calls[0][4], False)

    def test_new_options_accept_smart_quotes_and_a_final_unquoted_prompt(self):
        for options in (
                'model=“Claude/Opus 5.5” template=simple-arkanoid '
                'name=seabattle prompt=Simple sea battle game',
                'model=“Claude/Opus 5.5” template=“simple-arkanoid” '
                'name=“seabattle” prompt=Simple sea battle game',
                'model=“Claude/Opus 5.5” template=“simple-arkanoid” '
                'name=“seabattle-game” prompt=Simple sea battle game'):
            with self.subTest(options=options):
                remaining, private, temporary_chat, model, template, name, prompt, error = Bot._new_options(options)
                self.assertEqual(remaining, "")
                self.assertIsNone(private)
                self.assertEqual(model, "Claude/Opus 5.5")
                self.assertEqual(template, "simple-arkanoid")
                self.assertIn(name, ("seabattle", "seabattle-game"))
                self.assertEqual(prompt, "Simple sea battle game")
                self.assertEqual(error, "")

    def test_new_prompt_option_creates_app_with_separate_request(self):
        self.bot.cfg = make_config(self.tmp, cloudflare_api_token="cf",
                                   cloudflare_account_id="a" * 32, cloudflare_zone_id="b" * 32)
        with patch("bot.service.CloudflareRoutes") as routes:
            routes.return_value.next_port.return_value = 8084
            routes.return_value.prepare.return_value = "Tunnel configured."
            self.say('/new prompt="build a starter app" name=fresh-app public')
        self.assertEqual(self.gh.created, [("me/fresh-app", False)])
        self.assertEqual(self.rt.calls[0][1],
                         NEW_APP_PROMPT.format(port=8084) + "build a starter app" + DEPLOY_NOTE)

    def test_new_prompt_option_supports_positional_repository(self):
        self.bot.cfg = make_config(self.tmp, cloudflare_api_token="cf",
                                   cloudflare_account_id="a" * 32, cloudflare_zone_id="b" * 32)
        with patch("bot.service.CloudflareRoutes") as routes:
            routes.return_value.next_port.return_value = 8084
            routes.return_value.prepare.return_value = "Tunnel configured."
            self.say('/new prompt="build it" fresh-app')
        self.assertEqual(self.gh.created, [("me/fresh-app", True)])
        self.assertEqual(self.rt.calls[0][1],
                         NEW_APP_PROMPT.format(port=8084) + "build it" + DEPLOY_NOTE)

    def test_new_rejects_invalid_duplicate_or_ambiguous_prompt(self):
        for command in ('/new name=fresh-app prompt=', '/new name=fresh-app prompt=""',
                        '/new name=fresh-app prompt="   "',
                        '/new name=fresh-app prompt="unclosed',
                        '/new name=fresh-app prompt=one prompt=two',
                        '/new name=fresh-app prompt="build it" extra text',
                        '/new prompt="build it"'):
            with self.subTest(command=command):
                self.say(command)
                self.assertTrue(any(text in self.tg.sent[-1]
                                    for text in ('Use prompt=', 'only once', 'Usage: /new')))
        self.assertEqual(self.gh.created, [])
        self.assertEqual(self.rt.calls, [])

    def test_new_rejects_missing_malformed_or_repeated_name_options(self):
        for command in ('/new name=', '/new name="" build it',
                        '/new name="two words" build it', '/new name="unclosed build it',
                        '/new name=one name=two build it', '/new name="fresh-app"'):
            with self.subTest(command=command):
                self.say(command)
                self.assertTrue("Use name=" in self.tg.sent[-1] or
                                "Specify name only once" in self.tg.sent[-1] or
                                "Usage: /new" in self.tg.sent[-1])
        self.assertEqual(self.gh.created, [])
        self.assertEqual(self.rt.calls, [])

    def test_new_model_option_overrides_only_the_new_app_run(self):
        self.bot.cfg = make_config(self.tmp, cloudflare_api_token="cf", cloudflare_account_id="a" * 32,
                                   cloudflare_zone_id="b" * 32)
        with patch("bot.service.CloudflareRoutes") as routes:
            routes.return_value.next_port.return_value = 8082
            routes.return_value.prepare.return_value = "Tunnel configured."
            self.say('/new model="Claude/Opus 5.5 (Preview)" public model-app build it')
        self.assertEqual(self.gh.created, [("me/model-app", False)])
        self.assertEqual(self.rt.calls[0][2], "Claude/Opus 5.5 (Preview)")
        self.assertEqual(self.state.model, "GPT/GPT-6")

    def test_new_can_copy_a_selected_local_app_template(self):
        source = self.cfg.repos_dir / "source-app"
        source.mkdir()
        (source / "README.md").write_text("selected template\n")
        (source / "app.txt").write_text("source content\n")
        self.bot.cfg = make_config(self.tmp, cloudflare_api_token="cf", cloudflare_account_id="a" * 32,
                                   cloudflare_zone_id="b" * 32)
        with patch("bot.service.CloudflareRoutes") as routes:
            routes.return_value.next_port.return_value = 8082
            routes.return_value.prepare.return_value = "Tunnel configured."
            self.say('/new template="source-app" template-copy build it')
        dest = self.cfg.repos_dir / "template-copy"
        self.assertEqual((dest / "README.md").read_text(), "selected template\n")
        self.assertEqual((dest / "app.txt").read_text(), "source content\n")
        self.assertNotIn(".git", git("ls-tree", "-r", "--name-only", "HEAD", cwd=dest).splitlines())

    def test_new_rejects_invalid_missing_or_repeated_templates(self):
        self.bot.cfg = make_config(self.tmp, cloudflare_api_token="cf", cloudflare_account_id="a" * 32,
                                   cloudflare_zone_id="b" * 32)
        self.say("/new template=../source new-app build it")
        self.assertIn("Template app names", self.tg.sent[-1])
        self.say("/new template=missing new-app build it")
        self.assertIn("not available locally", self.tg.sent[-1])
        self.say("/new template=eurofish template=eurofish new-app build it")
        self.assertIn("only once", self.tg.sent[-1])
        self.say("/new template=eurofish eurofish build it")
        self.assertIn("must differ", self.tg.sent[-1])
        self.assertEqual(self.gh.created, [])

    def test_new_rejects_invalid_or_repeated_model_options(self):
        self.say("/new model=invalid model-app build it")
        self.assertIn("provider/model", self.tg.sent[-1])
        self.say("/new model=GPT/GPT-6 model=Claude/Opus model-app build it")
        self.assertIn("only once", self.tg.sent[-1])
        self.assertEqual(self.gh.created, [])

    def test_visibility_choice_for_new_and_repo(self):
        self.bot.cfg = make_config(self.tmp, cloudflare_api_token="cf", cloudflare_account_id="a" * 32,
                                   cloudflare_zone_id="b" * 32)
        with patch("bot.service.CloudflareRoutes") as routes:
            routes.return_value.next_port.return_value = 8080
            routes.return_value.prepare.return_value = "Tunnel configured."
            self.say("/new public open-app build it")
        self.assertEqual(self.gh.created, [("me/open-app", False)])
        self.say("/repo private closed-app")
        self.assertEqual(self.gh.created[-1], ("me/closed-app", True))
        self.say("/repo public app")
        self.assertEqual(len(self.gh.created), 2)  # Existing visibility is not changed.
        self.say("/new public")
        self.assertIn("Usage: /new", self.tg.sent[-1])

    def test_new_refuses_existing_repo_and_requires_name_and_request(self):
        self.say("/new")
        self.assertIn("Usage: /new", self.tg.sent[-1])
        self.say("/new app")
        self.assertIn("Usage: /new", self.tg.sent[-1])
        self.bot.cfg = make_config(self.tmp, cloudflare_api_token="cf", cloudflare_account_id="a" * 32,
                                   cloudflare_zone_id="b" * 32)
        self.say("/new app build it")
        self.assertIn("already exists", self.tg.sent[-1])
        self.assertEqual(self.gh.created, [])
        self.assertEqual(self.state.repo, "")

    def test_new_requires_cloudflare_and_a_dns_safe_app_name(self):
        self.say("/new fresh-app build it")
        self.assertIn("Configure Cloudflare", self.tg.sent[-1])
        self.bot.cfg = make_config(self.tmp, cloudflare_api_token="cf", cloudflare_account_id="a" * 32,
                                   cloudflare_zone_id="b" * 32)
        for name in ("bad_name", "BadName", "bad.name"):
            self.say(f"/new {name} build it")
            self.assertIn("lowercase DNS labels", self.tg.sent[-1])
        self.assertEqual(self.gh.created, [])

    def test_delete_removes_app_resources_and_clears_active_repository(self):
        self.say("/repo app")
        self.bot.cfg = make_config(self.tmp, cloudflare_api_token="cf", cloudflare_account_id="a" * 32,
                                   cloudflare_zone_id="b" * 32)
        self.deployer.stop_result = True
        with patch("bot.service.CloudflareRoutes") as routes:
            routes.return_value.delete_route.return_value = "Deleted route: https://app.kurwalab.fyi."
            self.say("/delete app")
            routes.return_value.delete_route.assert_called_once_with("app", missing_ok=True)
        self.assertEqual(self.gh.deleted, ["me/app"])
        self.assertFalse(self.dest.exists())
        self.assertEqual(self.deployer.stops, [("app", self.dest)])
        self.assertEqual(self.deployer.unregistered, [("app", self.dest)])
        self.assertEqual(self.state.repo, "")
        self.assertEqual(State(self.tmp / "state.json", "x").repo, "")
        self.assertEqual(self.state.app_owner("me/app"), "")
        self.assertIn("Deletion completed for me/app", self.tg.sent[-1])
        self.assertIn("Service process: stopped the running app", self.tg.sent[-1])

    def test_delete_validates_arguments_and_app_name(self):
        self.say("/delete")
        self.assertIn("Usage: /delete", self.tg.sent[-1])
        self.say("/delete app extra")
        self.assertIn("Usage: /delete", self.tg.sent[-1])
        self.say("/delete BadName")
        self.assertIn("lowercase DNS labels", self.tg.sent[-1])
        self.assertEqual(self.gh.deleted, [])

    def test_delete_refuses_the_bot_repository(self):
        (self.cfg.repos_dir / "linux-copilot-bot").mkdir(exist_ok=True)
        self.say("/delete linux-copilot-bot")
        self.assertIn("cannot be deleted as an app", self.tg.sent[-1])
        self.assertEqual(self.gh.deleted, [])

    def test_delete_without_cloudflare_still_attempts_github_and_local_cleanup(self):
        self.say("/repo app")
        self.say("/delete app")
        self.assertEqual(self.gh.deleted, ["me/app"])
        self.assertFalse(self.dest.exists())
        report = self.tg.sent[-1]
        self.assertIn("GitHub: deleted me/app", report)
        self.assertIn("Local folder: deleted", report)
        self.assertIn("Cloudflare is not configured", report)

    def test_delete_missing_local_folder_does_not_block_remote_deletions(self):
        self.bot.cfg = make_config(self.tmp, cloudflare_api_token="cf", cloudflare_account_id="a" * 32,
                                   cloudflare_zone_id="b" * 32)
        with patch("bot.service.CloudflareRoutes") as routes:
            routes.return_value.delete_route.return_value = "Deleted route: https://app.kurwalab.fyi."
            self.say("/delete app")
            routes.return_value.delete_route.assert_called_once_with("app", missing_ok=True)
        self.assertIn("Local folder:", self.tg.sent[-1])
        self.assertIn("was not found; nothing to delete", self.tg.sent[-1])
        self.assertEqual(self.gh.deleted, ["me/app"])

    def test_delete_reports_missing_resources_as_successful_noops(self):
        self.bot.cfg = make_config(self.tmp, cloudflare_api_token="cf", cloudflare_account_id="a" * 32,
                                   cloudflare_zone_id="b" * 32)
        self.gh.repos.clear()
        with patch("bot.service.CloudflareRoutes") as routes:
            routes.return_value.delete_route.return_value = (
                "route https://app.kurwalab.fyi and its matching CNAME were not found; nothing to delete.")
            self.say("/delete app")
        report = self.tg.sent[-1]
        self.assertIn("Deletion completed for me/app", report)
        self.assertIn("GitHub: me/app was not found", report)
        self.assertIn("Local folder:", report)
        self.assertIn("Tunnel route:", report)

    def test_delete_attempts_other_resources_after_github_and_tunnel_failures(self):
        self.say("/repo app")
        self.bot.cfg = make_config(self.tmp, cloudflare_api_token="cf", cloudflare_account_id="a" * 32,
                                   cloudflare_zone_id="b" * 32)
        self.gh.delete_error = GitHubError("denied")
        with patch("bot.service.CloudflareRoutes") as routes:
            routes.return_value.delete_route.side_effect = CloudflareError("unavailable")
            self.say("/delete app")
        self.assertFalse(self.dest.exists())
        report = self.tg.sent[-1]
        self.assertIn("GitHub: failed to delete me/app", report)
        self.assertIn("Local folder: deleted", report)
        self.assertIn("Tunnel route: failed to delete", report)

    def test_delete_local_safety_failure_does_not_block_remote_deletions(self):
        self.dest.mkdir(parents=True)
        (self.dest / "unrelated.txt").write_text("keep")
        self.bot.cfg = make_config(self.tmp, cloudflare_api_token="cf", cloudflare_account_id="a" * 32,
                                   cloudflare_zone_id="b" * 32)
        with patch("bot.service.CloudflareRoutes") as routes:
            routes.return_value.delete_route.return_value = "Deleted route: https://app.kurwalab.fyi."
            self.say("/delete app")
        self.assertTrue(self.dest.exists())
        self.assertEqual(self.gh.deleted, ["me/app"])
        self.assertIn("not a Git checkout", self.tg.sent[-1])

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
        self.assertIn("copilot-app-app.service failed", self.tg.sent[-1])

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

    def test_cloudflare_command_routes_only_on_request(self):
        self.bot.cfg = make_config(self.tmp, cloudflare_api_token="cf", cloudflare_account_id="a" * 32,
                                   cloudflare_zone_id="b" * 32)
        with patch("bot.service.CloudflareRoutes") as routes:
            routes.return_value.prepare.return_value = "Tunnel configured."
            self.say("/cloudflare eurofish 8080")
            routes.return_value.prepare.assert_called_once_with("eurofish", 8080)
        self.assertIn("Tunnel configured", self.tg.sent[-1])

    def test_cloudflare_list_and_delete_commands(self):
        self.bot.cfg = make_config(self.tmp, cloudflare_api_token="cf", cloudflare_account_id="a" * 32,
                                   cloudflare_zone_id="b" * 32)
        with patch("bot.service.CloudflareRoutes") as routes:
            routes.return_value.list_routes.return_value = "Routes on tunnel:\n- app.example -> service"
            routes.return_value.delete_route.return_value = "Deleted route."
            self.say("/cloudflare list")
            routes.return_value.list_routes.assert_called_once_with()
            self.say("/cloudflare delete eurofish")
            routes.return_value.delete_route.assert_called_once_with("eurofish")
        self.assertIn("Deleted route", self.tg.sent[-1])

    def test_cloudflare_delete_requires_a_valid_route_name(self):
        for command in ("/cloudflare delete", "/cloudflare delete bad.name", "/cloudflare list extra"):
            self.say(command)
            self.assertTrue("Usage:" in self.tg.sent[-1] or "must be" in self.tg.sent[-1])

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

    def test_admin_registers_all_existing_apps_but_not_the_bot_folder(self):
        for name in ("existing-one", "existing-two", "linux-copilot-bot"):
            (self.cfg.repos_dir / name).mkdir(exist_ok=True)
        self.say("/register")
        registered = {name for name, _ in self.deployer.registered}
        self.assertIn("eurofish", registered)
        self.assertIn("existing-one", registered)
        self.assertIn("existing-two", registered)
        self.assertNotIn("linux-copilot-bot", registered)
        self.assertEqual(self.state.app_owner("me/existing-one"), ADMIN_OWNER)
        self.assertIn("Systemd app service registration", self.tg.sent[-1])

    def test_app_controls_enforce_regular_user_ownership(self):
        self.bot.cfg = make_config(self.tmp, owner_ids=frozenset({42}), user_ids=frozenset({77, 88}))
        for name in ("mine", "theirs"):
            (self.cfg.repos_dir / name).mkdir(exist_ok=True)
        self.state.set_app_owner("me/mine", telegram_owner(77))
        self.state.set_app_owner("me/theirs", telegram_owner(88))
        self.user_say(77, "/app start mine")
        self.assertEqual(self.deployer.starts, [("mine", self.cfg.repos_dir / "mine")])
        self.assertEqual(self.tg.sent[-1], "✅ mine is running: https://mine.kurwalab.fyi")
        self.deployer.stop_result = True
        self.user_say(77, "/app stop mine")
        self.assertEqual(self.deployer.stops, [("mine", self.cfg.repos_dir / "mine")])
        self.assertEqual(self.tg.sent[-1], "✅ mine is stopped.")
        self.user_say(77, "/app start missing")
        self.assertEqual(self.tg.sent[-1], "App not found: missing")
        self.user_say(77, "/app stop theirs")
        self.assertIn("only start or stop apps you created", self.tg.sent[-1])
        self.assertEqual(self.deployer.stops, [("mine", self.cfg.repos_dir / "mine")])
        self.say("/app start theirs")
        self.assertEqual(self.deployer.starts[-1], ("theirs", self.cfg.repos_dir / "theirs"))
        self.assertIn("copilot-app-theirs.service is running (pid 1)", self.tg.sent[-1])

    def test_app_control_buttons_show_only_regular_users_owned_apps(self):
        self.bot.cfg = make_config(self.tmp, owner_ids=frozenset({42}), user_ids=frozenset({77, 88}))
        for name in ("mine", "theirs"):
            (self.cfg.repos_dir / name).mkdir(exist_ok=True)
        self.state.set_app_owner("me/mine", telegram_owner(77))
        self.state.set_app_owner("me/theirs", telegram_owner(88))
        self.user_say(77, START_APP)
        labels = {button["text"] for row in self.tg.markups[-1]["keyboard"] for button in row}
        self.assertIn("mine", labels)
        self.assertNotIn("theirs", labels)

    def test_list_reports_every_local_folder_with_admin_details(self):
        for name in ("ihatepdf", "eurofish", "stravall", "carcalendar", "eurocat"):
            (self.cfg.repos_dir / name).mkdir(exist_ok=True)
        self.deployer.running = [("eurofish", 101), ("stravall", 202)]
        self.bot.cfg = make_config(self.tmp, cloudflare_api_token="cf", cloudflare_account_id="a" * 32,
                                   cloudflare_zone_id="b" * 32)
        with patch("bot.service.CloudflareRoutes") as routes:
            routes.return_value.app_routes.return_value = [
                {"app": "eurofish", "hostname": "eurofish.kurwalab.fyi",
                 "service": "http://127.0.0.1:8080"},
                {"app": "eurocat", "hostname": "eurocat.kurwalab.fyi",
                 "service": "http://127.0.0.1:8084"},
            ]
            self.say("/list")
        listing = self.tg.sent[-1]
        for name in ("ihatepdf", "eurofish", "stravall", "carcalendar", "eurocat"):
            self.assertIn(f"name: {name}", listing)
            self.assertIn(f"folder: {self.cfg.repos_dir / name}", listing)
        self.assertIn("name: eurofish — status: running", listing)
        self.assertIn("name: stravall — status: running", listing)
        self.assertIn("name: eurocat — status: stopped", listing)
        self.assertIn("port: 8084 — url: https://eurocat.kurwalab.fyi", listing)
        self.assertIn("name: ihatepdf — status: stopped", listing)
        self.assertIn("port: not configured — url: not configured", listing)

    def test_list_shows_the_bot_only_to_admins_and_uses_detailed_systemd_status(self):
        self.bot.cfg = make_config(self.tmp, owner_ids=frozenset({42}), user_ids=frozenset({77}))
        (self.cfg.repos_dir / "linux-copilot-bot").mkdir(exist_ok=True)
        self.deployer.statuses["eurofish"] = "failed"
        self.user_say(42, "/list")
        self.assertIn("name: eurofish — status: failed", self.tg.sent[-1])
        self.assertIn("name: linux-copilot-bot (this bot, copilot-telegram-bot.service) — status: running (pid 7)",
                      self.tg.sent[-1])
        self.assertNotIn("copilot-app-linux-copilot-bot", self.tg.sent[-1])
        self.user_say(77, "/list")
        self.assertNotIn("linux-copilot-bot", self.tg.sent[-1])
        self.user_say(42, USER_VIEW)
        self.user_say(42, "/list")
        self.assertNotIn("linux-copilot-bot", self.tg.sent[-1])

    def make_bot_checkout(self):
        """A bot checkout with an ignored .env, cloned from a local origin standing in for GitHub."""
        origin = make_origin(self.tmp, "bot.git")
        seed = self.tmp / "bot-seed"
        git("clone", str(origin), str(seed))
        (seed / ".gitignore").write_text(".env\n")
        git("add", "-A", cwd=seed)
        git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-m", "ignore", cwd=seed)
        git("push", "origin", "main", cwd=seed)
        live = self.cfg.repos_dir / "linux-copilot-bot"
        git("clone", str(origin), str(live))
        (live / ".env").write_text("TELEGRAM_BOT_TOKEN=secret\n")
        self.gh.repos["me/linux-copilot-bot"] = {"clone_url": str(origin), "default_branch": "main"}
        return origin, live

    def test_admin_fixes_the_bot_from_a_separate_clone_and_restarts_it(self):
        origin, live = self.make_bot_checkout()
        self.say(FIX_APP)
        labels = {button["text"] for row in self.tg.markups[-1]["keyboard"] for button in row}
        self.assertIn("me/linux-copilot-bot", labels)
        self.say("linux-copilot-bot")
        self.say("GPT/GPT-9 Pro")
        self.say(TEMPORARY_CHAT)
        self.say("Improve the help text")
        self.assertEqual(len(self.rt.calls), 1)
        self.assertNotEqual(self.rt.calls[0][0], live)
        self.assertEqual(self.rt.calls[0][1], "Improve the help text" + BOT_DEPLOY_NOTE)
        self.assertEqual(git("log", "-1", "--format=%s", cwd=origin), "Copilot: Improve the help text")
        self.assertTrue((live / "Makefile").is_file())
        self.assertEqual((live / ".env").read_text(), "TELEGRAM_BOT_TOKEN=secret\n")
        self.assertEqual(self.deployer.calls, [])
        self.assertEqual(self.deployer.bot_restarts, 1)
        self.assertIn("copilot-telegram-bot.service is restarting", self.tg.sent[-2])
        self.assertEqual(self.gh.created, [])
        self.assertEqual(self.state.app_owner("me/linux-copilot-bot"), "")
        self.assertEqual(self.state.repo, "me/linux-copilot-bot")

    def test_bot_fix_outside_systemd_is_not_restarted(self):
        _, live = self.make_bot_checkout()
        self.deployer.as_service = False
        self.say("/fix linux-copilot-bot tidy it")
        self.assertEqual(self.deployer.bot_restarts, 0)
        self.assertIn("not running as its systemd service", self.tg.sent[-1])
        self.assertTrue((live / "Makefile").is_file())

    def test_regular_users_never_see_or_change_the_bot(self):
        self.bot.cfg = make_config(self.tmp, owner_ids=frozenset({42}), user_ids=frozenset({77}),
                                   cloudflare_api_token="cf", cloudflare_account_id="a" * 32,
                                   cloudflare_zone_id="b" * 32)
        self.make_bot_checkout()
        self.state.set_active_repo(None, "me/linux-copilot-bot")
        self.state.set_app_owner("me/linux-copilot-bot", ADMIN_OWNER)
        for label in (FIX_APP, START_APP):
            self.user_say(77, label)
            self.assertNotIn("linux-copilot-bot", str(self.tg.sent[-1]) + str(self.tg.markups[-1]))
        self.bot._dialogs.clear()
        self.user_say(77, "/fix linux-copilot-bot change it")
        self.assertIn("only fix apps you created", self.tg.sent[-1])
        self.user_say(77, "/app stop linux-copilot-bot")
        self.assertEqual(self.tg.sent[-1], "App not found: linux-copilot-bot")
        self.user_say(77, "/new template=linux-copilot-bot copy-app build it")
        self.assertIn("Template app is not available locally", self.tg.sent[-1])
        with patch("bot.service.CloudflareRoutes") as routes:
            routes.return_value.app_routes.return_value = [
                {"app": "linux-copilot-bot", "hostname": "linux-copilot-bot.kurwalab.fyi",
                 "service": "http://127.0.0.1:9000"},
                {"app": "eurofish", "hostname": "eurofish.kurwalab.fyi", "service": "http://127.0.0.1:8080"},
            ]
            self.user_say(77, "/list")
            self.assertNotIn("linux-copilot-bot", self.tg.sent[-1])
            self.user_say(77, CREATE_APP)
            self.user_say(77, "member-app")
            self.user_say(77, "Claude/Opus 9")
        labels = {button["text"] for row in self.tg.markups[-1]["keyboard"] for button in row}
        self.assertIn("eurofish", labels)
        self.assertNotIn("linux-copilot-bot", labels)
        self.assertEqual(self.rt.calls, [])
        self.assertEqual(self.gh.created, [])
        self.assertEqual(self.deployer.bot_restarts, 0)

    def test_regular_list_omits_admin_only_folder_and_port(self):
        self.bot.cfg = make_config(self.tmp, owner_ids=frozenset({42}), user_ids=frozenset({77}),
                                   cloudflare_api_token="cf", cloudflare_account_id="a" * 32,
                                   cloudflare_zone_id="b" * 32)
        self.deployer.running = [("eurofish", 101)]
        with patch("bot.service.CloudflareRoutes") as routes:
            routes.return_value.app_routes.return_value = [{
                "app": "eurofish", "hostname": "eurofish.kurwalab.fyi",
                "service": "http://127.0.0.1:8080",
            }]
            self.user_say(77, "/list")
        self.assertEqual(
            self.tg.sent[-1],
            "Apps:\n- name: eurofish — status: running — url: https://eurofish.kurwalab.fyi",
        )

    def test_list_reports_unavailable_routes_without_cloudflare_configuration(self):
        self.say("/list")
        self.assertIn("name: eurofish — status: stopped", self.tg.sent[-1])
        self.assertIn("port: unavailable — url: unavailable", self.tg.sent[-1])
        self.say("/list extra")
        self.assertEqual(self.tg.sent[-1], "Usage: /list")

    def test_list_button_is_confirmed_before_the_list_is_built_and_sent_once(self):
        stop = threading.Event()
        confirmed = threading.Event()
        offsets = []
        update = {"update_id": 9, "message": {"from": {"id": 42}, "chat": {"id": 1, "type": "private"},
                                              "text": LIST_APPS}}

        def get_updates(offset, poll_seconds=30):
            offsets.append(offset)
            if len(offsets) == 1:
                return [update]
            if offset > update["update_id"]:
                confirmed.set()
            stop.set()
            return [] if offset > update["update_id"] else [update]

        status = self.deployer.status
        def slow_status(app, repo_dir):
            self.assertTrue(confirmed.wait(10))
            return status(app, repo_dir)

        self.deployer.status = slow_status
        self.tg.get_updates = get_updates
        stop.wait = lambda delay: None
        self.bot.run_forever(stop)
        self.bot._list_worker.join(timeout=30)
        self.assertEqual(offsets, [0, 10])
        self.assertEqual(len(self.tg.sent), 1)
        self.assertTrue(self.tg.sent[0].startswith("Apps:"))

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
