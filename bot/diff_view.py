"""Diff summary of a commit, rendered like git diff --stat: per-file counts and a compact +/− bar."""

from __future__ import annotations

# Plain text characters keep the bar at text size; Telegram renders every emoji, including colored dots, large.
ADDED, DELETED, FILE = "+", "\u2212", "\U0001f4c4"
BAR_WIDTH = 5
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


def _bar(added: int, deleted: int, peak: int) -> str:
    """Bar proportional to the largest file change; each nonzero side shows at least one character."""
    total = added + deleted
    if not total:
        return ""
    width = max(1, round(BAR_WIDTH * total / peak), bool(added) + bool(deleted))
    plus = min(max(round(width * added / total), 1 if added else 0), width - (1 if deleted else 0))
    return ADDED * plus + DELETED * (width - plus)


def format_changes(rows) -> str:
    """Render per-file +/- counts with bars and a colored total line; empty when nothing changed."""
    rows = list(rows)
    if not rows:
        return ""
    added = sum(a or 0 for a, _, _ in rows)
    deleted = sum(d or 0 for _, d, _ in rows)
    count = len(rows)
    lines = [f"Changes: {count} file{'s' if count != 1 else ''} \u00b7 \U0001f7e2 +{added} \U0001f534 \u2212{deleted}"]
    peak = max(((a or 0) + (d or 0) for a, d, _ in rows), default=0)
    for a, d, path in rows[:MAX_FILES]:
        if a is None or d is None:
            lines.append(f"{FILE} {path}  binary")
            continue
        bar = _bar(a, d, peak)
        lines.append(f"{FILE} {path}  +{a} \u2212{d}" + (f"  {bar}" if bar else ""))
    if count > MAX_FILES:
        lines.append(f"\u2026 and {count - MAX_FILES} more file(s)")
    return "\n".join(lines)
