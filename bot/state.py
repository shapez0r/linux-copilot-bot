"""Persistent app ownership and per-user bot settings."""
from __future__ import annotations

import json
import os
from pathlib import Path

ADMIN_OWNER = "kurwalab.fyi admins"


def telegram_owner(user_id: int) -> str:
    return f"telegram:{user_id}"


def owner_label(owner: str) -> str:
    if owner == ADMIN_OWNER:
        return owner
    if owner.startswith("telegram:"):
        return f"Telegram user {owner.partition(':')[2]}"
    return owner


class State:
    def __init__(self, path: Path, default_model: str):
        self._path = path
        self.repo = ""
        self.model = default_model
        self.app_owners = {}
        self.active_repos = {}
        try:
            data = json.loads(path.read_text())
            self.repo = str(data.get("repo", ""))
            self.model = str(data.get("model", default_model))
            owners = data.get("app_owners", {})
            active = data.get("active_repos", {})
            if isinstance(owners, dict):
                self.app_owners = {
                    str(repo): str(owner) for repo, owner in owners.items()
                    if isinstance(repo, str) and isinstance(owner, str) and "/" in repo and owner
                }
            if isinstance(active, dict):
                self.active_repos = {
                    str(user_id): str(repo) for user_id, repo in active.items()
                    if str(user_id).isdigit() and isinstance(repo, str) and "/" in repo
                }
        except (OSError, ValueError, AttributeError):
            pass
        if self.repo:
            self.app_owners.setdefault(self.repo, ADMIN_OWNER)

    def app_owner(self, repo: str) -> str:
        key = self._repo_key(repo)
        return self.app_owners.get(key, "") if key else ""

    def set_app_owner(self, repo: str, owner: str, overwrite: bool = False):
        key = self._repo_key(repo)
        if key and (overwrite or key not in self.app_owners):
            self.app_owners[key] = owner

    def remove_app(self, repo: str):
        key = self._repo_key(repo)
        if key:
            self.app_owners.pop(key, None)
        self.clear_active_repo(repo)

    def get_active_repo(self, user_id, is_admin: bool = False) -> str:
        if user_id is not None and str(user_id) in self.active_repos:
            return self.active_repos[str(user_id)]
        return self.repo if is_admin else ""

    def set_active_repo(self, user_id, repo: str, is_admin: bool = False):
        if user_id is not None:
            self.active_repos[str(user_id)] = repo
        if is_admin or user_id is None:
            self.repo = repo

    def clear_active_repo(self, repo: str):
        target = repo.lower()
        if self.repo.lower() == target:
            self.repo = ""
        self.active_repos = {
            user_id: active for user_id, active in self.active_repos.items()
            if active.lower() != target
        }

    def repos_for_owner(self, owner: str):
        return [repo for repo, app_owner in self.app_owners.items() if app_owner == owner]

    def _repo_key(self, repo: str) -> str:
        target = repo.lower()
        return next((key for key in self.app_owners if key.lower() == target), repo)

    def save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "repo": self.repo,
            "model": self.model,
            "app_owners": self.app_owners,
            "active_repos": self.active_repos,
        }, sort_keys=True))
        os.replace(tmp, self._path)
