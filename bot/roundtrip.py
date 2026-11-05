"""Runs the Copilot round-trip engine as a subprocess and relays its progress."""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

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
ANSWER_MAX_CHARS = 4000
# Fenced code (closed, or still streaming at the end of the text) is not progress output.
CODE_FENCE = re.compile(r"^[ \t]*(```|~~~)[^\n]*\n.*?(?:^[ \t]*\1[ \t]*$|\Z)", re.M | re.S)
CURRENT_STATUS_MAX_CHARS = 500
# Latest characters of one reasoning passage kept in its collapsible quote.
REASONING_MAX_CHARS = 1500
REASONING_ICON = "💭"
HEARTBEAT_STAGE = "heartbeat"
HEARTBEAT_ICON = "⏳"
# Engine stages that keep the latest heartbeat line while Copilot is still responding.
HEARTBEAT_KEEP_STAGES = frozenset({HEARTBEAT_STAGE, "task"})
# Chromium's single-instance locks and rebuildable caches are not copied into a parallel session's profile.
PROFILE_COPY_IGNORE = shutil.ignore_patterns(
    "Singleton*", "Cache", "Code Cache", "GPUCache", "GrShaderCache", "GraphiteDawnCache", "ShaderCache",
    "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache", "Crashpad")


class RoundTripError(Exception):
    """Raised when the engine fails; the message is safe to show to an authorized user."""


class Cancelled(Exception):
    """Raised when an authorized user cancelled the run."""


def format_timestamp(moment=None) -> str:
    """Return the local time as hours:minutes:seconds."""
    moment = moment or datetime.now().astimezone()
    return f"{moment:%H:%M:%S}"


class _Reasoning(str):
    """Reasoning text rendered as a collapsed Telegram quote instead of a plain status line."""


class StatusTimeline:
    """Keep first-seen stage timestamps stable while an edited status message evolves."""

    def __init__(self, clock=None):
        self._clock = clock
        self._timestamps = {}
        self._states = {}
        self.current = ""

    def _now(self):
        value = self._clock() if self._clock else format_timestamp()
        return format_timestamp(value) if isinstance(value, datetime) else str(value)

    def stage(self, key, label: str, state: str = "", current: bool = False) -> str:
        normalized = " ".join(str(label).split())
        new = key not in self._timestamps
        if new:
            self._timestamps[key] = self._now()
        previous = self._states.get(key)
        self._states[key] = state
        if current and normalized and (new or previous != state):
            self.current = normalized
        return self._timestamps[key]

    def now(self) -> str:
        """Return the current timestamp in the timeline's format."""
        return self._now()

    def detail(self, key, text: str):
        normalized = _current_text(text)
        if normalized and key not in self._timestamps:
            self._timestamps[key] = self._now()
            self.current = normalized


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
    """Return activity titles, the latest visible reasoning paragraph and interim answers."""
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
            block_line_count = len(raw_block.get("lines", [])) \
                if isinstance(raw_block.get("lines"), list) else 0
            steps = []
            for raw_step in raw_block.get("steps") if isinstance(raw_block.get("steps"), list) else []:
                if not isinstance(raw_step, dict):
                    continue
                title = " ".join(str(raw_step.get("title") or "Reasoning").split())
                kind = str(raw_step.get("kind") or "").lower()
                detail_line_count = len(raw_step.get("lines", [])) \
                    if isinstance(raw_step.get("lines"), list) else 0
                try:
                    detail_count = max(detail_line_count, int(raw_step.get("total") or 0))
                except (TypeError, ValueError):
                    detail_count = detail_line_count
                if kind not in ACTIVITY_KINDS:
                    kind = ("coding" if CODING_STEP.match(title) else
                            "completed" if not THOUGHT_STEP.match(title) and detail_count == 0 else "thought")
                step = {"title": title, "kind": kind}
                reasoning = _reasoning_lines(raw_step.get("lines")) if kind == "thought" else []
                if reasoning:
                    step["reasoning"] = reasoning
                if raw_block.get("active") is True and kind == "thought" and THOUGHT_STEP.match(title):
                    current = _last_visible_line(raw_step.get("lines"))
                    if current:
                        step["current"] = current
                steps.append(step)
            try:
                block_total = max(block_line_count, int(raw_block.get("total") or 0))
            except (TypeError, ValueError):
                block_total = block_line_count
            block = {
                "index": index,
                "title": " ".join(str(raw_block.get("title") or "Reasoning").split()),
                "active": raw_block.get("active") is True,
                "layout": "working" if raw_block.get("layout") == "working" else "section",
                "total": block_total,
                "steps": steps,
            }
            if not steps and not CODING_STEP.match(block["title"]):
                reasoning = _reasoning_lines(raw_block.get("lines"))
                if reasoning:
                    block["reasoning"] = reasoning
            current = next((step.get("current") for step in reversed(steps) if step.get("current")), "")
            if block["active"] and not current and THOUGHT_STEP.match(block["title"]):
                current = _last_visible_line(raw_block.get("lines"))
            if current:
                block["current"] = current
            blocks.append(block)
        answers = []
        for answer in value.get("answers") if isinstance(value.get("answers"), list) else []:
            if not isinstance(answer, dict) or not isinstance(answer.get("text"), str):
                continue
            text = _without_code(answer["text"])
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


