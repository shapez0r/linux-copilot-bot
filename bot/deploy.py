"""Registers and controls bot-managed applications as systemd system services."""
from __future__ import annotations

import grp
import os
import pwd
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import ROOT, SECRET_NAMES, Config

SYSTEMD_TIMEOUT_SECONDS = 30
APP_SERVICE_PREFIX = "copilot-app-"
BOT_SERVICE = "copilot-telegram-bot.service"
BOT_APP = "linux-copilot-bot"
_APP_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


@dataclass
class DeployResult:
    ok: bool
    message: str


@dataclass(frozen=True)
class ServiceState:
    load: str = "not-found"
    active: str = "inactive"
    sub: str = "dead"
    pid: int = 0

    @property
    def registered(self) -> bool:
        return self.load == "loaded"

    @property
    def running(self) -> bool:
        return self.active == "active"

    def display(self) -> str:
        if not self.registered:
            return "not registered"
        if self.running:
            return f"running (pid {self.pid})" if self.pid else "running"
        if self.active == "inactive":
            return "stopped"
        if self.active == "failed":
            return "failed"
        return f"{self.active} ({self.sub})" if self.sub and self.sub != self.active else self.active


class DeployError(RuntimeError):
    """Raised when a systemd service cannot be managed safely."""


def app_env(environ=None) -> dict:
    """Return an app environment without bot secrets or the bot virtual environment."""
    env = {k: v for k, v in (os.environ if environ is None else environ).items() if k not in SECRET_NAMES}
    venv = env.pop("VIRTUAL_ENV", "")
    if venv:
        env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                      if p and not p.startswith(venv + os.sep))
    return env


def _unit_path(value: str) -> str:
    """Escape a path for a systemd directive."""
    return (value.replace("%", "%%").replace("\\", "\\x5c").replace(" ", "\\x20")
            .replace("\t", "\\x09").replace("\r", "\\x0d").replace("\n", "\\x0a"))


