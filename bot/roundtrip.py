"""Runs the Copilot round-trip engine as a subprocess and relays its progress."""
from __future__ import annotations

import json
import os
import re
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
LIVE_TEXT_LIMIT = 500
_CODING_STEP = re.compile(r"^(coding\b|code execution\b|executing code\b|running code\b)", re.I)
_THOUGHT_STEP = re.compile(r"^\s*(reasoning|reasoned|thinking|thought|thought for)\b", re.I)


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
        marks = {"completed": "\u2705", "active": "\U0001f504", "pending": "\u23f3",
                 "failed": "\u274c"}
        lines.append("Steps:")
        for step in steps[:20]:
            if not isinstance(step, dict):
                continue
            step_title = _one_line(step.get("title"), 150)
            if step_title:
                lines.append(f"{marks.get(str(step.get('state') or '').lower(), '\u2022')} {step_title}")
    return "\n".join(lines)


def _section_steps(block):
    """Normalize visible activity steps using the extension's display rules."""
    steps = []
    for value in block.get("steps") if isinstance(block.get("steps"), list) else []:
        if not isinstance(value, dict):
            continue
        title = _one_line(value.get("title"), 180)
        lines = value.get("lines") if isinstance(value.get("lines"), list) else []
        try:
            total = max(int(value.get("total") or 0), len(lines))
        except (TypeError, ValueError):
            total = len(lines)
        supplied = str(value.get("kind") or "").lower()
        if supplied == "coding" or _CODING_STEP.match(title):
            kind = "coding"
            lines = []
        elif supplied == "completed" or (not _THOUGHT_STEP.match(title) and total == 0):
            kind = "completed"
            lines = []
        else:
            kind = "thought"
        steps.append({"title": title or "Reasoning", "kind": kind, "lines": lines})
    if steps:
        return steps
    lines = block.get("lines") if isinstance(block.get("lines"), list) else []
    return [{"title": "Reasoning", "kind": "thought", "lines": lines}] if lines else []


def _working_summary(blocks) -> str:
    """Keep the latest completed activity title while the next step is running."""
    completed = current = ""
    for block in blocks:
        if not isinstance(block, dict):
            continue
        steps = _section_steps(block)
        finished = [step for step in steps if step["kind"] == "completed"]
        if finished:
            completed = finished[-1]["title"] or completed
        elif not block.get("active"):
            completed = ((steps[-1]["title"] if steps else _one_line(block.get("title"), 180))
                         or completed)
        if block.get("active"):
            current = ((steps[-1]["title"] if steps else _one_line(block.get("title"), 180))
                       or current)
    return completed or current or "Reasoning"


def _latest_visible_prose(blocks) -> str:
    block = next((item for item in reversed(blocks)
                  if isinstance(item, dict) and item.get("active")), None)
    if block is None:
        block = next((item for item in reversed(blocks) if isinstance(item, dict)), None)
    if not block:
        return ""
    for step in reversed(_section_steps(block)):
        if step["kind"] != "thought":
            continue
        paragraph = " ".join(str(line) for line in step["lines"] if str(line).strip())
        if paragraph:
            return _one_line(paragraph, LIVE_TEXT_LIMIT)
    lines = block.get("lines") if isinstance(block.get("lines"), list) else []
    return _one_line(" ".join(str(line) for line in lines if str(line).strip()), LIVE_TEXT_LIMIT)


def format_reasoning_progress(payload) -> str:
    """Format structured activity and live prose for the edited Telegram status."""
    if not isinstance(payload, dict):
        return ""
    blocks = payload.get("blocks") if isinstance(payload.get("blocks"), list) else []
    lines = []
    if blocks:
        if any(isinstance(block, dict) and block.get("layout") == "working" for block in blocks):
            lines.append(f"Working through the steps: {_working_summary(blocks)}")
        else:
            block = next((item for item in reversed(blocks)
                          if isinstance(item, dict) and item.get("active")), None)
            if block is None:
                block = next((item for item in reversed(blocks) if isinstance(item, dict)), None)
            if block:
                label = "Current activity" if block.get("active") else "Latest activity"
                lines.append(f"{label}: {_one_line(block.get('title') or 'Reasoning', 180)}")
        paragraph = _latest_visible_prose(blocks)
        if paragraph:
            lines.append(f"Current reasoning: {paragraph}")
    answers = payload.get("answers") if isinstance(payload.get("answers"), list) else []
    answer = next((str(item["text"]) for item in reversed(answers)
                   if isinstance(item, dict) and str(item.get("text") or "").strip()), "")
    answer = _one_line(answer, LIVE_TEXT_LIMIT)
    if answer:
        lines.append(f"Latest response: {answer}")
    return "\n".join(lines)


def _combined_progress(task: str, reasoning: str, heading: str = "Copilot is working.") -> str:
    details = [part for part in (task, reasoning) if part]
    if not details:
        details.append("Waiting for visible task or reasoning details.")
    return heading + "\n" + "\n".join(details)


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
            task_progress = live_progress = ""
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
                                  _combined_progress(task_progress, live_progress))
                    elif stage == "waiting" and (task_progress or live_progress):
                        on_status("waiting", _combined_progress(task_progress, live_progress, message))
                    else:
                        on_status(stage, message)
                elif reasoning is not None:
                    updated = format_reasoning_progress(reasoning)
                    if updated != live_progress:
                        live_progress = updated
                        on_status("reasoning", _combined_progress(task_progress, live_progress))
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
