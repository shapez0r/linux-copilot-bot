#!/usr/bin/env python3
"""Archive committed Git files, request Copilot changes, and safely replace the worktree."""
from __future__ import annotations
import argparse, atexit, base64, hashlib, json, os, re, shutil, subprocess, sys, tempfile, threading, time, zipfile
from pathlib import Path, PurePosixPath

DEFAULT_URL = "https://m365.cloud.microsoft/chat/"
BASE_PROMPT = (
    "Here is my repository archive. The attached .txt file contains ZIP bytes. "
    "Rename it to .zip and extract it. Make only the requested fixes, preserve all other files, "
    "then return the complete fixed repository as a downloadable ZIP file. "
    "Do not include a .git directory. Read and follow AGENTS.md and "
    ".github/copilot-instructions.md when present. After changes, review README files and "
    "comments in all repository files: keep README content logically correct and aligned "
    "with the repository state, and comments concise, with no historical references. "
    "To fix: {request}"
)
# Attach the current repository snapshot to every saved-chat follow-up.
FOLLOWUP_PROMPT = (
    "Here is the current state of my repository archive for this follow-up request. It supersedes any "
    "earlier archive in this chat (your sandbox was reset, so earlier files are no longer available). "
    "The attached .txt file contains ZIP bytes. "
    "Rename it to .zip and extract it. Make only the requested fixes, preserve all other files, "
    "then return the complete fixed repository as a downloadable ZIP file. "
    "Do not include a .git directory. Read and follow AGENTS.md and "
    ".github/copilot-instructions.md when present. After changes, review README files and "
    "comments in all repository files: keep README content logically correct and aligned "
    "with the repository state, and comments concise, with no historical references. "
    "To fix: {request}"
)


ATTACHMENTS_NOTE = (
    "\n\nAdditional files are attached as context for this request "
    "(they are not part of the repository): {names}"
)
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic"}


def resolve_attachments(paths):
    """Validate extra files (images, documents, ...) to attach alongside the archive."""
    result = []
    for raw in paths or []:
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            raise SystemExit(f"Attachment not found or not a file: {raw}")
        if path not in result:
            result.append(path)
    return result


CHAT_PREFIX = "ROUNDTRIP_CHAT:"


def emit_chat(url):
    """Report the saved Copilot chat URL so later prompts can continue the same conversation."""
    print(CHAT_PREFIX + json.dumps({"url": url}), file=sys.stderr, flush=True)


def validate_chat_url(url):
    """A chat to continue must be an https Copilot conversation URL."""
    from urllib.parse import urlparse
    parsed = urlparse(url or "")
    if parsed.scheme != "https" or not parsed.netloc:
        raise SystemExit(f"Invalid --chat-url (an https Copilot chat URL is required): {url}")
    return url


def conversation_url(current, base):
    """The page URL once Copilot has created a saved conversation, or None while it is still the start page."""
    from urllib.parse import urlparse
    cur, start = urlparse(current or ""), urlparse(base or "")
    if cur.scheme != "https" or not cur.netloc or cur.netloc != start.netloc:
        return None
    if (cur.path.rstrip("/"), cur.query, cur.fragment) == (start.path.rstrip("/"), start.query, start.fragment):
        return None
    return current


def emit_status(stage, message, tasks=None):
    """Line-delimited progress on stderr; stdout remains the final JSON result."""
    payload = {"stage": stage, "message": message}
    if tasks is not None:
        payload["tasks"] = tasks
    print("ROUNDTRIP_STATUS:" + json.dumps(payload),
          file=sys.stderr, flush=True)

# Time given to the Copilot web UI to finish rendering after navigation (visible and hidden browser).
UI_SETTLE_MS = 2500
# Number of most recent reasoning lines streamed for each reasoning block.
REASONING_TAIL_LINES = 10
REASONING_PREFIX = "ROUNDTRIP_REASONING:"
# Maximum characters kept (latest part) of each live intermediate answer segment.
ANSWER_MAX_CHARS = 4000
# Selectors for Copilot assistant messages (live answers and the final chat response).
ASSISTANT_SELECTORS = (
    '[data-message-author-role="assistant"]',
    '[data-role="assistant"]',
    '[data-testid*="assistant-message" i]',
    '[data-testid*="copilot-message" i]',
    '[class*="assistant-message" i]',
)


def emit_reasoning(blocks, answers=None):
    """Stream reasoning blocks and answers as JSON lines; answer.after counts preceding blocks."""
    print(REASONING_PREFIX + json.dumps({"blocks": blocks, "answers": answers or []}),
          file=sys.stderr, flush=True)


# Screen-reader labels Copilot puts in front of messages ("Copilot said:", "You said:").
_SPEAKER_LABEL = re.compile(r"^\s*(copilot|you)\s+said\s*:?\s*", re.I)


def strip_speaker_label(text):
    "Remove a leading 'Copilot said:' style label; a label-only text becomes empty."
    text = (text or "").strip()
    while True:
        stripped = _SPEAKER_LABEL.sub("", text, count=1).strip()
        if stripped == text:
            return text
        text = stripped


def merge_live(prev_blocks, prev_answers, blocks, answers, final=False):
    """Merge streamed steps and answers; final visible answers take precedence when available."""
    prev_blocks = prev_blocks or []
    merged_blocks = []
    for i, block in enumerate(blocks or []):
        block = dict(block)
        old = prev_blocks[i] if i < len(prev_blocks) else None
        if old and not block.get("lines") and old.get("lines"):
            block["lines"] = list(old["lines"])
            block["total"] = max(int(old.get("total") or 0), len(old["lines"]))
        # Retain streamed step details while their panels are collapsed.
        old_steps = (old or {}).get("steps") or []
        new_steps = block.get("steps") or []
        if len(new_steps) < len(old_steps):
            new_steps = new_steps + [dict(s) for s in old_steps[len(new_steps):]]
        steps = []
        for k, item in enumerate(new_steps):
            item = dict(item)
            prev = old_steps[k] if k < len(old_steps) else None
            if prev and prev.get("title") == item.get("title") and not item.get("lines") and prev.get("lines"):
                item["lines"] = list(prev["lines"])
                item["total"] = max(int(prev.get("total") or 0), len(prev["lines"]))
            steps.append(item)
        block["steps"] = steps
        merged_blocks.append(block)
    # Keep temporarily missing blocks unless the UI replaced the activity container.
    layout_replaced = bool(merged_blocks and prev_blocks and
                           merged_blocks[0].get("layout") != prev_blocks[0].get("layout"))
    if not layout_replaced:
        for old in prev_blocks[len(merged_blocks):]:
            merged_blocks.append(dict(old, active=False))
    if final:
        merged_blocks = [dict(block, active=False) for block in merged_blocks]
    texts = {}
    for answer in prev_answers or []:
        texts[answer["after"]] = answer["text"]
    current = {}
    for answer in answers or []:
        text = strip_speaker_label(answer.get("text"))
        if not text:
            continue
        after = answer.get("after", 0)
        old = texts.get(after, "")
        current[after] = text
        # Prefer current visible text; retain longer streamed text when a re-render truncates it.
        if not (len(text) < len(old) and text.lstrip("\u2026") in old):
            texts[after] = text
    # Text that moved to another position on screen is not shown twice.
    for after in list(texts):
        if after not in current and any(texts[after] == t for a, t in current.items() if a != after):
            del texts[after]
    if final and current:
        texts = current
    merged_answers = [{"after": after, "text": texts[after]} for after in sorted(texts) if texts[after]]
    return merged_blocks, merged_answers


# Q or Cancel requests Stop before closing the browser and exiting.
QUIT_EXIT_CODE = 130
_quit_event = threading.Event()


class QuitRequested(Exception):
    "Raised at a safe point after Q / Cancel was requested."


def quit_requested():
    return _quit_event.is_set()


def raise_if_quit():
    if _quit_event.is_set():
        raise QuitRequested()


def _quit_key(ch):
    return isinstance(ch, str) and ch.strip().lower() == "q"


def start_quit_listener():
    "Watch stdin for Q (terminal key press, no Enter needed) or a q/quit/cancel line (pipe)."
    stdin = sys.stdin
    if stdin is None:
        return False
    try:
        tty_input = stdin.isatty()
    except Exception:
        tty_input = False

    def windows_console():
        import msvcrt
        while not _quit_event.is_set():
            if msvcrt.kbhit():
                if _quit_key(msvcrt.getwch()):
                    _quit_event.set()
            else:
                time.sleep(0.1)

    def posix_terminal():
        import select, termios, tty
        fd = stdin.fileno()
        old = termios.tcgetattr(fd)
        restore = lambda: termios.tcsetattr(fd, termios.TCSADRAIN, old)
        atexit.register(restore)
        tty.setcbreak(fd)
        try:
            while not _quit_event.is_set():
                ready, _, _ = select.select([fd], [], [], 0.2)
                if ready and _quit_key(os.read(fd, 1).decode("utf-8", "ignore")):
                    _quit_event.set()
        finally:
            restore()

    def pipe_lines():
        while not _quit_event.is_set():
            line = stdin.readline()
            if not line:
                return  # EOF: no quit requests can arrive any more.
            if line.strip().lower() in ("q", "quit", "cancel", "stop"):
                _quit_event.set()

    def worker():
        try:
            if tty_input and os.name == "nt":
                windows_console()
            elif tty_input:
                posix_terminal()
            else:
                pipe_lines()
        except Exception:
            pass

    threading.Thread(target=worker, name="quit-listener", daemon=True).start()
    return tty_input


def pause(page, ms):
    "page.wait_for_timeout that returns early (raising QuitRequested) when a quit is requested."
    end = time.monotonic() + ms / 1000
    while True:
        raise_if_quit()
        left = end - time.monotonic()
        if left <= 0:
            return
        page.wait_for_timeout(min(250, max(1, int(left * 1000))))


def visible_stop_buttons(page, sel):
    "Hover current task cards first; use page Stop controls only when no task card exists."
    try:
        read_task_cards(page)
        cards = page.locator('[data-roundtrip-task-card]')
        current = [cards.nth(i) for i in range(cards.count())
                   if cards.nth(i).is_visible() and not is_prior(cards.nth(i))]
        scopes = current[-1:] or [page]
        buttons = []
        for scope in scopes:
            if current:
                scope.hover(timeout=1500)
            stop = scope.locator(sel["stop"])
            for i in range(stop.count()):
                button = stop.nth(i)
                if button.is_visible() and button.evaluate("""el => {
                    if (el.closest('[data-roundtrip-prior-msg]')) return false;
                    for (let node = el; node; node = node.parentElement) {
                        if (getComputedStyle(node).opacity === '0') return false;
                    }
                    return true;
                }"""):
                    buttons.append(button)
        return buttons
    except Exception:
        return []


