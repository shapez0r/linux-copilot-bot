import shutil
import signal
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import make_config
from bot.deploy import BOT_SERVICE, DeployError, Deployer, ServiceState, app_env


class FakeSystemdDeployer(Deployer):
    def __init__(self, cfg, bot_repo_dir):
        super().__init__(cfg, bot_repo_dir)
        self._unit_dir = cfg.state_dir / "units"
        self.calls = []
        self.enabled = set()
        self.states = {}

    def _write_unit(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def _remove_unit(self, path):
        path.unlink(missing_ok=True)

    def _systemctl(self, args, check=True):
        self.calls.append(tuple(args))
        command = args[0]
        service = args[1] if command == "show" else args[-1]
        if command == "show":
            state = self.states.get(service, ServiceState())
            output = (f"LoadState={state.load}\nActiveState={state.active}\n"
                      f"SubState={state.sub}\nMainPID={state.pid}\n")
            return SimpleNamespace(returncode=0, stdout=output, stderr="")
        if command == "is-enabled":
            return SimpleNamespace(returncode=0 if service in self.enabled else 1, stdout="", stderr="")
        if command == "enable":
            self.enabled.add(service)
        elif command in ("start", "restart"):
            self.states[service] = ServiceState("loaded", "active", "running", 123)
        elif command == "stop" or (command == "disable" and "--now" in args):
            self.states[service] = ServiceState("loaded", "inactive", "dead", 0)
            self.enabled.discard(service)
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repos" / "app"
        self.repo.mkdir(parents=True)
        self.bot_repo = self.tmp / "repos" / "linux-copilot-bot"
        self.bot_repo.mkdir()
        self.deployer = FakeSystemdDeployer(make_config(self.tmp, settle_seconds=1), self.bot_repo)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def makefile(self, recipe="sleep 60"):
        (self.repo / "Makefile").write_text("run:\n\t" + recipe + "\n")

    def test_missing_run_target_is_not_registered(self):
        (self.repo / "Makefile").write_text("build:\n\ttrue\n")
        self.assertFalse(self.deployer.has_run_target(self.repo))
        result = self.deployer.redeploy("app", self.repo)
        self.assertFalse(result.ok)
        self.assertIn("make run", result.message)
        self.assertFalse(self.deployer.service_path("app").exists())

    def test_registration_writes_and_enables_a_system_service(self):
        self.makefile()
        with mock.patch.object(self.deployer, "_remove_reboot_entry", return_value=True):
            message = self.deployer.ensure_service("app", self.repo)
        unit = self.deployer.service_path("app").read_text()
        self.assertIn(f"WorkingDirectory={self.repo}", unit)
        self.assertRegex(unit, r"ExecStart=/.+/make run")
        self.assertIn("UnsetEnvironment=TELEGRAM_BOT_TOKEN GITHUB_TOKEN CLOUDFLARE_API_TOKEN", unit)
        self.assertIn("WantedBy=multi-user.target", unit)
        self.assertIn("User=", unit)
        self.assertIn("Group=", unit)
        self.assertIn("matching crontab entry was removed", message)
        self.assertIn(self.deployer.service_name("app"), self.deployer.enabled)
        self.assertIn(("daemon-reload",), self.deployer.calls)

    def test_privileged_commands_use_noninteractive_sudo(self):
        deployer = Deployer(make_config(self.tmp), self.bot_repo)
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch("bot.deploy.subprocess.run", return_value=completed) as run:
            deployer._systemctl(["daemon-reload"])
        self.assertEqual(run.call_args.args[0], ["sudo", "-n", "--", "systemctl", "daemon-reload"])

        with mock.patch.object(deployer, "_sudo", return_value=completed) as sudo:
            deployer._write_unit(Path("/etc/systemd/system/example.service"), "[Unit]\n")
        command = sudo.call_args.args[0]
        self.assertEqual(command[:5], ["install", "-D", "-m", "0644", "--"])
        self.assertEqual(command[-1], "/etc/systemd/system/example.service")

    def test_registration_is_idempotent(self):
        self.makefile()
        with mock.patch.object(self.deployer, "_remove_reboot_entry", return_value=False):
            self.deployer.ensure_service("app", self.repo)
            self.deployer.calls.clear()
            message = self.deployer.ensure_service("app", self.repo)
        self.assertIn("already registered and enabled", message)
        self.assertNotIn(("daemon-reload",), self.deployer.calls)
        self.assertFalse(any(call[0] == "enable" for call in self.deployer.calls))

    def test_existing_unit_file_is_kept_on_start_stop_and_redeploy(self):
        self.makefile()
        path = self.deployer.service_path("app")
        path.parent.mkdir(parents=True, exist_ok=True)
        edited = "[Service]\nExecStart=/usr/bin/make run\nEnvironment=PORT=9000\n"
        path.write_text(edited)
        with mock.patch.object(self.deployer, "_remove_reboot_entry", return_value=False), \
             mock.patch("bot.deploy.time.monotonic", side_effect=[0, 2, 2, 0, 2, 2]), \
             mock.patch.object(self.deployer, "_write_unit") as write:
            self.assertTrue(self.deployer.start("app", self.repo).ok)
            self.assertTrue(self.deployer.stop("app", self.repo))
            self.assertTrue(self.deployer.redeploy("app", self.repo).ok)
            message = self.deployer.ensure_service("app", self.repo)
        write.assert_not_called()
        self.assertEqual(path.read_text(), edited)
        self.assertNotIn(("daemon-reload",), self.deployer.calls)
        self.assertIn("already registered and enabled", message)

    def test_redeploy_restarts_service_and_reports_systemd_state(self):
        self.makefile()
        with mock.patch.object(self.deployer, "_remove_reboot_entry", return_value=False), \
             mock.patch("bot.deploy.time.monotonic", side_effect=[0, 2, 2]):
            result = self.deployer.redeploy("app", self.repo)
        self.assertTrue(result.ok, result.message)
        self.assertIn(("restart", self.deployer.service_name("app")), self.deployer.calls)
        self.assertEqual(self.deployer.status("app", self.repo), "running (pid 123)")
        self.assertEqual(self.deployer.running_apps(), [("app", 123)])

    def test_start_and_stop_use_the_app_service(self):
        self.makefile()
        with mock.patch.object(self.deployer, "_remove_reboot_entry", return_value=False), \
             mock.patch("bot.deploy.time.monotonic", side_effect=[0, 2, 2]):
            self.assertTrue(self.deployer.start("app", self.repo).ok)
        self.assertTrue(self.deployer.stop("app", self.repo))
        self.assertEqual(self.deployer.status("app", self.repo), "stopped")
        self.assertFalse(self.deployer.stop("app", self.repo))

    def test_failed_service_reports_status_and_log(self):
        self.makefile()
        service = self.deployer.service_name("app")
        self.deployer.states[service] = ServiceState("loaded", "failed", "failed", 0)
        self.deployer._log_dir.mkdir(parents=True)
        self.deployer.log_path("app").write_text("boom\n")
        result = self.deployer._wait_running("app")
        self.assertFalse(result.ok)
        self.assertIn("failed", result.message)
        self.assertIn("boom", result.message)

    def test_unregister_disables_and_removes_the_service(self):
        self.makefile()
        with mock.patch.object(self.deployer, "_remove_reboot_entry", return_value=False):
            self.deployer.ensure_service("app", self.repo)
            message = self.deployer.unregister("app", self.repo)
        self.assertIn("disabled and removed", message)
        self.assertFalse(self.deployer.service_path("app").exists())
        self.assertIn(("disable", "--now", self.deployer.service_name("app")), self.deployer.calls)

    def test_bot_registration_uses_make_run_without_starting_a_duplicate(self):
        (self.bot_repo / "Makefile").write_text("run:\n\tpython3 -m bot\n")
        with mock.patch.object(self.deployer, "_remove_reboot_entry", return_value=False):
            message = self.deployer.ensure_bot_service(self.bot_repo)
        unit = (self.deployer._unit_dir / BOT_SERVICE).read_text()
        self.assertIn(f"WorkingDirectory={self.bot_repo}", unit)
        self.assertIn("ExecStart=", unit)
        self.assertIn(" run\n", unit)
        self.assertIn(BOT_SERVICE, self.deployer.enabled)
        self.assertFalse(any(call[0] in ("start", "restart") for call in self.deployer.calls))
        self.assertIn("registered and enabled", message)

    def test_bot_repository_is_excluded_from_running_apps(self):
        self.makefile()
        app_service = self.deployer.service_name("app")
        self.deployer.states[app_service] = ServiceState("loaded", "active", "running", 5)
        self.assertTrue(self.deployer.is_bot_repo(self.bot_repo))
        self.assertEqual(self.deployer.running_apps(), [("app", 5)])

    def test_bot_status_and_restart_use_the_bot_service(self):
        self.deployer.states[BOT_SERVICE] = ServiceState("loaded", "active", "running", 9)
        self.assertEqual(self.deployer.bot_status(), "running (pid 9)")
        self.deployer.restart_bot()
        self.assertIn(("restart", "--no-block", BOT_SERVICE), self.deployer.calls)
        self.assertTrue(Deployer.bot_runs_as_service({"INVOCATION_ID": "abc"}))
        self.assertFalse(Deployer.bot_runs_as_service({}))

    def test_bot_restart_treats_termination_by_the_restart_as_success(self):
        deployer = Deployer(make_config(self.tmp), self.bot_repo)
        for returncode in (0, -signal.SIGTERM):
            with self.subTest(returncode=returncode), mock.patch.object(
                    deployer, "_sudo", return_value=SimpleNamespace(returncode=returncode, stdout="", stderr="")) as sudo:
                deployer.restart_bot()
            self.assertEqual(sudo.call_args.args[0], ["systemctl", "restart", "--no-block", BOT_SERVICE])
        failed = SimpleNamespace(returncode=1, stdout="", stderr="Access denied")
        with mock.patch.object(deployer, "_sudo", return_value=failed), self.assertRaisesRegex(
                DeployError, "Access denied"):
            deployer.restart_bot()

    def test_registration_removes_only_the_matching_reboot_entry(self):
        self.makefile()
        line = f"@reboot cd {self.repo} && git pull && make run"
        current = SimpleNamespace(returncode=0, stdout=f"MAILTO=ops@example.com\n{line}\n", stderr="")
        installed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch("bot.deploy.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"), \
             mock.patch("bot.deploy.subprocess.run", side_effect=[
                 SimpleNamespace(returncode=0), current, installed,
             ]) as run:
            message = self.deployer.ensure_service("app", self.repo)
        self.assertIn("matching crontab entry was removed", message)
        self.assertEqual(run.call_args_list[-1].kwargs["input"], "MAILTO=ops@example.com\n")

    def test_app_env_drops_secrets_and_the_bot_virtualenv(self):
        env = app_env({"TELEGRAM_BOT_TOKEN": "t", "GITHUB_TOKEN": "g", "CLOUDFLARE_API_TOKEN": "c",
                       "HOME": "/h", "VIRTUAL_ENV": "/v", "PATH": "/v/bin:/usr/bin"})
        self.assertEqual(env, {"HOME": "/h", "PATH": "/usr/bin"})


if __name__ == "__main__":
    unittest.main()