def _without_code(text: str) -> str:
    text = CODE_FENCE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_activity_event(line: str):
    """Return activity titles and states without detail text."""
    live = parse_live_event(line)
    if live is None:
        return None
    return [{"title": block["title"], "active": block["active"], "layout": block["layout"],
             "steps": [{"title": step["title"], "kind": step["kind"]} for step in block["steps"]]}
            for block in live["blocks"]]


def _section_steps(block):
    return block.get("steps") or []


def _last_visible_line(value):
    if not isinstance(value, list):
        return ""
    return next((_current_text(line) for line in reversed(value)
                 if isinstance(line, str) and _current_text(line)), "")


def _reasoning_lines(value):
    if not isinstance(value, list):
        return []
    lines = (" ".join(line.split()) for line in value if isinstance(line, str))
    return [line for line in lines if line]


def _append_reasoning(lines, reasoning):
    """Add reasoning lines as one collapsible quote, keeping the latest text when it is long."""
    text = "\n".join(reasoning)
    if not text:
        return
    if len(text) > REASONING_MAX_CHARS:
        text = "…" + text[-(REASONING_MAX_CHARS - 1):].lstrip()
    lines.append(_Reasoning(text))


def _current_text(value) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > CURRENT_STATUS_MAX_CHARS:
        text = text[:CURRENT_STATUS_MAX_CHARS - 1].rstrip() + "…"
    return text


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
    return current or completed or "Reasoning"


def _append_answer(lines, text):
    if lines:
        lines.append("")
    lines.extend(text.splitlines())


def format_prompt_status(message: str, tasks, blocks, answers=None, timeline=None, stage="status",
                         heartbeat=None) -> str:
    """Render a timestamped activity timeline as Telegram HTML; reasoning text is shown collapsed.

    heartbeat is an optional (timestamp, message) pair shown as the last line, refreshed on every
    engine heartbeat so the user can see that the prompt is still running.
    """
    lines, answers = [], answers or []
    timeline = timeline or StatusTimeline()
    by_after = {}
    for answer in answers:
        after = max(0, min(len(blocks), int(answer.get("after") or 0)))
        by_after[after] = by_after.get(after, "") + ("\n\n" if after in by_after else "") + answer["text"]

    if not tasks and not blocks:
        status = (message if message and (by_after or stage != "status") else
                  "Copilot is executing the prompt." if tasks is None else
                  "Copilot task card closed; waiting for the final response.")
        stamp = timeline.stage(("status", stage, status), status, stage, current=True)
        lines.append(f"[{stamp}] {status}")
        if 0 in by_after:
            _append_answer(lines, by_after[0])
        _append_heartbeat(lines, heartbeat)
        return _limit_status(lines, timeline.current)

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
        active_step = any(step["state"] == "active" for step in task["steps"])
        task_current = (task.get("active", False) and not active_step) or (not task["steps"] and not timeline.current)
        task_stamp = timeline.stage(("task", title), title, task["state"], current=task_current)
        lines.extend((f"[{task_stamp}] {marker} {title}", task["state"]))
        markers = {"completed": "✓", "active": "▶", "pending": "○", "failed": "✗"}
        for position, step in enumerate(task["steps"]):
            step_current = step["state"] == "active" or (
                not task.get("active") and not timeline.current and position == len(task["steps"]) - 1)
            step_stamp = timeline.stage(("task-step", title, position, step["title"]), step["title"],
                                        step["state"], current=step_current)
            lines.append(f"  [{step_stamp}] {markers[step['state']]} {step['title']}")
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
            summary = _working_summary(group)
            stamp = timeline.stage(("working", start, summary), summary,
                                   "active" if active else "completed",
                                   current=active or not timeline.current)
            lines.append(f"[{stamp}] {'▶' if active else '✓'} Working through the steps: {summary}")
            _append_reasoning(lines, [line for item in group
                                      for source in ([item] + _section_steps(item))
                                      for line in source.get("reasoning") or []])
            current_block = next((item for item in reversed(group) if item.get("active")), None)
            if current_block and current_block.get("current"):
                timeline.detail(("reasoning", start, current_block["current"]), current_block["current"])
        else:
            index += 1
            title = block.get("title") or "Reasoning"
            steps = _section_steps(block)
            block_current = (block.get("active", False) and not steps) or (not steps and not timeline.current)
            stamp = timeline.stage(("block", block.get("index", index - 1), title), title,
                                   "active" if block.get("active") else "completed", current=block_current)
            lines.append(f"[{stamp}] {'▶' if block.get('active') else '✓'} {title}")
            _append_reasoning(lines, block.get("reasoning") or [])
            for position, step in enumerate(steps):
                kind = step.get("kind")
                step_title = step.get("title") or "Reasoning"
                step_current = position == len(steps) - 1 and (block.get("active", False) or not timeline.current)
                step_stamp = timeline.stage(("block-step", block.get("index", index - 1), position, step_title),
                                            step_title, kind or "thought",
                                            current=step_current)
                lines.append(f"  [{step_stamp}] {'✓' if kind == 'completed' else '•'} {step_title}")
                _append_reasoning(lines, step.get("reasoning") or [])
            if block.get("active") and block.get("current"):
                timeline.detail(("reasoning", block.get("index", index - 1), block["current"]), block["current"])
        if index in by_after:
            _append_answer(lines, by_after[index])
    _append_heartbeat(lines, heartbeat)
    return _limit_status(lines, timeline.current)


