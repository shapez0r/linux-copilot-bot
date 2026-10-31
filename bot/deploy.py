"""Runs each app in the background and publishes its local listener through Cloudflare."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .cloudflare import CloudflareError, CloudflareRoutes
from .config import SECRET_NAMES, Config

STOP_GRACE_SECONDS = 15


@dataclass
class DeployResult:
    ok: bool
    message: str


def app_env(environ=None) -> dict:
    """Environment for deployed apps: no bot secrets and no leakage of the bot's own virtualenv."""
    env = {k: v for k, v in (os.environ if environ is None else environ).items() if k not in SECRET_NAMES}
    venv = env.pop("VIRTUAL_ENV", "")
    if venv:
        env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                      if p and not p.startswith(venv + os.sep))
    return env


def _alive(pid: int) -> bool:
    """True for a running process; zombies count as exited."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat[stat.rindex(")") + 2:].split()[0] != "Z"


def _cwd(pid: int):
    try:
        return Path(os.readlink(f"/proc/{pid}/cwd"))
    except OSError:
        return None


def _listening_ports(process_group: int) -> list[int]:
    """Return TCP ports listened on by processes in a Linux process group."""
    inodes = set()
    for stat_path in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat_path.read_text().split(") ", 1)[1].split()
            if int(fields[2]) != process_group:
                continue
            for fd in (stat_path.parent / "fd").iterdir():
                target = os.readlink(fd)
                if target.startswith("socket:["):
                    inodes.add(target[8:-1])
        except (OSError, ValueError, IndexError):
            continue
    ports = set()
    for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        try:
            rows = table.read_text().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            fields = row.split()
            if len(fields) > 9 and fields[3] == "0A" and fields[9] in inodes:
                ports.add(int(fields[1].rsplit(":", 1)[1], 16))
    return sorted(ports)


class Deployer:
    def __init__(self, cfg: Config, routes=None):
        self._cfg = cfg
        self._routes = routes if routes is not None else (CloudflareRoutes(cfg) if cfg.cloudflare_enabled else None)
        self._run_dir = cfg.state_dir / "run"
        self._log_dir = cfg.state_dir / "logs"
        self._children = {}  # Popen handles started by this process, so exited apps are reaped.

    def log_path(self, app: str) -> Path:
        return self._log_dir / f"{app}.log"

    def _pid_file(self, app: str) -> Path:
        return self._run_dir / f"{app}.pid"

    def _read_pid(self, app: str):
        try:
            return int(json.loads(self._pid_file(app).read_text())["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def has_run_target(self, repo_dir: Path) -> bool:
        """Whether `make run` resolves to a recipe (checked with a dry run)."""
        done = subprocess.run(["make", "-n", "run"], cwd=repo_dir, env=app_env(), stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        return done.returncode == 0

    def status(self, app: str, repo_dir: Path) -> str:
        pid = self._read_pid(app)
        if pid and _alive(pid) and _cwd(pid) == repo_dir.resolve():
            return f"running (pid {pid})"
        return "not running"

    def stop(self, app: str, repo_dir: Path) -> bool:
        """Stop the process group started by an earlier deploy; True when a group was signalled."""
        pid = self._read_pid(app)
        handle = self._children.pop(app, None)
        self._pid_file(app).unlink(missing_ok=True)
        if pid is None:
            return False
        if _alive(pid) and _cwd(pid) != repo_dir.resolve():
            return False  # Stale pid reused by an unrelated process.
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            if handle is not None:
                handle.poll()
            return False
        if not self._wait_gone(pid, handle, STOP_GRACE_SECONDS):
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            self._wait_gone(pid, handle, 5)
        return True

    @staticmethod
    def _wait_gone(pid: int, handle, seconds: float) -> bool:
        """Wait until no process of the group remains; reaps the leader when this process started it."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if handle is not None:
                handle.poll()
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.2)
        return False

    def redeploy(self, app: str, repo_dir: Path) -> DeployResult:
        """Stop the previous instance, start `make run` and report whether it stayed up."""
        if not self.has_run_target(repo_dir):
            return DeployResult(False, "No `run` target in the Makefile; nothing was started.")
        self.stop(app, repo_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._run_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_path(app)
        with open(log_path, "ab") as log:
            log.write(f"\n=== make run {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n".encode())
            log.flush()
            proc = subprocess.Popen(["make", "run"], cwd=repo_dir, env=app_env(), stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + self._cfg.settle_seconds
        while time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.25)
        code = proc.poll()
        if code is None:
            self._children[app] = proc
            self._pid_file(app).write_text(json.dumps({"pid": proc.pid}))
            message = f"`make run` is running (pid {proc.pid})."
            if self._routes is not None:
                ports = _listening_ports(proc.pid)
                if len(ports) != 1:
                    detail = "no TCP listener" if not ports else "multiple TCP listeners: " + ", ".join(map(str, ports))
                    return DeployResult(False, f"{message}\nTunnel route failed: found {detail} in the app process group.")
                try:
                    message += "\n" + self._routes.prepare(app, ports[0])
                except CloudflareError as error:
                    return DeployResult(False, f"{message}\nTunnel route failed: {error}")
            return DeployResult(True, message)
        if code == 0:
            return DeployResult(True, "`make run` finished successfully.")
        return DeployResult(False, f"`make run` exited with code {code}.\n{self.log_tail(app)}")

    def log_tail(self, app: str, lines: int = 15) -> str:
        try:
            return "\n".join(self.log_path(app).read_text(errors="replace").splitlines()[-lines:])
        except OSError:
            return ""
