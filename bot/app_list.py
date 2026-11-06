"""Telegram HTML app list: a status summary followed by one card per app."""
from __future__ import annotations

import html
from typing import NamedTuple

from .telegram_api import MESSAGE_LIMIT, chunk_text

TITLE = "📱 <b>Apps</b>"

# (status prefix, icon, summary label); the first matching prefix wins and "changing" is the fallback.
_STATUSES = (
    ("running", "🟢", "running"),
    ("stopped", "⚪", "stopped"),
    ("starting", "🟡", "starting"),
    ("failed", "🔴", "failed"),
    ("not registered", "⚫", "not registered"),
    ("unavailable", "❓", "unavailable"),
)
_OTHER = ("🟡", "changing")


class AppEntry(NamedTuple):
    """One list row. Empty fields are omitted, so callers decide what each role may see."""
    name: str
    status: str
    url: str = ""
    port: str = ""
    folder: str = ""
    note: str = ""


def _kind(status: str):
    lowered = status.lower()
    return next(((icon, label) for prefix, icon, label in _STATUSES if lowered.startswith(prefix)), _OTHER)


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _summary(entries) -> str:
    counts = {}
    for entry in entries:
        label = _kind(entry.status)[1]
        counts[label] = counts.get(label, 0) + 1
    order = [(icon, label) for _, icon, label in _STATUSES] + [_OTHER]
    return " · ".join(f"{icon} {counts[label]} {label}" for icon, label in order if label in counts)


def _title(entry: AppEntry) -> str:
    return f"{_kind(entry.status)[0]} <b>{_esc(entry.name)}</b> · {_esc(entry.status)}"


def _detail_lines(entry: AppEntry):
    lines = []
    if entry.note:
        lines.append(f"🤖 {_esc(entry.note)}")
    if entry.url:
        # Plain URL text: Telegram links it itself and opens it without the confirmation that
        # explicit <a href> hyperlinks trigger.
        lines.append(f"🔗 {_esc(entry.url)}")
    if entry.port:
        lines.append(f"🔌 Port {_esc(entry.port)}")
    if entry.folder:
        lines.append(f"📁 <code>{_esc(entry.folder)}</code>")
    return lines


def _card(entry: AppEntry) -> str:
    lines = _detail_lines(entry)
    return _title(entry) + ("\n<blockquote>" + "\n".join(lines) + "</blockquote>" if lines else "")


def _plain_details(entry: AppEntry) -> str:
    fields = (("Status: ", entry.status), ("", entry.note), ("URL: ", entry.url),
              ("Port: ", entry.port), ("Folder: ", entry.folder))
    return "\n".join(label + value for label, value in fields if value)


def app_list_messages(entries):
    """Yield HTML messages within the Telegram limit; later pages repeat a short heading."""
    entries = list(entries)
    summary = _summary(entries)

    def page_text(cards, first):
        if not first:
            head = TITLE + " (continued)"
        else:
            head = TITLE + ("\n" + summary if summary else "")
        return head + "\n\n" + "\n\n".join(cards)

    cards, first = [], True
    for entry in entries:
        card = _card(entry)
        if cards and len(page_text(cards + [card], first)) > MESSAGE_LIMIT:
            yield page_text(cards, first)
            cards, first = [], False
        if len(page_text([card], first)) > MESSAGE_LIMIT:
            # A card too large for one message is split as plain text; escaping happens after
            # splitting so entities stay intact.
            yield page_text([f"{_kind(entry.status)[0]} <b>{_esc(entry.name)}</b>"], first)
            first = False
            for part in chunk_text(_plain_details(entry), limit=600):
                yield html.escape(part)
        else:
            cards.append(card)
    if cards:
        yield page_text(cards, first)
