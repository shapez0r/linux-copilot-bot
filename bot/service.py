"""Authorized-user commands and the repository request pipeline."""
from __future__ import annotations

import html
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
from .state import ADMIN_OWNER, State, telegram_owner
from .telegram_api import Telegram, TelegramError

log = logging.getLogger("bot")

# Keep generated apps compatible with process supervision.
DEPLOY_NOTE = (
    "\n\nThis app is deployed by running `make run` from the repository root. Keep a Makefile whose `run` "
    "target starts one HTTP server in the foreground, and keep secrets and runtime data outside the repository."
)
NEW_REPO_TEMPLATE = "eurofish"
NEW_APP_PROMPT = (
    "Use this app as the template for a new app running on port {port}. Keep its architecture, stack and "
    "styling by default, but change any of them when the request below asks for it.\n\n"
)
KNOWN_MODELS = (
    "Claude/Sonnet 5.5",
    "Claude/Opus 5.5",
    "GPT/GPT-6",
    "GPT/GPT-6.1 Sol",
)

CREATE_APP = "➕ Create an app"
LIST_APPS = "📱 List apps"
FIX_APP = "🛠 Fix an app"
START_APP = "▶️ Start an app"
STOP_APP = "⏹ Stop an app"
REGISTER_SERVICES = "⚙️ Register app services"
DELETE_APP = "🗑 Delete an app"
TUNNEL_ROUTES = "☁️ Tunnel routes"
SELECT_REPOSITORY = "📂 Select repository"
SET_MODEL = "🤖 Set model"
SHOW_STATUS = "ℹ️ Status"
CANCEL_ACTION = "❌ Cancel / stop"
BACK_TO_MENU = "⬅️ Main menu"
CUSTOM_MODEL = "⌨️ Enter another model"
LIST_ROUTES = "📋 List routes"
ADD_ROUTE = "➕ Add or update route"
DELETE_ROUTE = "🗑 Delete route"
PRIVATE_REPO = "🔒 Private repository"
PUBLIC_REPO = "🌐 Public repository"
CONFIRM_DELETE = "✅ Yes, delete it"
KEEP_APP = "↩️ No, keep it"
TEMPORARY_CHAT = "🕶 Temporary chat"
REGULAR_CHAT = "💾 Regular chat"


def _keyboard(rows):
    """Build a persistent Telegram reply keyboard from button-label rows."""
    return {
        "keyboard": [[{"text": label} for label in row] for row in rows],
        "resize_keyboard": True,
        "is_persistent": True,
    }


MAIN_KEYBOARD = _keyboard([
    [CREATE_APP, LIST_APPS],
    [FIX_APP, DELETE_APP],
    [START_APP, STOP_APP],
    [REGISTER_SERVICES],
    [TUNNEL_ROUTES, SELECT_REPOSITORY],
    [SET_MODEL, SHOW_STATUS],
    [CANCEL_ACTION],
])
USER_KEYBOARD = _keyboard([
    [CREATE_APP, LIST_APPS],
    [FIX_APP],
    [START_APP, STOP_APP],
])
MODEL_KEYBOARD = _keyboard([
    [KNOWN_MODELS[0], KNOWN_MODELS[1]],
    [KNOWN_MODELS[2], KNOWN_MODELS[3]],
    [CUSTOM_MODEL],
    [BACK_TO_MENU],
])
ROUTES_KEYBOARD = _keyboard([
    [LIST_ROUTES],
    [ADD_ROUTE, DELETE_ROUTE],
    [BACK_TO_MENU],
])
CANCEL_KEYBOARD = _keyboard([[CANCEL_ACTION], [BACK_TO_MENU]])
VISIBILITY_KEYBOARD = _keyboard([[PRIVATE_REPO, PUBLIC_REPO], [CANCEL_ACTION], [BACK_TO_MENU]])
CHAT_MODE_KEYBOARD = _keyboard([[TEMPORARY_CHAT, REGULAR_CHAT], [CANCEL_ACTION], [BACK_TO_MENU]])

NEW_USAGE = (
    "Usage: /new [public|private] [tempchat|notempchat] [model=<provider/model>] [template=<app>] "
    'name="<name|owner/name>" prompt="<request>"'
)

HELP = """Use the buttons below to create and manage apps. You can also send a request as plain text to change the active repository, commit, push and redeploy it with `make run`.

/menu - show the app-management menu
/new [public|private] [tempchat|notempchat] [model=<provider/model>] [template=<app>] name="<name|owner/name>" prompt="<request>" - create, build, deploy and publish a new app (private by default)
/delete <name|owner/name> - permanently delete an app service, GitHub repository, local checkout and tunnel route
/repo [public|private] <name|owner/name> - select a repository; a missing one is created from the eurofish reference app
/repo - show the active repository
/fix <repo> <request> - select a repository and run a request in one message
/fix [browser|nobrowser] [tempchat|notempchat] <request> - run against the active repository with per-request browser/chat options
/list - list every local app with systemd status and tunnel details
/app start|stop <name> - start or stop an app service
/register [all|<name>] - register and enable systemd services for existing local apps
/cloudflare <subdomain> <port> - route <subdomain>.kurwalab.fyi to http://127.0.0.1:<port>
/cloudflare list - list tunnel routes
/cloudflare delete <subdomain> - delete a tunnel route by name
/model <provider/model> - choose the Copilot model (for example GPT/GPT-6)
/status - show repository, model, job and app state
/cancel - stop the running job
/help - show this message"""
USER_HELP = """Use the buttons below to create and list apps, and to fix, start, or stop apps you created.

/menu - show the app menu
/new [model=<provider/model>] [template=<app>] name="<name|owner/name>" prompt="<request>" - create, build, deploy and publish a new app in a public repository
/fix <repo> <request> - fix an app you created
/fix [browser|nobrowser] [tempchat|notempchat] <request> - fix your active app
/list - list every local app with its status and public URL
/app start|stop <name> - start or stop one of your app services
/cancel - stop your own running job
/help - show this message"""

ADMIN_ONLY_BUTTONS = {
    DELETE_APP, TUNNEL_ROUTES, SELECT_REPOSITORY, SET_MODEL, SHOW_STATUS,
    LIST_ROUTES, ADD_ROUTE, DELETE_ROUTE, REGISTER_SERVICES,
}
ADMIN_ONLY_COMMANDS = {"delete", "repo", "cloudflare", "model", "status", "register"}
ADMIN_ONLY_MESSAGE = "This action is available only to kurwalab.fyi admins."

_BOT_ERRORS = (RepoError, GitError, GitHubError, RoundTripError, CloudflareError, DeployError)