def response_is_running(page, sel):
    "Copilot is running while the task-card-first lookup exposes a Stop control."
    return bool(visible_stop_buttons(page, sel))


def stop_generation(page, sel, timeout_seconds=15):
    "Click Copilot's Stop control for the running response and wait until it is gone."
    started = time.monotonic()
    clicked, last_click = False, 0.0
    while time.monotonic() - started < timeout_seconds:
        buttons = visible_stop_buttons(page, sel)
        now = time.monotonic()
        if not buttons:
            if clicked:
                return True
            # The Stop control can take a moment to replace Send after submission.
            if now - started >= STOP_APPEAR_GRACE_SECONDS:
                return False
        elif not clicked or now - last_click >= 3:
            try:
                buttons[-1].click(timeout=3000)
                clicked, last_click = True, now
                emit_status("cancelled", "Clicked Stop in Copilot Chat.")
            except Exception:
                pass
        page.wait_for_timeout(300)
    return clicked


def wait_for_response_end(page, sel, timeout_seconds=60):
    "Wait (bounded) until Copilot's Stop control is gone."
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not response_is_running(page, sel):
            return True
        pause(page, 500)
    return False


def wait_for_ui(page, ms=UI_SETTLE_MS):
    """Let the Copilot UI load properly before interacting with it."""
    emit_status("browser", f"Letting Copilot UI load ({ms / 1000:g}s)...")
    page.wait_for_timeout(ms)


