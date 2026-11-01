"""Runs the Copilot round-trip engine as a subprocess and relays its progress."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections import deque

from .config import SECRET_NAMES, Config

STATUS_PREFIX = "ROUNDTRIP_STATUS:"
REASONING_PREFIX = "ROUNDTRIP_REASONING:"
QUIT_GRACE_SECONDS = 30  # Time the engine gets to click Stop in Copilot and exit after a cancel request.
QUIT_EXIT_CODE = 130


class RoundTripError(Exception):
    """Raised when the engine fails; the message is safe to show to the owner."""


class Cancelled(Exception):
    """Raised when the owner cancelled the run."""


def parse_status_event(line: str):
    """Return a validated engine status payload, else None."""
    if not line.startswith(STATUS_PREFIX):
        return None
    try:
        value = json.loads(line[len(STATUS_PREFIX):])
        if not isinstance(value, dict) or "stage" not in value or "message" not in value:
            return None
        return value
    except (ValueError, KeyError, TypeError):
        return None


def parse_status(line: str):
    """Return (stage, message) for an engine progress line, else None."""
    value = parse_status_event(line)
    return (str(value["stage"]), str(value["message"])) if value else None


def parse_reasoning(line: str):
    """Return a validated live-output payload, else None."""
    if not line.startswith(REASONING_PREFIX):
        return None
    try:
        value = json.loads(line[len(REASONING_PREFIX):])
        return value if isinstance(value, dict) else None
    except (ValueError, TypeError):
        return None


def _one_line(value, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "\u2026"


def format_task_progress(tasks) -> str:
    """Format the latest task card without relaying generated code or execution details."""
    if not isinstance(tasks, list) or not tasks:
        return ""
    task = tasks[-1] if isinstance(tasks[-1], dict) else {}
    title = _one_line(task.get("title") or "Copilot task", 180)
    state = _one_line(task.get("state") or "In progress", 40).lower()
    lines = [f"Task: {title} ({state})"]
    steps = task.get("steps") if isinstance(task.get("steps"), list) else []
    if steps:
        marks = {"completed": "\u2705", "active": "\U0001f504", "pending": "\u23f3"}
        lines.append("Steps:")
        for step in steps[:20]:
            if not isinstance(step, dict):
                continue
            step_title = _one_line(step.get("title"), 150)
            if step_title:
                lines.append(f"{marks.get(str(step.get('state') or '').lower(), '\u2022')} {step_title}")
    return "\n".join(lines)


def format_reasoning_progress(payload) -> str:
    """Return the latest visible model paragraph, capped for a compact Telegram status."""
    if not isinstance(payload, dict):
        return ""
    paragraph = ""
    blocks = payload.get("blocks") if isinstance(payload.get("blocks"), list) else []
    block = next((item for item in reversed(blocks)
                  if isinstance(item, dict) and item.get("active")), None)
    if block is None:
        block = next((item for item in reversed(blocks) if isinstance(item, dict)), None)
    if block:
        steps = block.get("steps") if isinstance(block.get("steps"), list) else []
        for step in reversed(steps):
            if not isinstance(step, dict) or step.get("kind") == "coding":
                continue
            lines = step.get("lines") if isinstance(step.get("lines"), list) else []
            paragraph = " ".join(str(line) for line in lines if str(line).strip())
            if paragraph:
                break
        if not paragraph:
            lines = block.get("lines") if isinstance(block.get("lines"), list) else []
            paragraph = " ".join(str(line) for line in lines if str(line).strip())
    if not paragraph:
        answers = payload.get("answers") if isinstance(payload.get("answers"), list) else []
        for answer in reversed(answers):
            if isinstance(answer, dict) and answer.get("text"):
                paragraph = str(answer["text"])
                break
    paragraph = _one_line(paragraph, 500)
    return f"Current reasoning: {paragraph}" if paragraph else ""


def _combined_progress(task: str, reasoning: str) -> str:
    details = [part for part in (task, reasoning) if part]
    if not details:
        details.append("Waiting for visible task or reasoning details.")
    return "Copilot is working.\n" + "\n".join(details)


class RoundTrip:
    def __init__(self, cfg: Config):
        self._cfg = cfg

    def command(self, repo_dir, request: str, model: str, show_browser=None, temporary_chat=None):
        show_browser = self._cfg.show_browser if show_browser is None else show_browser
        temporary_chat = self._cfg.temporary_chat if temporary_chat is None else temporary_chat
        cmd = [sys.executable, str(self._cfg.engine), "--profile", str(self._cfg.profile), "fix",
               "--repo", str(repo_dir), "--model", model, "--timeout", str(self._cfg.timeout)]
        if show_browser:
            cmd.append("--show-browser")
        if not temporary_chat:
            cmd.append("--no-temporary-chat")
        return cmd + ["--", request]

    def run(self, repo_dir, request: str, model: str, on_status, cancel: threading.Event,
            show_browser=None, temporary_chat=None) -> dict:
        """Run one round-trip; returns the engine's JSON result. `cancel` requests a graceful stop."""
        command = self.command(repo_dir, request, model, show_browser, temporary_chat)
        proc = subprocess.Popen(command, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
                                env={k: v for k, v in os.environ.items() if k not in SECRET_NAMES})
        tail, out = deque(maxlen=8), []

        def read_stderr():
            task_progress = reasoning_progress = ""
            for line in proc.stderr:
                line = line.rstrip()
                event = parse_status_event(line)
                reasoning = parse_reasoning(line)
                if event:
                    stage, message = str(event["stage"]), str(event["message"])
                    if stage == "task":
                        updated = format_task_progress(event.get("tasks"))
                        changed = updated != task_progress
                        task_progress = updated
                        on_status("task" if changed else "waiting",
                                  _combined_progress(task_progress, reasoning_progress))
                    elif stage == "waiting" and (task_progress or reasoning_progress):
                        on_status("waiting", _combined_progress(task_progress, reasoning_progress))
                    else:
                        on_status(stage, message)
                elif reasoning is not None:
                    updated = format_reasoning_progress(reasoning)
                    if updated and updated != reasoning_progress:
                        reasoning_progress = updated
                        on_status("reasoning", _combined_progress(task_progress, reasoning_progress))
                elif line.strip() and not line.startswith((REASONING_PREFIX, "ROUNDTRIP_CHAT:")):
                    tail.append(line.strip())

        readers = [threading.Thread(target=read_stderr, daemon=True),
                   threading.Thread(target=lambda: out.append(proc.stdout.read()), daemon=True)]
        for reader in readers:
            reader.start()
        quit_sent = None
        while proc.poll() is None:
            if cancel.is_set() and quit_sent is None:
                quit_sent = time.monotonic()
                try:
                    proc.stdin.write("q\n")
                    proc.stdin.flush()
                except OSError:
                    pass
            if quit_sent is not None and time.monotonic() - quit_sent > QUIT_GRACE_SECONDS:
                proc.kill()
            time.sleep(0.25)
        for reader in readers:
            reader.join(timeout=5)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass
        # A run that finished before the stop took effect has already applied its changes.
        if proc.returncode != 0 and (cancel.is_set() or proc.returncode == QUIT_EXIT_CODE):
            raise Cancelled()
        if proc.returncode != 0:
            raise RoundTripError("The Copilot round-trip failed: " + (" | ".join(tail) or f"exit code {proc.returncode}"))
        try:
            return json.loads("".join(out))
        except ValueError:
            raise RoundTripError("The Copilot round-trip returned no valid result.") from None
