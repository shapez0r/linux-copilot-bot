"""Git operations on app checkouts: clone, fast-forward sync, commit and push."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .config import Config, redact
from .repos import same_remote


class GitError(Exception):
    """Raised when a Git command fails; the message is safe to show to the owner."""


class Git:
    def __init__(self, cfg: Config, github_login: str = "x-access-token"):
        self._cfg = cfg
        self._login = github_login

    def _env(self):
        # ZIP extraction may discard executable permissions on the credential helper.
        askpass = self._cfg.askpass
        try:
            if not os.access(askpass, os.X_OK):
                askpass.chmod(askpass.stat().st_mode | 0o100)
        except OSError as exc:
            raise GitError(f"Cannot make Git credential helper executable: {exc.strerror}") from None
        return dict(
            os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS=str(self._cfg.askpass),
            BOT_GIT_USER=self._login, BOT_GIT_TOKEN=self._cfg.github_token,
            GIT_AUTHOR_NAME=self._cfg.git_name, GIT_AUTHOR_EMAIL=self._cfg.git_email,
            GIT_COMMITTER_NAME=self._cfg.git_name, GIT_COMMITTER_EMAIL=self._cfg.git_email)

    def run(self, *args, cwd: Path = None) -> str:
        try:
            done = subprocess.run(["git", *args], cwd=cwd, env=self._env(), text=True, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
        except subprocess.TimeoutExpired:
            raise GitError(f"git {args[0]} timed out") from None
        if done.returncode != 0:
            detail = redact((done.stderr or done.stdout).strip(), self._cfg.secrets)
            raise GitError(f"git {args[0]} failed: {detail[-600:]}")
        return done.stdout.strip()

    def ensure_clone(self, url: str, dest: Path, owner: str, name: str) -> bool:
        """Clone into dest when missing; otherwise require it to be a checkout of the same repository."""
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            self.run("clone", url, str(dest))
            return True
        if not (dest / ".git").exists():
            raise GitError(f"{dest} exists but is not a Git checkout.")
        origin = self.run("remote", "get-url", "origin", cwd=dest)
        if not same_remote(origin, owner, name):
            raise GitError(f"{dest} is a checkout of a different repository ({redact(origin, self._cfg.secrets)}).")
        return False

    def sync(self, dest: Path) -> str:
        """Fast-forward the checked-out branch to origin and return its name."""
        try:
            branch = self.run("symbolic-ref", "--short", "HEAD", cwd=dest)
        except GitError:
            raise GitError("The checkout is on a detached HEAD; check out a branch first.") from None
        if self.run("status", "--porcelain", cwd=dest):
            raise GitError("The checkout has uncommitted changes; commit, stash or discard them first.")
        self.run("fetch", "origin", cwd=dest)
        self.run("pull", "--ff-only", "origin", branch, cwd=dest)
        self.run("rev-parse", "--verify", "HEAD", cwd=dest)
        return branch

    def is_dirty(self, dest: Path) -> bool:
        return bool(self.run("status", "--porcelain", cwd=dest))

    def commit_and_push(self, dest: Path, prompt: str):
        """Commit all changes and push the branch; returns (short sha, branch) or None without changes."""
        self.run("add", "-A", cwd=dest)
        if not self.run("status", "--porcelain", cwd=dest):
            return None
        first = " ".join(prompt.split())
        subject = "Copilot: " + (first if len(first) <= 60 else first[:59].rstrip() + "\u2026")
        self.run("commit", "-m", subject, "-m", prompt.strip()[:2000], cwd=dest)
        branch = self.run("symbolic-ref", "--short", "HEAD", cwd=dest)
        self.run("push", "origin", branch, cwd=dest)
        return self.run("rev-parse", "--short", "HEAD", cwd=dest), branch