# Capture visible-output signatures, reasoning steps and interleaved answers without code blocks.
SCREEN_SCRIPT = r"""
async (args) => {
  const tail = args.tail, custom = args.selector;
  const titleRe = /^\s*(reasoning|reasoned|thinking|thought|thought for|worked for)\b/i;
  // Match short step-name headers while a section is running.
  const stepRe = /^[A-Z][a-z]+ing(?:\s+(?:and\s+)?[a-z][a-z-]*){0,3}$/;
  const doneRe = /^\s*(reasoned|thought|worked)\s+for\b/i;
  // Code-execution sections are represented by their title only.
  const omitRe = /^(coding\b|code execution\b|executing code\b|running code\b)/i;
  const lineMax = 2000;
  const visible = el => !!el && el.getClientRects().length > 0;
  const textOf = el => (el && (el.innerText || el.textContent) || '').trim();
  const prior = el => el.hasAttribute('data-roundtrip-prior') ||
    !!(el.parentElement && el.parentElement.closest('[data-roundtrip-prior-msg]'));
  const inCurrentAssistant = el => (args.assistant || []).some(selector => {
    try {
      const root = el.closest(selector);
      return !!root && !root.closest('[data-roundtrip-prior-msg]');
    } catch (_) { return false; }
  });
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const inWorkingSteps = control => {
    for (let el = control.parentElement, depth = 0; el && depth < 12; el = el.parentElement, depth++) {
      if (/^(MAIN|BODY|HTML)$/.test(el.tagName)) break;
      const labels = [el, ...el.querySelectorAll('h1, h2, h3, button, summary, span, div')];
      if (labels.some(label => visible(label) && !prior(label) &&
          /^working through the steps(?:\s*[:：]|$)/i.test(textOf(label)))) return true;
    }
    return false;
  };
  // Reveal every row before reading the timeline.
  const expandedMore = new WeakSet();
  while (true) {
    const more = Array.from(document.querySelectorAll('button, [role="button"]'))
      .filter(el => visible(el) && !prior(el) && !expandedMore.has(el) &&
        /^show more$/i.test(textOf(el)) && inWorkingSteps(el));
    if (!more.length) break;
    for (const control of more) {
      expandedMore.add(control);
      try { control.click(); } catch (_) { /* best effort */ }
    }
    await wait(150);
  }
  // Exclude screen-reader-only text from streamed answers.
  const srOnly = (el, cs) => {
    if (cs.position !== 'absolute' && cs.position !== 'fixed') return false;
    if (/rect\(\s*0(px)?[\s,]+0(px)?[\s,]+0(px)?[\s,]+0(px)?\s*\)/.test(cs.clip || '') ||
        /inset\(\s*50%/.test(cs.clipPath || '')) return true;
    const r = el.getBoundingClientRect();
    return (r.width <= 1 || r.height <= 1) && /hidden|clip/.test(cs.overflow || '');
  };
  // Exclude collapsed, transparent and content-hidden elements.
  const notShown = (el, cs) => {
    if (srOnly(el, cs) || el.hidden || el.hasAttribute('inert') || cs.opacity === '0' ||
        cs.contentVisibility === 'hidden') return true;
    if (!/hidden|clip|auto|scroll/.test((cs.overflow || '') + ' ' + (cs.overflowY || '') + ' ' + (cs.overflowX || ''))) return false;
    const r = el.getBoundingClientRect();
    return r.height <= 1 || r.width <= 1;
  };
  // Stream prose and inline code, but omit code blocks.
  const codeSel = 'pre, [class*="code-block" i], [class*="codeblock" i], [class*="code_block" i]';
  const isCode = el => {
    if (!el || el.nodeType !== 1) return false;
    const tag = el.tagName.toUpperCase();
    if (tag === 'PRE') return true;
    if (tag === 'CODE') {
      const d = getComputedStyle(el).display;
      return !d.startsWith('inline') && d !== 'contents';
    }
    try { return el.matches(codeSel); } catch (_) { return false; }
  };
  // Visible text of an element without its code blocks (and without `exclude`, e.g. a <summary>).
  const proseOf = (el, exclude) => {
    if (!el) return '';
    let out = '';
    const skipTags = /^(SCRIPT|STYLE|NOSCRIPT|SVG|TEMPLATE|IFRAME|CANVAS|TEXTAREA|INPUT|SELECT)$/;
    const walk = node => {
      if (node.nodeType === 3) { out += node.nodeValue.replace(/\s+/g, ' '); return; }
      if (node.nodeType !== 1) return;
      if (node === exclude || isCode(node) || skipTags.test(node.tagName.toUpperCase()) ||
          node.getAttribute('aria-hidden') === 'true') return;
      const cs = getComputedStyle(node);
      if (cs.display === 'none' || cs.visibility === 'hidden' || notShown(node, cs)) return;
      if (node.tagName === 'BR') { out += '\n'; return; }
      // Buttons (e.g. a step's expander) are kept on their own line so step names stay separate.
      const block = !cs.display.startsWith('inline') && cs.display !== 'contents' ||
        node.tagName === 'BUTTON' || node.getAttribute('role') === 'button';
      if (block) out += '\n';
      for (const child of node.childNodes) walk(child);
      if (block) out += '\n';
    };
    walk(el);
    return out.split('\n').map(l => l.replace(/[ \t\u00a0]+/g, ' ').trim()).filter(Boolean).join('\n');
  };
  let customHeads = null;
  if (custom) { try { customHeads = Array.from(document.querySelectorAll(custom)); } catch (_) { customHeads = null; } }
  let heads = customHeads || Array.from(document.querySelectorAll('button, [role="button"], summary, [aria-expanded]'));
  // Some responses expose a plain duration header above coding cards, without a task plan.
  const plainHeads = new Set();
  if (!customHeads) {
    const labelRe = /^(?:reasoning|thinking|(?:reasoned|thought|worked) for (?:\d+(?:\.\d+)?\s*(?:ms|s|m|h)\s*)+)$/i;
    for (const selector of args.assistant || []) {
      for (const root of document.querySelectorAll(selector)) {
        for (const el of root.querySelectorAll('span, p, div, h2, h3')) {
          if (!visible(el) || prior(el) || !labelRe.test(textOf(el)) ||
              el.closest('button, [role="button"], summary, pre, code') ||
              el.querySelector('button, [role="button"], summary, [aria-expanded]') ||
              Array.from(el.children).some(child => labelRe.test(textOf(child)))) continue;
          plainHeads.add(el);
        }
      }
    }
    heads = Array.from(new Set([...heads, ...plainHeads])).sort((a, b) =>
      a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1);
  }
  const headTitle = h => textOf(h).split('\n')[0].replace(/[\s>\u203a\u00bb\u2304\u2335\u02c5\u25be\u25b8]+$/, '').trim();
  const chrome = 'form, nav, header, footer, [role="toolbar"], [role="menu"], [role="menubar"], [role="dialog"], [role="tablist"]';
  heads = heads.filter(h => {
    const t = textOf(h);
    if (prior(h)) return false;
    if (!visible(h) || !t || t.length > 120) return false;
    if (customHeads) return true;
    if (titleRe.test(t)) return true;
    const expandable = h.tagName === 'SUMMARY' || h.hasAttribute('aria-expanded') || !!h.querySelector('[aria-expanded]');
    let inChrome = false;
    try { inChrome = !!h.closest(chrome); } catch (_) { inChrome = false; }
    // Activity can be a standalone control without a task or timeline wrapper.
    return expandable && !inChrome && stepRe.test(headTitle(h)) &&
      (inWorkingSteps(h) || inCurrentAssistant(h));
  });
  // Keep the outermost header when several nested elements match the same title.
  heads = heads.filter(h => !heads.some(o => o !== h && o.contains(h)));
  // Nested step toggles belong to their enclosing section.
  const depth = el => { let d = 0; for (let e = el; e; e = e.parentElement) d++; return d; };
  const nestedIn = (h, o) => {
    if (depth(o) >= depth(h) || !(o.compareDocumentPosition(h) & Node.DOCUMENT_POSITION_FOLLOWING)) return false;
    let anc = o.parentElement;
    for (let k = 0; k < 4 && anc; k++, anc = anc.parentElement) if (anc.contains(h)) return true;
    return false;
  };
  if (!customHeads) heads = heads.filter(h => !heads.some(o => o !== h && nestedIn(h, o)));
  const expandedState = h => {
    if (h.tagName === 'SUMMARY') return h.parentElement && h.parentElement.open ? 'true' : 'false';
    const own = h.getAttribute('aria-expanded');
    if (own !== null) return own;
    const inner = h.querySelector('[aria-expanded]');
    return inner ? inner.getAttribute('aria-expanded') : null;
  };
  const isHead = el => heads.some(h => h === el || el.contains(h));
  // Accept linked panels, verified collapsible siblings, or non-toggle steps cards as section content.
  const store = window.__roundtripSections || (window.__roundtripSections =
    {content: new WeakMap(), outside: new WeakSet()});
  const shows = el => {
    if (!el || !el.isConnected || !visible(el) || !textOf(el)) return false;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none') return false;
    const r = el.getBoundingClientRect();
    return r.height > 1 && r.width > 1;
  };
  const linkedEl = h => {
    const ctrl = h.getAttribute('aria-controls') ||
      (h.querySelector('[aria-controls]') || {getAttribute: () => null}).getAttribute('aria-controls');
    if (ctrl) { const target = document.getElementById(ctrl.split(/\s+/)[0]); if (target) return target; }
    const ids = [h.id, ...Array.from(h.querySelectorAll('[id]')).map(e => e.id)].filter(Boolean);
    for (const id of ids) {
      try {
        const target = document.querySelector('[aria-labelledby~="' + CSS.escape(id) + '"]');
        if (target && target !== h && !h.contains(target) && !target.contains(h)) return target;
      } catch (_) { /* invalid id */ }
    }
    return null;
  };
  // Icons/chevrons next to a header do not end its wrapper; any other element (text, a panel) does.
  const decorative = el => !/[\p{L}\p{N}]/u.test(textOf(el)) &&
    (/^(svg|img|i|span|path|use)$/i.test(el.tagName) || !!el.querySelector('svg, img'));
  // The element following the header's own wrappers (the header plus its icons).
  const nextCandidate = h => {
    let el = h;
    for (let d = 0; d < 4 && el.parentElement && el.parentElement !== document.body; d++) {
      const parent = el.parentElement;
      if (Array.from(parent.children).some(c => c !== el && !decorative(c))) break;
      el = parent;
    }
    const next = el.nextElementSibling;
    return next && !isHead(next) && textOf(next) ? next : null;
  };
  const toggleable = h => h.tagName === 'SUMMARY' || expandedState(h) !== null;
  const firstLine = el => (proseOf(el).split('\n')[0] || '').trim();
  const stepsCard = el => {
    const line = firstLine(el);
    return stepRe.test(line) || titleRe.test(line) && line.length <= 40 && !/[.,:;!?]/.test(line);
  };
  const contentEl = h => {
    const linked = linkedEl(h);
    if (linked) return linked;
    const c = nextCandidate(h);
    if (!c || store.outside.has(c)) return null;
    if (!toggleable(h)) return stepsCard(c) ? c : null;
    if (expandedState(h) === 'false') return null;
    // Reuse verified panel ownership unless the candidate is known to be outside the section.
    if (store.content.has(h)) return c;
    // Use the following panel for running sections; verify finished sections before reading.
    return doneRe.test(headTitle(h)) ? null : c;
  };
  const waitFor = async (test, ms) => {
    for (let t = 0; t < ms; t += 100) { if (test()) return true; await wait(100); }
    return test();
  };
  // Text visible right after a collapsed finished section is never that section's content.
  const markOutside = () => {
    for (const h of heads) {
      if (h.tagName === 'SUMMARY' || expandedState(h) !== 'false' || !doneRe.test(headTitle(h)) || linkedEl(h)) continue;
      const c = nextCandidate(h);
      if (c && shows(c)) store.outside.add(c);
    }
  };
  // Collapse and reopen finished sections once to verify panel ownership.
  const verifySections = async () => {
    for (const h of heads) {
      if (h.tagName === 'SUMMARY' || expandedState(h) !== 'true' || !doneRe.test(headTitle(h)) || linkedEl(h)) continue;
      const c = nextCandidate(h);
      if (!c || store.outside.has(c) || store.content.has(h)) continue;
      try {
        h.click();
        const collapsed = await waitFor(() => expandedState(h) === 'false', 600);
        const hidden = collapsed && await waitFor(() => !shows(c), 1200);
        if (!collapsed || !hidden) store.outside.add(c); else store.content.set(h, true);
        if (expandedState(h) === 'false') { h.click(); await waitFor(() => expandedState(h) === 'true', 600); }
      } catch (_) { store.outside.add(c); }
    }
  };
  const contentFor = h => {
    if (h.tagName === 'SUMMARY' && h.parentElement) return proseOf(h.parentElement, h);
    return proseOf(contentEl(h));
  };
  const containsCode = h => {
    const panel = h.tagName === 'SUMMARY' ? h.parentElement : contentEl(h);
    return !!panel && Array.from(panel.querySelectorAll('pre, code, [class*="code-block" i], [class*="codeblock" i], [class*="code_block" i]'))
      .some(isCode);
  };
  // Plain labels qualify only when the adjacent panel actually contains working steps.
  heads = heads.filter(h => !plainHeads.has(h) || contentEl(h));
  markOutside();
  await verifySections();
  // Expand step panels before reading them; code blocks remain excluded from streamed output.
  for (const h of heads) {
    const title = headTitle(h);
    if (expandedState(h) === 'false' && h.dataset.roundtripExpanded !== title) {
      try {
        h.click();
        if (await waitFor(() => expandedState(h) === 'true', 600)) h.dataset.roundtripExpanded = title;
      } catch (_) { /* retry on a later snapshot */ }
    }
    if (expandedState(h) === 'false') continue;
    // A newly opened unlinked finished panel still needs ownership verification.
    await verifySections();
    const panel = h.tagName === 'SUMMARY' ? h.parentElement : contentEl(h);
    if (!panel) continue;
    const toggles = Array.from(panel.querySelectorAll('button, [role="button"], summary, [aria-expanded]'));
    for (const step of toggles) {
      if (step === h || step.contains(h) || !visible(step)) continue;
      if (expandedState(step) !== 'false') continue;
      // Only short step headings qualify, not arbitrary controls inside a panel.
      const name = headTitle(step);
      if (!(stepRe.test(name) || titleRe.test(name) && name.length <= 40 && !/[.,:;!?]/.test(name))) continue;
      try { step.click(); await waitFor(() => expandedState(step) === 'true', 600); }
      catch (_) { /* retry on a later snapshot */ }
    }
  }
  // Collapse finished sections after reading so interim answers remain visible.
  const collapseAfterRead = [];
  const blocks = heads.map((h, index) => {
    const title = headTitle(h);
    const expanded = expandedState(h);
    const raw = expanded === 'false' ? [] : contentFor(h).split(/\r?\n/)
      .map(l => l.trim()).filter(Boolean).map(l => l.length > lineMax ? l.slice(0, lineMax) + '\u2026' : l);
    const active = !doneRe.test(title);
    if (!active && expanded === 'true' && h.dataset.roundtripCollapsed !== title) {
      const reads = (Number(h.dataset.roundtripReads) || 0) + 1;
      h.dataset.roundtripReads = String(reads);
      // Steps already streamed are kept by the script (merge_live) while the section is collapsed.
      if (raw.length || reads >= 3) collapseAfterRead.push([h, title]);
    }
    // Split the section into steps at step-title lines ("Reasoning", "Creating file", ...).
    const steps = [];
    let step = null;
    for (const line of raw) {
      if (stepRe.test(line) || titleRe.test(line) && line.length <= 40 && !/[.,:;!?]/.test(line)) {
        step = {title: line, lines: []}; steps.push(step); continue;
      }
      if (!step) { step = {title: active && !titleRe.test(title) ? title : 'Reasoning', lines: []}; steps.push(step); }
      step.lines.push(line);
    }
    // Keep completed titled rows distinct from expandable thought details.
    const kept = steps.map(s => {
      if (omitRe.test(s.title)) return {title: s.title, kind: 'coding', total: 0, lines: []};
      const kind = titleRe.test(s.title) || s.lines.length ? 'thought' : 'completed';
      return {title: s.title, kind, total: s.lines.length, lines: s.lines.slice(-tail)};
    });
    if (containsCode(h) && !kept.some(s => s.kind === 'coding'))
      kept.push({title: 'Coding and executing', kind: 'coding', total: 0, lines: []});
    const lines = [];
    for (const s of steps) if (!omitRe.test(s.title)) lines.push(...s.lines);
    return {index, title, active, layout: inWorkingSteps(h) ? 'working' : 'section',
      total: lines.length, lines: lines.slice(-tail), steps: kept};
  });
  // Collect current-turn answers from assistant messages or the enclosing turn, using the richer layout.
  const answers = [];
  try {
    const norm = s => String(s || '').replace(/\s+/g, ' ').trim();
    const outermost = list => list.filter(n => !list.some(o => o !== n && o.contains(n)));
    let found = [];
    for (const s of args.assistant || []) {
      try { found.push(...Array.from(document.querySelectorAll(s))); } catch (_) { /* invalid selector */ }
    }
    found = outermost(Array.from(new Set(found)).filter(n => visible(n) && textOf(n) &&
      !n.closest('[data-roundtrip-prior-msg]')))
      .sort((a, b) => a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1);
    // The current turn: the outermost ancestor of the latest reasoning header that excludes the user's prompt.
    const snippet = norm(args.prompt).slice(0, 60);
    let region = null;
    if (heads.length && snippet) {
      let el = heads[heads.length - 1].parentElement;
      while (el && el.parentElement && el.parentElement !== document.body &&
             !norm(textOf(el.parentElement)).includes(snippet)) el = el.parentElement;
      // In a continued chat the region must not reach into earlier turns.
      if (el && !norm(textOf(el)).includes(snippet) && !el.querySelector('[data-roundtrip-prior-msg]') &&
          !el.closest('[data-roundtrip-prior-msg]')) region = el;
    }
    // Prior-turn messages are marked before submission; keep every current response fragment.
    const turnNodes = found;
    // Reasoning headers split the text; the content of expanded sections (their steps) is skipped.
    const breaks = new Map(), skip = new Set();
    heads.forEach((h, i) => {
      breaks.set(h.tagName === 'SUMMARY' && h.parentElement ? h.parentElement : h, i);
      // Skip only verified section panels; preserve adjacent interim answers.
      if (h.tagName !== 'SUMMARY' && !(expandedState(h) === 'false' && doneRe.test(headTitle(h)))) {
        const c = contentEl(h);
        if (c) skip.add(c);
      }
    });
    const ignored = /^(SCRIPT|STYLE|NOSCRIPT|SVG|BUTTON|TEXTAREA|INPUT|SELECT|TEMPLATE|IFRAME|CANVAS)$/;
    const collect = roots => {
      const segments = [];
      let cur = null;
      const walk = (node, root, pre) => {
        if (node.nodeType === 3) { cur.text += pre ? node.nodeValue : node.nodeValue.replace(/\s+/g, ' '); return; }
        if (node.nodeType !== 1) return;
        const el = node;
        if (el !== root && breaks.has(el)) { segments.push(cur); cur = {after: breaks.get(el) + 1, text: ''}; return; }
        if (skip.has(el) || el.hasAttribute('data-roundtrip-task-card') || isCode(el) ||
            ignored.test(el.tagName.toUpperCase()) || el.getAttribute('role') === 'button' ||
            el.getAttribute('aria-hidden') === 'true') return;
        const cs = getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden' || notShown(el, cs)) return;
        if (el.tagName === 'BR') { cur.text += '\n'; return; }
        const block = !cs.display.startsWith('inline') && cs.display !== 'contents';
        const keep = pre || /^pre/.test(cs.whiteSpace);
        if (block) cur.text += '\n';
        for (const child of el.childNodes) walk(child, root, keep);
        if (block) cur.text += '\n';
      };
      for (const root of roots) {
        // Text of this root starts after every reasoning header that precedes it on screen.
        const before = heads.filter(h => !root.contains(h) &&
          (h.compareDocumentPosition(root) & Node.DOCUMENT_POSITION_FOLLOWING)).length;
        cur = {after: before, text: ''};
        walk(root, root, false);
        segments.push(cur);
      }
      const out = [];
      for (const seg of segments) {
        let text = seg.text.split('\n').map(l => l.replace(/[ \t\u00a0]+/g, ' ').trim())
          .filter(l => !/^(copilot|you)\s+said\s*:?$/i.test(l))
          // A status line rendered twice in a row ("Checking that now..." x2) is shown once.
          .filter((l, k, all) => !l || k === 0 || l !== all[k - 1]).join('\n')
          .replace(/\n{2,}/g, '\n').trim();
        if (!text) continue;
        if (text.length > args.maxChars) text = '\u2026' + text.slice(-args.maxChars);
        const last = out[out.length - 1];
        if (last && last.after === seg.after) last.text += '\n\n' + text; else out.push({after: seg.after, text});
      }
      return out;
    };
    const fromMessages = turnNodes.length ? collect(turnNodes) : [];
    const fromRegion = region ? collect([region]) : [];
    // Equal segment counts can still omit interim prose outside assistant-message wrappers.
    const textSize = segments => segments.reduce((size, segment) => size + norm(segment.text).length, 0);
    answers.push(...(textSize(fromRegion) > textSize(fromMessages) ? fromRegion : fromMessages));
  } catch (_) { /* live answers are best effort */ }
  for (const [h, title] of collapseAfterRead) {
    h.dataset.roundtripCollapsed = title;
    try { h.click(); } catch (_) { /* best effort */ }
  }
  // Signature of on-screen text; elapsed-time counters and animated dots are ignored.
  const root = document.querySelector('main') || document.body;
  const text = textOf(root).replace(/\d+(\.\d+)?\s*(ms|s|sec|secs|seconds?|m|min|mins|minutes?)\b/gi, '')
    .replace(/[.\u2026]+/g, '');
  let hash = 5381;
  for (let i = 0; i < text.length; i++) hash = ((hash * 33) ^ text.charCodeAt(i)) >>> 0;
  return {signature: text.length + ':' + hash, blocks, answers};
}
"""


