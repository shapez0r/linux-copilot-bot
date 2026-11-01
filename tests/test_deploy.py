import os
import shutil
import socket
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from support import make_config
from bot.deploy import Deployer, _alive, app_env


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repos" / "app"
        self.repo.mkdir(parents=True)
        self.deployer = Deployer(make_config(self.tmp, settle_seconds=1))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(lambda: self.deployer.stop("app", self.repo))

    def makefile(self, recipe):
        (self.repo / "Makefile").write_text("run:\n\t" + recipe + "\n")

    def pid(self):
        return self.deployer._read_pid("app")

    def test_missing_run_target(self):
        (self.repo / "Makefile").write_text("build:\n\ttrue\n")
        self.assertFalse(self.deployer.has_run_target(self.repo))
        self.assertFalse(self.deployer.redeploy("app", self.repo).ok)
        self.assertFalse(Deployer(make_config(self.tmp)).has_run_target(self.tmp))

    def test_redeploy_replaces_the_running_instance(self):
        self.makefile("echo started; sleep 60")
        self.assertTrue(self.deployer.redeploy("app", self.repo).ok)
        first = self.pid()
        self.assertTrue(_alive(first))
        self.assertTrue(self.deployer.status("app", self.repo).startswith("running"))
        self.assertTrue(self.deployer.redeploy("app", self.repo).ok)
        second = self.pid()
        self.assertNotEqual(first, second)
        self.assertFalse(_alive(first))
        self.assertTrue(_alive(second))
        self.assertIn("started", self.deployer.log_tail("app"))
        self.assertTrue(self.deployer.stop("app", self.repo))
        self.assertEqual(self.deployer.status("app", self.repo), "not running")

    def test_failing_run_reports_the_log(self):
        self.makefile("echo boom; exit 3")
        result = self.deployer.redeploy("app", self.repo)
        self.assertFalse(result.ok)
        self.assertIn("code 2", result.message)  # make reports the failed recipe with exit code 2.
        self.assertIn("boom", result.message)
        self.assertIsNone(self.pid())

    def test_run_that_finishes_successfully(self):
        self.makefile("true")
        self.assertTrue(self.deployer.redeploy("app", self.repo).ok)

    def test_stale_pid_of_another_directory_is_left_alone(self):
        self.deployer._run_dir.mkdir(parents=True)
        self.deployer._pid_file("app").write_text('{"pid": %d}' % os.getpid())
        self.assertFalse(self.deployer.stop("app", self.repo))

    def test_app_env_drops_secrets_and_the_bot_virtualenv(self):
        env = app_env({"TELEGRAM_BOT_TOKEN": "t", "GITHUB_TOKEN": "g", "CLOUDFLARE_API_TOKEN": "c",
                       "HOME": "/h", "VIRTUAL_ENV": "/v",
                       "PATH": "/v/bin:/usr/bin"})
        self.assertEqual(env, {"HOME": "/h", "PATH": "/usr/bin"})

    def test_redeploy_never_changes_cloudflare_routes(self):
        self.makefile("sleep 60")
        with mock.patch("bot.cloudflare.CloudflareRoutes.prepare", side_effect=AssertionError("unexpected route")):
            result = self.deployer.redeploy("app", self.repo)
        self.assertTrue(result.ok, result.message)
        self.assertTrue(_alive(self.pid()))

    def test_reboot_entry_is_added_once_without_replacing_other_entries(self):
        current = mock.Mock(returncode=0, stdout="MAILTO=ops@example.com\n", stderr="")
        installed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("bot.deploy.subprocess.run", side_effect=[current, installed]) as run:
            message = self.deployer.ensure_reboot(self.repo)
        line = f"@reboot cd {self.repo} && git pull && make run"
        self.assertIn(line, message)
        self.assertEqual(run.call_args_list[1].kwargs["input"], f"MAILTO=ops@example.com\n{line}\n")

        current.stdout += line + "\n"
        with mock.patch("bot.deploy.subprocess.run", return_value=current) as run:
            message = self.deployer.ensure_reboot(self.repo)
        self.assertIn("already contains", message)
        run.assert_called_once()

if __name__ == "__main__":
    unittest.main()