def _append_heartbeat(lines, heartbeat):
    if heartbeat:
        stamp, text = heartbeat
        if lines:
            lines.append("")
        lines.append(f"[{stamp}] {HEARTBEAT_ICON} {text}")


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def _render_line(line) -> str:
    """Return Telegram HTML for one status line; reasoning is an expandable blockquote behind an icon."""
    if isinstance(line, _Reasoning):
        return f"<blockquote expandable>{REASONING_ICON}\n{_escape(line)}</blockquote>"
    return _escape(line)


def _visible_length(line) -> int:
    return len(REASONING_ICON) + 1 + len(line) if isinstance(line, _Reasoning) else len(line)


def _limit_status(lines, current):
    """Render the status as Telegram HTML, dropping the oldest lines beyond the visible-text limit."""
    header = f"Currently doing: {_current_text(current) or 'Waiting for Copilot'}"
    available = TASK_STATUS_LIMIT - len(header) - 2
    lines = list(lines)
    size = sum(_visible_length(line) for line in lines) + max(len(lines) - 1, 0)
    if size > available:
        size += 2  # leading ellipsis line
        while len(lines) > 1 and size - _visible_length(lines[0]) - 1 >= available:
            size -= _visible_length(lines.pop(0)) + 1
        excess = size - available
        if excess > 0:
            lines[0] = type(lines[0])(lines[0][excess:].lstrip())
        while lines and not lines[0]:
            lines.pop(0)
        lines.insert(0, "…")
    body = "\n".join(_render_line(line) for line in lines)
    return _escape(header) + ("\n\n" + body if body else "")


def format_stage_status(message: str, timeline=None, stage="job") -> str:
    """Format a single bot stage for the shared edited progress message."""
    timeline = timeline or StatusTimeline()
    stamp = timeline.stage(("bot-stage", stage, message), message, stage, current=True)
    return _limit_status([f"[{stamp}] {message}"], timeline.current)


def format_task_status(message: str, tasks) -> str:
    """Render the latest Copilot task card as a bounded Telegram HTML status."""
    return format_prompt_status(message, tasks, [])