# Detect Copilot's long-running task card, expand it, and report its visible plan.
TASK_SCRIPT = r"""
async () => {
  const visible = el => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  const norm = value => String(value || '').replace(/\s+/g, ' ').trim();
  const stateRe = /^(in progress|completed?|done|failed|cancell?ed|task stopped|stopped)$/i;
  const prior = el => el.hasAttribute('data-roundtrip-prior') ||
    !!el.closest('[data-roundtrip-prior-msg]');
  const linesOf = el => String(el && el.innerText || '').split(/\r?\n/).map(norm).filter(Boolean);
  const stateText = el => [el && el.innerText, el && el.textContent, el && el.getAttribute('aria-label'),
    el && el.getAttribute('title'), el && el.getAttribute('data-state'), el && el.getAttribute('data-status')]
    .map(norm).find(value => stateRe.test(value)) || '';
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const stateOf = el => {
    const nodes = [el, ...Array.from(el.querySelectorAll('*'))];
    const signal = nodes.map(node => [node.className && String(node.className), node.getAttribute('aria-label'),
      node.getAttribute('title'), node.getAttribute('data-state'), node.getAttribute('data-status'),
      node.getAttribute('data-icon-name')].filter(Boolean).join(' ')).join(' ').toLowerCase();
    const text = norm(el.innerText);
    if (/\b(complet(?:e|ed)|done|check(?:ed|mark)?|success)\b/.test(signal) || /^[\u2713\u2714]/.test(text)) return 'completed';
    if (el.matches('[aria-current="step"], [aria-busy="true"]') || el.querySelector('[aria-current="step"], [aria-busy="true"]') ||
        /\b(in[-_ ]?progress|current|active|running|loading|spinner|progress)\b/.test(signal) ||
        nodes.some(node => typeof node.getAnimations === 'function' && node.getAnimations().some(a => a.playState === 'running'))) return 'active';
    if (/\b(pending|not[-_ ]?started|todo|queued)\b/.test(signal)) return 'pending';
    return 'unknown';
  };
  const planOf = (card, title, state, lines) => {
    let rows = Array.from(card.querySelectorAll('li, [role="listitem"], [aria-current="step"], [data-state], [data-status]'))
      .filter(el => visible(el) && !prior(el));
    rows = rows.filter(el => !rows.some(parent => parent !== el && parent.contains(el)));
    let items = rows.map(el => ({title: norm(el.innerText || el.textContent), state: stateOf(el)}))
      .filter(item => item.title && !stateRe.test(item.title));
    if (!items.length) items = lines.map(line => norm(line.replace(title, '').replace(state, '')))
      .filter(line => line && !stateRe.test(line) && !/^(show more|working through the steps|thought process|stop|cancel)$/i.test(line))
      .map(itemTitle => ({title: itemTitle, state: 'unknown'}));
    items = items.slice(0, 20);
    const activeIndex = items.findIndex(item => item.state === 'active');
    const completedIndex = items.reduce((last, item, index) => item.state === 'completed' ? index : last, -1);
    let inferredActive = activeIndex;
    if (/^in progress$/i.test(state) && inferredActive < 0) inferredActive = Math.min(completedIndex + 1, items.length - 1);
    return items.map((item, index) => ({title: item.title, state: item.state !== 'unknown' ? item.state :
      /^in progress$/i.test(state) ? index < inferredActive ? 'completed' : index === inferredActive ? 'active' : 'pending' :
      /^(completed?|done)$/i.test(state) ? 'completed' : 'pending'}));
  };
  const statusNodes = Array.from(document.querySelectorAll('span, p, div, button, [role="status"], [role="button"], [aria-label], [data-state], [data-status]'))
    .filter(el => visible(el) && !prior(el) && stateText(el) &&
      !Array.from(el.children).some(child => stateText(child)));
  const tasks = [];
  const seen = new Set();
  for (const statusNode of statusNodes) {
    const state = stateText(statusNode);
    let card = null, fallback = null;
    for (let el = statusNode.parentElement, depth = 0; el && depth < 10; el = el.parentElement, depth++) {
      if (prior(el) || /^(MAIN|BODY|HTML)$/.test(el.tagName)) break;
      if (el.matches('form, nav, header, footer, [role="dialog"], [role="menu"]')) break;
      const lines = linesOf(el), text = norm(el.innerText);
      if (!text || text === state || text.length > 4000 || lines.length > 30) continue;
      const hasControl = !!el.querySelector('button, summary, [role="button"], [aria-expanded]');
      const hasPlan = !!el.querySelector('li, [role="listitem"], [aria-current="step"], [data-state], [data-status]');
      if ((hasControl || hasPlan) && !fallback) fallback = el;
      if ((hasControl || hasPlan) && (lines.length >= 3 || hasPlan)) { card = el; break; }
    }
    card = card || fallback;
    if (!card || seen.has(card)) continue;
    seen.add(card);
    let lines = linesOf(card);
    let title = '';
    const titleNodes = Array.from(card.querySelectorAll('h1, h2, h3, h4, h5, h6, strong, [data-testid*="title" i], [class*="title" i]'));
    for (const node of titleNodes) {
      const text = norm(node.textContent);
      if (visible(node) && text && text.length <= 200 && !stateRe.test(text) && !node.contains(statusNode) &&
          (node.compareDocumentPosition(statusNode) & Node.DOCUMENT_POSITION_FOLLOWING)) { title = text; break; }
    }
    if (!title) {
      const stateIndex = lines.findIndex(line => stateRe.test(line));
      if (stateIndex > 0) title = lines[stateIndex - 1];
      else {
        const combined = lines.find(line => line.toLowerCase().includes(state.toLowerCase()));
        title = norm(combined && combined.replace(new RegExp(state, 'i'), ''));
      }
    }
    title = title || lines.find(line => !stateRe.test(line)) || 'Copilot task';
    const collapsed = Array.from(card.querySelectorAll('details:not([open]) > summary, button[aria-expanded="false"], [role="button"][aria-expanded="false"]'))
      .filter(el => visible(el) && !prior(el));
    if (collapsed.length) {
      try { collapsed[0].click(); await wait(200); lines = linesOf(card); } catch (_) { /* retry later */ }
    }
    if (lines.length < 3) {
      for (let el = card.parentElement, depth = 0; el && depth < 5; el = el.parentElement, depth++) {
        const wider = linesOf(el);
        if (wider.length >= 3 && wider.length <= 30 &&
            norm(el.innerText).toLowerCase().includes(state.toLowerCase())) {
          card = el; lines = wider; break;
        }
      }
    }
    card.setAttribute('data-roundtrip-task-card', '1');
    const steps = planOf(card, title, state, lines);
    tasks.push({title, state, active: /^in progress$/i.test(state), steps});
  }
  return tasks;
}
"""


# Mark existing output so saved-chat follow-ups read only the new response.
MARK_PRIOR_SCRIPT = r"""
(args) => {
  let n = 0;
  for (const s of args.messages) {
    try { document.querySelectorAll(s).forEach(el => { el.setAttribute('data-roundtrip-prior-msg', '1'); n++; }); }
    catch (_) { /* invalid selector */ }
  }
  document.querySelectorAll('button, a, [role="button"], summary, [aria-expanded]')
    .forEach(el => el.setAttribute('data-roundtrip-prior', '1'));
  return n;
}
"""


