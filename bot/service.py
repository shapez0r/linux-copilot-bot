"""Authorized-user commands and the repository request pipeline."""
from __future__ import annotations

import logging
import re
import shutil
import threading
import time

from .config import Config, redact
from .cloudflare import CloudflareError, CloudflareRoutes
from .deploy import DeployError, Deployer
from .github_api import GitHub, GitHubError
from .gitops import Git, GitError
from .repos import RepoError, local_path, parse_repo_ref
from .roundtrip import (Cancelled, RoundTrip, RoundTripError, StatusTimeline,
                        format_stage_status)
from .state import State
from .telegram_api import Telegram, TelegramError

log = logging.getLogger("bot")

# Keep generated apps compatible with process supervision.
DEPLOY_NOTE = (
    "\n\nThis app is deployed by running `make run` from the repository root. Keep a Makefile whose `run` "
    "target starts one HTTP server in the foreground, and keep secrets and runtime data outside the repository."
)
NEW_REPO_TEMPLATE = "eurofish"
NEW_APP_PROMPT = (
    "Instead of this app, let's create another app, with the same architecture, stack and styling, "
    "but running on the {port}\n\n"
)

NEW_USAGE = (
    "Usage: /new [public|private] [model=<provider/model>] [template=<app>] "
    'name="<name|owner/name>" prompt="<request>"'
)

HELP = """Send a request as plain text and I will change the active repository with Copilot, commit, push and redeploy it with `make run`.

/new [public|private] [model=<provider/model>] [template=<app>] name="<name|owner/name>" prompt="<request>" - create, build, deploy and publish a new app
/delete <name|owner/name> - permanently delete an app's GitHub repository, local checkout and tunnel route
/repo [public|private] <name|owner/name> - select a repository; a missing one is created from the eurofish reference app
/repo - show the active repository
/fix <repo> <request> - select a repository and run a request in one message
/fix [browser|nobrowser] [tempchat|notempchat] <request> - run against the active repository with per-request browser/chat options
/list - list running apps
/cloudflare <subdomain> <port> - route <subdomain>.kurwalab.fyi to http://127.0.0.1:<port>
/cloudflare list - list tunnel routes
/cloudflare delete <subdomain> - delete a tunnel route by name
/model <provider/model> - choose the Copilot model (for example GPT/GPT-6)
/status - show repository, model, job and app state
/cancel - stop the running job
/help - show this message"""

_BOT_ERRORS = (RepoError, GitError, GitHubError, RoundTripError, CloudflareError, DeployError)


