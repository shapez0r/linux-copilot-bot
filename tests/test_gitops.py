import shutil
import tempfile
import unittest
from pathlib import Path

from support import git, make_config, make_origin
from bot.gitops import Git, GitError


class GitOpsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.origin = make_origin(self.tmp)
        self.git = Git(make_config(self.tmp))
        self.dest = self.tmp / "repos" / "app"

    def clone(self):
        # Local paths stand in for GitHub URLs; the remote check accepts only github.com remotes.
        return self.git.run("clone", str(self.origin), str(self.dest))

    def test_clone_sync_commit_push(self):
        self.clone()
        self.assertEqual(self.git.sync(self.dest), "main")
        (self.dest / "app.py").write_text("print(1)\n")
        sha, branch = self.git.commit_and_push(self.dest, "Add the app\nwith details")
        self.assertEqual(branch, "main")
        self.assertEqual(git("rev-parse", "--short", "main", cwd=self.origin), sha)
        self.assertEqual(git("log", "-1", "--format=%an|%s", cwd=self.origin), "Bot|Copilot: Add the app with details")

    def test_commit_message_holds_the_request_and_complete_copilot_output(self):
        self.clone()
        (self.dest / "app.py").write_text("print(1)\n")
        output = "# Summary\n" + "Changed app.py with gh-secret. " * 6000
        self.git.commit_and_push(self.dest, "Add the app", output)
        body = git("log", "-1", "--format=%B", cwd=self.origin)
        self.assertTrue(body.startswith("Copilot: Add the app\n\nAdd the app\n\nCopilot output:\n# Summary\n"))
        self.assertIn("Changed app.py with ***.", body)
        self.assertNotIn("gh-secret", body)
        self.assertEqual(body.count("Changed app.py"), 6000)
        self.assertEqual(list(self.git._cfg.state_dir.glob("commit-message-*")), [])

    def test_seed_from_template_creates_one_initial_commit_without_source_git_metadata(self):
        template = self.tmp / "eurofish"
        (template / "src").mkdir(parents=True)
        (template / "README.md").write_text("reference app\n")
        (template / "src" / "app.py").write_text("print('reference')\n")
        git("init", "-b", "main", str(template))
        git("add", "-A", cwd=template)
        git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-m", "template history",
            cwd=template)
        empty = self.tmp / "empty.git"
        git("init", "--bare", "-b", "main", str(empty))

        branch = self.git.seed_from_template(str(empty), self.dest, template)

        self.assertEqual(branch, "main")
        self.assertEqual((self.dest / "src" / "app.py").read_text(), "print('reference')\n")
        self.assertEqual(git("rev-list", "--count", "HEAD", cwd=self.dest), "1")
        self.assertEqual(git("log", "-1", "--format=%s", cwd=self.dest), "Initial commit")
        self.assertNotIn(".git", git("ls-tree", "-r", "--name-only", "HEAD", cwd=self.dest).splitlines())

    def test_nothing_to_commit(self):
        self.clone()
        self.assertIsNone(self.git.commit_and_push(self.dest, "noop"))

    def test_sync_fast_forwards_and_rejects_dirty_checkouts(self):
        self.clone()
        other = self.tmp / "other"
        git("clone", str(self.origin), str(other))
        (other / "new.txt").write_text("x")
        git("add", "-A", cwd=other)
        git("-c", "user.name=t", "-c", "user.email=t@e.com", "commit", "-m", "more", cwd=other)
        git("push", "origin", "main", cwd=other)
        self.git.sync(self.dest)
        self.assertTrue((self.dest / "new.txt").exists())
        (self.dest / "stray.txt").write_text("y")
        with self.assertRaisesRegex(GitError, "uncommitted"):
            self.git.sync(self.dest)

    def test_detached_head_is_rejected(self):
        self.clone()
        git("checkout", "--detach", cwd=self.dest)
        with self.assertRaisesRegex(GitError, "detached"):
            self.git.sync(self.dest)

    def test_existing_directory_must_match_the_repository(self):
        self.dest.mkdir(parents=True)
        with self.assertRaisesRegex(GitError, "not a Git checkout"):
            self.git.ensure_clone("unused", self.dest, "me", "app")
        git("init", str(self.dest))
        git("remote", "add", "origin", "https://github.com/me/other.git", cwd=self.dest)
        with self.assertRaisesRegex(GitError, "different repository"):
            self.git.ensure_clone("unused", self.dest, "me", "app")
        git("remote", "set-url", "origin", "https://github.com/me/app.git", cwd=self.dest)
        self.assertFalse(self.git.ensure_clone("unused", self.dest, "me", "app"))
        self.git.require_checkout(self.dest, "me", "app")

    def test_origin_repo_reads_github_remotes_only(self):
        self.assertIsNone(self.git.origin_repo(self.dest))
        self.clone()
        self.assertIsNone(self.git.origin_repo(self.dest))
        git("remote", "set-url", "origin", "https://github.com/me/linux-copilot-bot.git", cwd=self.dest)
        self.assertEqual(self.git.origin_repo(self.dest), ("me", "linux-copilot-bot"))
        git("remote", "set-url", "origin", "git@github.com:org/bot", cwd=self.dest)
        self.assertEqual(self.git.origin_repo(self.dest), ("org", "bot"))

    def test_askpass_runs_from_an_executable_copy_without_changing_the_source(self):
        helper = self.tmp / "git-askpass"
        helper.write_bytes(self.git._cfg.askpass.read_bytes())
        helper.chmod(0o600)
        cfg = make_config(self.tmp, askpass=helper)
        git_client = Git(cfg)
        self.assertTrue(git_client.run("--version").startswith("git version"))
        self.assertEqual(helper.stat().st_mode & 0o777, 0o600)
        copy = cfg.state_dir / "git-askpass"
        self.assertEqual(git_client._env()["GIT_ASKPASS"], str(copy))
        self.assertTrue(copy.stat().st_mode & 0o100)
        self.assertEqual(copy.read_bytes(), helper.read_bytes())

    def test_sync_undoes_a_mode_only_change_of_the_credential_helper(self):
        self.clone()
        helper = self.dest / "bin" / "git-askpass"
        helper.parent.mkdir()
        helper.write_bytes(self.git._cfg.askpass.read_bytes())
        helper.chmod(0o644)
        git("add", "-A", cwd=self.dest)
        git("-c", "user.name=t", "-c", "user.email=t@e.com", "commit", "-m", "helper", cwd=self.dest)
        git("push", "origin", "main", cwd=self.dest)
        git("config", "core.fileMode", "true", cwd=self.dest)
        helper.chmod(0o755)
        git_client = Git(make_config(self.tmp, askpass=helper))
        self.assertEqual(git_client.sync(self.dest), "main")
        self.assertEqual(git("status", "--porcelain", cwd=self.dest), "")
        helper.write_text("#!/bin/sh\necho changed\n")
        with self.assertRaisesRegex(GitError, "uncommitted"):
            git_client.sync(self.dest)

    def test_errors_do_not_leak_the_token(self):
        with self.assertRaises(GitError) as caught:
            self.git.run("clone", "https://gh-secret@invalid.invalid/x.git", str(self.tmp / "x"))
        self.assertNotIn("gh-secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