def mark_prior_output(page):
    "Mark earlier turns; returns the number of earlier assistant messages found (0 on failure)."
    try:
        return int(page.evaluate(MARK_PRIOR_SCRIPT, {"messages": list(ASSISTANT_SELECTORS)}) or 0)
    except Exception:
        return 0


def is_prior(node):
    "True for elements that were already on screen before this prompt was sent."
    try:
        return bool(node.evaluate("e => e.hasAttribute('data-roundtrip-prior') || "
                                  "!!e.closest('[data-roundtrip-prior-msg]')"))
    except Exception:
        return False


def read_screen(page, sel, prompt=""):
    """Return (signature, reasoning blocks, live answers); failures yield (None, None, None)."""
    try:
        data = page.evaluate(SCREEN_SCRIPT, {"tail": REASONING_TAIL_LINES, "selector": sel.get("reasoning") or "",
                                             "assistant": list(ASSISTANT_SELECTORS), "prompt": prompt,
                                             "maxChars": ANSWER_MAX_CHARS})
        return data.get("signature"), data.get("blocks") or [], data.get("answers") or []
    except Exception:
        return None, None, None


def read_task_cards(page):
    """Expand and read current-turn long-running task cards."""
    try:
        tasks = page.evaluate(TASK_SCRIPT)
        return tasks if isinstance(tasks, list) else []
    except Exception:
        return []


def task_status_message(tasks):
    """Describe the latest visible long-running task card."""
    if not tasks:
        return ""
    task = tasks[-1]
    title = str(task.get("title") or "Copilot task").strip()
    state = str(task.get("state") or "In progress").strip()
    count = len(task.get("steps") or [])
    detail = f"; {count} plan item{'s' if count != 1 else ''} visible" if count else ""
    return f'Copilot task "{title}" is {state.lower()}{detail}.'


def run(cmd, cwd=None, capture=True):
    # Isolate child stdin from the Cancel listener and disable interactive Git prompts.
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, env=env,
                          stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


def repo_root(path: Path) -> Path:
    try:
        return Path(run(["git", "-C", str(path), "rev-parse", "--show-toplevel"]).stdout.strip()).resolve()
    except Exception as e:
        raise SystemExit(f"Not a Git worktree: {path}\n{e}")


def ensure_clean(root: Path, allow_dirty: bool):
    dirty = run(["git", "status", "--porcelain=v1"], cwd=root).stdout
    if dirty and not allow_dirty:
        raise SystemExit("Worktree is dirty. Commit/stash first, or explicitly use --allow-dirty.\n" + dirty)


def make_archive(root: Path, out_txt: Path):
    zip_path = out_txt.with_suffix(".zip")
    run(["git", "archive", "--format=zip", f"--output={zip_path}", "HEAD"], cwd=root)
    zip_path.replace(out_txt)
    with zipfile.ZipFile(out_txt) as z:
        names = z.namelist()
        if not names or any(n == ".git" or n.startswith(".git/") for n in names):
            raise SystemExit("Invalid source archive")
    return hashlib.sha256(out_txt.read_bytes()).hexdigest(), len(names)


def safe_members(z: zipfile.ZipFile):
    result = []
    for info in z.infolist():
        p = PurePosixPath(info.filename)
        if p.is_absolute() or ".." in p.parts or not p.parts:
            raise SystemExit(f"Unsafe archive path: {info.filename}")
        if p.parts[0] == ".git":
            raise SystemExit("Returned archive contains .git")
        # Reject Unix symlinks.
        if ((info.external_attr >> 16) & 0o170000) == 0o120000:
            raise SystemExit(f"Returned archive contains symlink: {info.filename}")
        result.append(info)
    return result


def normalize_download(src: Path, dst_zip: Path):
    data = src.read_bytes()
    # Chat may return ZIP bytes with a .txt filename.
    dst_zip.write_bytes(data)
    if not zipfile.is_zipfile(dst_zip):
        raise SystemExit(f"Downloaded file is not a ZIP archive: {src.name}")
    with zipfile.ZipFile(dst_zip) as z:
        members = safe_members(z)
        if not any(not x.is_dir() for x in members):
            raise SystemExit("Downloaded ZIP contains no files")
    return hashlib.sha256(data).hexdigest()


def backup_and_replace(root: Path, result_zip: Path, backup_dir: Path):
    """Validate and replace non-.git entries, backing up all files and restoring on failure."""
    root = root.resolve()
    backup_dir = backup_dir.resolve()
    if backup_dir == root or root in backup_dir.parents:
        raise SystemExit("Backup directory must be outside the repository")
    if result_zip.resolve() == root or root in result_zip.resolve().parents:
        raise SystemExit("Result ZIP must be outside the repository")

    def copy_entry(src: Path, dst: Path):
        if src.is_symlink():
            dst.symlink_to(os.readlink(src), target_is_directory=src.is_dir())
        elif src.is_dir():
            shutil.copytree(src, dst, symlinks=True)
        else:
            shutil.copy2(src, dst)

    def clear_worktree():
        for child in root.iterdir():
            if child.name == ".git":
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()

    with tempfile.TemporaryDirectory(prefix="copilot-extract-") as td:
        stage = Path(td)
        with zipfile.ZipFile(result_zip) as z:
            members = safe_members(z)
            z.extractall(stage, members)
        children = list(stage.iterdir())
        source = children[0] if len(children) == 1 and children[0].is_dir() else stage
        if not any(p.is_file() for p in source.rglob("*")):
            raise SystemExit("Returned repository contains no files")
        if (source / ".git").exists():
            raise SystemExit("Returned repository contains .git")

        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = backup_dir / f"{root.name}-{stamp}-{time.time_ns()}"
        backup.mkdir()
        try:
            for child in root.iterdir():
                if child.name != ".git":
                    copy_entry(child, backup / child.name)
        except Exception:
            shutil.rmtree(backup)
            raise

        try:
            clear_worktree()
            for child in source.iterdir():
                copy_entry(child, root / child.name)
        except Exception:
            clear_worktree()
            for child in backup.iterdir():
                copy_entry(child, root / child.name)
            raise
    return backup

def selectors_from_env():
    return {
        "composer": os.getenv("COPILOT_COMPOSER", 'textarea, [contenteditable="true"][role="textbox"]'),
        "file": os.getenv("COPILOT_FILE_INPUT", 'input[type="file"]'),
        "send": os.getenv("COPILOT_SEND", 'button[aria-label*="Send" i], button[data-testid*="send" i]'),
        "download": os.getenv("COPILOT_DOWNLOAD", 'a[download], a[href*="download" i], button:has-text("Download")'),
        "model_button": os.getenv("COPILOT_MODEL_BUTTON", 'button[aria-label*="model" i], button[aria-haspopup="menu"]:has-text("Auto")'),
        # A visible Stop control, in the composer or task card, is the response-running signal.
        "stop": os.getenv("COPILOT_STOP", 'button[aria-label*="Stop" i]:not([aria-label*="temporary" i]), '
                          '[role="button"][aria-label*="Stop" i]:not([aria-label*="temporary" i]), '
                          'button[title*="Stop" i]:not([title*="temporary" i]), '
                          '[role="button"][title*="Stop" i]:not([title*="temporary" i]), '
                          'button[data-testid*="stop" i], [role="button"][data-testid*="stop" i], '
                          'button:has([data-icon-name*="stop" i]), [role="button"]:has([data-icon-name*="stop" i]), '
                          'button:text-is("Stop"), [role="button"]:text-is("Stop")'),
        "temporary_chat": os.getenv("COPILOT_TEMPORARY_CHAT", 'button[aria-label*="temporary" i], [role="button"][aria-label*="temporary" i], '
                                    'button[title*="temporary" i], button[data-testid*="temporary" i]'),
        # Optional override for reasoning headers; default detects "Reasoning"/"Reasoned for Ns" toggles.
        "reasoning": os.getenv("COPILOT_REASONING", ""),
    }


# Strict indicators track upload progress; soft spinners may persist after completion.
BUSY_STRICT = '[role="progressbar"]:visible, [aria-busy="true"]:visible'
BUSY_SOFT = '[data-testid*="progress" i]:visible, [class*="spinner" i]:visible'
THUMBNAILS = 'img[src^="blob:"]:visible, img[src^="data:"]:visible'


def _count(page, selector):
    try:
        return page.locator(selector).count()
    except Exception:
        return 0


# Attachment chips usually expose a remove/close control; a new one indicates a new chip.
CHIP_CONTROLS = ('button[aria-label*="remove" i]:visible, button[aria-label*="delete" i]:visible, '
                 '[data-testid*="attachment" i]:visible, [class*="attachment" i]:visible')
# Bounded recognition window per attempt before trying another attach method.
RECOGNIZE_SECONDS = 45


def _name_keys(filename):
    """Full name plus shorter prefixes, because chips often truncate long names."""
    stem = filename.split(".")[0]
    keys = [filename, filename[:14], stem[:10]]
    return [k for i, k in enumerate(keys) if len(k) >= 4 and k not in keys[:i]]


def _name_count(page, filename):
    """Count visible elements showing the file name as text or chip attributes (aria-label/title/alt)."""
    total = 0
    for key in _name_keys(filename):
        try:
            loc = page.get_by_text(key, exact=False)
            total += sum(1 for i in range(loc.count()) if loc.nth(i).is_visible())
        except Exception:
            pass
        quoted = key.replace("\\", "\\\\").replace('"', '\\"')
        try:
            loc = page.locator(f'[aria-label*="{quoted}" i], [title*="{quoted}" i], img[alt*="{quoted}" i]')
            total += sum(1 for i in range(loc.count()) if loc.nth(i).is_visible())
        except Exception:
            pass
    return total


def attachment_snapshot(page, filename=""):
    """Indicator counts taken before attaching, so pre-existing ones are not mistaken for this upload."""
    return {"strict": _count(page, BUSY_STRICT), "soft": _count(page, BUSY_SOFT),
            "thumbs": _count(page, THUMBNAILS), "chips": _count(page, CHIP_CONTROLS),
            "names": _name_count(page, filename) if filename else 0}


def _recognized(page, filename, baseline, image):
    """True when this file's chip is visible: its name, or (images) a new thumbnail/chip."""
    if _name_count(page, filename) > baseline.get("names", 0):
        return True
    if _count(page, CHIP_CONTROLS) > baseline.get("chips", 0):
        return True
    return image and _count(page, THUMBNAILS) > baseline.get("thumbs", 0)


