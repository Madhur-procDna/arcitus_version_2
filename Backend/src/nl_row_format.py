"""
NL row formatting utilities.

Main export: append_row_count_note
  Appends a concise row-count summary + CSV download prompt to the LLM answer
  before the "Sources checked:" line.  The LLM itself writes the descriptive
  Top Results bullets — this module no longer dumps raw field:value rows.
"""

from __future__ import annotations

from datetime import datetime

_CSV_THRESHOLD = 10  # show download note only when rows exceed this


def format_month_label(date_str: object) -> str:
    """Format ISO/month-like values as `Jan '25` for trend chart X-axis labels."""
    text = str(date_str or "").strip()
    if not text:
        return ""
    for fmt, candidate in (
        ("%Y-%m-%d", text[:10]),
        ("%Y-%m", text[:7]),
        ("%b %Y", text),
        ("%B %Y", text),
    ):
        try:
            return datetime.strptime(candidate, fmt).strftime("%b '%y")
        except ValueError:
            pass
    return text


def normalize_chart_month_labels(chart: dict | None) -> dict | None:
    """Normalize chart payload month labels before API serialization."""
    if not isinstance(chart, dict) or not isinstance(chart.get("data"), list):
        return chart
    data = []
    for row in chart["data"]:
        if not isinstance(row, dict):
            data.append(row)
            continue
        nxt = dict(row)
        if "name" in nxt:
            nxt["name"] = format_month_label(nxt["name"])
        elif "month" in nxt:
            nxt["month"] = format_month_label(nxt["month"])
        data.append(nxt)
    out = dict(chart)
    out["data"] = data
    return out


def append_row_count_note(answer: str, *, total: int) -> str:
    """
    Row-count note disabled — CSV download button provides data access.
    Function kept for API compatibility; returns answer unchanged.
    """
    return answer


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
