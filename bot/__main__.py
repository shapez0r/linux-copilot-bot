"""Entry point: `python -m bot` runs the bot; `python -m bot login` signs in to Copilot."""
from __future__ import annotations

import argparse
import logging
import shutil
import signal
import subprocess
import sys
import threading

from . import config
from .deploy import Deployer
from .github_api import GitHub, GitHubError
from .gitops import Git
from .roundtrip import RoundTrip
from .service import Bot
from .state import State
from .telegram_api import Telegram

log = logging.getLogger("bot")


def login(cfg: config.Config) -> int:
    """Open a visible browser to sign in; the profile is shared with the bot's hidden runs."""
    return subprocess.call([sys.executable, str(cfg.engine), "--profile", str(cfg.profile), "login"])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bot", description=__doc__)
    parser.add_argument("command", nargs="?", choices=["run", "login"], default="run")
    parser.add_argument("--env-file", help="settings file (default: .env in the repository root)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = config.load(env_file=args.env_file)
    except config.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2
    if args.command == "login":
        return login(cfg)

    missing = [tool for tool in ("git", "make") if not shutil.which(tool)]
    if missing:
        print("Missing required tools: " + ", ".join(missing), file=sys.stderr)
        return 2
    if not cfg.profile.exists():
        print(f"No Copilot browser profile at {cfg.profile}. Run `make login` first.", file=sys.stderr)
        return 2
    try:
        gh = GitHub(cfg.github_token, cfg.github_owner)
        log.info("GitHub account: %s", gh.login)
    except GitHubError as e:
        print(f"GitHub check failed: {config.redact(str(e), cfg.secrets)}", file=sys.stderr)
        return 2
    cfg.repos_dir.mkdir(parents=True, exist_ok=True)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)

    bot = Bot(cfg, Telegram(cfg.telegram_token), gh, Git(cfg, gh.login), RoundTrip(cfg), Deployer(cfg),
              State(cfg.state_dir / "state.json", cfg.model))
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    log.info("Listening for messages from %d authorized Telegram user(s)", len(cfg.owner_ids or (cfg.owner_id,)))
    bot.run_forever(stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
