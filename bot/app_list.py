"""Telegram-safe app tables with complete, role-filtered details."""
from __future__ import annotations

import html

from .telegram_api import MESSAGE_LIMIT, chunk_text


def app_list_messages(entries):
    """Render bounded HTML pages, repeating the table headings on each page."""
    def render(page):
        width = max(3, *(len(item[0]) for item in page))
        rows = [f"{'App':<{width}}  Status", f"{'-' * width}  {'-' * 14}"]
        rows.extend(f"{name:<{width}}  {status.split(' (', 1)[0][:24]}"
                    for name, status, details in page)
        table = '<pre>' + html.escape('\n'.join(rows)) + '</pre>'
        details = '\n\n'.join(html.escape(item[2]) for item in page)
        return 'Apps:\n' + table + '\n\n' + details

    page = []
    for entry in entries:
        if page and len(render(page + [entry])) > MESSAGE_LIMIT:
            yield render(page)
            page = []
        if len(render([entry])) > MESSAGE_LIMIT:
            name, status, details = entry
            yield render([(name, status, '')])
            # Escape after splitting so entities and tags remain intact.
            for part in chunk_text(details, limit=600):
                yield html.escape(part)
        else:
            page.append(entry)
    if page:
        yield render(page)
