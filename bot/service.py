"""Access requests, authorized-user commands, and the repository request pipeline."""
from __future__ import annotations

import html
import itertools
import logging
import re
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .app_list import AppEntry, app_list_messages
from .diff_view import format_changes, parse_numstat
from .config import Config, redact
from .cloudflare import CloudflareError, CloudflareRoutes
from .deploy import BOT_APP, BOT_SERVICE, DeployError, Deployer
from .github_api import GitHub, GitHubError
from .gitops import Git, GitError
from .repos import RepoError, local_path, parse_repo_ref
from .roundtrip import (Cancelled, RoundTrip, RoundTripError, StatusTimeline,
                        format_stage_status, format_timestamp)
from .state import ADMIN_OWNER, State, telegram_owner
from .telegram_api import DOWNLOAD_LIMIT, Telegram, TelegramError

log = logging.getLogger("bot")

# Keep generated apps compatible with process supervision.
DEPLOY_NOTE = (
    "\n\nThis app is deployed by running `make run` from the repository root. Keep a Makefile whose `run` "
    "target starts one HTTP server in the foreground, and keep secrets and runtime data outside the repository."
)
# The bot's own repository runs as the Telegram bot service instead of an HTTP app.
BOT_DEPLOY_NOTE = (
    "\n\nThis repository is the Telegram bot that manages the apps. It runs with `make run` from the repository "
    "root as a systemd service. Keep that run target, and keep secrets and runtime data outside the repository."
)
NEW_REPO_TEMPLATE = "eurofish"
NEW_APP_PROMPT = (
    "Use this app as the template for a new app running on port {port}. Keep its architecture, stack and "
    "styling by default, but change any of them when the request below asks for it.\n\n"
)
# Files sent to the bot are attached to the next app request in the same chat.
MAX_ATTACHMENTS = 10
# Bounds the pending access-request queue, which any Telegram user can add to.
MAX_ACCESS_REQUESTS = 100
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
USER_VIEW = "👤 Switch to user view"
ADMIN_VIEW = "🛡 Switch to admin view"
REQUEST_ACCESS = "🔑 Request access"
ACCESS_REQUESTS = "📨 Access requests"
APPROVE_ACCESS = "✅ Approve access"
REJECT_ACCESS = "🚫 Reject request"
RUNNING_SESSIONS = "🧵 Running sessions"
STOP_SESSION = "⏹ Stop session"
SHOW_COMMANDS = "/cli"


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
    [REGISTER_SERVICES, ACCESS_REQUESTS],
    [TUNNEL_ROUTES, RUNNING_SESSIONS],
    [CANCEL_ACTION, SHOW_COMMANDS],
    [USER_VIEW],
])
USER_KEYBOARD = _keyboard([
    [CREATE_APP, LIST_APPS],
    [FIX_APP],
    [START_APP, STOP_APP],
    [SHOW_COMMANDS],
])
# Administrators in user view get the regular-user menu plus a way back to admin view.
ADMIN_USER_VIEW_KEYBOARD = _keyboard([
    [CREATE_APP, LIST_APPS],
    [FIX_APP],
    [START_APP, STOP_APP],
    [SHOW_COMMANDS],
    [ADMIN_VIEW],
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
GUEST_KEYBOARD = _keyboard([[REQUEST_ACCESS]])
DECISION_KEYBOARD = _keyboard([[APPROVE_ACCESS, REJECT_ACCESS], [CANCEL_ACTION], [BACK_TO_MENU]])
REMOVE_KEYBOARD = {"remove_keyboard": True}

NEW_USAGE = (
    "Usage: /new [public|private] [tempchat|notempchat] [model=<provider/model>] [template=<app>] "
    'name="<name|owner/name>" prompt="<request>"'
)

ADMIN_FIX_USAGE = (
    "Usage: /fix [model=<provider/model>] [tempchat|notempchat] [browser|nobrowser] "
    'app="<name|owner/name>" prompt="<request>"'
)
USER_FIX_USAGE = 'Usage: /fix [model=<provider/model>] app="<name>" prompt="<request>"'

ATTACH_HELP = (
    f"Send files, screenshots or photos (up to {MAX_ATTACHMENTS}, each at most 20 MB) to attach them to your next "
    "create or fix request; a file caption is used as the request text. Cancel / stop discards attached files."
)

HELP = f"""Use the buttons below to create and manage apps. Every action starts from these buttons or a command; text outside a guided step only shows the menu. Each user runs one session (a create, fix or other job) at a time, in parallel with other users. Use {USER_VIEW} in the main menu to use the bot as a regular user.

{ATTACH_HELP}

/menu - show the app-management menu
/new [public|private] [tempchat|notempchat] [model=<provider/model>] [template=<app>] name="<name|owner/name>" prompt="<request>" - create, build, deploy and publish a new app (private by default)
/delete <name|owner/name> - permanently delete an app service, GitHub repository, local checkout and tunnel route
/repo [public|private] <name|owner/name> - select a repository; a missing one is created from the eurofish reference app
/repo - show the active repository
/fix [model=<provider/model>] [tempchat|notempchat] [browser|nobrowser] app="<name|owner/name>" prompt="<request>" - fix an app with the same choices as Fix an app; linux-copilot-bot changes this bot and restarts it
/list - list every local app and this bot with systemd status and tunnel details
/app start|stop <name> - start or stop an app service
/register [all|<name>] - register and enable systemd services for existing local apps
/cloudflare <subdomain> <port> - route <subdomain>.kurwalab.fyi to http://127.0.0.1:<port>
/cloudflare list - list tunnel routes
/cloudflare delete <subdomain> - delete a tunnel route by name
/model <provider/model> - choose the Copilot model (for example GPT/GPT-6)
/status - show repository, model, session and app state
/requests - show pending access requests and approved users
/approve <user id> - give a Telegram user regular-user access
/reject <user id> - decline a pending access request
/revoke <user id> - remove a regular user's access
/sessions - list every user's running session
/sessions stop <number> - stop a running session
/cancel - stop your running session and discard attached files
/help or /cli - show this message"""
ADMIN_VIEW_HINT = f"You are an admin in user view. Use {ADMIN_VIEW} in the main menu to return to admin view."
USER_HELP = f"""Use the buttons below to create and list apps, and to fix, start, or stop apps you created. You can run one session (a create, fix or other job) at a time, in parallel with other users.

{ATTACH_HELP}

/menu - show the app menu
/new [model=<provider/model>] [template=<app>] name="<name|owner/name>" prompt="<request>" - create, build, deploy and publish a new app in a public repository
/fix [model=<provider/model>] app="<name>" prompt="<request>" - fix an app you created with the same choices as Fix an app
/list - list every local app with its status and public URL
/app start|stop <name> - start or stop one of your apps
/cancel - stop your running session and discard attached files
/help or /cli - show this message"""

ADMIN_ONLY_BUTTONS = {
    DELETE_APP, TUNNEL_ROUTES,
    LIST_ROUTES, ADD_ROUTE, DELETE_ROUTE, REGISTER_SERVICES, ACCESS_REQUESTS, RUNNING_SESSIONS,
}
ADMIN_ONLY_COMMANDS = {"delete", "repo", "cloudflare", "model", "status", "register",
                       "requests", "approve", "reject", "revoke", "sessions"}
ADMIN_ONLY_MESSAGE = "This action is available only to kurwalab.fyi admins."
USER_VIEW_ADMIN_ONLY_MESSAGE = f"This action is available only in admin view. Use {ADMIN_VIEW} in the main menu."
GUEST_WELCOME = (f"This bot creates and manages apps for approved users. Tap {REQUEST_ACCESS} to ask a "
                 "kurwalab.fyi admin for access; you will be notified when it is approved.")
GUEST_PENDING = "Your access request is waiting for admin approval. You will be notified when it is approved."
GUEST_NO_ACCESS = f"You do not have access to this bot yet. Send /start and tap {REQUEST_ACCESS}."
# The welcome text points to the role's menu; each menu shows only the buttons that role can use.
USER_WELCOME = ADMIN_WELCOME = "\U0001f44b Welcome! This bot builds, deploys and runs web apps with Copilot.\n\nUse the buttons below."
NOTHING_STARTED = "That message did not start anything."
RESTART_PENDING_MESSAGE = ("The bot restarts to apply an update as soon as the running sessions finish. "
                           "Try again after the restart.")



class AttachmentError(Exception):
    """Raised when a file sent to the bot cannot be downloaded for a request."""


_BOT_ERRORS = (RepoError, GitError, GitHubError, RoundTripError, CloudflareError, DeployError, AttachmentError)


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


class Session:
    """One running job of a user, such as a Copilot round-trip; each user runs at most one at a time."""

    def __init__(self, number: int, user_id, chat_id: int, label: str, app: str = ""):
        self.number, self.user_id, self.chat_id, self.label = number, user_id, chat_id, label
        # The app folder this session changes; two sessions never work on the same app.
        self.app = app.lower()
        self.started, self.started_at = time.monotonic(), format_timestamp()
        self.cancel = threading.Event()
        # Tunnel port reserved by an app creation until the session ends.
        self.port = None
        self.thread = None

    def elapsed(self) -> str:
        seconds = int(time.monotonic() - self.started)
        hours, rest = divmod(seconds, 3600)
        minutes, seconds = divmod(rest, 60)
        return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"


class Bot:
    def __init__(self, cfg: Config, tg: Telegram, gh: GitHub, git: Git, roundtrip: RoundTrip,
                 deployer: Deployer, state: State):
        self.cfg, self.tg, self.gh, self.git = cfg, tg, gh, git
        self.roundtrip, self.deployer, self.state = roundtrip, deployer, state
        # Running sessions keyed by Telegram user id; guarded by _sessions_lock.
        self._sessions = {}
        self._sessions_lock = threading.Lock()
        self._session_numbers = itertools.count(1)
        self._port_lock = threading.Lock()
        self._local = threading.local()
        self._idle_cancel = threading.Event()
        # Chat to report to while a bot restart waits for the running sessions to finish.
        self._restart_chat = None
        self._restart_pending = False
        self._names = {}
        self._worker = None
        self._list_worker = None
        self._dialogs = {}
        self._pending_files = {}
        self._media_groups = {}

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
            try:
                self.flush_media_groups()
            except Exception:
                log.exception("media group handling failed")
        self.shutdown()

    def shutdown(self):
        sessions = self._running_sessions()
        for session in sessions:
            session.cancel.set()
        deadline = time.monotonic() + 60
        for session in sessions:
            if session.thread is not None:
                session.thread.join(timeout=max(0.0, deadline - time.monotonic()))

    def handle_update(self, update: dict):
        message = update.get("message") or {}
        sender = (message.get("from") or {}).get("id")
        chat = message.get("chat") or {}
        if not isinstance(sender, int) or chat.get("type") != "private":
            log.warning("ignored message from user %s", sender)
            return
        if not self._authorized(sender):
            self._handle_guest(chat["id"], sender, message)
            return
        name = self._display_name(message.get("from") or {})
        if name:
            self._names[sender] = name
        text = (message.get("text") or "").strip()
        files = self._message_files(message)
        if files:
            self._receive_files(chat["id"], sender, files, (message.get("caption") or "").strip(),
                                message.get("media_group_id"))
        elif text:
            self.handle_text(chat["id"], text, sender)
        else:
            self.show_welcome(chat["id"], sender, NOTHING_STARTED)

    # ---- Attachments ---------------------------------------------------

    @staticmethod
    def _message_files(message: dict):
        """Return the files of one Telegram message as {file_id, name, size} entries."""
        def entry(item, name):
            if not isinstance(item, dict) or not item.get("file_id"):
                return None
            return {"file_id": item["file_id"], "name": item.get("file_name") or name,
                    "size": int(item.get("file_size") or 0)}

        photos = [photo for photo in message.get("photo") or [] if isinstance(photo, dict)]
        sticker = message.get("sticker") or {}
        sticker_suffix = ".tgs" if sticker.get("is_animated") else ".webm" if sticker.get("is_video") else ".webp"
        candidates = [
            # Telegram lists photo sizes from smallest to largest.
            entry(photos[-1], "photo.jpg") if photos else None,
            entry(message.get("animation"), "animation.mp4"),
            # An animation is also reported as a document; attach it once.
            None if message.get("animation") else entry(message.get("document"), "file"),
            entry(message.get("video"), "video.mp4"),
            entry(message.get("audio"), "audio.mp3"),
            entry(message.get("voice"), "voice.ogg"),
            entry(message.get("video_note"), "video-note.mp4"),
            entry(sticker, "sticker" + sticker_suffix),
        ]
        return [item for item in candidates if item]

    def _receive_files(self, chat_id: int, user_id, files, caption: str, media_group=None):
        """Queue files for the next request; a caption runs as that request with the queued files."""
        error = self._queue_files(chat_id, files)
        if media_group:
            # Album items arrive as separate messages; the caption is handled after the whole batch.
            group = self._media_groups.setdefault((chat_id, media_group),
                                                  {"user_id": user_id, "caption": "", "failed": False})
            group["caption"] = group["caption"] or caption
            if error:
                group["failed"] = True
                self.say(chat_id, error)
            return
        if error:
            self.say(chat_id, error + (" The caption was not run as a request." if caption else ""))
        elif caption:
            self.handle_text(chat_id, caption, user_id)
        else:
            self._acknowledge_files(chat_id)

    def flush_media_groups(self):
        """Handle the captions of albums received in the last polling batch."""
        groups, self._media_groups = self._media_groups, {}
        for (chat_id, _), group in groups.items():
            if group["failed"]:
                if group["caption"]:
                    self.say(chat_id, "The caption was not run as a request because a file could not be "
                                      "attached. Send the request text to use the attached files.")
            elif group["caption"]:
                self.handle_text(chat_id, group["caption"], group["user_id"])
            else:
                self._acknowledge_files(chat_id)

    def _queue_files(self, chat_id: int, files) -> str:
        """Add files to the chat's pending attachments; returns an error message when refused."""
        pending = self._pending_files.setdefault(chat_id, [])
        for item in files:
            if item["size"] > DOWNLOAD_LIMIT:
                return (f"{item['name']} is larger than 20 MB, the largest file a Telegram bot can download; "
                        "it was not attached.")
            if len(pending) >= MAX_ATTACHMENTS:
                return f"At most {MAX_ATTACHMENTS} files can be attached to one request; {item['name']} was not attached."
            pending.append(item)
        return ""

    def _acknowledge_files(self, chat_id: int):
        pending = self._pending_files.get(chat_id) or []
        if not pending:
            return
        names = ", ".join(item["name"] for item in pending)
        self.say(chat_id, f"\U0001f4ce Attached for the next request ({len(pending)}): {names}\n"
                          "Send the request text to start it, or use Cancel / stop to discard the files.")

    def _discard_files(self, chat_id: int) -> str:
        """Drop the chat's pending attachments and describe what was dropped."""
        pending = self._pending_files.pop(chat_id, None)
        return f" Discarded {len(pending)} attached file(s)." if pending else ""

    def _spawn_request(self, chat_id: int, job, after=None, user_id=None, label: str = "", app: str = "") -> bool:
        """Start a create or fix session with the chat's pending attachments, which it then consumes."""
        files = list(self._pending_files.get(chat_id) or [])
        started = self._spawn(chat_id, lambda: job(files), after=after, user_id=user_id, label=label, app=app)
        if started:
            self._pending_files.pop(chat_id, None)
        return started

    @contextmanager
    def _downloaded(self, files, progress, label: str):
        """Download attachments into a temporary folder that is removed after the request."""
        if not files:
            yield []
            return
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="attachments-", dir=self.cfg.state_dir) as work:
            paths, used = [], set()
            progress.update(f"{label}: downloading {len(files)} attached file(s)...")
            for item in files:
                if self._cancel.is_set():
                    raise Cancelled()
                path = Path(work) / self._attachment_name(item["name"], used)
                try:
                    paths.append(self.tg.download_file(item["file_id"], path))
                except TelegramError as error:
                    raise AttachmentError(f"Could not download the attached file {item['name']}: "
                                          f"{redact(str(error), self.cfg.secrets)}") from None
            yield paths

    @staticmethod
    def _attachment_name(name: str, used: set) -> str:
        """Return a safe, unique file name for an attachment."""
        base = re.sub(r"[^\w.\- ]+", "_", Path(str(name)).name).strip(" .")[:100] or "file"
        stem, dot, suffix = base.rpartition(".")
        stem, suffix = (stem, dot + suffix) if stem else (base, "")
        candidate, index = base, 2
        while candidate.lower() in used:
            candidate, index = f"{stem}-{index}{suffix}", index + 1
        used.add(candidate.lower())
        return candidate

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

    def _is_configured_admin(self, user_id) -> bool:
        return user_id is None or self.cfg.is_admin(user_id)

    def _authorized(self, user_id) -> bool:
        """Configured admins and users approved through an access request may use the bot."""
        return self._is_configured_admin(user_id) or self.state.is_approved(user_id)

    def _is_admin(self, user_id) -> bool:
        """Return whether the sender acts as an admin; admins in user view get regular-user behavior."""
        return self._is_configured_admin(user_id) and not self.state.user_view(user_id)

    def _main_keyboard(self, user_id):
        if self._is_admin(user_id):
            return MAIN_KEYBOARD
        return ADMIN_USER_VIEW_KEYBOARD if self._is_configured_admin(user_id) else USER_KEYBOARD

    def _admin_only(self, chat_id: int, user_id=None):
        self.say(chat_id, USER_VIEW_ADMIN_ONLY_MESSAGE if self._is_configured_admin(user_id)
                 else ADMIN_ONLY_MESSAGE)

    def _switch_view(self, chat_id: int, user_view: bool, user_id=None):
        """Let a configured admin choose between admin and regular-user behavior."""
        if not self._is_configured_admin(user_id):
            self.say(chat_id, ADMIN_ONLY_MESSAGE)
            return
        self.state.set_user_view(user_id, user_view)
        self.state.save()
        text = (f"User view enabled. You now see and use the bot as a regular user. {ADMIN_VIEW_HINT}"
                if user_view else "Admin view enabled. All admin actions and details are available.")
        self.show_menu(chat_id, text, user_id)

    def show_menu(self, chat_id: int, text: str = "What would you like to do?", user_id=None):
        self._dialogs.pop(chat_id, None)
        self.say_keyboard(chat_id, text, self._main_keyboard(user_id))

    def welcome_text(self, user_id=None, lead: str = "") -> str:
        """Return the welcome text; administrators in user view also get the hint for switching back."""
        if self._is_admin(user_id):
            text = ADMIN_WELCOME
        elif self._is_configured_admin(user_id):
            text = f"{USER_WELCOME}\n\n{ADMIN_VIEW_HINT}"
        else:
            text = USER_WELCOME
        return f"{lead}\n\n{text}" if lead else text

    def help_text(self, user_id=None) -> str:
        """Return the commands the sender can use, shown by /help and the /cli button."""
        if self._is_admin(user_id):
            return HELP
        if self._is_configured_admin(user_id):
            return f"{USER_HELP}\n\n{ADMIN_VIEW_HINT}"
        return USER_HELP

    def show_welcome(self, chat_id: int, user_id=None, lead: str = ""):
        self.show_menu(chat_id, self.welcome_text(user_id, lead), user_id)

    # ---- Access requests -----------------------------------------------

    @staticmethod
    def _display_name(sender: dict) -> str:
        """A short label for a Telegram user: full name and @username when present."""
        name = " ".join(str(sender.get(key) or "").strip() for key in ("first_name", "last_name")).strip()
        username = str(sender.get("username") or "").strip()
        label = " ".join(part for part in (name, f"@{username}" if username else "") if part)
        return " ".join(label.split())[:64]

    @staticmethod
    def _person_label(user_id, info: dict) -> str:
        name = (info or {}).get("name") or "unknown name"
        return f"{name} · {user_id}"

    def _handle_guest(self, chat_id: int, user_id: int, message: dict):
        """Serve a user without access: /start offers the request button; everything else is refused."""
        text = (message.get("text") or "").strip()
        command = text.split()[0][1:].split("@")[0].lower() if text.startswith("/") else ""
        if text == REQUEST_ACCESS:
            self._request_access(chat_id, user_id, self._display_name(message.get("from") or {}))
        elif self.state.has_request(user_id):
            self.say_keyboard(chat_id, GUEST_PENDING, GUEST_KEYBOARD)
        elif command in ("start", "menu", "help"):
            self.say_keyboard(chat_id, GUEST_WELCOME, GUEST_KEYBOARD)
        else:
            self.say_keyboard(chat_id, GUEST_NO_ACCESS, GUEST_KEYBOARD)

    def _request_access(self, chat_id: int, user_id: int, name: str):
        if self.state.has_request(user_id):
            self.say_keyboard(chat_id, GUEST_PENDING, GUEST_KEYBOARD)
            return
        if len(self.state.access_requests) >= MAX_ACCESS_REQUESTS:
            self.say_keyboard(chat_id, "The access request queue is full. Try again later.", GUEST_KEYBOARD)
            return
        self.state.add_request(user_id, name)
        self.state.save()
        self.say_keyboard(chat_id, "Your access request was sent to the kurwalab.fyi admins. "
                                   "You will be notified when it is approved.", GUEST_KEYBOARD)
        label = self._person_label(user_id, self.state.access_requests[str(user_id)])
        for admin_id in sorted(self.cfg.admin_ids):
            self.say(admin_id, f"📨 New access request from {label}.\n"
                               f"Review it with {ACCESS_REQUESTS} in admin view, or send /approve {user_id} "
                               f"or /reject {user_id}.")

    def _access_summary(self) -> str:
        requests = sorted(self.state.access_requests.items(), key=lambda item: item[1]["time"])
        approved = sorted(self.state.approved_users.items(), key=lambda item: item[1]["time"])
        lines = [f"Pending access requests ({len(requests)}):"]
        lines += [f"- {self._person_label(user_id, info)}" for user_id, info in requests] or ["- none"]
        lines += ["", f"Approved users ({len(approved)}):"]
        lines += [f"- {self._person_label(user_id, info)}" for user_id, info in approved] or ["- none"]
        return "\n".join(lines)

    def _show_access_requests(self, chat_id: int, user_id=None):
        """Open the request queue as a picker of pending requests."""
        requests = sorted(self.state.access_requests.items(), key=lambda item: item[1]["time"])
        if not requests:
            self.show_menu(chat_id, self._access_summary(), user_id)
            return
        self._dialogs[chat_id] = {"flow": "access", "step": "choose", "data": {}, "user_id": user_id}
        choices = [self._person_label(request_id, info) for request_id, info in requests]
        self.say_keyboard(chat_id, self._access_summary() + "\n\nChoose a request to review.",
                          self._choice_keyboard(choices))

    def _handle_access_dialog(self, chat_id: int, dialog: dict, text: str, user_id=None):
        if dialog["step"] == "choose":
            match = re.search(r"(\d+)\s*$", text)
            target = match.group(1) if match else ""
            if not self.state.has_request(target):
                self.say(chat_id, "Choose one of the listed requests.")
                return
            dialog["step"], dialog["data"]["target"] = "decide", int(target)
            label = self._person_label(target, self.state.access_requests[target])
            self.say_keyboard(chat_id, f"Approve or reject access for {label}?", DECISION_KEYBOARD)
            return
        if text not in (APPROVE_ACCESS, REJECT_ACCESS):
            self.say(chat_id, f"Choose {APPROVE_ACCESS} or {REJECT_ACCESS}.")
            return
        self._dialogs.pop(chat_id, None)
        command = "approve" if text == APPROVE_ACCESS else "reject"
        self.cmd_access(chat_id, command, str(dialog["data"]["target"]), user_id)

    def cmd_requests(self, chat_id: int, arg: str, user_id=None):
        if arg:
            self.say(chat_id, "Usage: /requests")
            return
        self._show_access_requests(chat_id, user_id)

    def cmd_access(self, chat_id: int, action: str, arg: str, user_id=None):
        """Approve or reject an access request, or revoke an approved user's access."""
        if not re.fullmatch(r"[1-9]\d{0,19}", arg):
            self.say(chat_id, f"Usage: /{action} <numeric Telegram user id>")
            return
        target = int(arg)
        if self.cfg.is_admin(target):
            self.show_menu(chat_id, f"{target} is a configured admin; admin access comes from TELEGRAM_ADMIN_IDS.",
                           user_id)
            return
        if action == "approve":
            if self.state.is_approved(target):
                self.show_menu(chat_id, f"{target} already has access.", user_id)
                return
            self.state.approve(target)
            self.state.save()
            label = self._person_label(target, self.state.approved_users[str(target)])
            self.say_keyboard(target, "Your access request was approved. Create and manage apps with the "
                                      "buttons below.", USER_KEYBOARD)
            self.show_menu(chat_id, f"Approved {label}. They can now use the bot as a regular user.", user_id)
        elif action == "reject":
            info = self.state.access_requests.get(str(target))
            if not self.state.reject(target):
                self.show_menu(chat_id, f"No pending access request from {target}.", user_id)
                return
            self.state.save()
            self.say_keyboard(target, "Your access request was declined.", GUEST_KEYBOARD)
            self.show_menu(chat_id, f"Rejected the access request from {self._person_label(target, info)}.",
                           user_id)
        else:
            info = self.state.approved_users.get(str(target))
            if not self.state.revoke(target):
                self.show_menu(chat_id, f"{target} is not an approved user.", user_id)
                return
            self.state.save()
            # Private chat ids equal user ids, so the revoked user's open dialog and held files are dropped
            # and a running session is stopped.
            self._dialogs.pop(target, None)
            self._pending_files.pop(target, None)
            session = self._session_of(target)
            if session is not None:
                session.cancel.set()
            self.say_keyboard(target, "Your access to this bot was removed.", REMOVE_KEYBOARD)
            self.show_menu(chat_id, f"Removed access for {self._person_label(target, info)}. Their apps are kept.",
                           user_id)

    # ---- Commands ------------------------------------------------------

    def handle_text(self, chat_id: int, text: str, user_id=None):
        if not self._authorized(user_id):
            return
        if text == BACK_TO_MENU:
            discarded = self._discard_files(chat_id)
            self.show_menu(chat_id, "What would you like to do?" + discarded, user_id=user_id)
            return
        if text == CANCEL_ACTION:
            self.cmd_cancel(chat_id, user_id)
            return
        if text in (USER_VIEW, ADMIN_VIEW):
            self._switch_view(chat_id, text == USER_VIEW, user_id)
            return
        if text == SHOW_COMMANDS:
            self.say_keyboard(chat_id, self.help_text(user_id), self._main_keyboard(user_id))
            return
        if text == REQUEST_ACCESS:
            self.show_menu(chat_id, "You already have access to this bot.", user_id)
            return
        if text in (CREATE_APP, LIST_APPS, FIX_APP, START_APP, STOP_APP, REGISTER_SERVICES,
                    DELETE_APP, TUNNEL_ROUTES, ACCESS_REQUESTS, RUNNING_SESSIONS,
                    LIST_ROUTES, ADD_ROUTE, DELETE_ROUTE):
            self._handle_menu_button(chat_id, text, user_id)
            return
        if chat_id in self._dialogs and not text.startswith("/"):
            self._handle_dialog(chat_id, text, user_id)
            return
        if not text.startswith("/"):
            # Free text outside a guided flow never starts a job; every action begins from the menu.
            self.show_welcome(chat_id, user_id, NOTHING_STARTED)
            return
        parts = re.split(r"\s+", text, maxsplit=1)
        command = parts[0][1:].split("@")[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if command in ADMIN_ONLY_COMMANDS and not self._is_admin(user_id):
            self._admin_only(chat_id, user_id)
            return
        if command == "start":
            self.show_welcome(chat_id, user_id)
        elif command in ("help", "cli"):
            self.say_keyboard(chat_id, self.help_text(user_id), self._main_keyboard(user_id))
        elif command == "menu":
            self.show_menu(chat_id, user_id=user_id)
        elif command == "new":
            self.cmd_new(chat_id, arg, user_id)
        elif command == "delete":
            self.cmd_delete(chat_id, arg, user_id)
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
            self.cmd_cloudflare(chat_id, arg, user_id)
        elif command == "model":
            self.cmd_model(chat_id, arg)
        elif command == "status":
            self.cmd_status(chat_id, user_id)
        elif command == "requests":
            self.cmd_requests(chat_id, arg, user_id)
        elif command in ("approve", "reject", "revoke"):
            self.cmd_access(chat_id, command, arg, user_id)
        elif command == "sessions":
            self.cmd_sessions(chat_id, arg, user_id)
        elif command == "cancel":
            self.cmd_cancel(chat_id, user_id)
        else:
            self.show_welcome(chat_id, user_id, f"Unknown command: /{command}.")

    def _handle_menu_button(self, chat_id: int, label: str, user_id=None):
        """Start a guided action selected from a reply-keyboard button."""
        if label in ADMIN_ONLY_BUTTONS and not self._is_admin(user_id):
            self._admin_only(chat_id, user_id)
            return
        if label == LIST_APPS:
            self.cmd_list(chat_id, "", user_id)
        elif label == REGISTER_SERVICES:
            self.cmd_register(chat_id, "all", user_id)
        elif label == TUNNEL_ROUTES:
            self._dialogs.pop(chat_id, None)
            self.say_keyboard(chat_id, "Manage Cloudflare Tunnel routes.", ROUTES_KEYBOARD)
        elif label == LIST_ROUTES:
            self.cmd_cloudflare(chat_id, "list", user_id)
        elif label == ACCESS_REQUESTS:
            self._show_access_requests(chat_id, user_id)
        elif label == RUNNING_SESSIONS:
            self._show_sessions(chat_id, user_id)
        elif label == CREATE_APP:
            if self._cloudflare_unavailable(chat_id) or self._job_blocks_dialog(chat_id, user_id):
                return
            self._dialogs[chat_id] = {"flow": "create", "step": "name", "data": {},
                                      "user_id": user_id}
            self.say_keyboard(
                chat_id,
                "Enter the new app name. Use a lowercase DNS label such as `invoice-dashboard`.",
                CANCEL_KEYBOARD,
            )
        elif label == FIX_APP:
            if self._job_blocks_dialog(chat_id, user_id):
                return
            choices = self._app_choices(user_id, include_active=True, include_bot=True)
            if not choices and not self._is_admin(user_id):
                self.say(chat_id, "You do not have any apps to fix. Create an app first.")
                return
            # Buttons show app names; each maps back to its recorded repository.
            apps = {}
            for repo in choices:
                apps.setdefault(repo.rsplit("/", 1)[-1], repo)
            self._dialogs[chat_id] = {"flow": "fix", "step": "repo", "data": {"apps": apps},
                                      "user_id": user_id}
            self.say_keyboard(
                chat_id,
                "Choose an app or enter its repository as `name` or `owner/name`.",
                self._choice_keyboard(list(apps)),
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
            if self._job_blocks_dialog(chat_id, user_id):
                return
            choices = self._app_choices(user_id, include_active=True)
            self._dialogs[chat_id] = {"flow": "delete", "step": "name", "data": {},
                                      "user_id": user_id}
            self.say_keyboard(
                chat_id,
                "Choose the app to delete, or enter its name. You will be asked to confirm.",
                self._choice_keyboard(choices),
            )
        elif label == ADD_ROUTE:
            if self._cloudflare_unavailable(chat_id) or self._job_blocks_dialog(chat_id, user_id):
                return
            self._dialogs[chat_id] = {"flow": "route-add", "step": "name", "data": {},
                                      "user_id": user_id}
            self.say_keyboard(chat_id, "Enter the lowercase subdomain for the route.", CANCEL_KEYBOARD)
        elif label == DELETE_ROUTE:
            if self._cloudflare_unavailable(chat_id) or self._job_blocks_dialog(chat_id, user_id):
                return
            self._dialogs[chat_id] = {"flow": "route-delete", "step": "name", "data": {},
                                      "user_id": user_id}
            self.say_keyboard(chat_id, "Enter the lowercase subdomain of the route to delete.", CANCEL_KEYBOARD)

    def _job_blocks_dialog(self, chat_id: int, user_id=None) -> bool:
        """Refuse to open a guided flow while the user's own session still runs."""
        session = self._session_of(user_id)
        if session is None:
            return False
        self.say(chat_id, f"Your session is still running: {session.label}. Wait for it or use Cancel / stop.")
        return True

    def _app_choices(self, user_id=None, include_active: bool = False, include_bot: bool = False):
        """Return recorded app repositories that have a local folder, matching /list; only admins
        can be offered the bot repository."""
        self._register_running_apps()
        owner = ADMIN_OWNER if self._is_admin(user_id) else telegram_owner(user_id)
        choices = list(self.state.app_owners) if self._is_admin(user_id) else self.state.repos_for_owner(owner)
        active = self.state.get_active_repo(user_id, self._is_admin(user_id))
        if include_active and active:
            choices.insert(0, active)
        choices = [repo for repo in choices if not self._is_bot_app(repo.rsplit("/", 1)[-1])
                   and self._has_app_folder(repo.rsplit("/", 1)[-1])]
        if include_bot and self._is_admin(user_id) and self._bot_folder() is not None:
            choices.append(self._bot_repo())
        return list(dict.fromkeys(choices))

    def _app_directories(self):
        return sorted(
            (path for path in self.cfg.repos_dir.iterdir()
             if path.is_dir() and not self.deployer.is_bot_repo(path)),
            key=lambda path: path.name.lower(),
        )

    def _has_app_folder(self, name: str) -> bool:
        folder = self.cfg.repos_dir / name
        return folder.is_dir() and not folder.is_symlink()

    def _is_bot_app(self, name: str) -> bool:
        return self.deployer.is_bot_repo(self.cfg.repos_dir / name.lower())

    def _bot_folder(self):
        folder = self.cfg.repos_dir / BOT_APP
        return folder if folder.is_dir() else None

    def _bot_repo(self) -> str:
        """Return the bot checkout's GitHub repository, falling back to the default owner."""
        origin = self.git.origin_repo(self.cfg.repos_dir / BOT_APP)
        owner, name = origin if origin else (self._owner_default(), BOT_APP)
        return f"{owner}/{name}"

    def _resolve_ref(self, ref: str):
        """Parse a repository reference; a bare bot app name resolves to the bot checkout's origin."""
        owner, name = parse_repo_ref(ref, self._owner_default())
        if "/" not in ref.strip().strip("/") and self._is_bot_app(name):
            owner, name = self._bot_repo().split("/", 1)
        return owner, name

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
        elif flow == "app-control":
            action = dialog["step"]
            self._dialogs.pop(chat_id, None)
            self.cmd_app(chat_id, f"{action} {text.strip()}", user_id)
        elif flow == "route-add":
            self._handle_route_add_dialog(chat_id, dialog, text)
        elif flow == "route-delete":
            self._handle_route_delete_dialog(chat_id, dialog, text)
        elif flow == "access":
            self._handle_access_dialog(chat_id, dialog, text, user_id)
        elif flow == "sessions":
            match = re.search(r"#(\d+)\s*$", text)
            if not match:
                self.say(chat_id, "Choose one of the listed sessions to stop.")
                return
            self._stop_session(chat_id, int(match.group(1)), user_id)
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
            started = self._spawn_request(
                chat_id,
                lambda files: self._new_app(chat_id, data["ref"], request, data["private"],
                                            data["model"], data["template"], dialog.get("user_id"),
                                            data.get("temporary_chat"), files),
                after=lambda: self.show_menu(chat_id, "App creation finished. Choose the next action.",
                                             dialog.get("user_id")),
                user_id=dialog.get("user_id"), label=f"Create {data['ref']}", app=self._app_key(data["ref"]),
            )
            if not started:
                self.show_menu(chat_id, "App creation did not start.", dialog.get("user_id"))

    def _ask_create_prompt(self, chat_id: int, dialog: dict):
        dialog["step"] = "prompt"
        self.say_keyboard(
            chat_id,
            "Describe the app to create. The template's styling, architecture and stack are used by default; "
            "include any changes you want in this prompt. You can also send files, screenshots or photos "
            "to attach to it, or use a file caption as the description.",
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
        self.say_keyboard(chat_id, "Describe what should be fixed or changed in this app. You can also send "
                                   "files, screenshots or photos to attach to the request, or use a file "
                                   "caption as the description.", CANCEL_KEYBOARD)

    def _begin_model_selection(self, chat_id: int, dialog: dict):
        """Check Copilot's Claude and GPT submenus before offering a model for this dialog."""
        dialog["step"] = "loading-models"
        self.say_keyboard(chat_id, "Checking the Claude and GPT models currently available in Copilot...",
                          CANCEL_KEYBOARD)
        started = self._spawn(chat_id, lambda: self._load_model_choices(chat_id, dialog),
                              user_id=dialog.get("user_id"), label="Model check")
        if not started:
            self._dialogs.pop(chat_id, None)

    def _load_model_choices(self, chat_id: int, dialog: dict):
        try:
            models = self.roundtrip.list_models(lambda stage, message: None, self._cancel)
            error = "" if models else "the model picker listed no Claude or GPT models"
        except RoundTripError as exc:
            models, error = [], str(exc)
        except Cancelled:
            # A session stopped by an admin leaves no dialog waiting for the model list.
            if self._dialogs.get(chat_id) is not dialog:
                raise
            self.show_menu(chat_id, "The model check was stopped. Choose the next action.", dialog.get("user_id"))
            return
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
                              user_id=dialog.get("user_id"), label="Template list")
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
            if (self.cfg.repos_dir / route["app"]).is_dir() and not self._is_bot_app(route["app"])
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
            text = dialog["data"].get("apps", {}).get(text.strip(), text)
            try:
                owner, name = self._resolve_ref(text)
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
        started = self._spawn_request(chat_id, lambda files: self._request(
                                          chat_id, repo, request, user_id=user_id, model=model,
                                          temporary_chat=temporary_chat, attachments=files),
                              after=lambda: self.show_menu(chat_id, "Fix finished. Choose the next action.",
                                                           user_id),
                              user_id=user_id, label=f"Fix {repo}", app=self._app_key(repo))
        if not started:
            self.show_menu(chat_id, "The fix did not start.", user_id)

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
            after=lambda: self.show_menu(chat_id, "Deletion attempt finished. Choose the next action.",
                                         dialog.get("user_id")),
            user_id=dialog.get("user_id"), label=f"Delete {ref}", app=self._app_key(ref),
        )
        if not started:
            self.show_menu(chat_id, "Deletion did not start.", dialog.get("user_id"))

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
            after=lambda: self.show_menu(chat_id, "Route update finished. Choose the next action.",
                                         dialog.get("user_id")),
            user_id=dialog.get("user_id"), label=f"Tunnel route {name}",
        )
        if not started:
            self.show_menu(chat_id, "The route update did not start.", dialog.get("user_id"))

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
            after=lambda: self.show_menu(chat_id, "Route deletion finished. Choose the next action.",
                                         dialog.get("user_id")),
            user_id=dialog.get("user_id"), label=f"Delete tunnel route {name}",
        )
        if not started:
            self.show_menu(chat_id, "Route deletion did not start.", dialog.get("user_id"))

    def cmd_cloudflare(self, chat_id: int, arg: str, user_id=None):
        """Manage only routes explicitly requested by an authorized user."""
        parts = arg.split()
        usage = "Usage: /cloudflare <subdomain> <port> | list | delete <subdomain>"
        if parts and parts[0] == "list":
            if parts != ["list"]:
                self.say(chat_id, usage)
                return
            if self._cloudflare_unavailable(chat_id):
                return
            self._spawn(chat_id, lambda: self.say(chat_id, CloudflareRoutes(self.cfg).list_routes()),
                        user_id=user_id, label="Tunnel route list")
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
            self._spawn(chat_id, lambda: self.say(chat_id, CloudflareRoutes(self.cfg).delete_route(route)),
                        user_id=user_id, label=f"Delete tunnel route {route}")
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
        self._spawn(chat_id, lambda: self.say(chat_id, CloudflareRoutes(self.cfg).prepare(route, int(raw_port))),
                    user_id=user_id, label=f"Tunnel route {route}")

    @staticmethod
    def _valid_route(route: str) -> bool:
        return bool(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", route))

    def _cloudflare_unavailable(self, chat_id: int) -> bool:
        if self.cfg.cloudflare_enabled:
            return False
        self.say(chat_id, "Configure Cloudflare API token, account ID and zone ID first.")
        return True

    def _can_fix(self, user_id, repo: str) -> bool:
        if self._is_admin(user_id):
            return True
        if self._is_bot_app(repo.rsplit("/", 1)[-1]):
            return False
        return self.state.app_owner(repo) == telegram_owner(user_id)

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
        session = self._session_of(user_id)
        lines = [f"Repository: {active or 'none selected'}", f"Model: {self.state.model}",
                 f"Your session: {session.label + ' (running ' + session.elapsed() + ')' if session else 'idle'}",
                 f"Running sessions of all users: {len(self._running_sessions())}"]
        if active:
            name = active.split("/", 1)[1]
            try:
                status = (self.deployer.bot_status() if self._is_bot_app(name)
                          else self.deployer.status(name, local_path(self.cfg.repos_dir, name)))
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

        self._spawn(chat_id, control, user_id=user_id, label=f"{action.capitalize()} {name}")

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

        self._spawn(chat_id, register, user_id=user_id, label="Register app services")

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
        admin = self._is_admin(user_id)
        try:
            apps = self._app_directories()
        except OSError as error:
            self.say(chat_id, f"Could not read the apps folder: {error}")
            return
        bot = self._bot_folder() if admin else None
        if not apps and bot is None:
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

        entries = []
        for folder in apps:
            name = folder.name
            try:
                status = self.deployer.status(name, folder)
            except DeployError as error:
                status = f"unavailable ({error})" if admin else "unavailable"
            route = routes.get(name.lower())
            if route:
                port = route["service"].rsplit(":", 1)[-1].rstrip("/")
                url = f"https://{route['hostname']}"
            else:
                port = url = "not configured" if routes_available else "unavailable"
            if admin:
                entries.append(AppEntry(name, status, url, port, str(folder)))
            else:
                entries.append(AppEntry(name, self._public_status(status), url))
        if bot is not None:
            try:
                status = self.deployer.bot_status()
            except DeployError as error:
                status = f"unavailable ({error})"
            entries.append(AppEntry(bot.name, status, folder=str(bot), note=f"this bot · {BOT_SERVICE}"))
        try:
            safe_entries = [AppEntry(*(redact(value, self.cfg.secrets) for value in entry)) for entry in entries]
            for message in app_list_messages(safe_entries):
                self.tg.send_message(chat_id, message, parse_mode="HTML")
        except TelegramError as error:
            log.warning("send failed: %s", redact(str(error), self.cfg.secrets))

    def cmd_cancel(self, chat_id: int, user_id=None):
        """Stop the sender's own session; admins stop other users' sessions from Running sessions."""
        discarded = self._discard_files(chat_id)
        session = self._session_of(user_id)
        if chat_id in self._dialogs:
            if self._dialogs[chat_id].get("step") == "loading-models" and session is not None:
                session.cancel.set()
            self.show_menu(chat_id, "Action cancelled." + discarded, user_id)
            return
        if session is not None:
            session.cancel.set()
            self.say(chat_id, f"Cancelling your session ({session.label}); Copilot is asked to stop." + discarded)
            return
        message = "You have no running session." + discarded
        if self._is_admin(user_id) and self._running_sessions():
            message += f" Use {RUNNING_SESSIONS} or /sessions to stop other users' sessions."
        self.say(chat_id, message)

    # ---- Sessions ------------------------------------------------------

    @property
    def _cancel(self) -> threading.Event:
        """Cancel event of the session running on the current thread."""
        session = getattr(self._local, "session", None)
        return session.cancel if session is not None else self._idle_cancel

    def _session_of(self, user_id):
        with self._sessions_lock:
            return self._sessions.get(user_id)

    def _running_sessions(self):
        with self._sessions_lock:
            return sorted(self._sessions.values(), key=lambda session: session.number)

    @staticmethod
    def _app_key(ref: str) -> str:
        """Return the app folder name a repository reference changes, or an empty string."""
        try:
            return parse_repo_ref(ref, "owner")[1].lower()
        except RepoError:
            return ""

    def _session_owner(self, user_id) -> str:
        if user_id is None:
            return "bot"
        info = self.state.approved_users.get(str(user_id)) or {}
        label = self._person_label(user_id, {"name": self._names.get(user_id) or info.get("name")})
        return label + (" (admin)" if self._is_configured_admin(user_id) else "")

    def _session_line(self, session: Session) -> str:
        line = (f"#{session.number} · {self._session_owner(session.user_id)} · {session.label} · "
                f"started {session.started_at}, running {session.elapsed()}")
        return line + (" · stopping" if session.cancel.is_set() else "")

    def cmd_sessions(self, chat_id: int, arg: str, user_id=None):
        parts = arg.split()
        if not parts:
            self._show_sessions(chat_id, user_id)
        elif len(parts) == 2 and parts[0].lower() == "stop" and parts[1].lstrip("#").isdecimal():
            self._stop_session(chat_id, int(parts[1].lstrip("#")), user_id)
        else:
            self.say(chat_id, "Usage: /sessions [stop <number>]")

    def _show_sessions(self, chat_id: int, user_id=None):
        """List every user's running session and offer a stop button for each."""
        sessions = self._running_sessions()
        if not sessions:
            self.show_menu(chat_id, "No sessions are running.", user_id)
            return
        lines = [f"Running sessions ({len(sessions)}):"] + [f"- {self._session_line(item)}" for item in sessions]
        self._dialogs[chat_id] = {"flow": "sessions", "step": "choose", "data": {}, "user_id": user_id}
        buttons = [f"{STOP_SESSION} #{item.number}" for item in sessions]
        self.say_keyboard(chat_id, "\n".join(lines) + f"\n\nChoose a session to stop, or tap {RUNNING_SESSIONS} "
                                                      "to refresh the list.",
                          self._choice_keyboard(buttons, [[RUNNING_SESSIONS]]))

    def _stop_session(self, chat_id: int, number: int, user_id=None):
        session = next((item for item in self._running_sessions() if item.number == number), None)
        if session is None:
            self.show_menu(chat_id, f"Session #{number} is not running.", user_id)
            return
        if session.cancel.is_set():
            self.show_menu(chat_id, f"Session #{number} is already stopping.", user_id)
            return
        session.cancel.set()
        if session.user_id != user_id:
            self.say(session.chat_id, f"\u23f9 A kurwalab.fyi admin stopped your running session: {session.label}.")
        self.show_menu(chat_id, f"Stopping session #{number} ({session.label}) of "
                                f"{self._session_owner(session.user_id)}; Copilot is asked to stop. Actions that "
                                "do not use Copilot finish their current step first.", user_id)

    def _session_refusal(self, user_id, app: str) -> str:
        """Return why a new session cannot start; the caller holds _sessions_lock."""
        if self._restart_pending:
            return RESTART_PENDING_MESSAGE
        own = self._sessions.get(user_id)
        if own is not None:
            return f"Your session is still running: {own.label}. Wait for it to finish or use Cancel / stop."
        if app and any(session.app == app for session in self._sessions.values()):
            return f"Another session is already working on {app}. Try again when it finishes."
        return ""

    def _reserve_port(self, routes: CloudflareRoutes) -> int:
        """Allocate the next tunnel port, skipping ports that other app creations hold until they end."""
        session = getattr(self._local, "session", None)
        with self._port_lock:
            with self._sessions_lock:
                reserved = {item.port for item in self._sessions.values() if item.port}
            port = routes.next_port(reserved=reserved)
            if session is not None:
                session.port = port
        return port

    def _schedule_bot_restart(self, chat_id: int):
        """Restart the bot once every session has ended, so no other user's session is interrupted."""
        with self._sessions_lock:
            self._restart_pending, self._restart_chat = True, chat_id

    def _restart_when_idle(self):
        with self._sessions_lock:
            if not self._restart_pending or self._sessions:
                return
            chat_id, self._restart_pending, self._restart_chat = self._restart_chat, False, None
        try:
            self.deployer.restart_bot()
        except DeployError as error:
            self.say(chat_id, f"\u274c {error}")

    def cmd_fix(self, chat_id: int, arg: str, user_id=None):
        """Run a fix request with the guided flow's choices: app, model, chat type (admins) and request."""
        admin = self._is_admin(user_id)
        usage = ADMIN_FIX_USAGE if admin else USER_FIX_USAGE
        arg, options, error = self._fix_options(arg)
        if error:
            self.say(chat_id, error)
            return
        if not admin and options["temporary_chat"] is not None:
            self.say(chat_id, "Only kurwalab.fyi admins can choose the Copilot chat type; omit tempchat and notempchat.")
            return
        if not admin and options["show_browser"] is not None:
            self.say(chat_id, "Only kurwalab.fyi admins can choose the browser visibility; omit browser and nobrowser.")
            return
        if options["model"] is not None and not self._valid_model(options["model"]):
            self.say(chat_id, "Use a provider/model pair such as GPT/GPT-6.")
            return
        ref, prompt = options["app"], options["prompt"]
        if ref is None or prompt is None or arg:
            self.say(chat_id, usage)
            return
        self.start_request(chat_id, ref, prompt, options["show_browser"], options["temporary_chat"],
                           user_id, options["model"])

    @staticmethod
    def _option_value(key: str, text: str):
        """Match key=value at the start of text; the value may use straight or typographic quotes."""
        return re.match(
            key + r'''=(?:"([^"]*)"|'([^']*)'|“([^”]*)”|‘([^’]*)’|([^\s'"“”‘’]+))(?=\s|$)''',
            text, flags=re.IGNORECASE)

    @classmethod
    def _fix_options(cls, arg: str):
        """Read leading /fix options in any order; returns (remaining text, options, error).

        app= (or name=) selects the app, model= the model for this request, prompt= the request;
        tempchat, notempchat, browser and nobrowser override the chat type and browser visibility.
        An unquoted final prompt= consumes the rest of the message.
        """
        remaining = arg.strip()
        options = {"app": None, "model": None, "prompt": None, "temporary_chat": None, "show_browser": None}
        while remaining:
            parts = remaining.split(None, 1)
            word = parts[0].lower()
            rest = parts[1].lstrip() if len(parts) > 1 else ""
            if word in ("tempchat", "notempchat"):
                options["temporary_chat"] = word == "tempchat"
            elif word in ("browser", "nobrowser"):
                options["show_browser"] = word == "browser"
            else:
                for key, field in (("app", "app"), ("name", "app"), ("model", "model"), ("prompt", "prompt")):
                    match = cls._option_value(key, remaining)
                    if match:
                        break
                else:
                    if re.match(r"(?:app|name|model|prompt)=", word):
                        key = word.split("=", 1)[0]
                        return remaining, options, f'Use {key}="<value>" with a nonempty value.'
                    break
                if options[field] is not None:
                    return remaining, options, f"Specify {field} only once."
                value = next(group for group in match.groups() if group is not None).strip()
                if not value or (field == "app" and len(value.split()) != 1):
                    return remaining, options, f'Use {key}="<value>" with a nonempty value{" without spaces" if field == "app" else ""}.'
                rest = remaining[match.end():].lstrip()
                if field == "prompt" and match.group(5) is not None and rest:
                    if re.search(r"(?:^|\s)prompt=", rest, flags=re.IGNORECASE):
                        return remaining, options, "Specify prompt only once."
                    value, rest = f"{value} {rest}", ""
                options[field] = value
            remaining = rest
        return remaining, options, ""

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
        self._spawn_request(chat_id, lambda files: self._new_app(
            chat_id, ref, request, private, model, template or NEW_REPO_TEMPLATE, user_id, temporary_chat,
            files), user_id=user_id, label=f"Create {ref}", app=self._app_key(ref))

    def cmd_delete(self, chat_id: int, arg: str, user_id=None):
        if len(arg.split()) != 1:
            self.say(chat_id, "Usage: /delete <name|owner/name>")
            return
        self._spawn(chat_id, lambda: self._delete_app(chat_id, arg), user_id=user_id, label=f"Delete {arg}",
                    app=self._app_key(arg))

    def cmd_repo(self, chat_id: int, arg: str, user_id=None):
        arg, private = self._creation_options(arg)
        if not arg:
            active = self.state.get_active_repo(user_id, self._is_admin(user_id))
            self.say(chat_id, f"Active repository: {active}" if active
                     else "No repository selected. Use /repo <name>.")
            return
        self._spawn(chat_id, lambda: self._select_repo(chat_id, arg, private=private, user_id=user_id),
                    user_id=user_id, label=f"Select {arg}", app=self._app_key(arg))

    def start_request(self, chat_id: int, ref: str, request: str,
                      show_browser=None, temporary_chat=None, user_id=None, model=None):
        try:
            owner, name = self._resolve_ref(ref)
        except RepoError as error:
            self.say(chat_id, str(error))
            return
        full = f"{owner}/{name}"
        if not self._can_fix(user_id, full):
            self.say(chat_id, "You can only fix apps you created.")
            return
        self._spawn_request(chat_id, lambda files: self._request(
            chat_id, full, request, show_browser, temporary_chat, user_id, model=model, attachments=files),
            user_id=user_id, label=f"Fix {full}", app=name)

    # ---- Job execution -------------------------------------------------

    def _spawn(self, chat_id: int, job, after=None, user_id=None, label: str = "", app: str = ""):
        """Run a job as the user's session; sessions of different users run in parallel."""
        with self._sessions_lock:
            refusal = self._session_refusal(user_id, app.lower())
            if not refusal:
                session = Session(next(self._session_numbers), user_id, chat_id, label or "Bot action", app)
                self._sessions[user_id] = session
        if refusal:
            self.say(chat_id, refusal)
            return False
        # Errors use the view that was active when the job started.
        admin = self._is_admin(user_id)

        def run():
            self._local.session = session
            try:
                job()
            except Cancelled:
                self.say(chat_id, "Cancelled.")
            except _BOT_ERRORS as e:
                self.say(chat_id, f"\u274c {e if admin else self._user_error(e)}")
            except Exception as e:
                log.exception("job failed")
                self.say(chat_id, f"\u274c Internal error: {e.__class__.__name__}. See the bot log.")
            finally:
                if after is not None:
                    try:
                        after()
                    except Exception:
                        log.exception("post-job UI update failed")
                with self._sessions_lock:
                    if self._sessions.get(user_id) is session:
                        del self._sessions[user_id]
                self._local.session = None
                self._restart_when_idle()

        session.thread = self._worker = threading.Thread(target=run, name=f"session-{session.number}", daemon=True)
        session.thread.start()
        return True

    def _user_error(self, error) -> str:
        """Describe a job failure to a regular user without service, tunnel or server-path details."""
        if isinstance(error, DeployError):
            return "The app could not be started or stopped. Ask an admin to check it."
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
        """Reduce a service status to running, starting or stopped for regular users."""
        lowered = status.lower()
        if lowered.startswith("running"):
            return "running"
        if lowered.startswith(("activating", "reloading")):
            return "starting"
        if lowered.startswith("unavailable"):
            return "unavailable"
        return "stopped"

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
        if self._is_bot_app(name):
            if created:
                raise RepoError(f"{owner}/{name} was not found on GitHub; the bot repository is never created.")
            managed_owner = ""
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
        owner, name = self._resolve_ref(arg)
        progress = Progress(self.tg, chat_id, self.cfg)
        dest, branch, how = self._prepare(owner, name, progress, new_only=new_only, private=private,
                                          managed_owner=ADMIN_OWNER)
        full = f"{owner}/{name}"
        if not self._is_bot_app(name):
            self.state.set_app_owner(full, ADMIN_OWNER)
        self.state.set_active_repo(user_id, full, self._is_admin(user_id))
        self.state.save()
        progress.flush()
        self.say(chat_id, f"\u2705 {owner}/{name} is {how} at {dest} (branch {branch}) and now active."
                 + (" Send your next message describing what to build." if new_only else ""))

    def _new_app(self, chat_id: int, ref: str, request: str, private: bool, model=None,
                  template: str = NEW_REPO_TEMPLATE, user_id=None, temporary_chat=None, attachments=()):
        owner, name = parse_repo_ref(ref, self._owner_default())
        if not self._valid_route(name):
            raise RepoError("App names must be lowercase DNS labels containing only letters, digits and hyphens.")
        if name == template:
            raise RepoError("The new app name must differ from its template app name.")
        full = f"{owner}/{name}"
        progress = Progress(self.tg, chat_id, self.cfg)
        if self._is_bot_app(name):
            raise RepoError(f"{full} already exists. Choose another app name.")
        if self._is_bot_app(template) or not (self.cfg.repos_dir / template).is_dir():
            raise RepoError(f"Template app is not available locally: {template}")
        # Files are downloaded before the repository is created, so a failed download leaves nothing behind.
        with self._downloaded(attachments, progress, full) as files:
            admin = self._is_admin(user_id)
            creator = ADMIN_OWNER if admin else telegram_owner(user_id)
            dest, _, _ = self._prepare(owner, name, progress, new_only=True, private=private,
                                       template_name=template, managed_owner=creator, admin=admin)
            self.state.set_active_repo(user_id, full, admin)
            self.state.save()
            routes = CloudflareRoutes(self.cfg)
            progress.update(f"{full}: checking tunnel routes for the next port..." if admin
                            else f"{full}: preparing the app address...")
            port = self._reserve_port(routes)
            prompt = NEW_APP_PROMPT.format(port=port) + request + DEPLOY_NOTE
            if self._cancel.is_set():
                raise Cancelled()
            progress.update(f"{full}: sending the repository to Copilot for port {port}..." if admin
                            else f"{full}: sending the repository to Copilot...")
            try:
                result = self.roundtrip.run(
                    dest, prompt, model or self.state.model,
                    lambda stage, msg: progress.update(self._repo_progress(full, msg), stage), self._cancel,
                    temporary_chat=temporary_chat, attachments=files, history=False)
            except Cancelled:
                progress.flush()
                self._warn_dirty(chat_id, dest, full, admin)
                raise
            progress.update(f"{full}: committing and pushing...")
            pushed = self.git.commit_and_push(dest, request, result.get("chat_response") or "")
            if pushed is None:
                progress.flush()
                self.say(chat_id, f"Copilot returned no changes to commit for {full}; nothing was deployed."
                                  + self._copilot_output(result))
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
                 show_browser=None, temporary_chat=None, user_id=None, model=None, attachments=()):
        owner, name = self._resolve_ref(ref)
        full = f"{owner}/{name}"
        if not self._can_fix(user_id, full):
            raise RepoError("You can only fix apps you created.")
        if self._is_bot_app(name):
            self._bot_request(chat_id, owner, name, request, show_browser, temporary_chat, user_id, model,
                              attachments)
            return
        progress = Progress(self.tg, chat_id, self.cfg)
        admin = self._is_admin(user_id)
        managed_owner = self.state.app_owner(full) or ADMIN_OWNER
        dest, branch, _ = self._prepare(owner, name, progress, managed_owner=managed_owner, admin=admin)
        self.state.set_active_repo(user_id, full, admin)
        self.state.save()
        if self._cancel.is_set():
            raise Cancelled()
        try:
            with self._downloaded(attachments, progress, full) as files:
                progress.update(f"{full}: sending the repository to Copilot...")
                result = self.roundtrip.run(dest, request + DEPLOY_NOTE, model or self.state.model,
                                            lambda stage, msg: progress.update(self._repo_progress(full, msg), stage),
                                            self._cancel, show_browser, temporary_chat,
                                            attachments=files)
        except Cancelled:
            progress.flush()
            self._warn_dirty(chat_id, dest, full, admin)
            raise
        progress.update(f"{full}: committing and pushing...")
        pushed = self.git.commit_and_push(dest, request, result.get("chat_response") or "")
        if pushed is None:
            progress.flush()
            self.say(chat_id, f"Copilot returned no changes to commit for {full}; nothing was deployed."
                              + self._copilot_output(result))
            return
        sha, pushed_branch = pushed
        result["numstat"] = self._numstat(dest)
        progress.update(f"{full}: restarting its systemd service..." if admin else f"{full}: restarting the app...")
        deploy = self.deployer.redeploy(name, dest)
        progress.flush()
        self.say(chat_id, self._report(full, sha, pushed_branch, result, deploy,
                                       url="" if admin else self._app_url(name)))

    def _bot_request(self, chat_id: int, owner: str, name: str, request: str,
                     show_browser=None, temporary_chat=None, user_id=None, model=None, attachments=()):
        """Change the bot's own repository from a separate clone, then update and restart the bot.

        The live checkout keeps its ignored files (.env, .venv), which a full worktree replacement would remove.
        The restart waits until every running session has ended.
        """
        full = f"{owner}/{name}"
        progress = Progress(self.tg, chat_id, self.cfg)
        live, branch, _ = self._prepare(owner, name, progress)
        self.state.set_active_repo(user_id, full, True)
        self.state.save()
        if self._cancel.is_set():
            raise Cancelled()
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="bot-fix-", dir=self.cfg.state_dir) as work:
            dest = Path(work) / name
            progress.update(f"{full}: cloning a separate working copy...")
            self.git.run("clone", "--branch", branch, self.gh.get_repo(owner, name)["clone_url"], str(dest))
            with self._downloaded(attachments, progress, full) as files:
                progress.update(f"{full}: sending the repository to Copilot...")
                result = self.roundtrip.run(dest, request + BOT_DEPLOY_NOTE, model or self.state.model,
                                            lambda stage, msg: progress.update(self._repo_progress(full, msg), stage),
                                            self._cancel, show_browser, temporary_chat,
                                            attachments=files)
            progress.update(f"{full}: committing and pushing...")
            pushed = self.git.commit_and_push(dest, request, result.get("chat_response") or "")
            if pushed is not None:
                result["numstat"] = self._numstat(dest)
        if pushed is None:
            progress.flush()
            self.say(chat_id, f"Copilot returned no changes to commit for {full}; nothing was deployed."
                              + self._copilot_output(result))
            return
        sha, pushed_branch = pushed
        progress.update(f"{full}: updating {live}...")
        self.git.sync(live)
        progress.flush()
        restart = self.deployer.bot_runs_as_service()
        lines = [f"\u2705 {full}: commit {sha} pushed to {pushed_branch}."]
        changes = self._changes(result)
        if changes:
            lines.append(changes)
        lines.append(f"Local checkout: {live} updated.")
        others = len(self._running_sessions()) - 1
        if not restart:
            lines.append("Restart: the bot is not running as its systemd service; restart it to apply the changes.")
        elif others > 0:
            lines.append(f"Restart: {BOT_SERVICE} restarts to apply the changes once the {others} other running "
                         "session(s) finish; new sessions wait for the restart.")
        else:
            lines.append(f"Restart: {BOT_SERVICE} is restarting to apply the changes; the bot is back shortly.")
        self.say(chat_id, "\n".join(lines) + self._copilot_output(result))
        if restart:
            self._schedule_bot_restart(chat_id)

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

    def _numstat(self, dest) -> str:
        """Read the pushed commit's per-file counts; a failure only drops the change summary."""
        try:
            return self.git.numstat(dest)
        except GitError as error:
            log.warning("cannot read the commit's changes: %s", redact(str(error), self.cfg.secrets))
            return ""

    @staticmethod
    def _changes(result: dict) -> str:
        """Diff-style change list of the commit, or the engine's summary line when counts are unavailable."""
        changes = format_changes(parse_numstat(result.get("numstat") or ""))
        if changes:
            return changes
        diff_lines = [line for line in (result.get("diff_stat") or "").splitlines() if line.strip()]
        return f"Changes: {diff_lines[-1].strip()}" if diff_lines else ""

    @staticmethod
    def _copilot_output(result: dict) -> str:
        """Copilot's final answer as the closing section of a result message."""
        output = (result.get("chat_response") or "(no final text returned)").strip()
        return f"\n\nCopilot output:\n{output}"

    @staticmethod
    def _report(full: str, sha: str, branch: str, result: dict, deploy, url: str = "") -> str:
        """Summarize a fix; a URL selects the regular-user form without service details."""
        mark = "\u2705" if deploy.ok else "\u26a0\ufe0f"
        lines = [f"{mark} {full}: commit {sha} pushed to {branch}."]
        changes = Bot._changes(result)
        if changes:
            lines.append(changes)
        if not url:
            lines.append(("Deploy: " if deploy.ok else "Pushed, but the redeploy failed: ") + deploy.message)
        elif deploy.ok:
            lines.append(f"App: {url}")
        else:
            lines.append("Pushed, but the redeploy failed. Ask an admin to check the app.")
        return "\n".join(lines) + Bot._copilot_output(result)

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
        return "\n".join(lines) + Bot._copilot_output(result)