def wait_for_attachment_ready(page, source_txt: Path, timeout_ms: int = 180000, require_name: bool = True,
                              baseline=None, recognize_seconds=None):
    """Recognize a new attachment and await settled progress; return False on recognition timeout."""
    filename = source_txt.name
    baseline = baseline or {"strict": 0, "soft": 0, "thumbs": 0, "chips": 0, "names": 0}
    started = time.monotonic()
    deadline = started + timeout_ms / 1000
    stable_since = None
    recognized_at = None
    last_update = started
    while time.monotonic() < deadline:
        now = time.monotonic()
        if recognized_at is None:
            if _recognized(page, filename, baseline, not require_name):
                recognized_at = now
                emit_status("uploading", f"Attachment recognized: {filename}; waiting for upload to finish...")
            elif recognize_seconds is not None and now - started >= recognize_seconds:
                return False
        strict = _count(page, BUSY_STRICT)
        soft = _count(page, BUSY_SOFT)
        settled = (recognized_at is not None and strict <= baseline["strict"]
                   and (soft <= baseline["soft"] or now - recognized_at >= 30))
        if settled:
            if stable_since is None:
                stable_since = now
            if now - stable_since >= 3:
                return True
        else:
            stable_since = None
        if now - last_update >= 10:
            state = "uploading" if recognized_at is not None else "waiting for attachment chip"
            emit_status("uploading", f"Waiting for {filename} ({state}, {int(now - started)}s)...")
            last_update = now
        pause(page, 500)
    if recognized_at is None:
        return False
    raise SystemExit(f"Attachment upload did not settle within {timeout_ms // 1000}s: {filename}")


def _accept_matches(accept, path: Path):
    """Whether a file input's accept attribute allows this file (empty accept allows anything)."""
    accept = (accept or "").strip().lower()
    if not accept:
        return True
    suffix = path.suffix.lower()
    image = suffix in IMAGE_SUFFIXES
    for token in (t.strip() for t in accept.split(",")):
        if not token:
            continue
        if token in ("*", "*/*") or (token.startswith(".") and path.name.lower().endswith(token)):
            return True
        if image and token in ("image/*", "image/" + suffix.lstrip(".").replace("jpg", "jpeg")):
            return True
    return False


def _file_inputs_for(page, sel, path: Path):
    """File inputs ordered by suitability: accept matches the file type first."""
    loc = page.locator(sel["file"])
    preferred, fallback = [], []
    for i in range(_count(page, sel["file"])):
        node = loc.nth(i)
        try:
            accept = node.get_attribute("accept", timeout=2000)
        except Exception:
            accept = None
        (preferred if _accept_matches(accept, path) else fallback).append(node)
    return preferred + fallback


def _attach_with_chooser(page, path: Path):
    """Open the composer's attach/upload control and supply the file via the file chooser."""
    import re
    name = re.compile(r"attach|upload|add (a )?file|add content|add files|add image|^add$|^\+$", re.I)
    buttons = page.get_by_role("button", name=name)
    for i in range(buttons.count() - 1, -1, -1):
        button = buttons.nth(i)
        try:
            if not button.is_visible():
                continue
            with page.expect_file_chooser(timeout=8000) as fc:
                button.click(timeout=3000)
                # Some menus require choosing an upload entry after opening the "+" button.
                try:
                    page.get_by_role("menuitem", name=re.compile(r"upload|attach|file|image", re.I)).first.click(timeout=2500)
                except Exception:
                    pass
            fc.value.set_files(str(path))
            return True
        except Exception:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
    return False


def attach_methods(page, sel, path: Path):
    """Yield attach attempts: each matching file input, then the file-chooser button flow."""
    for node in _file_inputs_for(page, sel, path):
        def via_input(node=node):
            node.set_input_files(str(path), timeout=15000)
            return True
        yield via_input
    yield lambda: _attach_with_chooser(page, path)


def attach_file(page, sel, path: Path, require_name: bool = True):
    """Try suitable file inputs, then the file chooser, with bounded upload recognition."""
    tried = 0
    for method in attach_methods(page, sel, path):
        baseline = attachment_snapshot(page, path.name)
        try:
            if not method():
                continue
        except Exception:
            continue
        tried += 1
        if tried > 1:
            emit_status("uploading", f"Retrying attachment of {path.name} (attempt {tried})...")
        if wait_for_attachment_ready(page, path, require_name=require_name, baseline=baseline,
                                     recognize_seconds=RECOGNIZE_SECONDS):
            return
        if tried >= 3:
            break
    raise SystemExit(f"Copilot did not accept the attachment {path.name}. Check that this file type "
                     "is supported by Copilot Chat or override COPILOT_FILE_INPUT.")


def send_with_retry(page, composer, send_selector, timeout_seconds=120):
    """Retry transiently disabled send controls, without resending an accepted prompt."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            # A cleared composer indicates that the chat accepted the submission.
            if not composer.input_value(timeout=2000).strip():
                return
        except Exception:
            try:
                if not composer.inner_text(timeout=2000).strip():
                    return
            except Exception:
                pass
        try:
            button = page.locator(send_selector).first
            raise_if_quit()
            if button.is_visible() and button.is_enabled():
                button.click(timeout=3000)
            else:
                pause(page, 1500)
                continue
        except QuitRequested:
            raise
        except Exception:
            pause(page, 1500)
            continue
        # Give the UI time to acknowledge before attempting another click.
        pause(page, 2500)
        try:
            if not composer.input_value(timeout=2000).strip():
                return
        except Exception:
            try:
                if not composer.inner_text(timeout=2000).strip():
                    return
            except Exception:
                pass
    raise SystemExit(f"Prompt was not accepted within {timeout_seconds}s after upload")

def parse_model(model: str):
    """Require an explicit provider/model pair; model names may contain punctuation."""
    if not isinstance(model, str) or model.count("/") != 1:
        raise ValueError("Model must be a provider/model pair (e.g. GPT/GPT-6)")
    provider, name = model.split("/", 1)
    if not provider.strip() or not name.strip():
        raise ValueError("Model must be a provider/model pair (e.g. GPT/GPT-6)")
    return provider, name

def select_model(page, model: str, sel):
    """Select a model through its provider submenu."""
    import re
    provider_key, requested = parse_model(model)
    button = page.locator(sel["model_button"]).first
    button.wait_for(state="visible", timeout=30000)
    button.click()
    page.wait_for_timeout(750)

    def visible_nodes(pattern):
        out = []
        loc = page.get_by_text(re.compile(pattern, re.I))
        for i in range(loc.count()):
            node = loc.nth(i)
            try:
                if node.is_visible(): out.append(node)
            except Exception: pass
        return out

    def row_for(node):
        semantic = node.locator('xpath=ancestor-or-self::*[@role="menuitem" or @role="option" or @aria-haspopup="menu" or self::button][1]')
        if semantic.count():
            try:
                if semantic.first.is_visible(): return semantic.first
            except Exception: pass
        cur = node
        for _ in range(7):
            cur = cur.locator('xpath=..')
            if not cur.count(): break
            try:
                box = cur.bounding_box()
                if box and box["width"] >= 180 and 35 <= box["height"] <= 110: return cur
            except Exception: pass
        return node

    is_claude = provider_key == "Claude"
    provider_name = "Claude/Anthropic" if is_claude else "GPT/OpenAI"
    patterns = ((r"^Claude\s*$", r"^Anthropic\s*$", r"^Claude\s+Anthropic\s*$",
                 r"^Anthropic\s+Claude\s*$") if is_claude else
                (r"^GPT\s*$", r"^OpenAI\s*$", r"^GPT\s+OpenAI\s*$"))
    provider = None
    for pattern in patterns:
        nodes = visible_nodes(pattern)
        if nodes:
            provider = row_for(nodes[-1]); break
    if provider is None:
        rows = page.locator('[role="menuitem"]:visible, [aria-haspopup="menu"]:visible')
        for i in range(rows.count()):
            row = rows.nth(i)
            try:
                text = " ".join(row.inner_text().split())
                terms = ("Claude", "Anthropic") if is_claude else ("GPT", "OpenAI")
                if all(re.search(r"\b" + term + r"\b", text, re.I) for term in terms):
                    provider = row; break
            except Exception: pass
    if provider is None:
        raise SystemExit(f"{provider_name} provider row not found after opening the model picker.")

    box = provider.bounding_box()
    if not box: raise SystemExit(f"{provider_name} provider row has no bounding box.")
    y = box["y"] + box["height"] / 2
    page.mouse.move(box["x"] + box["width"] * .25, y)
    page.wait_for_timeout(300)
    page.mouse.move(box["x"] + box["width"] - 10, y, steps=16)
    page.wait_for_timeout(1800)
    pattern = r"^" + re.escape(requested) + r"\s*$"
    options = visible_nodes(pattern)
    if not options:
        provider.click(); page.wait_for_timeout(1200); options = visible_nodes(pattern)
    if not options: raise SystemExit(f"Model {requested!r} was not found in the {provider_name} submenu.")
    option = row_for(options[-1])
    if option.get_attribute("aria-disabled") == "true": raise SystemExit(f"Model {requested!r} is disabled.")
    option.click(); page.wait_for_timeout(1000)


def temporary_chat_active(page, sel):
    """True when Copilot shows the temporary chat state (toggle pressed, heading or composer hint)."""
    import re
    try:
        toggles = page.locator(sel["temporary_chat"])
        for i in range(toggles.count()):
            node = toggles.nth(i)
            if not node.is_visible():
                continue
            for attr in ("aria-pressed", "aria-checked", "aria-selected"):
                if (node.get_attribute(attr) or "").lower() == "true":
                    return True
            label = (node.get_attribute("aria-label") or node.get_attribute("title") or "").lower()
            if re.search(r"\b(exit|leave|end|turn off|disable|stop)\b", label):
                return True
    except Exception:
        pass
    for locator in (page.get_by_text(re.compile(r"^\s*Temporary chat\s*$", re.I)),
                    page.locator('[placeholder*="Temporarily" i], [aria-placeholder*="Temporarily" i], '
                                 '[data-placeholder*="Temporarily" i], [aria-label*="Temporarily" i]')):
        try:
            if any(locator.nth(i).is_visible() for i in range(locator.count())):
                return True
        except Exception:
            pass
    return False


def enable_temporary_chat(page, sel, timeout_seconds=30):
    """Turn on Copilot's Temporary chat (top-right toggle) before model selection, without toggling it off."""
    if temporary_chat_active(page, sel):
        return
    button = page.locator(sel["temporary_chat"]).first
    try:
        button.wait_for(state="visible", timeout=timeout_seconds * 1000)
    except Exception:
        raise SystemExit("Temporary chat button was not found. Override COPILOT_TEMPORARY_CHAT, "
                         "or run with --no-temporary-chat.")
    button.click()
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        page.wait_for_timeout(500)
        if temporary_chat_active(page, sel):
            page.wait_for_timeout(500)
            return
    raise SystemExit("Temporary chat could not be enabled (Copilot did not show the temporary chat state).")


