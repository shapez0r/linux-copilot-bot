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
REASONING_TAIL = 10
ANSWER_MAX_CHARS = 4000


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
                if isinstance(task.get("response"), str) and task["response"].strip():
                    tasks[-1]["response"] = task["response"].strip()
        return {"stage": stage, "message": message, "tasks": tasks}
    except (ValueError, KeyError, TypeError):
        return None


def parse_status(line: str):
    """Return (stage, message) for an engine progress line, else None."""
    event = parse_status_event(line)
    return (event["stage"], event["message"]) if event else None


def parse_live_event(line: str):
    """Return the extension-compatible activity and live-answer event."""
    if not line.startswith(REASONING_PREFIX):
        return None
    try:
        value = json.loads(line[len(REASONING_PREFIX):])
        if not isinstance(value, dict) or not isinstance(value.get("blocks"), list):
            return None
        blocks = []
        for index, raw_block in enumerate(value["blocks"]):
            if not isinstance(raw_block, dict):
                continue
            block_lines = [str(item) for item in raw_block.get("lines", [])] \
                if isinstance(raw_block.get("lines"), list) else []
            block_lines = block_lines[-REASONING_TAIL:]
            steps = []
            for raw_step in raw_block.get("steps") if isinstance(raw_block.get("steps"), list) else []:
                if not isinstance(raw_step, dict):
                    continue
                title = " ".join(str(raw_step.get("title") or "Reasoning").split())
                kind = str(raw_step.get("kind") or "").lower()
                detail_lines = [str(item) for item in raw_step.get("lines", [])] \
                    if isinstance(raw_step.get("lines"), list) else []
                detail_lines = detail_lines[-REASONING_TAIL:]
                try:
                    detail_count = max(len(detail_lines), int(raw_step.get("total") or 0))
                except (TypeError, ValueError):
                    detail_count = len(detail_lines)
                if kind not in ACTIVITY_KINDS:
                    kind = ("coding" if CODING_STEP.match(title) else
                            "completed" if not THOUGHT_STEP.match(title) and detail_count == 0 else "thought")
                if kind == "coding":
                    steps.append({"title": title, "kind": kind, "total": 0, "lines": [], "hidden": True})
                else:
                    steps.append({"title": title, "kind": kind, "total": detail_count,
                                  "lines": detail_lines})
            try:
                block_total = max(len(block_lines), int(raw_block.get("total") or 0))
            except (TypeError, ValueError):
                block_total = len(block_lines)
            blocks.append({
                "index": index,
                "title": " ".join(str(raw_block.get("title") or "Reasoning").split()),
                "active": raw_block.get("active") is True,
                "layout": "working" if raw_block.get("layout") == "working" else "section",
                "total": block_total,
                "lines": block_lines,
                "steps": steps,
            })
        answers = []
        for answer in value.get("answers") if isinstance(value.get("answers"), list) else []:
            if not isinstance(answer, dict) or not isinstance(answer.get("text"), str):
                continue
            text = answer["text"].strip()
            if not text:
                continue
            if len(text) > ANSWER_MAX_CHARS:
                text = "…" + text[-ANSWER_MAX_CHARS:]
            try:
                after = int(float(answer.get("after") or 0))
            except (TypeError, ValueError):
                after = 0
            answers.append({"after": max(0, min(len(blocks), after)), "text": text})
        return {"blocks": blocks, "answers": answers}
    except (TypeError, ValueError):
        return None


def parse_activity_event(line: str):
    """Return activity titles and states without detail text."""
    live = parse_live_event(line)
    if live is None:
        return None
    return [{"title": block["title"], "active": block["active"], "layout": block["layout"],
             "steps": [{"title": step["title"], "kind": step["kind"]} for step in block["steps"]]}
            for block in live["blocks"]]


def _section_steps(block):
    if block.get("steps"):
        return block["steps"]
    if block.get("lines"):
        return [{"title": "Reasoning", "kind": "thought", "lines": block["lines"],
                 "total": block.get("total", len(block["lines"]))}]
    return []


def _working_summary(blocks):
    completed, current = "", ""
    for block in blocks:
        details = _section_steps(block)
        finished = [detail for detail in details if detail.get("kind") == "completed"]
        if finished:
            completed = finished[-1].get("title") or completed
        elif not block.get("active"):
            completed = (details[-1].get("title") if details else block.get("title")) or completed
        if block.get("active"):
            current = (details[-1].get("title") if details else block.get("title")) or current
    return completed or current or "Reasoning"


