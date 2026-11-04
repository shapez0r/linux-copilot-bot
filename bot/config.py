"""Environment-based configuration; real environment variables override the .env file."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "GPT/GPT-6"
SECRET_NAMES = ("TELEGRAM_BOT_TOKEN", "GITHUB_TOKEN", "CLOUDFLARE_API_TOKEN")


class ConfigError(Exception):
    """Raised for missing or invalid settings."""


@dataclass(frozen=True)
class Config:
    telegram_token: str
    owner_ids: frozenset[int]
    github_token: str
    github_owner: str  # Empty means the token's own account.
    repos_dir: Path
    state_dir: Path
    profile: Path
    model: str
    timeout: int
    show_browser: bool
    temporary_chat: bool
    settle_seconds: int
    git_name: str
    git_email: str
    engine: Path = ROOT / "engine" / "copilot_repo_roundtrip.py"
    askpass: Path = ROOT / "bin" / "git-askpass"
    cloudflare_api_token: str = ""
    cloudflare_account_id: str = ""
    cloudflare_zone_id: str = ""
    cloudflare_tunnel: str = "kurwalab-tunnel"
    cloudflare_domain: str = "kurwalab.fyi"
    user_ids: frozenset[int] = frozenset()

    @property
    def admin_ids(self) -> frozenset[int]:
        return self.owner_ids

    @property
    def authorized_ids(self) -> frozenset[int]:
        return self.owner_ids | self.user_ids

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.owner_ids

    @property
    def secrets(self):
        return [s for s in (self.telegram_token, self.github_token, self.cloudflare_api_token) if s]

    @property
    def cloudflare_enabled(self) -> bool:
        return bool(self.cloudflare_api_token)


def parse_env_file(path: Path) -> dict:
    """Parse KEY=VALUE lines; blank lines and # comments are ignored, matching quotes are removed."""
    values = {}
    if not path.is_file():
        return values
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key:
            raise ConfigError(f"{path}:{number}: expected KEY=VALUE")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _flag(value: str, name: str) -> bool:
    lowered = value.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false")


def _integer(value: str, name: str, minimum: int) -> int:
    try:
        number = int(value)
    except ValueError:
        raise ConfigError(f"{name} must be an integer") from None
    if number < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    return number


def _user_ids(value: str, name: str, required: bool = True) -> frozenset[int]:
    if not value and not required:
        return frozenset()
    parts = [part.strip() for part in value.split(",")]
    if not parts or any(not part for part in parts):
        raise ConfigError(f"{name} must contain comma-separated positive integers")
    try:
        ids = frozenset(int(part) for part in parts)
    except ValueError:
        raise ConfigError(f"{name} must contain comma-separated positive integers") from None
    if any(user_id < 1 for user_id in ids):
        raise ConfigError(f"{name} must contain comma-separated positive integers")
    return ids


