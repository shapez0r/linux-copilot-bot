"""Persistent app ownership, bot access, and per-user bot settings."""
from __future__ import annotations

import json
import os
import time
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
        self.user_view_ids = set()
        # Telegram user ids (as strings) mapped to {"name": display label, "time": epoch seconds}.
        self.approved_users = {}
        self.access_requests = {}
        try:
            data = json.loads(path.read_text())
            self.repo = str(data.get("repo", ""))
            self.model = str(data.get("model", default_model))
            owners = data.get("app_owners", {})
            active = data.get("active_repos", {})
            user_view = data.get("user_view_ids", [])
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
            if isinstance(user_view, list):
                self.user_view_ids = {str(user_id) for user_id in user_view if str(user_id).isdigit()}
            self.approved_users = self._people(data.get("approved_users"))
            self.access_requests = self._people(data.get("access_requests"))
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

    def user_view(self, user_id) -> bool:
        """Return whether an administrator chose to use the bot as a regular user."""
        return user_id is not None and str(user_id) in self.user_view_ids

    def set_user_view(self, user_id, enabled: bool):
        if user_id is None:
            return
        if enabled:
            self.user_view_ids.add(str(user_id))
        else:
            self.user_view_ids.discard(str(user_id))

    @staticmethod
    def _people(value) -> dict:
        if not isinstance(value, dict):
            return {}
        people = {}
        for user_id, info in value.items():
            if not str(user_id).isdigit() or int(user_id) < 1 or not isinstance(info, dict):
                continue
            try:
                stamp = int(info.get("time") or 0)
            except (TypeError, ValueError):
                stamp = 0
            people[str(user_id)] = {"name": str(info.get("name") or ""), "time": stamp}
        return people

    def is_approved(self, user_id) -> bool:
        return user_id is not None and str(user_id) in self.approved_users

    def has_request(self, user_id) -> bool:
        return user_id is not None and str(user_id) in self.access_requests

    def add_request(self, user_id, name: str):
        self.access_requests[str(user_id)] = {"name": name, "time": int(time.time())}

    def approve(self, user_id, name: str = ""):
        """Grant regular-user access; the request's display name is kept when none is given."""
        request = self.access_requests.pop(str(user_id), {})
        self.approved_users[str(user_id)] = {"name": name or request.get("name", ""), "time": int(time.time())}

    def reject(self, user_id) -> bool:
        return self.access_requests.pop(str(user_id), None) is not None

    def revoke(self, user_id) -> bool:
        return self.approved_users.pop(str(user_id), None) is not None

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
            "user_view_ids": sorted(self.user_view_ids, key=int),
            "approved_users": self.approved_users,
            "access_requests": self.access_requests,
        }, sort_keys=True))
        os.replace(tmp, self._path)
