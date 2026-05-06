"""
NL row formatting utilities.

Main export: append_row_count_note
  Appends a concise row-count summary + CSV download prompt to the LLM answer
  before the "Sources checked:" line.  The LLM itself writes the descriptive
  Top Results bullets — this module no longer dumps raw field:value rows.
"""

from __future__ import annotations

_CSV_THRESHOLD = 10  # show download note only when rows exceed this


def append_row_count_note(answer: str, *, total: int) -> str:
    """
    Append a row-count summary line before 'Sources checked:'.

    When total > _CSV_THRESHOLD:
      "Total rows: 247 — showing top 10.  [Download full dataset (247 rows) as CSV]"
    Otherwise:
      "Total rows: 3"
    """
    if total <= 0:
        return answer

    if total > _CSV_THRESHOLD:
        note = (
            f"**Total rows:** {total} — showing top 10.  "
            f"[Download full dataset ({total} rows) as CSV]"
        )
    else:
        note = f"**Total rows:** {total}"

    sep = "Sources checked:"
    if sep in answer:
        head, tail = answer.split(sep, 1)
        return f"{head.rstrip()}\n\n{note}\n\n{sep}{tail}"
    return f"{answer.rstrip()}\n\n{note}"


# ── kept for internal use / tests ─────────────────────────────────────────────

def rows_to_nl_bullets(
    rows: list[dict],
    *,
    display_limit: int = _CSV_THRESHOLD,
) -> tuple[str, int]:
    """Mechanical NL bullets — used only in unit tests / fallback."""
    total = len(rows)
    if not rows:
        return "", 0
    n = min(total, display_limit)
    lines: list[str] = []
    for i in range(n):
        row = rows[i]
        parts = [f"{k}: {v}" for k, v in row.items() if v is not None and str(v).strip()]
        if not parts:
            continue
        if len(parts) > 6:
            sub = "\n".join(f"  - {p}" for p in parts)
            lines.append(f"- Row {i + 1}:\n{sub}")
        else:
            lines.append("- " + " · ".join(parts))
    return "\n".join(lines), total