def load(environ=None, env_file=None) -> Config:
    """Build the configuration from an optional .env file overlaid with environment variables."""
    environ = os.environ if environ is None else environ
    file_path = Path(env_file or environ.get("BOT_ENV_FILE") or ROOT / ".env").expanduser()
    values = {**parse_env_file(file_path), **{k: v for k, v in environ.items() if k in _KNOWN}}

    def get(name, default=""):
        return values.get(name, default).strip()

    admin_name = next((name for name in ("TELEGRAM_ADMIN_IDS", "TELEGRAM_ADMIN_ID", "TELEGRAM_OWNER_ID")
                       if get(name)), "TELEGRAM_ADMIN_IDS")
    user_name = next((name for name in ("TELEGRAM_USER_IDS", "TELEGRAM_USER_ID") if get(name)),
                     "TELEGRAM_USER_IDS")
    admin_ids = _user_ids(get(admin_name), admin_name) if get(admin_name) else frozenset()
    user_ids = _user_ids(get(user_name), user_name, required=False)
    missing = [n for n in ("TELEGRAM_BOT_TOKEN", "GITHUB_TOKEN") if not get(n)]
    if not admin_ids:
        missing.append("TELEGRAM_ADMIN_IDS")
    if missing:
        raise ConfigError("Missing required settings: " + ", ".join(missing))
    overlap = admin_ids & user_ids
    if overlap:
        raise ConfigError("Telegram user ids cannot be both admins and regular users: " +
                          ", ".join(str(user_id) for user_id in sorted(overlap)))
    model = get("COPILOT_MODEL", DEFAULT_MODEL)
    if model.count("/") != 1 or not all(part.strip() for part in model.split("/")):
        raise ConfigError("COPILOT_MODEL must be a provider/model pair such as GPT/GPT-6")
    cloudflare = {
        "CLOUDFLARE_API_TOKEN": get("CLOUDFLARE_API_TOKEN"),
        "CLOUDFLARE_ACCOUNT_ID": get("CLOUDFLARE_ACCOUNT_ID"),
        "CLOUDFLARE_ZONE_ID": get("CLOUDFLARE_ZONE_ID"),
    }
    if any(cloudflare.values()):
        absent = [name for name, value in cloudflare.items() if not value]
        if absent:
            raise ConfigError("Cloudflare routing also requires: " + ", ".join(absent))
        for name in ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_ZONE_ID"):
            if not re.fullmatch(r"[0-9a-fA-F]{32}", cloudflare[name]):
                raise ConfigError(f"{name} must be a 32-character hexadecimal id")
    home = Path.home()
    return Config(
        telegram_token=get("TELEGRAM_BOT_TOKEN"),
        owner_ids=admin_ids,
        github_token=get("GITHUB_TOKEN"),
        github_owner=get("GITHUB_OWNER"),
        repos_dir=Path(get("REPOS_DIR", "/home/localadmin/repos")).expanduser(),
        state_dir=Path(get("STATE_DIR", str(home / ".copilot-telegram-bot"))).expanduser(),
        profile=Path(get("COPILOT_PROFILE", str(home / ".copilot-repo-browser"))).expanduser(),
        model=model,
        timeout=_integer(get("COPILOT_TIMEOUT", "30"), "COPILOT_TIMEOUT", 1),
        show_browser=_flag(get("BOT_SHOW_BROWSER", "false"), "BOT_SHOW_BROWSER"),
        temporary_chat=_flag(get("BOT_TEMPORARY_CHAT", "true"), "BOT_TEMPORARY_CHAT"),
        settle_seconds=_integer(get("DEPLOY_SETTLE_SECONDS", "10"), "DEPLOY_SETTLE_SECONDS", 1),
        git_name=get("GIT_AUTHOR_NAME", "Copilot Telegram Bot"),
        git_email=get("GIT_AUTHOR_EMAIL", "copilot-telegram-bot@users.noreply.github.com"),
        cloudflare_api_token=cloudflare["CLOUDFLARE_API_TOKEN"],
        cloudflare_account_id=cloudflare["CLOUDFLARE_ACCOUNT_ID"],
        cloudflare_zone_id=cloudflare["CLOUDFLARE_ZONE_ID"],
        cloudflare_tunnel="kurwalab-tunnel",
        cloudflare_domain="kurwalab.fyi",
        user_ids=user_ids,
    )


_KNOWN = {
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_ADMIN_IDS", "TELEGRAM_ADMIN_ID", "TELEGRAM_USER_IDS",
    "TELEGRAM_USER_ID", "TELEGRAM_OWNER_ID", "GITHUB_TOKEN", "GITHUB_OWNER", "REPOS_DIR", "STATE_DIR",
    "COPILOT_PROFILE", "COPILOT_MODEL", "COPILOT_TIMEOUT", "BOT_SHOW_BROWSER", "BOT_TEMPORARY_CHAT",
    "DEPLOY_SETTLE_SECONDS", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "CLOUDFLARE_API_TOKEN",
    "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_ZONE_ID",
}


def redact(text: str, secrets) -> str:
    """Hide secret values in text that is logged or sent to Telegram."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text
