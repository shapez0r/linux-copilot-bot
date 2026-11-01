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
TASK_STATUS_LIMIT = 3500
TASK_STATES = frozenset({"completed", "active", "pending", "failed"})
ACTIVITY_KINDS = frozenset({"thought", "completed", "coding"})
CODING_STEP = re.compile(r"^(coding\b|code execution\b|executing code\b|running code\b)", re.I)
THOUGHT_STEP = re.compile(r"^\s*(reasoning|reasoned|thinking|thought|thought for)\b", re.I)


class RoundTripError(Exception):
    """Raised when the engine fails; the message is safe to show to an authorized user."""


class Cancelled(Exception):
    """Raised when an authorized user cancelled the run."""


def parse_status_event(line: str):
    """Return a validated engine progress event, including a structured task plan when present."""
    if not line.startswith(STATUS_PREFIX):
        return None
    try:
        value = json.loads(line[len(STATUS_PREFIX):])
        stage, message = str(value["stage"]), str(value["message"])
        tasks = None
        if isinstance(value.get("tasks"), list):
            tasks = []
            for task in value["tasks"]:
                if not isinstance(task, dict):
                    continue
                state = str(task.get("state") or "In progress").strip()
                steps = []
                for step in task.get("steps") if isinstance(task.get("steps"), list) else []:
                    if not isinstance(step, dict):
                        continue
                    title = " ".join(str(step.get("title") or "").split())
                    if not title:
                        continue
                    step_state = str(step.get("state") or "pending").lower()
                    steps.append({"title": title,
                                  "state": step_state if step_state in TASK_STATES else "pending"})
                tasks.append({
                    "title": " ".join(str(task.get("title") or "Copilot task").split()),
                    "state": state,
                    "active": task.get("active") is True or bool(re.match(r"^in progress$", state, re.I)),
                    "steps": steps,
                })
        return {"stage": stage, "message": message, "tasks": tasks}
    except (ValueError, KeyError, TypeError):
        return None


def parse_status(line: str):
    """Return (stage, message) for an engine progress line, else None."""
    event = parse_status_event(line)
    return (event["stage"], event["message"]) if event else None


def parse_activity_event(line: str):
    """Return activity titles and states without reasoning, answer, or code content."""
    if not line.startswith(REASONING_PREFIX):
        return None
    try:
        value = json.loads(line[len(REASONING_PREFIX):])
        if not isinstance(value, dict) or not isinstance(value.get("blocks"), list):
            return None
        blocks = []
        for raw_block in value["blocks"]:
            if not isinstance(raw_block, dict):
                continue
            steps = []
            for raw_step in raw_block.get("steps") if isinstance(raw_block.get("steps"), list) else []:
                if not isinstance(raw_step, dict):
                    continue
                title = " ".join(str(raw_step.get("title") or "Reasoning").split())
                kind = str(raw_step.get("kind") or "").lower()
                if kind not in ACTIVITY_KINDS:
                    detail_count = len(raw_step.get("lines")) if isinstance(raw_step.get("lines"), list) else 0
                    try:
                        detail_count = max(detail_count, int(raw_step.get("total") or 0))
                    except (TypeError, ValueError):
                        pass
                    kind = ("coding" if CODING_STEP.match(title) else
                            "completed" if not THOUGHT_STEP.match(title) and detail_count == 0 else "thought")
                steps.append({"title": title, "kind": kind})
            blocks.append({
                "title": " ".join(str(raw_block.get("title") or "Reasoning").split()),
                "active": raw_block.get("active") is True,
                "layout": "working" if raw_block.get("layout") == "working" else "section",
                "steps": steps,
            })
        return blocks
    except (TypeError, ValueError):
        return None


def format_prompt_status(message: str, tasks, blocks) -> str:
    """Render task plans and activity titles while omitting generated details and code."""
    lines = []
    if tasks:
        task = tasks[-1]
        title = task["title"] or "Copilot task"
        lines = [f"Prompt execution: {title}", f"Status: {task['state']}"]
        markers = {"completed": "✓", "active": "▶", "pending": "○", "failed": "✗"}
        lines.extend(f"{markers[step['state']]} {step['title']}" for step in task["steps"])
    elif not blocks:
        lines.append("Copilot is executing the prompt." if tasks is None else
                     "Copilot task card closed; waiting for the final response.")
    if blocks:
        if lines:
            lines.append("")
        lines.append("Activity:")
        for block in blocks:
            lines.append(f"{'▶' if block['active'] else '✓'} {block['title']}")
            for step in block["steps"]:
                marker = "✓" if step["kind"] == "completed" else "•"
                lines.append(f"  {marker} {step['title']}")
    timing = re.search(r"\b\d+s elapsed;.*$", message, re.I)
    if timing:
        lines.extend(("", timing.group(0)))
    text = "\n".join(lines)
    if len(text) > TASK_STATUS_LIMIT:
        text = text[:TASK_STATUS_LIMIT - 1].rstrip() + "…"
    return text


def format_task_status(message: str, tasks) -> str:
    """Render the latest Copilot task card as a bounded plain-text Telegram status."""
    return format_prompt_status(message, tasks, [])


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
            live_tasks, live_blocks, task_message = None, [], ""
            for line in proc.stderr:
                line = line.rstrip()
                status = parse_status_event(line)
                if status:
                    if status["stage"] == "task":
                        live_tasks, task_message = status["tasks"], status["message"]
                        message = format_prompt_status(task_message, live_tasks, live_blocks)
                    else:
                        message = status["message"]
                    on_status(status["stage"], message)
                    continue
                blocks = parse_activity_event(line)
                if blocks is not None:
                    live_blocks = blocks
                    if live_blocks or live_tasks:
                        on_status("activity", format_prompt_status(task_message, live_tasks, live_blocks))
                elif line.strip() and not line.startswith("ROUNDTRIP_CHAT:"):
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
