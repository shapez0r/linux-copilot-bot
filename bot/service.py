"""Owner commands and the request pipeline: prepare repo, Copilot round-trip, commit/push, redeploy."""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone

from .config import Config, redact
from .cloudflare import CloudflareError, CloudflareRoutes
from .deploy import Deployer
from .github_api import GitHub, GitHubError
from .gitops import Git, GitError
from .repos import RepoError, local_path, parse_repo_ref
from .roundtrip import Cancelled, RoundTrip, RoundTripError
from .state import State
from .telegram_api import Telegram, TelegramError

log = logging.getLogger("bot")

# Keep generated apps compatible with process supervision.
DEPLOY_NOTE = (
    "\n\nThis app is deployed by running `make run` from the repository root. Keep a Makefile whose `run` "
    "target starts one HTTP server in the foreground, and keep secrets and runtime data outside the repository."
)
NEW_REPO_TEMPLATE = "eurofish"

HELP = """Send a request as plain text and I will change the active repository with Copilot, commit, push and redeploy it with `make run`.

/new [public|private] <name|owner/name> - create a repository and select it; send your next message to start working
/repo [public|private] <name|owner/name> - select a repository; a missing one is created from the eurofish reference app
/repo - show the active repository
/fix <repo> <request> - select a repository and run a request in one message
/fix [browser|nobrowser] [tempchat|notempchat] <request> - run against the active repository with per-request browser/chat options
/cloudflare <subdomain> <port> - route <subdomain>.kurwalab.fyi to http://127.0.0.1:<port>
/cloudflare delete <subdomain> - delete the route for <subdomain>.kurwalab.fyi
/model <provider/model> - choose the Copilot model (for example GPT/GPT-6)
/status - show repository, model, job and app state
/cancel - stop the running job
/help - show this message"""

_BOT_ERRORS = (RepoError, GitError, GitHubError, RoundTripError, CloudflareError)


class Progress:
    """One Telegram message that is edited as a job advances, throttled to respect rate limits."""

    MIN_INTERVAL = 3.0
    ACTIVE_MARKS = ("\U0001f504", "\u23f3", "\u231b")

    def __init__(self, tg: Telegram, chat_id: int, cfg: Config):
        self._tg, self._chat, self._cfg = tg, chat_id, cfg
        self._id, self._shown, self._pending, self._last = None, "", "", 0.0
        self._status, self._ongoing, self._last_model_output, self._frame = "", False, None, 0

    def begin_prompt(self):
        """Add activity and model-output metadata while Copilot handles a prompt."""
        self._ongoing, self._last_model_output = True, None

    def end_prompt(self):
        self._ongoing = False
        self._pending = self._status

    def update(self, text: str, model_output: bool = False):
        self._status = redact(text, self._cfg.secrets)
        if model_output:
            self._last_model_output = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        if self._ongoing:
            mark = self.ACTIVE_MARKS[self._frame % len(self.ACTIVE_MARKS)]
            self._frame += 1
            last_output = self._last_model_output or "not received yet"
            self._pending = f"{mark} {self._status}\nLast model output: {last_output}"
        else:
            self._pending = self._status
        if self._id is None or time.monotonic() - self._last >= self.MIN_INTERVAL:
            self.flush()

    def flush(self):
        if not self._pending or self._pending == self._shown:
            return
        try:
            if self._id is None:
                self._id = self._tg.send_message(self._chat, self._pending)
            else:
                self._tg.edit_message(self._chat, self._id, self._pending)
            self._shown, self._last = self._pending, time.monotonic()
        except TelegramError as e:
            log.warning("progress update failed: %s", e)


