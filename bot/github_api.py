"""GitHub REST client for looking up and creating repositories."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

API = "https://api.github.com"


class GitHubError(Exception):
    """Raised when GitHub rejects a request or cannot be reached."""


class GitHub:
    def __init__(self, token: str, owner: str = "", base_url: str = API):
        self._token, self._base = token, base_url
        self._login = ""
        self.default_owner = owner

    def _request(self, method: str, path: str, payload=None, allow_404=False):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self._base + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "copilot-telegram-bot"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            if e.code == 404 and allow_404:
                return None
            detail = ""
            try:
                detail = json.load(e).get("message", "")
            except Exception:
                pass
            raise GitHubError(f"GitHub {method} {path}: HTTP {e.code} {detail}".strip()) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            raise GitHubError(f"GitHub {method} {path}: {e.__class__.__name__}") from None

    @property
    def login(self) -> str:
        """Account that owns the token; also the default repository owner."""
        if not self._login:
            self._login = self._request("GET", "/user")["login"]
            self.default_owner = self.default_owner or self._login
        return self._login

    def get_repo(self, owner: str, name: str):
        """Repository metadata, or None when it does not exist or is not visible to the token."""
        return self._request("GET", f"/repos/{owner}/{name}", allow_404=True)

    def create_repo(self, owner: str, name: str, private: bool = True):
        """Create an empty repository for the local app template's initial commit."""
        path = "/user/repos" if owner.lower() == self.login.lower() else f"/orgs/{owner}/repos"
        return self._request("POST", path, {"name": name, "private": private, "auto_init": False})