class Progress:
    """One Telegram message that is edited as a job advances, throttled to respect rate limits."""

    MIN_INTERVAL = 3.0

    def __init__(self, tg: Telegram, chat_id: int, cfg: Config):
        self._tg, self._chat, self._cfg = tg, chat_id, cfg
        self._id, self._shown, self._pending, self._last = None, "", "", 0.0
        self._timeline = StatusTimeline()

    def update(self, text: str, stage="job"):
        if not text.startswith("Currently doing: "):
            text = format_stage_status(text, self._timeline, stage)
        self._pending = redact(text, self._cfg.secrets)
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
        if sender not in self.cfg.owner_ids or chat.get("type") != "private":
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
        elif command == "delete":
            self.cmd_delete(chat_id, arg)
        elif command == "repo":
            self.cmd_repo(chat_id, arg)
        elif command == "fix":
            self.cmd_fix(chat_id, arg)
        elif command == "list":
            self.cmd_list(chat_id, arg)
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
        """Manage only routes explicitly requested by an authorized user."""
        parts = arg.split()
        usage = "Usage: /cloudflare <subdomain> <port> | list | delete <subdomain>"
        if parts and parts[0] == "list":
            if parts != ["list"]:
                self.say(chat_id, usage)
                return
            if self._cloudflare_unavailable(chat_id):
                return
            self._spawn(chat_id, lambda: self.say(chat_id, CloudflareRoutes(self.cfg).list_routes()))
            return
        if parts and parts[0] == "delete":
            if len(parts) != 2:
                self.say(chat_id, usage)
                return
            route = parts[1]
            if not self._valid_route(route):
                self.say(chat_id, "Subdomain must be a lowercase DNS label (letters, digits, hyphens).")
                return
            if self._cloudflare_unavailable(chat_id):
                return
            self._spawn(chat_id, lambda: self.say(chat_id, CloudflareRoutes(self.cfg).delete_route(route)))
            return
        if len(parts) != 2:
            self.say(chat_id, usage)
            return
        route, raw_port = parts
        if not self._valid_route(route):
            self.say(chat_id, "Subdomain must be a lowercase DNS label (letters, digits, hyphens).")
            return
        if not raw_port.isascii() or not raw_port.isdecimal() or not 1 <= int(raw_port) <= 65535:
            self.say(chat_id, "Port must be an integer from 1 to 65535.")
            return
        if self._cloudflare_unavailable(chat_id):
            return
        self._spawn(chat_id, lambda: self.say(chat_id, CloudflareRoutes(self.cfg).prepare(route, int(raw_port))))

    @staticmethod
    def _valid_route(route: str) -> bool:
        return bool(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", route))

    def _cloudflare_unavailable(self, chat_id: int) -> bool:
        if self.cfg.cloudflare_enabled:
            return False
        self.say(chat_id, "Configure Cloudflare API token, account ID and zone ID first.")
        return True

    def cmd_model(self, chat_id: int, arg: str):
        if not arg:
            self.say(chat_id, f"Model: {self.state.model}")
        elif not self._valid_model(arg):
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

    def cmd_list(self, chat_id: int, arg: str):
        if arg:
            self.say(chat_id, "Usage: /list")
            return
        apps = self.deployer.running_apps()
        if not apps:
            self.say(chat_id, "No apps are running.")
            return
        self.say(chat_id, "\n".join(["Running apps:", *(f"- {app} (pid {pid})" for app, pid in apps)]))

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

    @staticmethod
    def _valid_model(model: str) -> bool:
        return model.count("/") == 1 and all(part.strip() for part in model.split("/"))

    @staticmethod
    def _new_options(arg: str):
        """Read leading visibility, name, model, template and prompt options in any order."""
        remaining = arg.lstrip()
        private, model, template, name, prompt = True, None, None, None, None
        while remaining:
            parts = remaining.split(None, 1)
            option = parts[0].lower()
            if option in ("public", "private"):
                private = option == "private"
                remaining = parts[1].lstrip() if len(parts) > 1 else ""
                continue
            match = re.match(r'''name=(?:"([^"]*)"|'([^']*)'|([^\s'" ]+))(?=\s|$)''', remaining,
                             flags=re.IGNORECASE)
            if match:
                if name is not None:
                    return remaining, private, None, None, None, None, "Specify name only once."
                name = next(value for value in match.groups() if value is not None).strip()
                if not name or len(name.split()) != 1:
                    return remaining, private, None, None, None, None, 'Use name="<name|owner/name>" without spaces.'
                remaining = remaining[match.end():].lstrip()
                continue
            if option.startswith("name="):
                return remaining, private, None, None, None, None, 'Use name="<name|owner/name>".'
            match = re.match(r'''model=(?:"([^"]*)"|'([^']*)'|(\S+))(?=\s|$)''', remaining,
                             flags=re.IGNORECASE)
            if match:
                if model is not None:
                    return remaining, private, None, None, None, None, "Specify model only once."
                model = next(value for value in match.groups() if value is not None).strip()
                remaining = remaining[match.end():].lstrip()
                continue
            if option.startswith("model="):
                return remaining, private, None, None, None, None, (
                    'Use model=<provider/model>; quote names containing spaces, such as '
                    'model="Claude/Opus 5.5".'
                )
            match = re.match(r'''template=(?:"([^"]*)"|'([^']*)'|(\S+))(?=\s|$)''', remaining,
                             flags=re.IGNORECASE)
            if match:
                if template is not None:
                    return remaining, private, None, None, None, None, "Specify template only once."
                template = next(value for value in match.groups() if value is not None).strip()
                remaining = remaining[match.end():].lstrip()
                continue
            if option.startswith("template="):
                return remaining, private, None, None, None, None, "Use template=<app>, such as template=eurofish."
            match = re.match(r'''prompt=(?:"([^"]*)"|'([^']*)'|([^\s'" ]+))(?=\s|$)''', remaining,
                             flags=re.IGNORECASE)
            if match:
                if prompt is not None:
                    return remaining, private, None, None, None, None, "Specify prompt only once."
                prompt = next(value for value in match.groups() if value is not None).strip()
                if not prompt:
                    return remaining, private, None, None, None, None, 'Use prompt="<request>" with a nonempty request.'
                remaining = remaining[match.end():].lstrip()
                continue
            if option.startswith("prompt="):
                return remaining, private, None, None, None, None, 'Use prompt="<request>"; quote requests containing spaces.'
            break
        return remaining, private, model, template, name, prompt, ""

    def cmd_new(self, chat_id: int, arg: str):
        arg, private, model, template, name, prompt, error = self._new_options(arg)
        if error:
            self.say(chat_id, error)
            return
        if model is not None and not self._valid_model(model):
            self.say(chat_id, "Use a provider/model pair such as GPT/GPT-6.")
            return
        if template is not None and not self._valid_route(template):
            self.say(chat_id, "Template app names must be lowercase DNS labels containing only letters, digits and hyphens.")
            return
        if prompt is not None:
            if name is None and len(arg.split()) == 1:
                name, arg = arg, ""
            if name is None or arg:
                self.say(chat_id, NEW_USAGE)
                return
            parts = [name, prompt]
        else:
            parts = [name, arg] if name is not None else arg.split(None, 1)
        if len(parts) != 2 or not parts[1].strip():
            self.say(chat_id, NEW_USAGE)
            return
        if self._cloudflare_unavailable(chat_id):
            return
        ref, request = parts[0], parts[1].strip()
        self._spawn(chat_id, lambda: self._new_app(
            chat_id, ref, request, private, model, template or NEW_REPO_TEMPLATE))

    def cmd_delete(self, chat_id: int, arg: str):
        if len(arg.split()) != 1:
            self.say(chat_id, "Usage: /delete <name|owner/name>")
            return
        if self._cloudflare_unavailable(chat_id):
            return
        self._spawn(chat_id, lambda: self._delete_app(chat_id, arg))

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

    def _prepare(self, owner: str, name: str, progress: Progress, new_only: bool = False, private: bool = True,
                 template_name: str = NEW_REPO_TEMPLATE):
        """Create a repository from a local app template, or update an existing checkout."""
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
            template = self.cfg.repos_dir / template_name
            progress.update(f"Copying {template} into the new repository...")
            branch = self.git.seed_from_template(repo["clone_url"], dest, template,
                                                 repo.get("default_branch") or "main")
            return dest, branch, f"created from {template_name}"
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

    def _new_app(self, chat_id: int, ref: str, request: str, private: bool, model=None,
                 template: str = NEW_REPO_TEMPLATE):
        owner, name = parse_repo_ref(ref, self._owner_default())
        if not self._valid_route(name):
            raise RepoError("App names must be lowercase DNS labels containing only letters, digits and hyphens.")
        if name == template:
            raise RepoError("The new app name must differ from its template app name.")
        full = f"{owner}/{name}"
        progress = Progress(self.tg, chat_id, self.cfg)
        if not (self.cfg.repos_dir / template).is_dir():
            raise RepoError(f"Template app is not available locally: {template}")
        dest, _, _ = self._prepare(owner, name, progress, new_only=True, private=private,
                                   template_name=template)
        self.state.repo = full
        self.state.save()
        routes = CloudflareRoutes(self.cfg)
        progress.update(f"{full}: checking tunnel routes for the next port...")
        port = routes.next_port()
        prompt = NEW_APP_PROMPT.format(port=port) + request + DEPLOY_NOTE
        if self._cancel.is_set():
            raise Cancelled()
        progress.update(f"{full}: sending the repository to Copilot for port {port}...")
        try:
            result = self.roundtrip.run(
                dest, prompt, model or self.state.model,
                lambda stage, msg: progress.update(self._repo_progress(full, msg), stage), self._cancel)
        except Cancelled:
            progress.flush()
            if self.git.is_dirty(dest):
                self.say(chat_id, "\u26a0\ufe0f The checkout has uncommitted changes from the interrupted run; "
                                  f"review {dest} before the next request.")
            raise
        progress.update(f"{full}: committing and pushing...")
        pushed = self.git.commit_and_push(dest, request)
        if pushed is None:
            progress.flush()
            output = (result.get("chat_response") or "(no final text returned)").strip()
            self.say(chat_id, f"Copilot returned no changes to commit for {full}; nothing was deployed.\n\n"
                              f"Copilot output:\n{output}")
            return
        sha, pushed_branch = pushed
        progress.update(f"{full}: deploying with make run on port {port}...")
        deploy = self.deployer.redeploy(name, dest)
        if not deploy.ok:
            progress.flush()
            self.say(chat_id, self._new_app_report(full, sha, pushed_branch, result, deploy.message,
                                                   error="The app was not routed because deployment failed."))
            return
        progress.update(f"{full}: creating https://{name}.{self.cfg.cloudflare_domain}...")
        try:
            tunnel = routes.prepare(name, port)
        except CloudflareError as error:
            progress.flush()
            self.say(chat_id, self._new_app_report(full, sha, pushed_branch, result, deploy.message,
                                                   error=f"Tunnel setup failed: {error}"))
            return
        progress.update(f"{full}: adding reboot startup to crontab...")
        try:
            startup = self.deployer.ensure_reboot(dest)
        except DeployError as error:
            progress.flush()
            self.say(chat_id, self._new_app_report(full, sha, pushed_branch, result, deploy.message,
                                                   tunnel=tunnel, error=f"Crontab setup failed: {error}"))
            return
        progress.flush()
        self.say(chat_id, self._new_app_report(full, sha, pushed_branch, result, deploy.message,
                                               tunnel=tunnel, startup=startup,
                                               url=f"https://{name}.{self.cfg.cloudflare_domain}"))

    def _delete_app(self, chat_id: int, ref: str):
        """Delete the resources created for one app after validating its local checkout."""
        owner, name = parse_repo_ref(ref, self._owner_default())
        if not self._valid_route(name):
            raise RepoError("App names must be lowercase DNS labels containing only letters, digits and hyphens.")
        full = f"{owner}/{name}"
        dest = local_path(self.cfg.repos_dir, name)
        if self.gh.get_repo(owner, name) is None:
            raise RepoError(f"GitHub repository {full} was not found.")
        self.git.require_checkout(dest, owner, name)

        stopped = self.deployer.stop(name, dest)
        startup = self.deployer.remove_reboot(dest)
        tunnel = CloudflareRoutes(self.cfg).delete_route(name)
        self.gh.delete_repo(owner, name)
        try:
            shutil.rmtree(dest)
        except OSError as error:
            raise RepoError(f"GitHub repository and tunnel route were deleted, but {dest} could not be removed: "
                            f"{error.strerror or error}") from None
        if self.state.repo.lower() == full.lower():
            self.state.repo = ""
            self.state.save()
        process = "Stopped the running app." if stopped else "No running app process was recorded."
        self.say(chat_id, f"✅ Deleted {full}.\n{process}\n{startup}\n{tunnel}\n"
                          f"GitHub: deleted {full}.\nLocal: deleted {dest}.")

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
        progress.update(f"{full}: sending the repository to Copilot...")
        try:
            result = self.roundtrip.run(dest, request + DEPLOY_NOTE, self.state.model,
                                        lambda stage, msg: progress.update(self._repo_progress(full, msg), stage),
                                        self._cancel,
                                        show_browser, temporary_chat)
        except Cancelled:
            progress.flush()
            if self.git.is_dirty(dest):
                self.say(chat_id, "\u26a0\ufe0f The checkout has uncommitted changes from the interrupted run; "
                                  f"review {dest} before the next request.")
            raise
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

    @staticmethod
    def _repo_progress(full: str, message: str) -> str:
        """Keep the current-activity header first while retaining repository context."""
        first, separator, rest = message.partition("\n")
        if first.startswith("Currently doing: "):
            return first + f"\nRepository: {full}" + (separator + rest if separator else "")
        return f"{full}: {message}"

    def _report(self, full: str, sha: str, branch: str, result: dict, deploy) -> str:
        diff_lines = [l for l in (result.get("diff_stat") or "").splitlines() if l.strip()]
        mark = "\u2705" if deploy.ok else "\u26a0\ufe0f"
        lines = [f"{mark} {full}: commit {sha} pushed to {branch}."]
        if diff_lines:
            lines.append(f"Changes: {diff_lines[-1].strip()}")
        lines.append(("Deploy: " if deploy.ok else "Pushed, but the redeploy failed: ") + deploy.message)
        return "\n".join(lines)

    @staticmethod
    def _new_app_report(full: str, sha: str, branch: str, result: dict, deploy_message: str,
                        tunnel: str = "", startup: str = "", url: str = "", error: str = "") -> str:
        mark = "\u26a0\ufe0f" if error else "\u2705"
        lines = [f"{mark} {full}: commit {sha} pushed to {branch}.", f"Deploy: {deploy_message}"]
        if tunnel:
            lines.append(tunnel)
        if startup:
            lines.append(startup)
        if url:
            lines.append(f"App: {url}")
        if error:
            lines.append(error)
        output = (result.get("chat_response") or "(no final text returned)").strip()
        lines.extend(["", "Copilot output:", output])
        return "\n".join(lines)
