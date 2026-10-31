"""Shared test helpers."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.config import Config  # noqa: E402


def make_config(tmp: Path, **overrides) -> Config:
    values = dict(
        telegram_token="tg-secret", owner_id=42, github_token="gh-secret", github_owner="",
        repos_dir=tmp / "repos", state_dir=tmp / "state", profile=tmp / "profile", model="GPT/GPT-6",
        timeout=30, temporary_chat=True, settle_seconds=2, git_name="Bot", git_email="bot@example.com",
        askpass=Path(__file__).resolve().parents[1] / "bin" / "git-askpass")
    values.update(overrides)
    return Config(**values)


def git(*args, cwd=None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE).stdout.strip()


def make_origin(tmp: Path, name: str = "origin.git") -> Path:
    """A bare repository with one commit on `main`, standing in for GitHub."""
    bare = tmp / name
    git("init", "--bare", "-b", "main", str(bare))
    seed = tmp / (name + "-seed")
    git("init", "-b", "main", str(seed))
    (seed / "README.md").write_text("hello\n")
    git("add", "-A", cwd=seed)
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-m", "init", cwd=seed)
    git("push", str(bare), "main", cwd=seed)
    return bare
