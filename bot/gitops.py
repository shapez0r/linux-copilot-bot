"""Git operations on app checkouts: clone, fast-forward sync, commit and push."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from .config import Config, redact
from .repos import same_remote


class GitError(Exception):
    """Raised when a Git command fails; the message is safe to show to an authorized user."""


class Git:
    # Parallel sessions share the helper copy in STATE_DIR.
    _helper_lock = threading.Lock()

    def __init__(self, cfg: Config, github_login: str = "x-access-token"):
        self._cfg = cfg
        self._login = github_login

    def _askpass(self) -> Path:
        """Return an executable copy of the credential helper outside every checkout.

        Changing the helper's mode in place would leave the bot's own checkout with an uncommitted change.
        """
        helper = self._cfg.state_dir / "git-askpass"
        with self._helper_lock:
            try:
                content = self._cfg.askpass.read_bytes()
                if not helper.is_file() or helper.read_bytes() != content:
                    helper.parent.mkdir(parents=True, exist_ok=True)
                    temporary = helper.with_suffix(".tmp")
                    temporary.write_bytes(content)
                    temporary.chmod(0o700)
                    os.replace(temporary, helper)
                elif not os.access(helper, os.X_OK):
                    helper.chmod(0o700)
            except OSError as exc:
                raise GitError(f"Cannot prepare the Git credential helper: {exc.strerror or exc}") from None
        return helper

    def _env(self):
        return dict(
            os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS=str(self._askpass()),
            BOT_GIT_USER=self._login, BOT_GIT_TOKEN=self._cfg.github_token,
            GIT_AUTHOR_NAME=self._cfg.git_name, GIT_AUTHOR_EMAIL=self._cfg.git_email,
            GIT_COMMITTER_NAME=self._cfg.git_name, GIT_COMMITTER_EMAIL=self._cfg.git_email)

    def _restore_helper_mode(self, dest: Path):
        """Undo a mode-only change of the credential helper inside dest; content changes are kept."""
        try:
            relative = self._cfg.askpass.resolve().relative_to(Path(dest).resolve()).as_posix()
        except (OSError, ValueError):
            return
        if not self.run("diff", "--name-only", "--", relative, cwd=dest):
            return
        if self.run("-c", "core.fileMode=false", "diff", "--name-only", "--", relative, cwd=dest):
            return
        self.run("checkout", "--", relative, cwd=dest)

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
        self.require_checkout(dest, owner, name)
        return False

    def require_checkout(self, dest: Path, owner: str, name: str):
        """Require dest to be a nonsymlink checkout of github.com/<owner>/<name>."""
        if dest.is_symlink() or not dest.is_dir() or not (dest / ".git").exists():
            raise GitError(f"{dest} exists but is not a Git checkout.")
        origin = self.run("remote", "get-url", "origin", cwd=dest)
        if not same_remote(origin, owner, name):
            raise GitError(f"{dest} is a checkout of a different repository ({redact(origin, self._cfg.secrets)}).")

    def origin_repo(self, dest: Path):
        """Return (owner, name) of a checkout's github.com origin, or None when it cannot be read."""
        if not dest.is_dir():
            return None
        try:
            origin = self.run("remote", "get-url", "origin", cwd=dest)
        except GitError:
            return None
        match = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", origin, re.I)
        return (match.group(1), match.group(2)) if match else None

    def seed_from_template(self, url: str, dest: Path, template: Path, branch: str = "main") -> str:
        """Create and push a repository whose first commit copies template without Git metadata."""
        if dest.exists():
            raise GitError(f"{dest} already exists; refusing to replace it while creating a repository.")
        if not template.is_dir():
            raise GitError(f"New repository template not found: {template}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copytree(template, dest, symlinks=True,
                            ignore=lambda _path, names: {name for name in names if name == ".git"})
        except OSError as exc:
            shutil.rmtree(dest, ignore_errors=True)
            raise GitError(f"Cannot copy new repository template: {exc.strerror or exc}") from None
        self.run("init", "-b", branch, cwd=dest)
        self.run("remote", "add", "origin", url, cwd=dest)
        self.run("add", "-A", cwd=dest)
        if not self.run("status", "--porcelain", cwd=dest):
            raise GitError(f"New repository template has no files Git can commit: {template}")
        self.run("commit", "-m", "Initial commit", cwd=dest)
        self.run("push", "-u", "origin", branch, cwd=dest)
        return branch

    def sync(self, dest: Path) -> str:
        """Fast-forward the checked-out branch to origin and return its name."""
        try:
            branch = self.run("symbolic-ref", "--short", "HEAD", cwd=dest)
        except GitError:
            raise GitError("The checkout is on a detached HEAD; check out a branch first.") from None
        self._restore_helper_mode(dest)
        if self.run("status", "--porcelain", cwd=dest):
            raise GitError("The checkout has uncommitted changes; commit, stash or discard them first.")
        self.run("fetch", "origin", cwd=dest)
        self.run("pull", "--ff-only", "origin", branch, cwd=dest)
        self.run("rev-parse", "--verify", "HEAD", cwd=dest)
        return branch

    def is_dirty(self, dest: Path) -> bool:
        return bool(self.run("status", "--porcelain", cwd=dest))

    def commit_and_push(self, dest: Path, prompt: str, output: str = ""):
        """Commit all changes and push the branch; returns (short sha, branch) or None without changes.

        The message holds the request and Copilot's final answer, so later fix round-trips can read
        what each change did from the Git history.
        """
        self.run("add", "-A", cwd=dest)
        if not self.run("status", "--porcelain", cwd=dest):
            return None
        first = " ".join(prompt.split())
        subject = "Copilot: " + (first if len(first) <= 60 else first[:59].rstrip() + "\u2026")
        message = f"{subject}\n\n{prompt.strip()[:2000]}"
        output = redact((output or "").strip(), self._cfg.secrets)
        if output:
            message += f"\n\nCopilot output:\n{output}"
        # A message file avoids command-line length limits for long outputs.
        self._cfg.state_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self._cfg.state_dir,
                                         prefix="commit-message-", suffix=".txt") as file:
            file.write(message + "\n")
            file.flush()
            self.run("commit", "--cleanup=whitespace", "-F", file.name, cwd=dest)
        branch = self.run("symbolic-ref", "--short", "HEAD", cwd=dest)
        self.run("push", "origin", branch, cwd=dest)
        return self.run("rev-parse", "--short", "HEAD", cwd=dest), branch

    def numstat(self, dest: Path) -> str:
        """Per-file insertion and deletion counts of the HEAD commit, as git --numstat output."""
        return self.run("show", "--numstat", "--format=", "--no-renames", "HEAD", cwd=dest)
