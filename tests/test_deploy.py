import os
import shutil
import tempfile
import unittest
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
        env = app_env({"TELEGRAM_BOT_TOKEN": "t", "GITHUB_TOKEN": "g", "HOME": "/h", "VIRTUAL_ENV": "/v",
                       "PATH": "/v/bin:/usr/bin"})
        self.assertEqual(env, {"HOME": "/h", "PATH": "/usr/bin"})


if __name__ == "__main__":
    unittest.main()