# Accept descriptive ZIP links and file chips, excluding uploaded .zip.txt archives.
ZIP_LINK_SELECTOR = ('a[href*=".zip" i], a:has-text(".zip"), [role="link"]:has-text(".zip"), '
                     'button:has-text(".zip"), a:has-text("ZIP"), [role="link"]:has-text("ZIP"), '
                     'button:has-text("ZIP")')
ZIP_NAME = re.compile(r"\bzip\b(?!\.txt)", re.I)
# A link that did not yield a file (e.g. it was rendered before the file was ready) is retried.
DOWNLOAD_RETRY_SECONDS = 10
DOWNLOAD_MAX_BACKOFF = 6
STOP_APPEAR_GRACE_SECONDS = 3
STOP_GONE_STABILITY_SECONDS = 1


def download_retry_due(count, last, now):
    "Keep retrying with a delay capped at sixty seconds."
    return not count or now - last >= DOWNLOAD_RETRY_SECONDS * min(count, DOWNLOAD_MAX_BACKOFF)


PREVIEW_DOWNLOAD_SELECTOR = ('a[download], button[aria-label*="download" i], '
                             '[role="button"][aria-label*="download" i], '
                             'button[title*="download" i], button:has-text("Download"), '
                             '[role="button"]:has-text("Download")')


def download_candidates(page, sel, exclude_names):
    "New (this turn) download links as (node, href, key), latest first; uploaded files are skipped."
    result, keys = [], set()
    excluded = [n.lower() for n in exclude_names if n]
    for selector, need_zip in ((sel["download"], False), (ZIP_LINK_SELECTOR, True)):
        try:
            loc = page.locator(selector)
            count = loc.count()
        except Exception:
            continue
        for i in range(count - 1, -1, -1):
            node = loc.nth(i)
            if is_prior(node):
                continue  # download link of an earlier turn
            try:
                if need_zip and not node.is_visible():
                    continue
                href = node.get_attribute("href", timeout=2000) or ""
                text = node.inner_text(timeout=2000) or ""
            except Exception:
                continue
            label = (text + " " + href).lower()
            if any(n in label for n in excluded) or (need_zip and not ZIP_NAME.search(text + " " + href)):
                continue
            key = href + text
            if key in keys:
                continue
            keys.add(key)
            result.append((node, href, key))
    return result


def _zip_name(name, fallback="copilot-result.zip"):
    name = Path(str(name or "").split("?")[0].split("#")[0]).name.strip()
    return name or fallback