class Progress:
    """One Telegram HTML message that is edited as a job advances, throttled to respect rate limits."""

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
                self._id = self._tg.send_message(self._chat, self._pending, parse_mode="HTML")
            else:
                self._tg.edit_message(self._chat, self._id, self._pending, parse_mode="HTML")
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
        self._job_user_id = None
        self._list_worker = None
        self._dialogs = {}

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
        if sender not in self.cfg.authorized_ids or chat.get("type") != "private":
            log.warning("ignored message from user %s", sender)
            return
        text = (message.get("text") or "").strip()
        if text:
            self.handle_text(chat["id"], text, sender)
        else:
            self.say(chat["id"], "Send the request as text.")

    def say(self, chat_id: int, text: str):
        try:
            self.tg.send_long(chat_id, redact(text, self.cfg.secrets))
        except TelegramError as e:
            log.warning("send failed: %s", redact(str(e), self.cfg.secrets))

    def say_keyboard(self, chat_id: int, text: str, keyboard):
        try:
            self.tg.send_long(chat_id, redact(text, self.cfg.secrets), reply_markup=keyboard)
        except TelegramError as e:
            log.warning("send failed: %s", redact(str(e), self.cfg.secrets))

    def _is_admin(self, user_id) -> bool:
        return user_id is None or self.cfg.is_admin(user_id)

    def _main_keyboard(self, user_id):
        return MAIN_KEYBOARD if self._is_admin(user_id) else USER_KEYBOARD

    def show_menu(self, chat_id: int, text: str = "What would you like to do?", user_id=None):
        self._dialogs.pop(chat_id, None)
        self.say_keyboard(chat_id, text, self._main_keyboard(user_id))

    # ---- Commands ------------------------------------------------------

    def handle_text(self, chat_id: int, text: str, user_id=None):
        if user_id is not None and user_id not in self.cfg.authorized_ids:
            return
        if text == BACK_TO_MENU:
            self.show_menu(chat_id, user_id=user_id)
            return
        if text == CANCEL_ACTION:
            self.cmd_cancel(chat_id, user_id)
            return
        if text in (CREATE_APP, LIST_APPS, FIX_APP, START_APP, STOP_APP, REGISTER_SERVICES,
                    DELETE_APP, TUNNEL_ROUTES,
                    SELECT_REPOSITORY, SET_MODEL, SHOW_STATUS, LIST_ROUTES, ADD_ROUTE, DELETE_ROUTE):
            self._handle_menu_button(chat_id, text, user_id)
            return
        if chat_id in self._dialogs and not text.startswith("/"):
            self._handle_dialog(chat_id, text, user_id)
            return
        if not text.startswith("/"):
            self.start_request(chat_id, "", text, user_id=user_id)
            return
        parts = re.split(r"\s+", text, maxsplit=1)
        command = parts[0][1:].split("@")[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if command in ADMIN_ONLY_COMMANDS and not self._is_admin(user_id):
            self.say(chat_id, ADMIN_ONLY_MESSAGE)
            return
        if command == "start":
            self.show_menu(chat_id, "Welcome. Create and manage apps with the buttons below.", user_id)
        elif command == "help":
            help_text = HELP if self._is_admin(user_id) else USER_HELP
            self.say_keyboard(chat_id, help_text, self._main_keyboard(user_id))
        elif command == "menu":
            self.show_menu(chat_id, user_id=user_id)
        elif command == "new":
            self.cmd_new(chat_id, arg, user_id)
        elif command == "delete":
            self.cmd_delete(chat_id, arg)
        elif command == "repo":
            self.cmd_repo(chat_id, arg, user_id)
        elif command == "fix":
            self.cmd_fix(chat_id, arg, user_id)
        elif command == "list":
            self.cmd_list(chat_id, arg, user_id)
        elif command == "app":
            self.cmd_app(chat_id, arg, user_id)
        elif command == "register":
            self.cmd_register(chat_id, arg, user_id)
        elif command == "cloudflare":
            self.cmd_cloudflare(chat_id, arg)
        elif command == "model":
            self.cmd_model(chat_id, arg)
        elif command == "status":
            self.cmd_status(chat_id, user_id)
        elif command == "cancel":
            self.cmd_cancel(chat_id, user_id)
        else:
            self.say(chat_id, "Unknown command. Send /help for the list.")

    def _handle_menu_button(self, chat_id: int, label: str, user_id=None):
        """Start a guided action selected from a reply-keyboard button."""
        if label in ADMIN_ONLY_BUTTONS and not self._is_admin(user_id):
            self.say(chat_id, ADMIN_ONLY_MESSAGE)
            return
        if label == LIST_APPS:
            self.cmd_list(chat_id, "", user_id)
        elif label == SHOW_STATUS:
            self.cmd_status(chat_id, user_id)
        elif label == REGISTER_SERVICES:
            self.cmd_register(chat_id, "all", user_id)
        elif label == TUNNEL_ROUTES:
            self._dialogs.pop(chat_id, None)
            self.say_keyboard(chat_id, "Manage Cloudflare Tunnel routes.", ROUTES_KEYBOARD)
        elif label == LIST_ROUTES:
            self.cmd_cloudflare(chat_id, "list")
        elif label == CREATE_APP:
            if self._cloudflare_unavailable(chat_id) or self._job_blocks_dialog(chat_id):
                return
            self._dialogs[chat_id] = {"flow": "create", "step": "name", "data": {},
                                      "user_id": user_id}
            self.say_keyboard(
                chat_id,
                "Enter the new app name. Use a lowercase DNS label such as `invoice-dashboard`.",
                CANCEL_KEYBOARD,
            )
        elif label == FIX_APP:
            if self._job_blocks_dialog(chat_id):
                return
            choices = self._app_choices(user_id, include_active=True)
            if not choices and not self._is_admin(user_id):
                self.say(chat_id, "You do not have any apps to fix. Create an app first.")
                return
            self._dialogs[chat_id] = {"flow": "fix", "step": "repo", "data": {},
                                      "user_id": user_id}
            self.say_keyboard(
                chat_id,
                "Choose an app or enter its repository as `name` or `owner/name`.",
                self._choice_keyboard(choices),
            )
        elif label in (START_APP, STOP_APP):
            choices = self._manageable_app_names(user_id)
            if not choices:
                self.say(chat_id, "You do not have any apps to manage." if not self._is_admin(user_id)
                         else "No apps are available.")
                return
            action = "start" if label == START_APP else "stop"
            self._dialogs[chat_id] = {"flow": "app-control", "step": action, "data": {},
                                      "user_id": user_id}
            self.say_keyboard(chat_id, f"Choose an app to {action}.", self._choice_keyboard(choices))
        elif label == DELETE_APP:
            if self._job_blocks_dialog(chat_id):
                return
            choices = self._app_choices(user_id, include_active=True)
            self._dialogs[chat_id] = {"flow": "delete", "step": "name", "data": {},
                                      "user_id": user_id}
            self.say_keyboard(
                chat_id,
                "Choose the app to delete, or enter its name. You will be asked to confirm.",
                self._choice_keyboard(choices),
            )
        elif label == SELECT_REPOSITORY:
            if self._job_blocks_dialog(chat_id):
                return
            self._dialogs[chat_id] = {"flow": "repo", "step": "repo", "data": {},
                                      "user_id": user_id}
            self.say_keyboard(chat_id, "Enter `name` or `owner/name` to select a repository.", CANCEL_KEYBOARD)
        elif label == SET_MODEL:
            self._dialogs[chat_id] = {"flow": "model", "step": "model", "data": {},
                                      "user_id": user_id}
            self.say_keyboard(chat_id, f"Choose a model. Current model: {self.state.model}", MODEL_KEYBOARD)
        elif label == ADD_ROUTE:
            if self._cloudflare_unavailable(chat_id) or self._job_blocks_dialog(chat_id):
                return
            self._dialogs[chat_id] = {"flow": "route-add", "step": "name", "data": {},
                                      "user_id": user_id}
            self.say_keyboard(chat_id, "Enter the lowercase subdomain for the route.", CANCEL_KEYBOARD)
        elif label == DELETE_ROUTE:
            if self._cloudflare_unavailable(chat_id) or self._job_blocks_dialog(chat_id):
                return
            self._dialogs[chat_id] = {"flow": "route-delete", "step": "name", "data": {},
                                      "user_id": user_id}
            self.say_keyboard(chat_id, "Enter the lowercase subdomain of the route to delete.", CANCEL_KEYBOARD)

    def _job_blocks_dialog(self, chat_id: int) -> bool:
        if not self._lock.locked():
            return False
        self.say(chat_id, "A job is already running. Wait for it or use Cancel / stop.")
        return True

    def _app_choices(self, user_id=None, include_active: bool = False):
        self._register_running_apps()
        owner = ADMIN_OWNER if self._is_admin(user_id) else telegram_owner(user_id)
        choices = list(self.state.app_owners) if self._is_admin(user_id) else self.state.repos_for_owner(owner)
        active = self.state.get_active_repo(user_id, self._is_admin(user_id))
        if include_active and active:
            choices.insert(0, active)
        return list(dict.fromkeys(choices))

    def _app_directories(self):
        return sorted(
            (path for path in self.cfg.repos_dir.iterdir()
             if path.is_dir() and not self.deployer.is_bot_repo(path)),
            key=lambda path: path.name.lower(),
        )

    def _tracked_repo_for_app(self, app: str):
        matches = [repo for repo in self.state.app_owners
                   if repo.rsplit("/", 1)[-1].lower() == app.lower()]
        return matches[0] if len(matches) == 1 else None

    def _can_manage_app(self, user_id, app: str) -> bool:
        if self._is_admin(user_id):
            return True
        repo = self._tracked_repo_for_app(app)
        return bool(repo and self.state.app_owner(repo) == telegram_owner(user_id))

    def _manageable_app_names(self, user_id=None):
        try:
            folders = self._app_directories()
        except OSError:
            return []
        return [folder.name for folder in folders if self._can_manage_app(user_id, folder.name)]

    def _register_running_apps(self):
        running = self.deployer.running_apps()
        changed = False
        for app, _ in running:
            if not self._tracked_repo_for_app(app):
                self.state.set_app_owner(f"{self._owner_default()}/{app}", ADMIN_OWNER)
                changed = True
        if changed:
            self.state.save()
        return running

    @staticmethod
    def _choice_keyboard(choices, extra_rows=None):
        rows = [choices[index:index + 2] for index in range(0, len(choices), 2)]
        rows.extend(extra_rows or [])
        rows.extend([[CANCEL_ACTION], [BACK_TO_MENU]])
        return _keyboard(rows)

    def _handle_dialog(self, chat_id: int, text: str, user_id=None):
        dialog = self._dialogs[chat_id]
        if dialog.get("user_id") != user_id:
            self._dialogs.pop(chat_id, None)
            self.say(chat_id, "That action expired. Open the menu and try again.")
            return
        flow, step = dialog["flow"], dialog["step"]
        if step == "loading-models":
            self.say(chat_id, "Still checking the available models. Wait for the list or use Cancel / stop.")
            return
        if flow == "create":
            self._handle_create_dialog(chat_id, dialog, text)
        elif flow == "fix":
            self._handle_fix_dialog(chat_id, dialog, text, user_id)
        elif flow == "delete":
            self._handle_delete_dialog(chat_id, dialog, text)
        elif flow == "repo":
            self._handle_repo_dialog(chat_id, text, user_id)
        elif flow == "model":
            self._handle_model_dialog(chat_id, dialog, text)
        elif flow == "app-control":
            action = dialog["step"]
            self._dialogs.pop(chat_id, None)
            self.cmd_app(chat_id, f"{action} {text.strip()}", user_id)
        elif flow == "route-add":
            self._handle_route_add_dialog(chat_id, dialog, text)
        elif flow == "route-delete":
            self._handle_route_delete_dialog(chat_id, dialog, text)
        else:
            self.show_menu(chat_id, "That action expired. Choose another action.", user_id)

    def _handle_create_dialog(self, chat_id: int, dialog: dict, text: str):
        step, data = dialog["step"], dialog["data"]
        if step == "name":
            try:
                _, name = parse_repo_ref(text, "owner")
            except RepoError as error:
                self.say(chat_id, str(error))
                return
            if not self._valid_route(name):
                self.say(chat_id, "App names must be lowercase DNS labels containing only letters, digits and hyphens.")
                return
            data["ref"] = text.strip()
            self._begin_model_selection(chat_id, dialog)
            return
        if step in ("model", "custom-model"):
            if self._handle_model_choice(chat_id, dialog, text):
                self._begin_template_selection(chat_id, dialog)
            return
        if step == "template":
            if text not in data["templates"]:
                self.say(chat_id, "Choose one of the templates loaded from the tunnel routes.")
                return
            data["template"] = text
            if self._is_admin(dialog.get("user_id")):
                dialog["step"] = "visibility"
                self.say_keyboard(chat_id, "Choose the GitHub repository visibility. Admin apps are private "
                                           "by default.", VISIBILITY_KEYBOARD)
                return
            data["private"] = False
            self._ask_create_prompt(chat_id, dialog)
            return
        if step == "visibility":
            if text not in (PRIVATE_REPO, PUBLIC_REPO):
                self.say_keyboard(chat_id, "Choose a private or public repository.", VISIBILITY_KEYBOARD)
                return
            data["private"] = text == PRIVATE_REPO
            self._ask_chat_mode(chat_id, dialog)
            return
        if step == "chat-mode":
            if self._handle_chat_mode(chat_id, dialog, text):
                self._ask_create_prompt(chat_id, dialog)
            return
        if step == "prompt":
            request = text.strip()
            if not request:
                self.say(chat_id, "Enter a nonempty app description.")
                return
            self._dialogs.pop(chat_id, None)
            started = self._spawn(
                chat_id,
                lambda: self._new_app(chat_id, data["ref"], request, data["private"],
                                      data["model"], data["template"], dialog.get("user_id"),
                                      data.get("temporary_chat")),
                after=lambda: self.show_menu(chat_id, "App creation finished. Choose the next action.",
                                             dialog.get("user_id")),
                user_id=dialog.get("user_id"),
            )
            if not started:
                self.show_menu(chat_id, "App creation could not start because another job is running.",
                               dialog.get("user_id"))

    def _ask_create_prompt(self, chat_id: int, dialog: dict):
        dialog["step"] = "prompt"
        self.say_keyboard(
            chat_id,
            "Describe the app to create. The template's styling, architecture and stack are used by default; "
            "include any changes you want in this prompt.",
            CANCEL_KEYBOARD,
        )

    def _ask_chat_mode(self, chat_id: int, dialog: dict):
        """Let an administrator choose whether the Copilot chat is kept in the Copilot UI."""
        dialog["step"] = "chat-mode"
        default = TEMPORARY_CHAT if self.cfg.temporary_chat else REGULAR_CHAT
        self.say_keyboard(chat_id, "Choose the Copilot chat type. A temporary chat is not saved in Copilot; "
                                   f"a regular chat stays in your Copilot chat history. Default: {default}.",
                          CHAT_MODE_KEYBOARD)

    def _handle_chat_mode(self, chat_id: int, dialog: dict, text: str) -> bool:
        """Store the chosen chat type; returns False while the dialog still needs a valid choice."""
        if text not in (TEMPORARY_CHAT, REGULAR_CHAT):
            self.say_keyboard(chat_id, "Choose a temporary or regular chat.", CHAT_MODE_KEYBOARD)
            return False
        dialog["data"]["temporary_chat"] = text == TEMPORARY_CHAT
        return True

    def _ask_fix_prompt(self, chat_id: int, dialog: dict):
        dialog["step"] = "prompt"
        self.say_keyboard(chat_id, "Describe what should be fixed or changed in this app.", CANCEL_KEYBOARD)

    def _begin_model_selection(self, chat_id: int, dialog: dict):
        """Check Copilot's Claude and GPT submenus before offering a model for this dialog."""
        dialog["step"] = "loading-models"
        self.say_keyboard(chat_id, "Checking the Claude and GPT models currently available in Copilot...",
                          CANCEL_KEYBOARD)
        started = self._spawn(chat_id, lambda: self._load_model_choices(chat_id, dialog),
                              user_id=dialog.get("user_id"))
        if not started:
            self._dialogs.pop(chat_id, None)

    def _load_model_choices(self, chat_id: int, dialog: dict):
        try:
            models = self.roundtrip.list_models(lambda stage, message: None, self._cancel)
            error = "" if models else "the model picker listed no Claude or GPT models"
        except RoundTripError as exc:
            models, error = [], str(exc)
        if self._dialogs.get(chat_id) is not dialog:
            return
        data = dialog["data"]
        purpose = "create the app" if dialog["flow"] == "create" else "fix the app"
        dialog["step"] = "model"
        if models:
            data["models"], data["allow_custom"] = models, False
            self.say_keyboard(chat_id, f"Choose the model that will {purpose}. These models are available in "
                                       "Copilot now.", self._choice_keyboard(models))
            return
        # Without a readable picker, the default list and free-form entry keep the flow usable.
        data["models"], data["allow_custom"] = list(KNOWN_MODELS), True
        self.say_keyboard(chat_id, f"Could not check the available models: {error}\n"
                                   f"Choose a default model that will {purpose}, or enter another one.",
                          MODEL_KEYBOARD)

    def _handle_model_choice(self, chat_id: int, dialog: dict, text: str) -> bool:
        """Store the chosen model; returns False while the dialog still needs a valid choice."""
        data = dialog["data"]
        if dialog["step"] == "custom-model":
            if not self._valid_model(text):
                self.say(chat_id, "Use a provider/model pair such as GPT/GPT-6.")
                return False
            data["model"] = text.strip()
            return True
        if text == CUSTOM_MODEL and data.get("allow_custom"):
            dialog["step"] = "custom-model"
            self.say_keyboard(chat_id, "Enter a provider/model value, such as `Provider/Model name`.",
                              CANCEL_KEYBOARD)
            return False
        if text not in data.get("models", ()):
            keyboard = MODEL_KEYBOARD if data.get("allow_custom") else self._choice_keyboard(data["models"])
            self.say_keyboard(chat_id, "Choose one of the listed models.", keyboard)
            return False
        data["model"] = text
        return True

    def _begin_template_selection(self, chat_id: int, dialog: dict):
        dialog["step"] = "loading-templates"
        self.say_keyboard(chat_id, "Loading template apps from the Cloudflare tunnel routes...", CANCEL_KEYBOARD)
        started = self._spawn(chat_id, lambda: self._load_template_choices(chat_id, dialog),
                              user_id=dialog.get("user_id"))
        if not started:
            self._dialogs.pop(chat_id, None)

    def _load_template_choices(self, chat_id: int, dialog: dict):
        try:
            route_apps = CloudflareRoutes(self.cfg).app_routes()
        except CloudflareError as error:
            self.show_menu(chat_id, f"Could not load template routes: {error}", dialog.get("user_id"))
            return
        templates = list(dict.fromkeys(
            route["app"] for route in route_apps
            if (self.cfg.repos_dir / route["app"]).is_dir()
        ))
        if not templates:
            self.show_menu(
                chat_id,
                "No template is available. A template needs both a local app checkout and a local HTTP route "
                "on the configured Cloudflare tunnel.",
                dialog.get("user_id"),
            )
            return
        if self._dialogs.get(chat_id) is not dialog:
            return
        dialog["data"]["templates"] = templates
        dialog["step"] = "template"
        self.say_keyboard(
            chat_id,
            "Choose a currently published app as the template. Its styling, architecture and stack are copied "
            "by default, and you can request changes in the next step.",
            self._choice_keyboard(templates),
        )

    def _handle_fix_dialog(self, chat_id: int, dialog: dict, text: str, user_id=None):
        if dialog["step"] == "repo":
            try:
                owner, name = parse_repo_ref(text, self._owner_default())
            except RepoError as error:
                self.say(chat_id, str(error))
                return
            full = f"{owner}/{name}"
            if not self._can_fix(user_id, full):
                self.say(chat_id, "You can only fix apps you created.")
                return
            dialog["data"]["repo"] = full
            self._begin_model_selection(chat_id, dialog)
            return
        if dialog["step"] in ("model", "custom-model"):
            if self._handle_model_choice(chat_id, dialog, text):
                if self._is_admin(user_id):
                    self._ask_chat_mode(chat_id, dialog)
                else:
                    self._ask_fix_prompt(chat_id, dialog)
            return
        if dialog["step"] == "chat-mode":
            if self._handle_chat_mode(chat_id, dialog, text):
                self._ask_fix_prompt(chat_id, dialog)
            return
        if dialog["step"] != "prompt":
            return
        request = text.strip()
        if not request:
            self.say(chat_id, "Enter a nonempty fix request.")
            return
        repo, model = dialog["data"]["repo"], dialog["data"]["model"]
        temporary_chat = dialog["data"].get("temporary_chat")
        self._dialogs.pop(chat_id, None)
        started = self._spawn(chat_id, lambda: self._request(chat_id, repo, request, user_id=user_id,
                                                             model=model, temporary_chat=temporary_chat),
                              after=lambda: self.show_menu(chat_id, "Fix finished. Choose the next action.",
                                                           user_id),
                              user_id=user_id)
        if not started:
            self.show_menu(chat_id, "The fix could not start because another job is running.", user_id)

    def _handle_delete_dialog(self, chat_id: int, dialog: dict, text: str):
        if dialog["step"] == "name":
            try:
                _, name = parse_repo_ref(text, "owner")
            except RepoError as error:
                self.say(chat_id, str(error))
                return
            if not self._valid_route(name):
                self.say(chat_id, "App names must be lowercase DNS labels containing only letters, digits and hyphens.")
                return
            dialog["data"]["ref"] = text.strip()
            dialog["step"] = "confirm"
            self.say_keyboard(
                chat_id,
                f"Delete {text.strip()} from GitHub, the local checkout, deployment startup and tunnel routes?",
                _keyboard([[CONFIRM_DELETE], [KEEP_APP], [CANCEL_ACTION]]),
            )
            return
        if text in (KEEP_APP, CANCEL_ACTION):
            self.show_menu(chat_id, "Deletion cancelled.")
            return
        if text != CONFIRM_DELETE:
            self.say(chat_id, "Choose Yes, delete it or No, keep it.")
            return
        ref = dialog["data"]["ref"]
        self._dialogs.pop(chat_id, None)
        started = self._spawn(
            chat_id,
            lambda: self._delete_app(chat_id, ref),
            after=lambda: self.show_menu(chat_id, "Deletion attempt finished. Choose the next action."),
        )
        if not started:
            self.show_menu(chat_id, "Deletion could not start because another job is running.")

    def _handle_repo_dialog(self, chat_id: int, text: str, user_id=None):
        try:
            parse_repo_ref(text, "owner")
        except RepoError as error:
            self.say(chat_id, str(error))
            return
        self._dialogs.pop(chat_id, None)
        started = self._spawn(chat_id, lambda: self._select_repo(chat_id, text, user_id=user_id),
                              after=lambda: self.show_menu(chat_id, "Repository selection finished.", user_id),
                              user_id=user_id)
        if not started:
            self.show_menu(chat_id, "Repository selection could not start because another job is running.",
                           user_id)

    def _handle_model_dialog(self, chat_id: int, dialog: dict, text: str):
        if dialog["step"] == "model" and text == CUSTOM_MODEL:
            dialog["step"] = "custom-model"
            self.say_keyboard(chat_id, "Enter a provider/model value.", CANCEL_KEYBOARD)
            return
        if dialog["step"] == "model" and text not in KNOWN_MODELS:
            self.say_keyboard(chat_id, "Choose a listed model or select Enter another model.", MODEL_KEYBOARD)
            return
        if not self._valid_model(text):
            self.say(chat_id, "Use a provider/model pair such as GPT/GPT-6.")
            return
        self.state.model = text.strip()
        self.state.save()
        self.show_menu(chat_id, f"Model set to {self.state.model}.")

    def _handle_route_add_dialog(self, chat_id: int, dialog: dict, text: str):
        if dialog["step"] == "name":
            if not self._valid_route(text):
                self.say(chat_id, "Subdomain must be a lowercase DNS label (letters, digits, hyphens).")
                return
            dialog["data"]["name"] = text
            dialog["step"] = "port"
            self.say_keyboard(chat_id, "Enter the local HTTP port (1-65535).", CANCEL_KEYBOARD)
            return
        if not text.isascii() or not text.isdecimal() or not 1 <= int(text) <= 65535:
            self.say(chat_id, "Port must be an integer from 1 to 65535.")
            return
        name, port = dialog["data"]["name"], int(text)
        self._dialogs.pop(chat_id, None)
        started = self._spawn(
            chat_id,
            lambda: self.say(chat_id, CloudflareRoutes(self.cfg).prepare(name, port)),
            after=lambda: self.show_menu(chat_id, "Route update finished. Choose the next action."),
        )
        if not started:
            self.show_menu(chat_id, "The route update could not start because another job is running.")

    def _handle_route_delete_dialog(self, chat_id: int, dialog: dict, text: str):
        if dialog["step"] == "name":
            if not self._valid_route(text):
                self.say(chat_id, "Subdomain must be a lowercase DNS label (letters, digits, hyphens).")
                return
            dialog["data"]["name"] = text
            dialog["step"] = "confirm"
            self.say_keyboard(chat_id, f"Delete the tunnel route for {text}?",
                              _keyboard([[CONFIRM_DELETE], [KEEP_APP], [CANCEL_ACTION]]))
            return
        if text in (KEEP_APP, CANCEL_ACTION):
            self.show_menu(chat_id, "Route deletion cancelled.")
            return
        if text != CONFIRM_DELETE:
            self.say(chat_id, "Choose Yes, delete it or No, keep it.")
            return
        name = dialog["data"]["name"]
        self._dialogs.pop(chat_id, None)
        started = self._spawn(
            chat_id,
            lambda: self.say(chat_id, CloudflareRoutes(self.cfg).delete_route(name)),
            after=lambda: self.show_menu(chat_id, "Route deletion finished. Choose the next action."),
        )
        if not started:
            self.show_menu(chat_id, "Route deletion could not start because another job is running.")

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

    def _can_fix(self, user_id, repo: str) -> bool:
        return self._is_admin(user_id) or self.state.app_owner(repo) == telegram_owner(user_id)

    def cmd_model(self, chat_id: int, arg: str):
        if not arg:
            self.say(chat_id, f"Model: {self.state.model}")
        elif not self._valid_model(arg):
            self.say(chat_id, "Use a provider/model pair such as GPT/GPT-6.")
        else:
            self.state.model = arg
            self.state.save()
            self.say(chat_id, f"Model set to {arg}.")

    def cmd_status(self, chat_id: int, user_id=None):
        active = self.state.get_active_repo(user_id, self._is_admin(user_id))
        lines = [f"Repository: {active or 'none selected'}", f"Model: {self.state.model}",
                 f"Job: {'running' if self._lock.locked() else 'idle'}"]
        if active:
            name = active.split("/", 1)[1]
            try:
                status = self.deployer.status(name, local_path(self.cfg.repos_dir, name))
            except DeployError as error:
                status = f"unavailable ({error})"
            lines.append(f"App: {status}")
        self.say(chat_id, "\n".join(lines))

    def cmd_app(self, chat_id: int, arg: str, user_id=None):
        parts = arg.split()
        if len(parts) != 2 or parts[0].lower() not in ("start", "stop"):
            self.say(chat_id, "Usage: /app start|stop <name>")
            return
        action, name = parts[0].lower(), parts[1]
        if not self._valid_route(name):
            self.say(chat_id, "App names must be lowercase DNS labels containing only letters, digits and hyphens.")
            return
        admin = self._is_admin(user_id)
        folder = self.cfg.repos_dir / name
        if not folder.is_dir() or self.deployer.is_bot_repo(folder):
            self.say(chat_id, f"App folder not found: {folder}" if admin else f"App not found: {name}")
            return
        if not self._can_manage_app(user_id, name):
            self.say(chat_id, "You can only start or stop apps you created.")
            return

        # Regular users see the app name and URL instead of systemd service details.
        def control():
            if action == "start":
                result = self.deployer.start(name, folder)
                if admin:
                    message = result.message
                elif result.ok:
                    message = f"{name} is running: {self._app_url(name)}"
                else:
                    message = f"{name} could not be started. Ask an admin to check it."
                self.say(chat_id, ("✅ " if result.ok else "⚠️ ") + message)
            else:
                stopped = self.deployer.stop(name, folder)
                if admin:
                    message = (f"Systemd service {self.deployer.service_name(name)} is stopped."
                               if stopped else f"Systemd service {self.deployer.service_name(name)} was already "
                                               "stopped or is not registered.")
                else:
                    message = f"{name} is stopped." if stopped else f"{name} was already stopped."
                self.say(chat_id, "✅ " + message)

        self._spawn(chat_id, control, user_id=user_id)

    def cmd_register(self, chat_id: int, arg: str, user_id=None):
        target = arg.strip() or "all"
        if target != "all" and (len(target.split()) != 1 or not self._valid_route(target)):
            self.say(chat_id, "Usage: /register [all|<name>]")
            return

        def register():
            try:
                folders = self._app_directories()
            except OSError as error:
                self.say(chat_id, f"Could not read the apps folder: {error}")
                return
            if target != "all":
                folders = [folder for folder in folders if folder.name == target]
            if not folders:
                self.say(chat_id, "No matching apps are available.")
                return
            lines = ["Systemd app service registration:"]
            changed = False
            for folder in folders:
                try:
                    lines.append(f"- {folder.name}: {self.deployer.ensure_service(folder.name, folder)}")
                    if not self._tracked_repo_for_app(folder.name):
                        self.state.set_app_owner(f"{self._owner_default()}/{folder.name}", ADMIN_OWNER)
                        changed = True
                except (DeployError, OSError) as error:
                    lines.append(f"- {folder.name}: failed: {error}")
            if changed:
                self.state.save()
            self.say(chat_id, "\n".join(lines))

        self._spawn(chat_id, register, user_id=user_id)

    def cmd_list(self, chat_id: int, arg: str, user_id=None):
        if arg:
            self.say(chat_id, "Usage: /list")
            return
        # Build the list off the polling thread so Telegram confirms the update before the slow
        # systemd and Cloudflare lookups; an unconfirmed update can be delivered again.
        worker = threading.Thread(target=self._send_app_list, args=(chat_id, user_id),
                                  name="list", daemon=True)
        self._list_worker = worker
        worker.start()

    def _send_app_list(self, chat_id: int, user_id=None):
        try:
            self._app_list(chat_id, user_id)
        except Exception as e:
            log.exception("app list failed")
            self.say(chat_id, f"\u274c Internal error: {e.__class__.__name__}. See the bot log.")

    def _app_list(self, chat_id: int, user_id=None):
        try:
            apps = self._app_directories()
        except OSError as error:
            self.say(chat_id, f"Could not read the apps folder: {error}")
            return
        if not apps:
            self.say(chat_id, "No apps are available.")
            return

        routes = {}
        routes_available = self.cfg.cloudflare_enabled
        if routes_available:
            try:
                for route in CloudflareRoutes(self.cfg).app_routes():
                    routes.setdefault(route["app"].lower(), route)
            except CloudflareError:
                routes_available = False

        lines = ["Apps:"]
        for folder in apps:
            name = folder.name
            try:
                status = self.deployer.status(name, folder)
            except DeployError as error:
                status = f"unavailable ({error})" if self._is_admin(user_id) else "unavailable"
            route = routes.get(name.lower())
            if route:
                port = route["service"].rsplit(":", 1)[-1].rstrip("/")
                url = f"https://{route['hostname']}"
            else:
                port = url = "not configured" if routes_available else "unavailable"
            if self._is_admin(user_id):
                lines.append(
                    f"- name: {name} — status: {status} — folder: {folder} — port: {port} — url: {url}"
                )
            else:
                lines.append(f"- name: {name} — status: {self._public_status(status)} — url: {url}")
        self.say(chat_id, "\n".join(lines))

    def cmd_cancel(self, chat_id: int, user_id=None):
        if chat_id in self._dialogs:
            if self._dialogs[chat_id].get("step") == "loading-models" and self._job_user_id == user_id:
                self._cancel.set()
            self.show_menu(chat_id, "Action cancelled.", user_id)
            return
        if self._lock.locked():
            if not self._is_admin(user_id) and self._job_user_id != user_id:
                self.say(chat_id, "You can only cancel your own running job.")
                return
            self._cancel.set()
            self.say(chat_id, "Cancelling; Copilot is asked to stop.")
        else:
            self.say(chat_id, "No job is running.")

    def cmd_fix(self, chat_id: int, arg: str, user_id=None):
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
            self.start_request(chat_id, "", " ".join(words), show_browser, temporary_chat, user_id)
            return
        ref, _, request = arg.partition(" ")
        if not ref or not request.strip():
            self.say(chat_id, "Usage: /fix <repo> <request>")
        else:
            self.start_request(chat_id, ref, request.strip(), user_id=user_id)

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
        """Read leading visibility, chat type, name, model, template and prompt options in any order.

        Visibility is None when neither public nor private is given; the chat type is None when neither
        tempchat nor notempchat is given.
        """
        remaining = arg.lstrip()
        private, temporary_chat, model, template, name, prompt = None, None, None, None, None, None
        while remaining:
            parts = remaining.split(None, 1)
            option = parts[0].lower()
            if option in ("public", "private"):
                private = option == "private"
                remaining = parts[1].lstrip() if len(parts) > 1 else ""
                continue
            if option in ("tempchat", "notempchat"):
                temporary_chat = option == "tempchat"
                remaining = parts[1].lstrip() if len(parts) > 1 else ""
                continue
            match = re.match(
                r'''name=(?:"([^"]*)"|'([^']*)'|“([^”]*)”|‘([^’]*)’|([^\s'"“”‘’]+))(?=\s|$)''',
                remaining, flags=re.IGNORECASE)
            if match:
                if name is not None:
                    return remaining, private, None, None, None, None, None, "Specify name only once."
                name = next(value for value in match.groups() if value is not None).strip()
                if not name or len(name.split()) != 1:
                    return remaining, private, None, None, None, None, None, 'Use name="<name|owner/name>" without spaces.'
                remaining = remaining[match.end():].lstrip()
                continue
            if option.startswith("name="):
                return remaining, private, None, None, None, None, None, 'Use name="<name|owner/name>".'
            match = re.match(
                r'''model=(?:"([^"]*)"|'([^']*)'|“([^”]*)”|‘([^’]*)’|([^\s'"“”‘’]+))(?=\s|$)''',
                remaining, flags=re.IGNORECASE)
            if match:
                if model is not None:
                    return remaining, private, None, None, None, None, None, "Specify model only once."
                model = next(value for value in match.groups() if value is not None).strip()
                remaining = remaining[match.end():].lstrip()
                continue
            if option.startswith("model="):
                return remaining, private, None, None, None, None, None, (
                    'Use model=<provider/model>; quote names containing spaces, such as '
                    'model="Claude/Opus 5.5".'
                )
            match = re.match(
                r'''template=(?:"([^"]*)"|'([^']*)'|“([^”]*)”|‘([^’]*)’|([^\s'"“”‘’]+))(?=\s|$)''',
                remaining, flags=re.IGNORECASE)
            if match:
                if template is not None:
                    return remaining, private, None, None, None, None, None, "Specify template only once."
                template = next(value for value in match.groups() if value is not None).strip()
                remaining = remaining[match.end():].lstrip()
                continue
            if option.startswith("template="):
                return remaining, private, None, None, None, None, None, "Use template=<app>, such as template=eurofish."
            match = re.match(
                r'''prompt=(?:"([^"]*)"|'([^']*)'|“([^”]*)”|‘([^’]*)’|([^\s'"“”‘’]+))(?=\s|$)''',
                remaining, flags=re.IGNORECASE)
            if match:
                if prompt is not None:
                    return remaining, private, None, None, None, None, None, "Specify prompt only once."
                prompt = next(value for value in match.groups() if value is not None).strip()
                if not prompt:
                    return remaining, private, None, None, None, None, None, 'Use prompt="<request>" with a nonempty request.'
                remaining = remaining[match.end():].lstrip()
                if match.group(5) is not None and remaining:
                    if re.search(r"(?:^|\s)prompt=", remaining, flags=re.IGNORECASE):
                        return remaining, private, None, None, None, None, None, "Specify prompt only once."
                    prompt = f"{prompt} {remaining}"
                    remaining = ""
                continue
            if option.startswith("prompt="):
                return remaining, private, None, None, None, None, None, 'Use prompt="<request>" with a nonempty request.'
            break
        return remaining, private, temporary_chat, model, template, name, prompt, ""

    def cmd_new(self, chat_id: int, arg: str, user_id=None):
        arg, private, temporary_chat, model, template, name, prompt, error = self._new_options(arg)
        if error:
            self.say(chat_id, error)
            return
        if temporary_chat is not None and not self._is_admin(user_id):
            self.say(chat_id, "Only kurwalab.fyi admins can choose the Copilot chat type; omit tempchat and notempchat.")
            return
        if model is not None and not self._valid_model(model):
            self.say(chat_id, "Use a provider/model pair such as GPT/GPT-6.")
            return
        # Admin apps are private unless public is requested; regular-user apps are always public.
        if self._is_admin(user_id):
            private = private is not False
        elif private:
            self.say(chat_id, "Apps created by regular users use public repositories; omit private.")
            return
        else:
            private = False
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
            chat_id, ref, request, private, model, template or NEW_REPO_TEMPLATE, user_id, temporary_chat),
            user_id=user_id)

    def cmd_delete(self, chat_id: int, arg: str):
        if len(arg.split()) != 1:
            self.say(chat_id, "Usage: /delete <name|owner/name>")
            return
        self._spawn(chat_id, lambda: self._delete_app(chat_id, arg))

    def cmd_repo(self, chat_id: int, arg: str, user_id=None):
        arg, private = self._creation_options(arg)
        if not arg:
            active = self.state.get_active_repo(user_id, self._is_admin(user_id))
            self.say(chat_id, f"Active repository: {active}" if active
                     else "No repository selected. Use /repo <name>.")
            return
        self._spawn(chat_id, lambda: self._select_repo(chat_id, arg, private=private, user_id=user_id),
                    user_id=user_id)

    def start_request(self, chat_id: int, ref: str, request: str,
                      show_browser=None, temporary_chat=None, user_id=None):
        active = self.state.get_active_repo(user_id, self._is_admin(user_id))
        if not ref and not active:
            if self._is_admin(user_id):
                self.say(chat_id, "Select a repository first with /repo <name>.")
            else:
                self.say(chat_id, "Create an app or choose Fix an app first.")
            return
        selected = ref or active
        try:
            owner, name = parse_repo_ref(selected, self._owner_default())
        except RepoError as error:
            self.say(chat_id, str(error))
            return
        full = f"{owner}/{name}"
        if not self._can_fix(user_id, full):
            self.say(chat_id, "You can only fix apps you created.")
            return
        self._spawn(chat_id, lambda: self._request(chat_id, full, request,
                                                   show_browser, temporary_chat, user_id),
                    user_id=user_id)

    # ---- Job execution -------------------------------------------------

    def _spawn(self, chat_id: int, job, after=None, user_id=None):
        if not self._lock.acquire(blocking=False):
            self.say(chat_id, "A job is already running. Wait for it or send /cancel.")
            return False
        self._cancel.clear()
        self._job_user_id = user_id

        def run():
            try:
                job()
            except Cancelled:
                self.say(chat_id, "Cancelled.")
            except _BOT_ERRORS as e:
                self.say(chat_id, f"\u274c {e if self._is_admin(user_id) else self._user_error(e)}")
            except Exception as e:
                log.exception("job failed")
                self.say(chat_id, f"\u274c Internal error: {e.__class__.__name__}. See the bot log.")
            finally:
                if after is not None:
                    try:
                        after()
                    except Exception:
                        log.exception("post-job UI update failed")
                self._job_user_id = None
                self._lock.release()

        self._worker = threading.Thread(target=run, name="job", daemon=True)
        self._worker.start()
        return True

    def _user_error(self, error) -> str:
        """Describe a job failure to a regular user without service, tunnel or server-path details."""
        if isinstance(error, DeployError):
            return "The app service could not be managed. Ask an admin to check it."
        if isinstance(error, CloudflareError):
            return "The app address could not be configured. Ask an admin to check it."
        text = str(error)
        for folder in (str(self.cfg.repos_dir), str(self.cfg.state_dir)):
            text = text.replace(folder + "/", "").replace(folder, "the server")
        return text

    def _app_url(self, name: str) -> str:
        return f"https://{name}.{self.cfg.cloudflare_domain}"

    @staticmethod
    def _public_status(status: str) -> str:
        """Return an app status without its process id."""
        return re.sub(r"\s*\(pid \d+\)", "", status)

    def _owner_default(self) -> str:
        return self.cfg.github_owner or self.gh.login

    def _prepare(self, owner: str, name: str, progress: Progress, new_only: bool = False, private: bool = True,
                 template_name: str = NEW_REPO_TEMPLATE, managed_owner: str = "", admin: bool = True):
        """Create a repository from a local app template, or update an existing checkout.

        Progress for regular users names the template and repository instead of server paths.
        """
        progress.update(f"Looking up {owner}/{name} on GitHub...")
        repo = self.gh.get_repo(owner, name)
        if new_only and repo is not None:
            raise RepoError(f"{owner}/{name} already exists. Use /repo to select it.")
        created = repo is None
        if created:
            progress.update(f"Creating {'private' if private else 'public'} repository {owner}/{name}...")
            repo = self.gh.create_repo(owner, name, private=private)
        if managed_owner:
            self.state.set_app_owner(f"{owner}/{name}", managed_owner, overwrite=created)
            self.state.save()
        dest = local_path(self.cfg.repos_dir, name)
        if created:
            template = self.cfg.repos_dir / template_name
            progress.update(f"Copying {template if admin else 'the ' + template_name + ' template'} "
                            "into the new repository...")
            branch = self.git.seed_from_template(repo["clone_url"], dest, template,
                                                 repo.get("default_branch") or "main")
            return dest, branch, f"created from {template_name}"
        progress.update(f"Preparing {dest if admin else owner + '/' + name}...")
        cloned = self.git.ensure_clone(repo["clone_url"], dest, owner, name)
        branch = self.git.sync(dest)
        return dest, branch, "cloned" if cloned else "already cloned"

    def _select_repo(self, chat_id: int, arg: str, new_only: bool = False, private: bool = True,
                     user_id=None):
        owner, name = parse_repo_ref(arg, self._owner_default())
        progress = Progress(self.tg, chat_id, self.cfg)
        dest, branch, how = self._prepare(owner, name, progress, new_only=new_only, private=private,
                                          managed_owner=ADMIN_OWNER)
        full = f"{owner}/{name}"
        self.state.set_app_owner(full, ADMIN_OWNER)
        self.state.set_active_repo(user_id, full, self._is_admin(user_id))
        self.state.save()
        progress.flush()
        self.say(chat_id, f"\u2705 {owner}/{name} is {how} at {dest} (branch {branch}) and now active."
                 + (" Send your next message describing what to build." if new_only else ""))

    def _new_app(self, chat_id: int, ref: str, request: str, private: bool, model=None,
                  template: str = NEW_REPO_TEMPLATE, user_id=None, temporary_chat=None):
        owner, name = parse_repo_ref(ref, self._owner_default())
        if not self._valid_route(name):
            raise RepoError("App names must be lowercase DNS labels containing only letters, digits and hyphens.")
        if name == template:
            raise RepoError("The new app name must differ from its template app name.")
        full = f"{owner}/{name}"
        progress = Progress(self.tg, chat_id, self.cfg)
        if not (self.cfg.repos_dir / template).is_dir():
            raise RepoError(f"Template app is not available locally: {template}")
        admin = self._is_admin(user_id)
        creator = ADMIN_OWNER if admin else telegram_owner(user_id)
        dest, _, _ = self._prepare(owner, name, progress, new_only=True, private=private,
                                   template_name=template, managed_owner=creator, admin=admin)
        self.state.set_active_repo(user_id, full, admin)
        self.state.save()
        routes = CloudflareRoutes(self.cfg)
        progress.update(f"{full}: checking tunnel routes for the next port..." if admin
                        else f"{full}: preparing the app address...")
        port = routes.next_port()
        prompt = NEW_APP_PROMPT.format(port=port) + request + DEPLOY_NOTE
        if self._cancel.is_set():
            raise Cancelled()
        progress.update(f"{full}: sending the repository to Copilot for port {port}..." if admin
                        else f"{full}: sending the repository to Copilot...")
        try:
            result = self.roundtrip.run(
                dest, prompt, model or self.state.model,
                lambda stage, msg: progress.update(self._repo_progress(full, msg), stage), self._cancel,
                temporary_chat=temporary_chat)
        except Cancelled:
            progress.flush()
            self._warn_dirty(chat_id, dest, full, admin)
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
        progress.update(f"{full}: registering and restarting its systemd service on port {port}..." if admin
                        else f"{full}: starting the app...")
        deploy = self.deployer.redeploy(name, dest)
        if not deploy.ok:
            progress.flush()
            error = ("The app was not routed because deployment failed." if admin
                     else "The app could not be started. Ask an admin to check it.")
            self.say(chat_id, self._new_app_report(full, sha, pushed_branch, result, deploy.message,
                                                   error=error, admin=admin))
            return
        url = self._app_url(name)
        progress.update(f"{full}: creating {url}...")
        try:
            tunnel = routes.prepare(name, port)
        except CloudflareError as error:
            progress.flush()
            error = (f"Tunnel setup failed: {error}" if admin
                     else "The app address could not be published. Ask an admin to check it.")
            self.say(chat_id, self._new_app_report(full, sha, pushed_branch, result, deploy.message,
                                                   error=error, admin=admin))
            return
        progress.flush()
        self.say(chat_id, self._new_app_report(full, sha, pushed_branch, result, deploy.message,
                                               tunnel=tunnel, url=url, admin=admin))

    def _delete_app(self, chat_id: int, ref: str):
        """Attempt every app cleanup independently and report each outcome."""
        explicit_owner = "/" in ref.strip()
        owner, name = parse_repo_ref(ref, "delete-owner")
        if not self._valid_route(name):
            raise RepoError("App names must be lowercase DNS labels containing only letters, digits and hyphens.")
        owner_error = None
        if not explicit_owner:
            try:
                owner = self._owner_default()
            except GitHubError as error:
                owner, owner_error = None, error
        full = f"{owner}/{name}" if owner else name
        dest = local_path(self.cfg.repos_dir, name)
        if self.deployer.is_bot_repo(dest):
            raise RepoError("The bot repository cannot be deleted as an app.")

        failed = False
        try:
            stopped = self.deployer.stop(name, dest)
            process = "Service process: stopped the running app." if stopped else "Service process: no running app was recorded."
        except (DeployError, OSError) as error:
            failed = True
            process = f"Service process: failed to stop: {error}"

        try:
            startup = self.deployer.unregister(name, dest)
        except (DeployError, OSError) as error:
            failed = True
            startup = f"Service: failed to unregister: {error}"

        github_clean = False
        if owner_error is not None:
            failed = True
            github = f"GitHub: could not resolve the owner for {name}: {owner_error}"
        else:
            try:
                deleted = self.gh.delete_repo(owner, name)
                github_clean = True
                github = (f"GitHub: deleted {full}." if deleted
                          else f"GitHub: {full} was not found; nothing to delete.")
            except GitHubError as error:
                failed = True
                github = f"GitHub: failed to delete {full}: {error}"

        local_clean = False
        try:
            if dest.is_symlink():
                raise GitError(f"{dest} is a symbolic link; refusing to remove it.")
            if not dest.exists():
                local_clean = True
                local = f"Local folder: {dest} was not found; nothing to delete."
            else:
                self.git.require_checkout(dest, owner or "", name)
                shutil.rmtree(dest)
                local_clean = True
                local = f"Local folder: deleted {dest}."
        except (GitError, OSError) as error:
            failed = True
            local = f"Local folder: failed to delete {dest}: {error}"

        if not self.cfg.cloudflare_enabled:
            failed = True
            tunnel = "Tunnel route: not attempted because Cloudflare is not configured."
        else:
            try:
                result = CloudflareRoutes(self.cfg).delete_route(name, missing_ok=True)
                tunnel = f"Tunnel route: {result}"
            except CloudflareError as error:
                failed = True
                tunnel = f"Tunnel route: failed to delete: {error}"

        tracked = full if owner else next(
            (repo for repo in self.state.app_owners if repo.lower().endswith(f"/{name.lower()}")), full)
        if github_clean and local_clean:
            try:
                self.state.remove_app(tracked)
                self.state.save()
                active = "Ownership and active repository selections: cleared."
            except OSError as error:
                failed = True
                active = f"Ownership and active repository selections: failed to clear: {error}"
        else:
            active = "Ownership and active repository selections: unchanged."

        heading = (f"⚠️ Deletion attempted for {full}; some cleanup failed or was skipped."
                   if failed else f"✅ Deletion completed for {full}.")
        self.say(chat_id, "\n".join([heading, github, local, tunnel, process, startup, active]))

    def _request(self, chat_id: int, ref: str, request: str,
                 show_browser=None, temporary_chat=None, user_id=None, model=None):
        owner, name = parse_repo_ref(ref, self._owner_default())
        full = f"{owner}/{name}"
        if not self._can_fix(user_id, full):
            raise RepoError("You can only fix apps you created.")
        progress = Progress(self.tg, chat_id, self.cfg)
        admin = self._is_admin(user_id)
        managed_owner = self.state.app_owner(full) or ADMIN_OWNER
        dest, branch, _ = self._prepare(owner, name, progress, managed_owner=managed_owner, admin=admin)
        self.state.set_active_repo(user_id, full, admin)
        self.state.save()
        if self._cancel.is_set():
            raise Cancelled()
        progress.update(f"{full}: sending the repository to Copilot...")
        try:
            result = self.roundtrip.run(dest, request + DEPLOY_NOTE, model or self.state.model,
                                        lambda stage, msg: progress.update(self._repo_progress(full, msg), stage),
                                        self._cancel,
                                        show_browser, temporary_chat)
        except Cancelled:
            progress.flush()
            self._warn_dirty(chat_id, dest, full, admin)
            raise
        progress.update(f"{full}: committing and pushing...")
        pushed = self.git.commit_and_push(dest, request)
        if pushed is None:
            progress.flush()
            self.say(chat_id, f"Copilot returned no changes to commit for {full}; nothing was deployed.")
            return
        sha, pushed_branch = pushed
        progress.update(f"{full}: restarting its systemd service..." if admin else f"{full}: restarting the app...")
        deploy = self.deployer.redeploy(name, dest)
        progress.flush()
        self.say(chat_id, self._report(full, sha, pushed_branch, result, deploy,
                                       url="" if admin else self._app_url(name)))

    def _warn_dirty(self, chat_id: int, dest, full: str, admin: bool):
        if not self.git.is_dirty(dest):
            return
        target = f"review {dest}" if admin else f"ask an admin to review {full}"
        self.say(chat_id, "\u26a0\ufe0f The checkout has uncommitted changes from the interrupted run; "
                          f"{target} before the next request.")

    @staticmethod
    def _repo_progress(full: str, message: str) -> str:
        """Keep the current-activity header first while retaining repository context."""
        first, separator, rest = message.partition("\n")
        if first.startswith("Currently doing: "):
            url = html.escape(f"https://github.com/{full}", quote=False)
            return first + f"\nRepository: {url}" + (separator + rest if separator else "")
        return f"{full}: {message}"

    @staticmethod
    def _report(full: str, sha: str, branch: str, result: dict, deploy, url: str = "") -> str:
        """Summarize a fix; a URL selects the regular-user form without service details."""
        diff_lines = [l for l in (result.get("diff_stat") or "").splitlines() if l.strip()]
        mark = "\u2705" if deploy.ok else "\u26a0\ufe0f"
        lines = [f"{mark} {full}: commit {sha} pushed to {branch}."]
        if diff_lines:
            lines.append(f"Changes: {diff_lines[-1].strip()}")
        if not url:
            lines.append(("Deploy: " if deploy.ok else "Pushed, but the redeploy failed: ") + deploy.message)
        elif deploy.ok:
            lines.append(f"App: {url}")
        else:
            lines.append("Pushed, but the redeploy failed. Ask an admin to check the app.")
        return "\n".join(lines)

    @staticmethod
    def _new_app_report(full: str, sha: str, branch: str, result: dict, deploy_message: str,
                        tunnel: str = "", startup: str = "", url: str = "", error: str = "",
                        admin: bool = True) -> str:
        """Summarize app creation; deployment and tunnel details are shown only to admins."""
        mark = "\u26a0\ufe0f" if error else "\u2705"
        lines = [f"{mark} {full}: commit {sha} pushed to {branch}."]
        if admin:
            lines.append(f"Deploy: {deploy_message}")
        if tunnel and admin:
            lines.append(tunnel)
        if startup and admin:
            lines.append(startup)
        if url:
            lines.append(f"App: {url}")
        if error:
            lines.append(error)
        output = (result.get("chat_response") or "(no final text returned)").strip()
        lines.extend(["", "Copilot output:", output])
        return "\n".join(lines)
