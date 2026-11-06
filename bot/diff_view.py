"""Diff summary of a commit: a total line and per-file addition and deletion counts."""

from __future__ import annotations

FILE = "\U0001f4c4"
MAX_FILES = 20


def parse_numstat(text: str):
    """Return (added, deleted, path) rows from git --numstat output; binary files have None counts."""
    rows = []
    for line in (text or "").splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        rows.append((int(added) if added.isdigit() else None,
                     int(deleted) if deleted.isdigit() else None, path))
    return rows


def format_changes(rows) -> str:
    """Render per-file +/- counts and a colored total line; empty when nothing changed."""
    rows = list(rows)
    if not rows:
        return ""
    added = sum(a or 0 for a, _, _ in rows)
    deleted = sum(d or 0 for _, d, _ in rows)
    count = len(rows)
    lines = [f"Changes: {count} file{'s' if count != 1 else ''} \u00b7 \U0001f7e2 +{added} \U0001f534 \u2212{deleted}"]
    for a, d, path in rows[:MAX_FILES]:
        if a is None or d is None:
            lines.append(f"{FILE} {path}  binary")
            continue
        lines.append(f"{FILE} {path}  +{a} \u2212{d}")
    if count > MAX_FILES:
        lines.append(f"\u2026 and {count - MAX_FILES} more file(s)")
    return "\n".join(lines)
