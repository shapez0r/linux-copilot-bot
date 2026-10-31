"""Repository references: parsing, validation and the local path derived from them."""
from __future__ import annotations

import re
from pathlib import Path

_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_URL = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/", re.I)


class RepoError(ValueError):
    """Raised for an unusable repository reference."""


def parse_repo_ref(text: str, default_owner: str):
    """Return (owner, name) from `name`, `owner/name` or a github.com URL."""
    value = _URL.sub("", (text or "").strip()).strip("/")
    if value.endswith(".git"):
        value = value[:-4]
    parts = value.split("/")
    if len(parts) == 1:
        owner, name = default_owner, parts[0]
    elif len(parts) == 2:
        owner, name = parts
    else:
        raise RepoError("Use `name` or `owner/name`.")
    if not name or not _NAME.match(name) or name.endswith(".") or ".." in name:
        raise RepoError("Repository names may contain letters, digits, '.', '_' and '-'.")
    if not owner or not _OWNER.match(owner):
        raise RepoError("Invalid repository owner.")
    return owner, name


def local_path(repos_dir: Path, name: str) -> Path:
    """The checkout directory for an app: <repos_dir>/<name>."""
    return repos_dir / name


def same_remote(url: str, owner: str, name: str) -> bool:
    """Whether a Git remote URL points at github.com/<owner>/<name>."""
    tail = re.sub(r"^.*github\.com[:/]", "", (url or "").strip(), flags=re.I)
    tail = tail[:-4] if tail.endswith(".git") else tail
    return tail.strip("/").lower() == f"{owner}/{name}".lower()
