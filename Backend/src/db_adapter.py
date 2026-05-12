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

# Flat ``arcutis_data`` full-window TRx (Jan 2025–Mar 2026): ZORYVE + Other BNST only.
# TCS columns are a subset of Other BNST — never add ``tcs_*`` on top of ``other_bnst_*``.
# Use column names that match the live ERD (underscore before month vs ``jan25`` style).
FULL_DATASET_TOTAL_TRX_SQL = """(
    COALESCE(zoryve_jan_25,0)+COALESCE(zoryve_feb_25,0)+COALESCE(zoryve_mar_25,0)
    +COALESCE(zoryve_apr_25,0)+COALESCE(zoryve_may_25,0)+COALESCE(zoryve_jun_25,0)
    +COALESCE(zoryve_jul_25,0)+COALESCE(zoryve_aug_25,0)+COALESCE(zoryve_sep_25,0)
    +COALESCE(zoryve_oct_25,0)+COALESCE(zoryve_nov_25,0)+COALESCE(zoryve_dec_25,0)
    +COALESCE(zoryve_jan_26,0)+COALESCE(zoryve_feb_26,0)+COALESCE(zoryve_mar_26,0)
    +COALESCE(other_bnst_jan_25,0)+COALESCE(other_bnst_feb_25,0)+COALESCE(other_bnst_mar_25,0)
    +COALESCE(other_bnst_apr_25,0)+COALESCE(other_bnst_may_25,0)+COALESCE(other_bnst_jun_25,0)
    +COALESCE(other_bnst_jul_25,0)+COALESCE(other_bnst_aug_25,0)+COALESCE(other_bnst_sep_25,0)
    +COALESCE(other_bnst_oct_25,0)+COALESCE(other_bnst_nov_25,0)+COALESCE(other_bnst_dec_25,0)
    +COALESCE(other_bnst_jan_26,0)+COALESCE(other_bnst_feb_26,0)+COALESCE(other_bnst_mar_26,0)
) AS total_trx"""

Q1_2026_ONLY_TRX_SQL = """(
    COALESCE(zoryve_jan_26,0)+COALESCE(zoryve_feb_26,0)+COALESCE(zoryve_mar_26,0)
    +COALESCE(other_bnst_jan_26,0)+COALESCE(other_bnst_feb_26,0)+COALESCE(other_bnst_mar_26,0)
) AS total_trx"""


# Fix 3: call-response metrics must align TRx to the call-observable period only
# (Q2 2025-Q1 2026 = Apr 2025-Mar 2026). Jan-Mar 2025 predates available calls.
CALL_ALIGNED_ZORYVE_MONTH_COLS: tuple[str, ...] = (
    "zoryve_apr_25", "zoryve_may_25", "zoryve_jun_25",
    "zoryve_jul_25", "zoryve_aug_25", "zoryve_sep_25",
    "zoryve_oct_25", "zoryve_nov_25", "zoryve_dec_25",
    "zoryve_jan_26", "zoryve_feb_26", "zoryve_mar_26",
)
CALL_PERIOD_CALL_COLS: tuple[str, ...] = (
    "q2_25_calls", "q3_25_calls", "q4_25_calls", "q1_26_calls",
)
CALL_ALIGNED_MONTH_COUNT = 12.0


def _coalesce_sum(cols: tuple[str, ...]) -> str:
    return "+".join(f"COALESCE({col},0)" for col in cols)


CALL_ALIGNED_ZORYVE_TRX_EXPR = _coalesce_sum(CALL_ALIGNED_ZORYVE_MONTH_COLS)
CALL_PERIOD_TOTAL_CALLS_EXPR = _coalesce_sum(CALL_PERIOD_CALL_COLS)

# Fix 1: average monthly ZORYVE response is per HCP per month, not bucket total / months.
AVG_MONTHLY_ZORYVE_TRX_PER_HCP_SQL = f"""ROUND(
    SUM({CALL_ALIGNED_ZORYVE_TRX_EXPR})
    / NULLIF(COUNT(npi_id), 0)
    / {CALL_ALIGNED_MONTH_COUNT},
    2
) AS avg_monthly_zoryve_trx_per_hcp"""

# Fix 2: inadequate response is strictly the 4-6 call bucket; 7+ calls are excluded.
INADEQUATE_RESPONSE_HCPS_SQL = f"""COUNT(*) FILTER (
    WHERE ({CALL_PERIOD_TOTAL_CALLS_EXPR}) BETWEEN 4 AND 6
      AND (({CALL_ALIGNED_ZORYVE_TRX_EXPR}) / {CALL_ALIGNED_MONTH_COUNT}) < 5
) AS inadequate_response_hcps"""

# Fix 4/5: TCS is intentionally absent from both fragments because TCS is inside Other BNST,
# and total TRx formulas are always ZORYVE + Other BNST only.


# Territory mapping hints for prompts (Mountain vs Midwest).
MOUNTAIN_REGION_STATES: frozenset[str] = frozenset(
    {"AZ", "CO", "ID", "MT", "NM", "NV", "UT", "WY"}
)
MIDWEST_NOT_MOUNTAIN_STATES: frozenset[str] = frozenset({"KS", "MO", "IA"})


def arcutis_geography_territory_prompt_block() -> str:
    """Mountain region valid states only; KS, MO, IA are Midwest-only (never Mountain)."""
    mtn = ", ".join(sorted(MOUNTAIN_REGION_STATES))
    mid = ", ".join(sorted(MIDWEST_NOT_MOUNTAIN_STATES))
    return (
        "**Geography — Mountain vs Midwest**\n"
        f"- **`region = 'Mountain'` — valid `state` values ONLY:** `{mtn}` (2-letter).\n"
        f"- **`{mid}` are Midwest-only** — NEVER include them in a Mountain `WHERE state IN (...)` filter.\n"
    )


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
    geo = arcutis_geography_territory_prompt_block().rstrip()
    try:
        from pharma_schema import arcutis_metric_calculation_guardrails_block

        # Query-generation guardrail: keep call-response formulas next to live columns.
        metric_guardrails = arcutis_metric_calculation_guardrails_block().rstrip()
    except Exception:
        metric_guardrails = ""
    prefix = "\n\n".join(x for x in (geo, metric_guardrails) if x)

    def _prepend_geo(fragment: Optional[str]) -> Optional[str]:
        if not fragment:
            return prefix if prefix else None
        if not prefix:
            return fragment
        combined = prefix + "\n\n" + fragment
        return combined[:max_chars] + ("\n… [truncated]" if len(combined) > max_chars else "")

    if not use_sqlite_backend():
        return _prepend_geo(
            _pg_live_table_names_prompt_text(
                max_list_items=max_list_items, max_chars=max_chars,
            ),
        )
    if (os.getenv("SDA_LIVE_TABLE_NAMES") or "").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        return prefix if prefix else None
    from data_loader import get_db

    db = get_db()
    if not db or not db.tables:
        return prefix if prefix else None

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
    text = prefix + "\n\n" + text if prefix else text
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
