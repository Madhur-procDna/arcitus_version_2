"""Format query result rows as plain NL lines (no markdown tables)."""

from __future__ import annotations

_DISPLAY_LIMIT = 10  # rows shown as NL bullets in the chat answer


_WIDE_ROW_THRESHOLD = 6  # fields per row above which we use one sub-bullet per field


def rows_to_nl_bullets(
    rows: list[dict],
    *,
    display_limit: int = _DISPLAY_LIMIT,
    label_prefix: str = "Row",
) -> tuple[str, int]:
    """
    Build NL bullets from rows.

    - Narrow rows (≤ _WIDE_ROW_THRESHOLD fields): one bullet, fields joined with ' · '
    - Wide rows (many fields, e.g. pivoted month columns): each field on its own indented line

    Returns (bullets_text, total_count).
    """
    total = len(rows)
    if not rows:
        return "", 0
    n = min(total, display_limit)
    lines: list[str] = []
    for i in range(n):
        row = rows[i]
        parts: list[str] = []
        for k, v in row.items():
            if v is None:
                continue
            sv = str(v).strip()
            if not sv:
                continue
            parts.append(f"{k}: {sv}")
        if not parts:
            continue
        if len(parts) > _WIDE_ROW_THRESHOLD:
            # Wide/pivoted row — each field gets its own readable line
            sub = "\n".join(f"  - {p}" for p in parts)
            lines.append(f"- {label_prefix} {i + 1}:\n{sub}")
        else:
            lines.append("- " + " · ".join(parts))
    return "\n".join(lines), total


def inject_nl_rows_before_sources(
    answer: str,
    rows: list[dict],
    *,
    display_limit: int = _DISPLAY_LIMIT,
) -> str:
    """
    Insert NL row bullets before the final 'Sources checked:' line (or append).

    Shows up to `display_limit` rows. When total > display_limit, adds a summary line
    with the total count and a note to use the CSV download for the full dataset.
    """
    bullets, total = rows_to_nl_bullets(rows, display_limit=display_limit)
    if not bullets:
        return answer

    if total > display_limit:
        header = (
            f"Total rows: {total} — showing top {display_limit} below. "
            f"Use **Download CSV** to get all {total} records."
        )
    else:
        header = f"Total rows: {total}"

    section = f"{header}\n\n{bullets}"

    sep = "Sources checked:"
    if sep in answer:
        head, tail = answer.split(sep, 1)
        return f"{head.rstrip()}\n\n{section}\n\n{sep}{tail}"
    return f"{answer.rstrip()}\n\n{section}"