class Deployer:
    # Parallel sessions must not overwrite each other's crontab edits.
    _crontab_lock = threading.Lock()

    def __init__(self, cfg: Config, bot_repo_dir: Path = ROOT):
        self._cfg = cfg
        self._bot_repo_dir = Path(bot_repo_dir).resolve()
        self._log_dir = cfg.state_dir / "logs"
        self._unit_dir = Path("/etc/systemd/system")
        uid = int(os.environ.get("SUDO_UID", os.getuid())) if os.geteuid() == 0 else os.getuid()
        account = pwd.getpwuid(uid)
        self._service_user = account.pw_name
        self._service_group = grp.getgrgid(account.pw_gid).gr_name
        self._service_home = Path(account.pw_dir)

    def log_path(self, app: str) -> Path:
        return self._log_dir / f"{app}.log"

    def is_bot_repo(self, path: Path) -> bool:
        try:
            return path.resolve() == self._bot_repo_dir or path.name == BOT_APP
        except OSError:
            return False

    @staticmethod
    def service_name(app: str) -> str:
        if not _APP_NAME.fullmatch(app):
            raise DeployError(f"invalid app name for a service: {app}")
        return f"{APP_SERVICE_PREFIX}{app}.service"

    def service_path(self, app: str) -> Path:
        return self._unit_dir / self.service_name(app)

    def has_run_target(self, repo_dir: Path) -> bool:
        """Return whether `make run` resolves to a recipe."""
        try:
            done = subprocess.run(
                ["make", "-n", "run"], cwd=repo_dir, env=app_env(), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return done.returncode == 0

    def _validate_repo(self, app: str, repo_dir: Path, must_exist: bool = True):
        self.service_name(app)
        try:
            resolved = repo_dir.resolve(strict=must_exist)
            parent = self._cfg.repos_dir.resolve(strict=True)
        except OSError as error:
            raise DeployError(f"cannot resolve the app folder: {error}") from None
        if resolved.parent != parent or resolved.name != app or (must_exist and not resolved.is_dir()):
            raise DeployError(f"app folder must be {self._cfg.repos_dir / app}")
        if self.is_bot_repo(resolved):
            raise DeployError("the bot repository is not an app")

    def _sudo(self, args: list[str], check: bool = True):
        command = ["sudo", "-n", "--", *args]
        try:
            done = subprocess.run(
                command, text=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=SYSTEMD_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DeployError(f"cannot run {' '.join(command)}: {error}") from None
        if check and done.returncode != 0:
            detail = done.stderr.strip() or done.stdout.strip() or f"exit code {done.returncode}"
            raise DeployError(f"{' '.join(command)} failed: {detail[-600:]}")
        return done

    def _systemctl(self, args: list[str], check: bool = True):
        return self._sudo(["systemctl", *args], check=check)

    def _state(self, service: str) -> ServiceState:
        done = self._systemctl([
            "show", service, "--no-pager", "--property=LoadState", "--property=ActiveState",
            "--property=SubState", "--property=MainPID",
        ], check=False)
        values = {}
        for line in done.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value
        if done.returncode != 0 and not values:
            detail = done.stderr.strip() or done.stdout.strip() or f"exit code {done.returncode}"
            raise DeployError(f"cannot read service {service}: {detail[-600:]}")
        try:
            pid = int(values.get("MainPID") or 0)
        except ValueError:
            pid = 0
        return ServiceState(
            load=values.get("LoadState", "not-found"),
            active=values.get("ActiveState", "inactive"),
            sub=values.get("SubState", "dead"),
            pid=pid,
        )

    def service_state(self, app: str) -> ServiceState:
        return self._state(self.service_name(app))

    def status(self, app: str, repo_dir: Path) -> str:
        self._validate_repo(app, repo_dir)
        return self.service_state(app).display()

    def bot_status(self) -> str:
        return self._state(BOT_SERVICE).display()

    @staticmethod
    def bot_runs_as_service(environ=None) -> bool:
        """Return whether this bot process was started by systemd and can be restarted through it."""
        return bool((os.environ if environ is None else environ).get("INVOCATION_ID"))

    def restart_bot(self):
        """Queue a bot service restart without waiting, so the current job can finish reporting first.

        The restart stops this process's own control group, so systemd may terminate the sudo child
        before it returns. A SIGTERM exit therefore means the restart is under way, not that it failed.
        """
        done = self._systemctl(["restart", "--no-block", BOT_SERVICE], check=False)
        if done.returncode not in (0, -signal.SIGTERM):
            detail = done.stderr.strip() or done.stdout.strip() or f"exit code {done.returncode}"
            raise DeployError(f"cannot restart {BOT_SERVICE}: {detail[-600:]}")

    def running_apps(self) -> list[tuple[str, int]]:
        """Return local apps whose systemd services are active."""
        try:
            folders = sorted(path for path in self._cfg.repos_dir.iterdir()
                             if path.is_dir() and not self.is_bot_repo(path))
        except OSError:
            return []
        running = []
        for folder in folders:
            if not _APP_NAME.fullmatch(folder.name):
                continue
            try:
                state = self.service_state(folder.name)
            except DeployError:
                continue
            if state.running:
                running.append((folder.name, state.pid))
        return running

    def _app_unit(self, app: str, repo_dir: Path) -> str:
        make = shutil.which("make") or "/usr/bin/make"
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise DeployError(f"cannot create the app log folder: {error}") from None
        return "\n".join([
            "[Unit]",
            f"Description=Copilot-managed app: {app}",
            "",
            "[Service]",
            "Type=simple",
            f"User={self._service_user}",
            f"Group={self._service_group}",
            f"Environment=HOME={_unit_path(str(self._service_home))}",
            f"WorkingDirectory={_unit_path(str(repo_dir.resolve()))}",
            f"ExecStart={_unit_path(make)} run",
            "Restart=on-failure",
            "RestartSec=5",
            "UnsetEnvironment=TELEGRAM_BOT_TOKEN GITHUB_TOKEN CLOUDFLARE_API_TOKEN VIRTUAL_ENV",
            f"StandardOutput=append:{_unit_path(str(self.log_path(app)))}",
            "StandardError=inherit",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ])

    def _bot_unit(self, repo_dir: Path) -> str:
        make = shutil.which("make") or "/usr/bin/make"
        return "\n".join([
            "[Unit]",
            "Description=Copilot Telegram repository bot",
            "",
            "[Service]",
            "Type=simple",
            f"User={self._service_user}",
            f"Group={self._service_group}",
            f"Environment=HOME={_unit_path(str(self._service_home))}",
            f"WorkingDirectory={_unit_path(str(repo_dir.resolve()))}",
            f"ExecStart={_unit_path(make)} run",
            "Restart=on-failure",
            "RestartSec=10",
            "TimeoutStopSec=90",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ])

    def _write_unit(self, path: Path, content: str):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as output:
                output.write(content)
                temporary = Path(output.name)
            self._sudo(["install", "-D", "-m", "0644", "--", str(temporary), str(path)])
        except OSError as error:
            raise DeployError(f"cannot prepare service {path.name}: {error}") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _remove_unit(self, path: Path):
        self._sudo(["rm", "-f", "--", str(path)])

    def _install_unit(self, service: str, content: str, keep_existing: bool = False) -> bool:
        """Write the unit when missing or different and enable it; keep_existing never rewrites a present unit."""
        path = self._unit_dir / service
        try:
            exists = path.is_file()
            changed = not exists or (not keep_existing and path.read_text(encoding="utf-8") != content)
        except OSError as error:
            raise DeployError(f"cannot read service {service}: {error}") from None
        if changed:
            self._write_unit(path, content)
            self._systemctl(["daemon-reload"])
        enabled = self._systemctl(["is-enabled", service], check=False).returncode == 0
        if not enabled:
            self._systemctl(["enable", service])
        return changed or not enabled

    def _remove_reboot_entry(self, repo_dir: Path) -> bool:
        """Remove the exact app startup line when crontab is available."""
        with self._crontab_lock:
            return self._remove_reboot_line(repo_dir)

    def _remove_reboot_line(self, repo_dir: Path) -> bool:
        crontab = shutil.which("crontab")
        if not crontab:
            return False
        line = f"@reboot cd {shlex.quote(str(repo_dir))} && git pull && make run"
        try:
            current = subprocess.run(
                [crontab, "-l"], text=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=SYSTEMD_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DeployError(f"cannot read the user crontab: {error}") from None
        if current.returncode != 0:
            if "no crontab" in current.stderr.lower():
                return False
            detail = current.stderr.strip() or f"exit code {current.returncode}"
            raise DeployError(f"cannot read the user crontab: {detail[-400:]}")
        if line not in current.stdout.splitlines():
            return False
        remaining = [entry for entry in current.stdout.splitlines() if entry != line]
        try:
            installed = subprocess.run(
                [crontab, "-"], input="\n".join(remaining) + "\n", text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=SYSTEMD_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DeployError(f"cannot remove the matching reboot entry: {error}") from None
        if installed.returncode != 0:
            detail = installed.stderr.strip() or f"exit code {installed.returncode}"
            raise DeployError(f"cannot remove the matching reboot entry: {detail[-400:]}")
        return True

    def ensure_bot_service(self, repo_dir: Path) -> str:
        """Install and enable the bot unit without starting a second bot process."""
        repo_dir = Path(repo_dir).resolve()
        changed = self._install_unit(BOT_SERVICE, self._bot_unit(repo_dir))
        removed = self._remove_reboot_entry(repo_dir)
        action = "registered and enabled" if changed else "already registered and enabled"
        suffix = " The matching crontab entry was removed." if removed else ""
        return f"Bot service {BOT_SERVICE} is {action}.{suffix}"

    def ensure_service(self, app: str, repo_dir: Path) -> str:
        """Install and enable one app unit without changing its running state.

        An existing unit file is left as it is, so manual edits survive starts, stops and redeploys.
        """
        repo_dir = Path(repo_dir)
        self._validate_repo(app, repo_dir)
        if not self.has_run_target(repo_dir):
            raise DeployError(f"{repo_dir} has no usable `make run` target")
        service = self.service_name(app)
        changed = self._install_unit(service, self._app_unit(app, repo_dir), keep_existing=True)
        removed = self._remove_reboot_entry(repo_dir)
        action = "registered and enabled" if changed else "already registered and enabled"
        suffix = " The matching crontab entry was removed." if removed else ""
        return f"Service {service} is {action}.{suffix}"

    def _wait_running(self, app: str) -> DeployResult:
        service = self.service_name(app)
        deadline = time.monotonic() + self._cfg.settle_seconds
        state = self.service_state(app)
        while time.monotonic() < deadline:
            if state.active in ("failed", "inactive"):
                break
            time.sleep(0.25)
            state = self.service_state(app)
        if state.running and time.monotonic() >= deadline:
            pid = f" (pid {state.pid})" if state.pid else ""
            return DeployResult(True, f"App service {service} is enabled and running{pid}.")
        detail = self.log_tail(app)
        message = f"App service {service} is {state.display()}."
        if detail:
            message += "\n" + detail
        return DeployResult(False, message)

    def start(self, app: str, repo_dir: Path) -> DeployResult:
        self.ensure_service(app, repo_dir)
        self._systemctl(["start", self.service_name(app)])
        return self._wait_running(app)

    def stop(self, app: str, repo_dir: Path) -> bool:
        repo_dir = Path(repo_dir)
        self._validate_repo(app, repo_dir, must_exist=False)
        state = self.service_state(app)
        if not state.registered:
            return False
        self._systemctl(["stop", self.service_name(app)])
        return state.active not in ("inactive", "failed")

    def redeploy(self, app: str, repo_dir: Path) -> DeployResult:
        """Install the app unit when missing and restart it with systemd."""
        try:
            self.ensure_service(app, repo_dir)
            self._systemctl(["restart", self.service_name(app)])
            return self._wait_running(app)
        except DeployError as error:
            return DeployResult(False, str(error))

    def unregister(self, app: str, repo_dir: Path) -> str:
        """Disable and remove an app unit and its matching crontab entry."""
        repo_dir = Path(repo_dir)
        self._validate_repo(app, repo_dir, must_exist=False)
        service = self.service_name(app)
        path = self.service_path(app)
        registered = path.is_file() or self.service_state(app).registered
        if registered:
            self._systemctl(["disable", "--now", service])
            self._remove_unit(path)
            self._systemctl(["daemon-reload"])
            self._systemctl(["reset-failed", service], check=False)
        removed = self._remove_reboot_entry(repo_dir)
        if registered:
            message = f"Service: disabled and removed {service}."
        else:
            message = "Service: no app service was registered."
        if removed:
            message += " The matching crontab entry was removed."
        return message

    def log_tail(self, app: str, lines: int = 15) -> str:
        try:
            return "\n".join(self.log_path(app).read_text(errors="replace").splitlines()[-lines:])
        except OSError:
            return ""