def click_download(ctx, page, node, work: Path, timeout_seconds=15):
    "Click a result or its preview Download control and save the file, else return None."
    raise_if_quit()
    got, before, url = [], set(ctx.pages), page.url
    watched, clicked = [], set()
    def on_download(d):
        got.append(d)
    def on_page(pg):
        if pg not in watched:
            watched.append(pg)
            pg.on("download", on_download)
    for pg in ctx.pages:
        on_page(pg)
    ctx.on("page", on_page)
    try:
        try:
            node.click(timeout=5000)
        except QuitRequested:
            raise
        except Exception:
            # Download navigation can interrupt the click after the file has started.
            pass
        deadline = time.monotonic() + timeout_seconds
        while not got and time.monotonic() < deadline:
            raise_if_quit()
            # File links can open a preview with its own Download control.
            for pg in list(ctx.pages):
                on_page(pg)
                if pg not in before or (pg == page and pg.url != url):
                    scope = pg
                elif pg == page:
                    scope = pg.locator('[role="dialog"]:visible')
                else:
                    continue
                try:
                    controls = scope.locator(PREVIEW_DOWNLOAD_SELECTOR)
                    for i in range(controls.count()):
                        control = controls.nth(i)
                        key = (pg, i, control.get_attribute("aria-label"), control.inner_text())
                        if key in clicked or not control.is_visible() or not control.is_enabled():
                            continue
                        clicked.add(key)
                        control.click(timeout=2000)
                        break
                except QuitRequested:
                    raise
                except Exception:
                    pass
                if got:
                    break
            if not got:
                pause(page, 250)
        if not got:
            return None
        target = work / _zip_name(got[0].suggested_filename)
        got[0].save_as(str(target))
        return target
    finally:
        try:
            ctx.remove_listener("page", on_page)
        except Exception:
            pass
        for pg in watched:
            try:
                pg.remove_listener("download", on_download)
            except Exception:
                pass
            if pg not in before and pg != page:
                try:
                    pg.close()
                except Exception:
                    pass
        # Restore the chat even when a preview download succeeds.
        if page.url != url:
            try:
                page.go_back(wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass


FETCH_BLOB_SCRIPT = r"""
async (href) => {
  const r = await fetch(href);
  if (!r.ok) return null;
  const bytes = new Uint8Array(await r.arrayBuffer());
  let s = '';
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}
"""


def fetch_download(ctx, page, href, work: Path):
    "Fetch a link's target directly (signed-in browser session); only ZIP content is accepted."
    from urllib.parse import urljoin
    if not href.startswith(("blob:", "data:")):
        href = urljoin(page.url, href)  # relative links ("/files/repo.zip")
    try:
        if href.startswith(("blob:", "data:")):
            encoded = page.evaluate(FETCH_BLOB_SCRIPT, href)
            data = base64.b64decode(encoded) if encoded else b""
        elif href.lower().startswith(("https://", "http://")):
            response = ctx.request.get(href, timeout=60000)
            data = response.body() if response.ok else b""
        else:
            return None
    except Exception:
        return None
    if not data.startswith(b"PK"):
        return None
    target = work / _zip_name(href if not href.startswith(("blob:", "data:")) else "")
    target.write_bytes(data)
    return target


def open_context(p, profile: Path, headless: bool, downloads: Path):
    return p.chromium.launch_persistent_context(
        user_data_dir=str(profile), headless=headless, accept_downloads=True,
        downloads_path=str(downloads), viewport={"width": 1440, "height": 1000},
    )


def cmd_login(args):
    from playwright.sync_api import sync_playwright
    profile = Path(args.profile).expanduser().resolve(); profile.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = open_context(p, profile, False, Path(tempfile.mkdtemp()))
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(args.url, wait_until="domcontentloaded", timeout=120000)
        print("Sign in in the opened browser. When Copilot Chat is usable, return here and press Enter.")
        input()
        ctx.close()
    print(f"Saved browser profile: {profile}")


def read_chat_response(page):
    """Read the latest assistant message, excluding the user's prompt."""
    for selector in ASSISTANT_SELECTORS:
        try:
            nodes = page.locator(selector)
            for i in range(nodes.count() - 1, -1, -1):
                node = nodes.nth(i)
                if is_prior(node):
                    continue  # earlier turn of a continued chat
                if node.is_visible():
                    text = node.inner_text(timeout=3000).strip()
                    if text:
                        return text
        except Exception:
            continue
    return ""


def cmd_fix(args):
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    parse_model(args.model)
    if getattr(args, "chat_url", None):
        validate_chat_url(args.chat_url)
        args.temporary_chat = False  # a continued chat is always a saved chat
    args.attachments = resolve_attachments(args.attach)
    if start_quit_listener():
        print("Press Q at any time to stop the Copilot response and quit.", file=sys.stderr, flush=True)
    emit_status("checking", "Checking repository and Git working tree...")
    root = repo_root(Path(args.repo).resolve())
    ensure_clean(root, args.allow_dirty)
    profile = Path(args.profile).expanduser().resolve()
    if not profile.exists():
        raise SystemExit("Browser profile missing. Run the login command first.")
    work_parent = Path(args.work_dir).expanduser().resolve() if args.work_dir else None
    if work_parent is not None:
        work_parent.mkdir(parents=True, exist_ok=True)
    # Delete temporary archives, diagnostics and rollback copies when the run exits.
    emit_status("preparing", "Preparing temporary workspace...")
    with tempfile.TemporaryDirectory(prefix="copilot-roundtrip-", dir=work_parent) as temporary_work:
        return run_roundtrip(args, root, profile, Path(temporary_work), sync_playwright, PlaywrightTimeout)

def run_roundtrip(args, root, profile, work, sync_playwright, PlaywrightTimeout):
    source_txt = work / f"{root.name}-{time.strftime('%Y%m%d-%H%M%S')}.zip.txt"
    emit_status("archiving", "Creating committed repository archive...")
    source_hash, count = make_archive(root, source_txt)
    emit_status("archiving", f"Archive ready ({count} entries).")
    chat_url = getattr(args, "chat_url", None)
    continuing = bool(chat_url)
    # A saved chat is only created when Temporary chat is off; its URL is reported for follow-ups.
    track_chat = continuing or not args.temporary_chat
    prompt = (FOLLOWUP_PROMPT if continuing else BASE_PROMPT).format(request=args.request.strip())
    attachments = getattr(args, "attachments", None) or []
    if attachments:
        prompt += ATTACHMENTS_NOTE.format(names=", ".join(a.name for a in attachments))
    sel = selectors_from_env()
    downloaded = None
    chat_response = ""
    raise_if_quit()
    with sync_playwright() as p:
        emit_status("browser", "Launching Copilot browser...")
        ctx = open_context(p, profile, not args.show_browser, work)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        submitted = False
        try:
            if continuing:
                emit_status("browser", "Reopening the saved Copilot chat...")
                page.goto(chat_url, wait_until="domcontentloaded", timeout=120000)
                wait_for_ui(page)
                if conversation_url(page.url, args.url) is None:
                    raise SystemExit("The saved Copilot chat could not be reopened (Copilot showed a new chat "
                                     "instead). Start a new chat.")
                emit_status("browser", "Continuing the saved Copilot chat.")
            else:
                emit_status("browser", "Opening Copilot Chat...")
                page.goto(args.url, wait_until="domcontentloaded", timeout=120000)
                wait_for_ui(page)
            # Enable Temporary chat before model selection; saved follow-ups never enable it.
            if args.temporary_chat and not continuing:
                emit_status("temporary", "Enabling Temporary chat...")
                enable_temporary_chat(page, sel)
                emit_status("temporary", "Temporary chat enabled.")
            emit_status("model", "Checking Copilot model selection...")
            select_model(page, args.model, sel)
            emit_status("uploading", "Attaching repository archive...")
            # Do not submit until the upload belongs to this same draft message.
            attach_file(page, sel, source_txt)
            for index, extra in enumerate(attachments, 1):
                emit_status("uploading", f"Attaching additional file {index}/{len(attachments)}: {extra.name}...")
                attach_file(page, sel, extra, require_name=extra.suffix.lower() not in IMAGE_SUFFIXES)
                emit_status("uploading", f"Additional file {index}/{len(attachments)} attached: {extra.name}")
            emit_status("prompt", "Preparing modification request...")
            composer = page.locator(sel["composer"]).last
            composer.wait_for(state="visible", timeout=60000)
            composer.fill(prompt)
            # Earlier turns of a continued chat must not be read as this prompt's response.
            prior_messages = mark_prior_output(page)
            prior_keys = set()
            if continuing:
                old_links = page.locator(sel["download"])
                for i in range(old_links.count()):
                    try:
                        href = old_links.nth(i).get_attribute("href", timeout=2000) or ""
                    except Exception:
                        continue
                    if href:
                        prior_keys.add(href)
                emit_status("prompt", f"Continuing after {prior_messages} earlier Copilot message(s).")
            emit_status("prompt", "Submitting request to Copilot...")
            # From here on a response may be running: a quit must click Stop before exiting.
            submitted = True
            send_with_retry(page, composer, sel["send"])
            reported_url = None
            if continuing:
                reported_url = conversation_url(page.url, args.url) or chat_url
                emit_chat(reported_url)
            emit_status("waiting", "Waiting for Copilot to generate a downloadable repository...")
            # Reset the inactivity timeout when visible output or a task card changes.
            idle_limit = args.timeout
            started = time.monotonic()
            last_activity = started
            last_signature = None
            last_live = None
            last_tasks = []
            live_blocks, live_answers = [], []
            # Retry unavailable files with bounded backoff until the inactivity deadline.
            attempts = {}
            upload_names = [source_txt.name] + [a.name for a in attachments]
            last_update = started
            stop_seen = False
            stop_absent_since = None
            while downloaded is None:
                raise_if_quit()
                now = time.monotonic()
                # A new saved chat gets its own URL once Copilot accepts the first prompt.
                if track_chat:
                    current_url = conversation_url(page.url, args.url)
                    if current_url and current_url != reported_url:
                        reported_url = current_url
                        emit_chat(current_url)
                now = time.monotonic()
                # Expand task cards so their plans and completed file controls are visible.
                tasks = read_task_cards(page)
                if tasks != last_tasks:
                    last_tasks = tasks
                    last_activity = now
                    emit_status("task", task_status_message(tasks) if tasks else "Copilot task card closed.", tasks)
                now = time.monotonic()
                running = response_is_running(page, sel)
                if running:
                    stop_seen = True
                    stop_absent_since = None
                elif stop_absent_since is None:
                    stop_absent_since = now
                response_finished = (not running and stop_absent_since is not None and
                                     now - stop_absent_since >= STOP_GONE_STABILITY_SECONDS and
                                     (stop_seen or now - started >= STOP_APPEAR_GRACE_SECONDS))
                # Do not inspect result links until the UI no longer offers Stop.
                if response_finished:
                    for node, href, key in download_candidates(page, sel, upload_names):
                        if href and href in prior_keys:
                            continue
                        count, last = attempts.get(key, (0, 0.0))
                        if not download_retry_due(count, last, now):
                            continue
                        attempts[key] = (count + 1, now)
                        # Fetch targets without navigation; click JavaScript-handled links as a fallback.
                        downloaded = fetch_download(ctx, page, href, work) if href else None
                        if downloaded is None:
                            try:
                                downloaded = click_download(ctx, page, node, work)
                            except QuitRequested:
                                raise
                            except Exception:
                                downloaded = None
                        if downloaded is not None:
                            break
                # Check downloads before optional reasoning capture.
                if downloaded is not None:
                    break
                signature, blocks, answers = read_screen(page, sel, prompt)
                if signature is not None and signature != last_signature:
                    if last_signature is not None:
                        last_activity = now
                    last_signature = signature
                # Reasoning blocks and intermediate (live) answers are streamed together.
                if blocks is not None:
                    live_blocks, live_answers = merge_live(live_blocks, live_answers, blocks, answers)
                live = None if blocks is None else (live_blocks, live_answers)
                if live is not None and live != last_live:
                    last_live = live
                    last_activity = now if (live_blocks or live_answers) else last_activity
                    emit_reasoning(live_blocks, live_answers)
                idle = now - last_activity
                if idle >= idle_limit:
                    break
                if now - last_update >= 15:
                    task_message = task_status_message(last_tasks)
                    prefix = task_message + " " if task_message else "Still waiting for Copilot response. "
                    emit_status("task" if task_message else "waiting",
                                f"{prefix}{int(now - started)}s elapsed; no new output for "
                                f"{int(idle)}s of {idle_limit}s allowed.",
                                last_tasks if task_message else None)
                    last_update = now
                if downloaded is None:
                    pause(page, 1500)
            if downloaded is None:
                page.screenshot(path=str(work / "copilot-timeout.png"), full_page=True)
                (work / "copilot-timeout.html").write_text(page.content(), encoding="utf-8")
                ctx.close()
                task_note = " " + task_status_message(last_tasks) if last_tasks else ""
                raise SystemExit(f"No downloadable result: Copilot showed no new output for {args.timeout}s."
                                 f"{task_note} Temporary diagnostics were removed with the run directory")
            # Download discovery starts only after Stop is gone; read the final response layout twice.
            emit_status("waiting", "Copilot response finished; downloadable repository found.")
            tasks = read_task_cards(page)
            if tasks != last_tasks:
                last_tasks = tasks
                emit_status("task", task_status_message(tasks) if tasks else "Copilot task card closed.", tasks)
            for attempt in range(2):
                pause(page, 1000)
                _, blocks, answers = read_screen(page, sel, prompt)
                if blocks is not None:
                    final_live = merge_live(live_blocks, live_answers, blocks, answers, final=attempt == 1)
                    if final_live != (live_blocks, live_answers):
                        live_blocks, live_answers = final_live
                        emit_reasoning(live_blocks, live_answers)
            if track_chat:
                current_url = conversation_url(page.url, args.url)
                if current_url and current_url != reported_url:
                    reported_url = current_url
                    emit_chat(current_url)
            chat_response = strip_speaker_label(read_chat_response(page))
            live_text = "\n\n".join(a["text"] for a in live_answers)
            if len(chat_response) < len(live_text):
                chat_response = live_text
            ctx.close()
        except QuitRequested:
            if submitted:
                emit_status("cancelled", "Quit requested: clicking Stop on the Copilot response...")
                if not stop_generation(page, sel):
                    emit_status("cancelled", "No running Copilot response was found to stop.")
            else:
                emit_status("cancelled", "Quit requested before the prompt was submitted.")
            try:
                ctx.close()
            except Exception:
                pass
            emit_status("cancelled", "Run cancelled. The repository was not changed.")
            raise SystemExit(QUIT_EXIT_CODE)
    if quit_requested():
        emit_status("cancelled", "Run cancelled before applying changes. The repository was not changed.")
        raise SystemExit(QUIT_EXIT_CODE)
    result_zip = work / f"{root.name}-modified.zip"
    emit_status("validating", "Validating downloaded ZIP contents...")
    result_hash = normalize_download(downloaded, result_zip)
    if result_hash == source_hash:
        raise SystemExit("Copilot returned a byte-identical archive; refusing to apply as no fix was detected.")
    emit_status("applying", "Backing up and applying repository changes...")
    backup = backup_and_replace(root, result_zip, work / "rollback")
    emit_status("review", "Collecting Git change summary...")
    diff = run(["git", "diff", "--stat"], cwd=root).stdout
    status = run(["git", "status", "--short"], cwd=root).stdout
    emit_status("completed", "Repository changes applied successfully.")
    print(json.dumps({"repo": str(root), "source_files": count, "result": "",
                      "backup": "", "status": status, "diff_stat": diff,
                      "chat_response": chat_response, "chat_url": reported_url or ""}, indent=2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="~/.copilot-repo-browser")
    ap.add_argument("--url", default=DEFAULT_URL)
    sp = ap.add_subparsers(dest="command", required=True)
    lp = sp.add_parser("login"); lp.set_defaults(fn=cmd_login)
    fp = sp.add_parser("fix")
    fp.add_argument("request")
    fp.add_argument("--repo", default=".")
    fp.add_argument("--work-dir", default=None, help="Optional parent for automatically deleted temporary round-trip files")
    fp.add_argument("--backup-dir", default=None, help="Deprecated; rollback copies are temporary and automatically deleted")
    fp.add_argument("--timeout", type=int, default=900,
                    help="Seconds without new on-screen output or a task-card change before giving up (default: 900)")
    fp.add_argument("--model", default="GPT/GPT-6",
                    help="Provider/model pair (default: GPT/GPT-6)")
    fp.add_argument("--show-browser", action="store_true", help="Show Chromium during fixes (default: invisible)")
    fp.add_argument("--headless", action="store_true", help="Deprecated; browser is already hidden by default")
    fp.add_argument("--allow-dirty", action="store_true")
    fp.add_argument("--temporary-chat", dest="temporary_chat", action="store_true", default=True,
                    help="Enable Copilot Temporary chat before selecting the model (default: enabled)")
    fp.add_argument("--no-temporary-chat", dest="temporary_chat", action="store_false",
                    help="Do not enable Temporary chat")
    fp.add_argument("--chat-url", default=None, metavar="URL",
                    help="Continue the saved Copilot chat at this URL (from a previous run's chat_url); "
                         "implies --no-temporary-chat. The current repository archive is attached again.")
    fp.add_argument("--attach", action="append", default=[], metavar="PATH",
                    help="Additional file (e.g. a picture) to attach to the prompt; repeat for several files")
    fp.set_defaults(fn=cmd_fix)
    args = ap.parse_args(); args.fn(args)

if __name__ == "__main__":
    main()