class Bot:
    def __init__(self, cfg: Config, tg: Telegram, gh: GitHub, git: Git, roundtrip: RoundTrip,
                 deployer: Deployer, state: State):
        self.cfg, self.tg, self.gh, self.git = cfg, tg, gh, git
        self.roundtrip, self.deployer, self.state = roundtrip, deployer, state
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._worker = None

    # ---- Telegram loop -------------------------------------------------

    def run_forever(self, stop: threading.Event):
        offset, delay = 0, 1
        while not stop.is_set():
            try:
                updates = self.tg.get_updates(offset)
                delay = 1
            except TelegramError as e:
                log.warning("polling failed: %s", redact(str(e), self.cfg.secrets))
                stop.wait(delay)
                delay = min(delay * 2, 30)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    self.handle_update(update)
                except Exception:
                    log.exception("update failed")
        self.shutdown()

    def shutdown(self):
        self._cancel.set()
        if self._worker is not None:
            self._worker.join(timeout=60)

    def handle_update(self, update: dict):
        message = update.get("message") or {}
        sender = (message.get("from") or {}).get("id")
        chat = message.get("chat") or {}
        if sender != self.cfg.owner_id or chat.get("type") != "private":
            log.warning("ignored message from user %s", sender)
            return
        text = (message.get("text") or "").strip()
        if text:
            self.handle_text(chat["id"], text)
        else:
            self.say(chat["id"], "Send the request as text.")

    def say(self, chat_id: int, text: str):
        try:
            self.tg.send_long(chat_id, redact(text, self.cfg.secrets))
        except TelegramError as e:
            log.warning("send failed: %s", redact(str(e), self.cfg.secrets))

    # ---- Commands ------------------------------------------------------

    def handle_text(self, chat_id: int, text: str):
        if not text.startswith("/"):
            self.start_request(chat_id, "", text)
            return
        parts = re.split(r"\s+", text, maxsplit=1)
        command = parts[0][1:].split("@")[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if command in ("start", "help"):
            self.say(chat_id, HELP)
        elif command == "new":
            self.cmd_new(chat_id, arg)
        elif command == "repo":
            self.cmd_repo(chat_id, arg)
        elif command == "fix":
            self.cmd_fix(chat_id, arg)
        elif command == "cloudflare":
            self.cmd_cloudflare(chat_id, arg)
        elif command == "model":
            self.cmd_model(chat_id, arg)
        elif command == "status":
            self.cmd_status(chat_id)
        elif command == "cancel":
            self.cmd_cancel(chat_id)
        else:
            self.say(chat_id, "Unknown command. Send /help for the list.")

    def cmd_cloudflare(self, chat_id: int, arg: str):
        """Create, update or delete only the route explicitly supplied by the owner."""
        parts = arg.split()
        deleting = len(parts) == 2 and parts[0].lower() == "delete"
        if len(parts) != 2:
            self.say(chat_id, "Usage: /cloudflare <subdomain> <port> or /cloudflare delete <subdomain>")
            return
        route = parts[1] if deleting else parts[0]
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", route):
            self.say(chat_id, "Subdomain must be a lowercase DNS label (letters, digits, hyphens).")
            return
        raw_port = None if deleting else parts[1]
        if raw_port is not None and (not raw_port.isascii() or not raw_port.isdecimal()
                                     or not 1 <= int(raw_port) <= 65535):
            self.say(chat_id, "Port must be an integer from 1 to 65535.")
            return
        if not self.cfg.cloudflare_enabled:
            self.say(chat_id, "Configure Cloudflare API token, account ID and zone ID first.")
            return
        if deleting:
            self._spawn(chat_id, lambda: self.say(chat_id, CloudflareRoutes(self.cfg).delete(route)))
        else:
            self._spawn(chat_id, lambda: self.say(
                chat_id, CloudflareRoutes(self.cfg).prepare(route, int(raw_port))))

    def cmd_model(self, chat_id: int, arg: str):
        if not arg:
            self.say(chat_id, f"Model: {self.state.model}")
        elif arg.count("/") != 1 or not all(p.strip() for p in arg.split("/")):
            self.say(chat_id, "Use a provider/model pair such as GPT/GPT-6.")
        else:
            self.state.model = arg
            self.state.save()
            self.say(chat_id, f"Model set to {arg}.")

    def cmd_status(self, chat_id: int):
        lines = [f"Repository: {self.state.repo or 'none selected'}", f"Model: {self.state.model}",
                 f"Job: {'running' if self._lock.locked() else 'idle'}"]
        if self.state.repo:
            name = self.state.repo.split("/", 1)[1]
            lines.append(f"App: {self.deployer.status(name, local_path(self.cfg.repos_dir, name))}")
        self.say(chat_id, "\n".join(lines))

    def cmd_cancel(self, chat_id: int):
        if self._lock.locked():
            self._cancel.set()
            self.say(chat_id, "Cancelling; Copilot is asked to stop.")
        else:
            self.say(chat_id, "No job is running.")

    def cmd_fix(self, chat_id: int, arg: str):
        """Run a request with optional browser and Temporary chat overrides."""
        words = arg.split()
        show_browser = temporary_chat = None
        used_option = False
        while words:
            option = words[0].lower()
            if option in ("browser", "nobrowser"):
                show_browser = option == "browser"
            elif option in ("tempchat", "notempchat"):
                temporary_chat = option == "tempchat"
            else:
                break
            used_option = True
            words.pop(0)
        if used_option:
            if not words:
                self.say(chat_id, "Usage: /fix [browser|nobrowser] [tempchat|notempchat] <request>")
                return
            self.start_request(chat_id, "", " ".join(words), show_browser, temporary_chat)
            return
        ref, _, request = arg.partition(" ")
        if not ref or not request.strip():
            self.say(chat_id, "Usage: /fix <repo> <request>")
        else:
            self.start_request(chat_id, ref, request.strip())

    @staticmethod
    def _creation_options(arg: str):
        """Read an optional visibility prefix; private remains the default."""
        parts = arg.split(None, 1)
        if parts and parts[0].lower() in ("public", "private"):
            return (parts[1].strip() if len(parts) > 1 else ""), parts[0].lower() == "private"
        return arg, True

    def cmd_new(self, chat_id: int, arg: str):
        arg, private = self._creation_options(arg)
        if not arg:
            self.say(chat_id, "Usage: /new [public|private] <name|owner/name>")
            return
        self._spawn(chat_id, lambda: self._select_repo(chat_id, arg, new_only=True, private=private))

    def cmd_repo(self, chat_id: int, arg: str):
        arg, private = self._creation_options(arg)
        if not arg:
            self.say(chat_id, f"Active repository: {self.state.repo}" if self.state.repo
                     else "No repository selected. Use /repo <name>.")
            return
        self._spawn(chat_id, lambda: self._select_repo(chat_id, arg, private=private))

    def start_request(self, chat_id: int, ref: str, request: str,
                      show_browser=None, temporary_chat=None):
        if not ref and not self.state.repo:
            self.say(chat_id, "Select a repository first with /repo <name>.")
            return
        self._spawn(chat_id, lambda: self._request(chat_id, ref or self.state.repo, request,
                                                   show_browser, temporary_chat))

    # ---- Job execution -------------------------------------------------

    def _spawn(self, chat_id: int, job):
        if not self._lock.acquire(blocking=False):
            self.say(chat_id, "A job is already running. Wait for it or send /cancel.")
            return
        self._cancel.clear()

        def run():
            try:
                job()
            except Cancelled:
                self.say(chat_id, "Cancelled.")
            except _BOT_ERRORS as e:
                self.say(chat_id, f"\u274c {e}")
            except Exception as e:
                log.exception("job failed")
                self.say(chat_id, f"\u274c Internal error: {e.__class__.__name__}. See the bot log.")
            finally:
                self._lock.release()

        self._worker = threading.Thread(target=run, name="job", daemon=True)
        self._worker.start()

    def _owner_default(self) -> str:
        return self.cfg.github_owner or self.gh.login

    def _prepare(self, owner: str, name: str, progress: Progress, new_only: bool = False, private: bool = True):
        """Create a repository from the reference app, or clone and update an existing repository."""
        progress.update(f"Looking up {owner}/{name} on GitHub...")
        repo = self.gh.get_repo(owner, name)
        if new_only and repo is not None:
            raise RepoError(f"{owner}/{name} already exists. Use /repo to select it.")
        created = repo is None
        if created:
            progress.update(f"Creating {'private' if private else 'public'} repository {owner}/{name}...")
            repo = self.gh.create_repo(owner, name, private=private)
        dest = local_path(self.cfg.repos_dir, name)
        if created:
            template = self.cfg.repos_dir / NEW_REPO_TEMPLATE
            progress.update(f"Copying {template} into the new repository...")
            branch = self.git.seed_from_template(repo["clone_url"], dest, template,
                                                 repo.get("default_branch") or "main")
            return dest, branch, f"created from {NEW_REPO_TEMPLATE}"
        progress.update(f"Preparing {dest}...")
        cloned = self.git.ensure_clone(repo["clone_url"], dest, owner, name)
        branch = self.git.sync(dest)
        return dest, branch, "cloned" if cloned else "already cloned"

    def _select_repo(self, chat_id: int, arg: str, new_only: bool = False, private: bool = True):
        owner, name = parse_repo_ref(arg, self._owner_default())
        progress = Progress(self.tg, chat_id, self.cfg)
        dest, branch, how = self._prepare(owner, name, progress, new_only=new_only, private=private)
        self.state.repo = f"{owner}/{name}"
        self.state.save()
        progress.flush()
        self.say(chat_id, f"\u2705 {owner}/{name} is {how} at {dest} (branch {branch}) and now active."
                 + (" Send your next message describing what to build." if new_only else ""))

    def _request(self, chat_id: int, ref: str, request: str,
                 show_browser=None, temporary_chat=None):
        owner, name = parse_repo_ref(ref, self._owner_default())
        full = f"{owner}/{name}"
        progress = Progress(self.tg, chat_id, self.cfg)
        dest, branch, _ = self._prepare(owner, name, progress)
        self.state.repo = full
        self.state.save()
        if self._cancel.is_set():
            raise Cancelled()
        progress.begin_prompt()
        progress.update(f"{full}: sending the repository to Copilot...")
        try:
            result = self.roundtrip.run(dest, request + DEPLOY_NOTE, self.state.model,
                                        lambda stage, msg: progress.update(
                                            f"{full}: {msg}", model_output=stage in ("task", "reasoning")),
                                        self._cancel,
                                        show_browser, temporary_chat)
        except Cancelled:
            progress.end_prompt()
            progress.flush()
            if self.git.is_dirty(dest):
                self.say(chat_id, "\u26a0\ufe0f The checkout has uncommitted changes from the interrupted run; "
                                  f"review {dest} before the next request.")
            raise
        except Exception:
            progress.end_prompt()
            progress.flush()
            raise
        progress.end_prompt()
        progress.update(f"{full}: committing and pushing...")
        pushed = self.git.commit_and_push(dest, request)
        if pushed is None:
            progress.flush()
            self.say(chat_id, f"Copilot returned no changes to commit for {full}; nothing was deployed.")
            return
        sha, pushed_branch = pushed
        progress.update(f"{full}: redeploying with make run...")
        deploy = self.deployer.redeploy(name, dest)
        progress.flush()
        self.say(chat_id, self._report(full, sha, pushed_branch, result, deploy))

    def _report(self, full: str, sha: str, branch: str, result: dict, deploy) -> str:
        diff_lines = [l for l in (result.get("diff_stat") or "").splitlines() if l.strip()]
        mark = "\u2705" if deploy.ok else "\u26a0\ufe0f"
        lines = [f"{mark} {full}: commit {sha} pushed to {branch}."]
        if diff_lines:
            lines.append(f"Changes: {diff_lines[-1].strip()}")
        lines.append(("Deploy: " if deploy.ok else "Pushed, but the redeploy failed: ") + deploy.message)
        return "\n".join(lines)
