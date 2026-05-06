"""
Query backend switch: **SQLite** (Arcetus Excel via ``data_loader``) vs **Postgres** (``postgres_runner``).

The QA pipeline imports ``run_query``, catalog helpers, and ``extract_failing_identifiers`` from here
so one codebase can target either engine.

- ``SDA_DATA_SOURCE=sqlite`` (default in ``config.Settings``) — in-memory DB from ``data_file_path``.
- ``SDA_DATA_SOURCE=postgres`` — existing ``PGHOST`` / ``PGDATABASE`` flow.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def use_sqlite_backend() -> bool:
    """True when the app should use ``data_loader`` instead of ``postgres_runner``."""
    raw = (os.getenv("SDA_DATA_SOURCE") or "").strip().lower()
    if raw in ("postgres", "postgresql", "pg"):
        return False
    if raw in ("sqlite", "arcetus", "file", "excel", "1", "true", "yes", "on"):
        return True
    try:
        from config import settings

        v = str(getattr(settings, "sda_data_source", "sqlite")).strip().lower()
        return v not in ("postgres", "postgresql", "pg")
    except Exception:
        return True


def sqlglot_dialect_for_backend() -> str:
    return "sqlite" if use_sqlite_backend() else (os.getenv("SQLGLOT_DIALECT") or "postgres").strip() or "postgres"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def extract_failing_identifiers(error_msg: str) -> List[str]:
    """Return quoted identifiers from SQL engine error messages."""
    found: List[str] = []
    for match in re.finditer(r'"([^"]+)"', error_msg):
        token = match.group(1)
        if token and not token.startswith("$"):
            found.append(token)
    return found


# ---------------------------------------------------------------------------
# Postgres implementations
# ---------------------------------------------------------------------------


def _pg_fetch_live_base_table_names(*, max_names: int = 800) -> List[str]:
    from postgres_runner_live import fetch_live_base_table_names as _fn

    return _fn(max_names=max_names)


def _pg_get_table_columns_hint(table_name: str) -> str:
    from postgres_runner_live import get_table_columns_hint as _fn

    return _fn(table_name)


def _pg_live_table_names_prompt_text(
    *,
    max_list_items: int = 260,
    max_chars: int = 14000,
) -> Optional[str]:
    from postgres_runner_live import live_table_names_prompt_text as _fn

    return _fn(max_list_items=max_list_items, max_chars=max_chars)


def _pg_run_query(
    sql: str, max_rows: int | None = None, unlimited: bool = False
) -> List[Dict[str, Any]]:
    from postgres_runner_live import run_query as _fn

    return _fn(sql, max_rows=max_rows, unlimited=unlimited)


# ---------------------------------------------------------------------------
# SQLite catalog + execution
# ---------------------------------------------------------------------------


def fetch_live_base_table_names(*, max_names: int = 800) -> List[str]:
    if not use_sqlite_backend():
        return _pg_fetch_live_base_table_names(max_names=max_names)
    from data_loader import get_db

    db = get_db()
    if not db or not db.tables:
        return []
    names = sorted(db.tables.keys())
    return names[:max_names]


def get_table_columns_hint(table_name: str) -> str:
    if not use_sqlite_backend():
        return _pg_get_table_columns_hint(table_name)
    from data_loader import get_db

    db = get_db()
    if not db:
        return ""
    key = table_name.strip()
    meta = db.tables.get(key)
    if meta is None:
        lk = key.lower()
        for tname, m in db.tables.items():
            if tname.lower() == lk:
                meta = m
                break
    if not meta:
        return f"Table `{table_name}` was not found in the loaded workbook."
    cols = ", ".join(meta.column_names)
    return f"Table `{meta.name}` columns: {cols}"


def live_table_names_prompt_text(
    *,
    max_list_items: int = 260,
    max_chars: int = 14000,
) -> Optional[str]:
    if not use_sqlite_backend():
        return _pg_live_table_names_prompt_text(
            max_list_items=max_list_items, max_chars=max_chars
        )
    if (os.getenv("SDA_LIVE_TABLE_NAMES") or "").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        return None
    from data_loader import get_db

    db = get_db()
    if not db or not db.tables:
        return None

    lines: List[str] = []
    for tname in sorted(db.tables.keys()):
        m = db.tables[tname]
        qcols = [c for c in m.column_names if " " in c]
        col_list = ", ".join(f'"{c}"' if " " in c else c for c in m.column_names)
        lines.append(f"  {m.name}({col_list})  -- {m.row_count} rows, sheet: {m.original_name!r}")
        if qcols:
            lines.append(
                f"    (columns with spaces must be double-quoted in SQL: {', '.join(repr(c) for c in qcols)})"
            )

    header = (
        "**SQLite** database loaded from the workbook — these are the **only** valid table names. "
        "Every **FROM** / **JOIN** must use one of them. Use **LIKE** with **COLLATE NOCASE** for "
        "case-insensitive text; **ILIKE** is not available. Prefer **strftime** for dates stored as text. "
        "Do **not** use PostgreSQL-only syntax (**::cast**, **ILIKE**, **DATE_TRUNC**, **GENERATE_SERIES**, …) "
        "unless SQLite supports it.\n\n"
        "**Live table columns (authoritative):**\n"
    )
    body = "\n".join(lines)
    text = header + body
    if len(text) > max_chars:
        text = text[:max_chars] + "\n… [truncated]"
    return text


def run_query(
    sql: str, max_rows: int | None = None, unlimited: bool = False
) -> List[Dict[str, Any]]:
    if not use_sqlite_backend():
        return _pg_run_query(sql, max_rows=max_rows, unlimited=unlimited)

    from config import settings
    from data_loader import execute_query

    try:
        if unlimited:
            cap = int(os.getenv("SQLITE_UNLIMITED_ROW_CAP", "1000000"))
            return execute_query(sql, limit=cap, apply_limit=True)
        lim = max_rows if max_rows is not None else int(settings.query_row_limit or 500)
        return execute_query(sql, limit=lim, apply_limit=True)
    except sqlite3.Error as exc:
        raise RuntimeError(f"SQLite query error: {exc}") from exc
    except RuntimeError:
        raise