def _append_answer(lines, text):
    if lines:
        lines.append("")
    lines.extend(text.splitlines())


def format_prompt_status(message: str, tasks, blocks, answers=None) -> str:
    """Render the same task, activity, and live-answer ordering as the extension."""
    lines, answers = [], answers or []
    by_after = {}
    for answer in answers:
        after = max(0, min(len(blocks), int(answer.get("after") or 0)))
        by_after[after] = by_after.get(after, "") + ("\n\n" if after in by_after else "") + answer["text"]

    if not tasks and not blocks:
        lines.append(message if by_after and message else
                     "Copilot is executing the prompt." if tasks is None else
                     "Copilot task card closed; waiting for the final response.")
        if 0 in by_after:
            _append_answer(lines, by_after[0])
        return _limit_status(lines)

    if 0 in by_after:
        _append_answer(lines, by_after[0])
    if tasks:
        task = tasks[-1]
        title = task["title"] or "Copilot task"
        if lines:
            lines.append("")
        marker = ("▶" if task.get("active") else "×" if re.search(r"stop|cancel", task["state"], re.I)
                  else "✗" if re.search(r"fail", task["state"], re.I)
                  else "✓" if re.search(r"complete|done", task["state"], re.I) else "○")
        lines.extend((f"{marker} {title}", task["state"]))
        markers = {"completed": "✓", "active": "▶", "pending": "○", "failed": "✗"}
        lines.extend(f"  {markers[step['state']]} {step['title']}" for step in task["steps"])
        if task.get("response"):
            _append_answer(lines, task["response"])

    index = 0
    while index < len(blocks):
        if lines:
            lines.append("")
        block = blocks[index]
        if block.get("layout") == "working":
            start = index
            index += 1
            while (index < len(blocks) and blocks[index].get("layout") == "working" and
                   index not in by_after):
                index += 1
            group = blocks[start:index]
            active = any(item.get("active") for item in group)
            lines.append(f"{'▶' if active else '✓'} Working through the steps: {_working_summary(group)}")
        else:
            index += 1
            lines.append(f"{'▶' if block.get('active') else '✓'} {block.get('title') or 'Reasoning'}")
            for step in _section_steps(block):
                kind = step.get("kind")
                lines.append(f"  {'✓' if kind == 'completed' else '•'} {step.get('title') or 'Reasoning'}")
                if kind == "thought" and block.get("active"):
                    lines.extend(f"    {detail}" for detail in step.get("lines", []))
        if index in by_after:
            _append_answer(lines, by_after[index])
    return _limit_status(lines)


def _limit_status(lines):
    text = "\n".join(lines)
    if len(text) > TASK_STATUS_LIMIT:
        text = "…" + text[-(TASK_STATUS_LIMIT - 1):].lstrip()
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
            live_tasks, live, current_message = None, {"blocks": [], "answers": []}, ""
            for line in proc.stderr:
                line = line.rstrip()
                status = parse_status_event(line)
                if status:
                    current_message = status["message"]
                    if status["tasks"] is not None:
                        live_tasks = status["tasks"]
                    if status["stage"] == "completed":
                        if live_tasks:
                            live_tasks = [task if re.search(r"stop|cancel|fail", task["state"], re.I) else
                                          {**task, "state": "Completed", "active": False,
                                           "steps": [{**step, "state": "completed"}
                                                     if step["state"] != "failed" else step
                                                     for step in task["steps"]]}
                                          for task in live_tasks]
                        live = {"blocks": [{**block, "active": False} for block in live["blocks"]],
                                "answers": live["answers"]}
                    if live_tasks or live["blocks"] or live["answers"]:
                        message = format_prompt_status(current_message, live_tasks, live["blocks"], live["answers"])
                    else:
                        message = current_message
                    on_status(status["stage"], message)
                    continue
                event = parse_live_event(line)
                if event is not None:
                    live = event
                    if live["blocks"] or live["answers"] or live_tasks:
                        on_status("activity", format_prompt_status(
                            current_message, live_tasks, live["blocks"], live["answers"]))
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