class RoundTrip:
    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._profile_lock = threading.Lock()
        self._profile_busy = False

    @contextmanager
    def browser_profile(self):
        """Yield a Copilot browser profile that no other engine process uses.

        Chromium allows one process per profile. The first engine run uses the signed-in profile itself,
        so refreshed sign-in state is kept; runs in parallel with it use a private temporary copy that is
        deleted afterwards.
        """
        with self._profile_lock:
            primary, self._profile_busy = not self._profile_busy, True
        if primary:
            try:
                yield self._cfg.profile
            finally:
                with self._profile_lock:
                    self._profile_busy = False
            return
        self._cfg.state_dir.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="profile-", dir=self._cfg.state_dir))
        try:
            copy = work / "profile"
            try:
                shutil.copytree(self._cfg.profile, copy, symlinks=True, ignore=PROFILE_COPY_IGNORE)
            except shutil.Error:
                pass  # Files that the running browser changed or removed during the copy are skipped.
            except OSError as error:
                raise RoundTripError("Cannot copy the Copilot browser profile for a parallel session: "
                                     f"{error.strerror or error}") from None
            yield copy
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def command(self, repo_dir, request: str, model: str, show_browser=None, temporary_chat=None,
                attachments=(), history=True, profile=None):
        show_browser = self._cfg.show_browser if show_browser is None else show_browser
        temporary_chat = self._cfg.temporary_chat if temporary_chat is None else temporary_chat
        cmd = [sys.executable, str(self._cfg.engine), "--profile", str(profile or self._cfg.profile), "fix",
               "--repo", str(repo_dir), "--model", model]
        if show_browser:
            cmd.append("--show-browser")
        if not temporary_chat:
            cmd.append("--no-temporary-chat")
        if not history:
            cmd.append("--no-history")
        for path in attachments or ():
            cmd.extend(["--attach", str(path)])
        return cmd + ["--", request]

    def run(self, repo_dir, request: str, model: str, on_status, cancel: threading.Event,
            show_browser=None, temporary_chat=None, attachments=(), history=True) -> dict:
        """Run one round-trip with optional extra files attached to the prompt; returns the engine's
        JSON result. `cancel` requests a graceful stop; history=False uploads no Git history."""
        with self.browser_profile() as profile:
            command = self.command(repo_dir, request, model, show_browser, temporary_chat, attachments, history,
                                   profile)
            return self._run_engine(command, on_status, cancel)

    def _run_engine(self, command, on_status, cancel: threading.Event) -> dict:
        proc = subprocess.Popen(command, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
                                env={k: v for k, v in os.environ.items() if k not in SECRET_NAMES})
        tail, out = deque(maxlen=8), []

        def read_stderr():
            live_tasks, live, current_message = None, {"blocks": [], "answers": []}, ""
            timeline, heartbeat, current_stage = StatusTimeline(), None, "status"
            for line in proc.stderr:
                line = line.rstrip()
                status = parse_status_event(line)
                if status:
                    if status["stage"] == HEARTBEAT_STAGE:
                        heartbeat = (timeline.now(), _current_text(status["message"]))
                        on_status(HEARTBEAT_STAGE, format_prompt_status(
                            current_message, live_tasks, live["blocks"], live["answers"], timeline,
                            current_stage, heartbeat))
                        continue
                    if status["stage"] not in HEARTBEAT_KEEP_STAGES:
                        heartbeat = None
                    current_message, current_stage = status["message"], status["stage"]
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
                    message = format_prompt_status(current_message, live_tasks, live["blocks"],
                                                   live["answers"], timeline, status["stage"], heartbeat)
                    on_status(status["stage"], message)
                    continue
                event = parse_live_event(line)
                if event is not None:
                    live = event
                    if live["blocks"] or live["answers"] or live_tasks:
                        on_status("activity", format_prompt_status(
                            current_message, live_tasks, live["blocks"], live["answers"],
                            timeline, "activity", heartbeat))
                elif line.strip() and not line.startswith("ROUNDTRIP_CHAT:"):
                    tail.append(line.strip())

        return self._finish(proc, read_stderr, out, tail, cancel, "The Copilot round-trip")

    def models_command(self, show_browser=None, temporary_chat=None, profile=None):
        show_browser = self._cfg.show_browser if show_browser is None else show_browser
        temporary_chat = self._cfg.temporary_chat if temporary_chat is None else temporary_chat
        cmd = [sys.executable, str(self._cfg.engine), "--profile", str(profile or self._cfg.profile), "models"]
        if show_browser:
            cmd.append("--show-browser")
        if not temporary_chat:
            cmd.append("--no-temporary-chat")
        return cmd

    def list_models(self, on_status, cancel: threading.Event) -> list[str]:
        """Return the provider/model pairs currently offered in Copilot's Claude and GPT submenus."""
        with self.browser_profile() as profile:
            proc = subprocess.Popen(self.models_command(profile=profile), stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
                                    env={k: v for k, v in os.environ.items() if k not in SECRET_NAMES})
            tail, out = deque(maxlen=8), []

            def read_stderr():
                for line in proc.stderr:
                    line = line.rstrip()
                    status = parse_status(line)
                    if status:
                        on_status(*status)
                    elif line.strip():
                        tail.append(line.strip())

            result = self._finish(proc, read_stderr, out, tail, cancel, "The Copilot model check")
        models = result.get("models") if isinstance(result, dict) else None
        if not isinstance(models, list):
            raise RoundTripError("The Copilot model check returned no model list.")
        valid = [" ".join(str(model).split()) for model in models]
        return list(dict.fromkeys(model for model in valid
                                  if model.count("/") == 1 and all(part.strip() for part in model.split("/"))))

    @staticmethod
    def _finish(proc, read_stderr, out, tail, cancel: threading.Event, label: str):
        """Relay engine output, forward a cancel request, and parse the JSON result."""
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
            raise RoundTripError(f"{label} failed: " + (" | ".join(tail) or f"exit code {proc.returncode}"))
        try:
            return json.loads("".join(out))
        except ValueError:
            raise RoundTripError(f"{label} returned no valid result.") from None
