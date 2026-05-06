"""Working Postgres query runner for SDA pipeline."""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from env_loader import load_application_dotenv
from pharma_schema import pharma_db_schema
from retry_utils import postgres_retry

logger = logging.getLogger(__name__)

try:
    import psycopg2
    from psycopg2 import sql as pg_sql
    from psycopg2.extras import RealDictCursor
except Exception as e:  # pragma: no cover - optional dependency
    psycopg2 = None  # type: ignore
    RealDictCursor = None  # type: ignore
    pg_sql = None  # type: ignore
    _IMPORT_ERROR = e
else:
    _IMPORT_ERROR = None


DEFAULT_CONNECT_TIMEOUT = 10
DEFAULT_STATEMENT_TIMEOUT_MS = 60000
DEFAULT_MAX_ROWS = 5000


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _get_env_var(*keys: str) -> str | None:
    for key in keys:
        value = os.getenv(key)
        if value and value.strip():
            return value.strip()
    return None


def _require_driver() -> None:
    if psycopg2 is None:
        raise RuntimeError(
            "psycopg2 is not installed. Install it with: pip install psycopg2-binary"
        ) from _IMPORT_ERROR


def _db_config() -> Dict[str, Any]:
    load_application_dotenv()
    host = _get_env_var("PGHOST", "pg_host", "POSTGRES_HOST")
    dbname = _get_env_var("PGDATABASE", "pg_dbname", "POSTGRES_DB")
    user = _get_env_var("PGUSER", "pg_user", "POSTGRES_USER")
    password = _get_env_var("PGPASSWORD", "pg_password", "POSTGRES_PASSWORD")
    port = int(_get_env_var("PGPORT", "pg_port") or 5432)
    connect_timeout = _env_int("POSTGRES_CONNECT_TIMEOUT", DEFAULT_CONNECT_TIMEOUT)

    missing: list[str] = []
    if not host:
        missing.append("PGHOST")
    if not dbname:
        missing.append("PGDATABASE")
    if not user:
        missing.append("PGUSER")
    if not password:
        missing.append("PGPASSWORD")
    if missing:
        raise RuntimeError("Postgres env incomplete: " + ", ".join(missing))

    return {
        "host": host,
        "port": port,
        "dbname": dbname,
        "user": user,
        "password": password,
        "connect_timeout": connect_timeout,
    }


def _set_session_search_path(cur: Any) -> None:
    if pg_sql is None:
        return
    schema = pharma_db_schema()
    cur.execute(
        pg_sql.SQL("SET LOCAL search_path TO {}, public").format(pg_sql.Identifier(schema))
    )


def _live_table_catalog_disabled() -> bool:
    return (os.getenv("SDA_LIVE_TABLE_NAMES") or "1").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    )


_SIMPLE_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def _quote_ident_if_needed(name: str) -> str:
    return name if _SIMPLE_IDENT_RE.match(name) else f'"{name}"'


def _json_safe_cell(value: Any) -> Any:
    if isinstance(value, Decimal):
        try:
            return float(value)
        except Exception:
            return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _fetch_live_catalog(max_names: int) -> Tuple[List[str], str]:
    if _live_table_catalog_disabled() or psycopg2 is None:
        return [], ""
    cfg = _db_config()
    schema = pharma_db_schema()
    try:
        with psycopg2.connect(**cfg) as conn:
            with conn.cursor() as cur:
                _set_session_search_path(cur)
                cur.execute(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = %s AND table_type = 'BASE TABLE'
                    ORDER BY table_name
                    LIMIT %s;
                    """,
                    (schema, max_names),
                )
                names = [str(r[0]) for r in cur.fetchall()]
                if not names:
                    return [], ""
                cur.execute(
                    """
                    SELECT table_name, column_name
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = ANY(%s)
                    ORDER BY table_name, ordinal_position
                    """,
                    (schema, names),
                )
                cols_by_table: Dict[str, List[str]] = {}
                for tname, cname in cur.fetchall():
                    t = str(tname)
                    c = str(cname)
                    cols_by_table.setdefault(t, []).append(c)
                lines: List[str] = []
                for t in names:
                    cols = cols_by_table.get(t, [])
                    qcols = ", ".join(_quote_ident_if_needed(c) for c in cols)
                    lines.append(f"  {_quote_ident_if_needed(t)}({qcols})")
                return names, "\n".join(lines)
    except Exception as exc:
        logger.warning("[postgres_runner_live] _fetch_live_catalog failed: %s", exc)
        return [], ""


def fetch_live_base_table_names(*, max_names: int = 800) -> List[str]:
    names, _ = _fetch_live_catalog(max_names=max_names)
    return names


def get_table_columns_hint(table_name: str) -> str:
    if psycopg2 is None:
        return ""
    cfg = _db_config()
    schema = pharma_db_schema()
    try:
        with psycopg2.connect(**cfg) as conn:
            with conn.cursor() as cur:
                _set_session_search_path(cur)
                cur.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s
                    ORDER BY ordinal_position
                    """,
                    (schema, table_name),
                )
                cols = [str(r[0]) for r in cur.fetchall()]
                if cols:
                    return f"Table `{table_name}` actual columns in schema `{schema}`: {', '.join(cols)}"
                return f"Table `{table_name}` was not found in schema `{schema}`."
    except Exception as exc:
        logger.warning("[postgres_runner_live] get_table_columns_hint(%s): %s", table_name, exc)
        return ""


def live_table_names_prompt_text(
    *,
    max_list_items: int = 260,
    max_chars: int = 14000,
) -> Optional[str]:
    names, col_lines = _fetch_live_catalog(max_names=max_list_items + 400)
    if not names:
        return None
    schema = pharma_db_schema()
    display = names[:max_list_items]
    suffix = ""
    if len(names) > max_list_items:
        suffix = f"\n... and {len(names) - max_list_items} more tables in schema `{schema}`."
    text = f"Schema `{schema}` live base tables. Use these exact table names in FROM/JOIN:\n" + ", ".join(display) + suffix
    if col_lines:
        text += (
            "\n\nLive table columns (authoritative; use exact identifiers and quoting):\n"
            + col_lines
        )
    if len(text) > max_chars:
        return text[:max_chars] + "\n... [truncated]"
    return text


@postgres_retry
def _execute_query(sql: str, max_rows: int | None) -> List[Dict[str, Any]]:
    cfg = _db_config()
    statement_timeout_ms = _env_int(
        "POSTGRES_STATEMENT_TIMEOUT_MS", DEFAULT_STATEMENT_TIMEOUT_MS
    )
    with psycopg2.connect(**cfg) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            _set_session_search_path(cur)
            cur.execute(f"SET LOCAL statement_timeout = {statement_timeout_ms};")
            cur.execute(sql)
            if max_rows is None:
                rows = cur.fetchall()
            else:
                rows = cur.fetchmany(max_rows)
            out: List[Dict[str, Any]] = []
            for row in rows:
                out.append({k: _json_safe_cell(v) for k, v in dict(row).items()})
            return out


def run_query(sql: str, max_rows: int | None = None, unlimited: bool = False) -> List[Dict[str, Any]]:
    _require_driver()
    if unlimited:
        return _execute_query(sql, None)
    if max_rows is None:
        max_rows = _env_int("POSTGRES_MAX_ROWS", DEFAULT_MAX_ROWS)
    try:
        return _execute_query(sql, max_rows)
    except Exception as exc:
        raise RuntimeError(f"Postgres execution failed: {exc}") from exc

