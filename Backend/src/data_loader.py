import os
import re
import sqlite3
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

@dataclass
class ColumnMeta:
    name: str
    dtype: str
    has_spaces: bool


@dataclass
class TableMeta:
    name: str          # SQL-safe identifier
    original_name: str # sheet / filename
    columns: list[ColumnMeta]
    row_count: int

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    @property
    def quoted_columns(self) -> list[str]:
        """Column names that require double-quoting in SQL (contain spaces)."""
        return [c.name for c in self.columns if c.has_spaces]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "original_name": self.original_name,
            "columns": self.column_names,
            "column_details": [
                {"name": c.name, "dtype": c.dtype, "has_spaces": c.has_spaces}
                for c in self.columns
            ],
            "row_count": self.row_count,
            "quoted_columns": self.quoted_columns,
        }


@dataclass
class DatabaseState:
    con: sqlite3.Connection
    tables: dict[str, TableMeta]
    file_name: str
    file_path: str
    loaded_at: str

    def to_info_dict(self) -> dict:
        """Serialisable summary — does NOT include the connection object."""
        return {
            "file_name": self.file_name,
            "file_path": self.file_path,
            "loaded_at": self.loaded_at,
            "table_count": len(self.tables),
            "tables": {name: meta.to_dict() for name, meta in self.tables.items()},
        }

_db_state: Optional[DatabaseState] = None

# Cache: ``f"{file_path}|{loaded_at}"`` → DISTINCT-hint markdown (invalidated on each load).
_LIVE_HINTS_CACHE: dict[str, str] = {}

# CREATE INDEX IF NOT EXISTS … on flat Arcutis sheet loaded as ``arcutis_data`` (SQLite).
_ARCUTIS_SQLITE_INDEX_SPECS: Tuple[Tuple[str, str], ...] = (
    ("idx_arcutis_area", "area"),
    ("idx_arcutis_state", "state"),
    ("idx_arcutis_region", "region"),
    ("idx_arcutis_target_flag", "q1_26_target_flag"),
    ("idx_arcutis_decile", "q1_26_decile"),
    ("idx_arcutis_specialty", "primary_specialty"),
    ("idx_arcutis_npi", "npi_id"),
)


def _configure_sqlite_performance(con: sqlite3.Connection) -> None:
    """Tune SQLite for analytical workloads (best-effort; ignored if unsupported)."""
    pragmas = (
        "PRAGMA journal_mode=WAL",
        "PRAGMA cache_size=-64000",  # ~64MB page cache (negative = KiB units)
        "PRAGMA synchronous=NORMAL",
        "PRAGMA temp_store=MEMORY",
    )
    for pragma in pragmas:
        try:
            con.execute(pragma)
        except sqlite3.Error as exc:
            logger.debug("SQLite pragma skipped (%s): %s", pragma.split()[1], exc)


def _ensure_arcutis_data_indexes_sqlite(
    con: sqlite3.Connection, tables: dict[str, TableMeta]
) -> None:
    """Create filter indexes on ``arcutis_data`` when that table exists with the columns."""
    tname = next((k for k in tables if k.lower() == "arcutis_data"), None)
    if not tname:
        return
    meta = tables[tname]
    col_by_lower = {c.name.lower(): c.name for c in meta.columns}
    tq = _sqlite_quote_ident(tname)
    created = 0
    for idx_name, col_logical in _ARCUTIS_SQLITE_INDEX_SPECS:
        actual = col_by_lower.get(col_logical.lower())
        if not actual:
            continue
        cq = _sqlite_quote_ident(actual)
        ddl = f"CREATE INDEX IF NOT EXISTS {_sqlite_quote_ident(idx_name)} ON {tq} ({cq})"
        try:
            con.execute(ddl)
            created += 1
        except sqlite3.Error as exc:
            logger.warning("SQLite index %s on %s.%s failed: %s", idx_name, tname, actual, exc)
    if created:
        logger.info("SQLite: ensured %d indexes on %s", created, tname)


def _sqlite_quote_ident(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def build_live_categorical_hints(db: DatabaseState, *, max_probes: int = 28) -> str:
    """
    Sample up to ``max_probes`` likely-categorical columns with ``SELECT DISTINCT`` from the
    loaded workbook. Injected into the SQL steward prompt so filters use **real** literals
    (Y/N, Full, Positive Engagement, …) instead of guessed ``Yes`` / ``Accepted``.
    """
    import re

    fp = f"{db.file_path}|{db.loaded_at}"
    if fp in _LIVE_HINTS_CACHE:
        return _LIVE_HINTS_CACHE[fp]

    cat_re = re.compile(
        r"(flag|dnc|credit|outcome|engagement|channel|barrier|status|alignment|segment|decile|"
        r"priority|opt|merge|creditable|classify|risk|theme|response|type|role|tier|level)",
        re.I,
    )

    pairs: list[tuple[str, ColumnMeta]] = []
    for tname, meta in sorted(db.tables.items(), key=lambda x: x[0].lower()):
        for cm in sorted(meta.columns, key=lambda c: c.name.lower()):
            if cat_re.search(cm.name):
                pairs.append((tname, cm))
    pairs = pairs[:max_probes]

    lines: list[str] = []
    for tname, cm in pairs:
        tq = _sqlite_quote_ident(tname)
        cq = _sqlite_quote_ident(cm.name)
        sql = f"SELECT DISTINCT {cq} AS v FROM {tq} WHERE {cq} IS NOT NULL LIMIT 25"
        try:
            cur = db.con.execute(sql)
            raw_vals: list[str] = []
            for (v,) in cur.fetchall():
                if v is None:
                    continue
                s = str(v).strip()
                if not s or s in raw_vals:
                    continue
                raw_vals.append(s)
                if len(raw_vals) >= 20:
                    break
        except (sqlite3.Error, TypeError, ValueError) as exc:
            logger.debug("live hint skip %s.%s: %s", tname, cm.name, exc)
            continue
        if not raw_vals:
            continue
        shown = ", ".join(raw_vals)
        if len(raw_vals) >= 20:
            shown += " …"
        lines.append(f"  • `{tname}`.`{cm.name}` → {shown}")

    if not lines:
        _LIVE_HINTS_CACHE[fp] = ""
        return ""

    header = (
        "LIVE DISTINCT VALUES (auto-sampled from this workbook — use these literals in WHERE / IN; "
        "do not assume Yes/No/Accepted unless they appear here):"
    )
    out = header + "\n" + "\n".join(lines)
    if len(out) > 9000:
        out = out[:8990] + "\n… [truncated]"
    _LIVE_HINTS_CACHE[fp] = out + "\n"
    return _LIVE_HINTS_CACHE[fp]


def get_db() -> Optional[DatabaseState]:
    """Return the currently loaded DatabaseState, or None if no file loaded yet."""
    return _db_state

def _safe_table_name(raw: str) -> str:
    """
    Convert an arbitrary sheet/file name into a valid SQLite identifier.
    Replaces all non-alphanumeric characters with underscores;
    prefixes with 't_' if the name starts with a digit.
    """
    safe = re.sub(r"[^\w]", "_", raw.strip())
    if safe and safe[0].isdigit():
        safe = "t_" + safe
    return safe

def _load_file_impl(path: Path) -> DatabaseState:
    """Load workbook at ``path`` (must exist and be readable)."""
    global _db_state

    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"Data file not found: {path}")

    suffix = path.suffix.lower()
    if suffix not in {".xlsx", ".xls", ".csv"}:
        raise ValueError(
            f"Unsupported file format '{suffix}'. "
            "Please provide an .xlsx, .xls, or .csv file."
        )

    logger.info("Loading data file: %s", path)
    _LIVE_HINTS_CACHE.clear()

    if suffix in {".xlsx", ".xls"}:
        raw_sheet_map: dict[str, pd.DataFrame] = pd.read_excel(
            path, sheet_name=None, header=None, dtype=str
        )
        sheet_map: dict[str, pd.DataFrame] = {}
        for sname, raw_df in raw_sheet_map.items():
            # Skip non-data sheets (question lists, notes, instructions)
            if sname.strip().lower() in ("questions", "question", "notes", "instructions", "readme"):
                logger.info("Skipping non-data sheet: %s", sname)
                continue
            # Find the real header row: first row where ≥3 cells are non-empty and mostly non-numeric
            header_row = 0
            for ri in range(min(5, len(raw_df))):
                row_vals = [str(v).strip() for v in raw_df.iloc[ri] if pd.notna(v) and str(v).strip()]
                if len(row_vals) == 0:
                    continue
                numeric_count = sum(1 for v in row_vals if re.match(r"^-?\d+(\.\d+)?$", v))
                if len(row_vals) >= 3 and (numeric_count / len(row_vals)) < 0.7:
                    header_row = ri
                    break
            df = pd.read_excel(path, sheet_name=sname, header=header_row, dtype=str)
            # Drop leading unnamed index columns (blank first column from Excel)
            df = df.loc[:, ~df.columns.str.fullmatch(r"Unnamed: \d+")]
            df = df.dropna(how="all")
            if df.empty:
                logger.warning("Skipping empty sheet: %s", sname)
                continue
            sheet_map[sname] = df
    else:  # CSV
        df = pd.read_csv(path, dtype=str)
        sheet_map = {path.stem: df}

    if not sheet_map:
        raise ValueError("The file contains no sheets / data.")

    con = sqlite3.connect(":memory:", check_same_thread=False)
    _configure_sqlite_performance(con)
    tables: dict[str, TableMeta] = {}

    for sheet_name, df in sheet_map.items():
        if df.empty:
            logger.warning("Skipping empty sheet: %s", sheet_name)
            continue

        df.columns = [str(c).strip() for c in df.columns]

        for col in df.columns:
            try:
                coerced = pd.to_numeric(df[col], errors="coerce")
                # Only apply if at least 80% of non-null values converted cleanly
                non_null = df[col].notna().sum()
                converted = coerced.notna().sum()
                if non_null > 0 and converted / non_null >= 0.8:
                    df[col] = coerced
            except Exception:
                pass

        table_name = _safe_table_name(sheet_name)

        tcs_cols = [c for c in df.columns if str(c).lower().startswith("tcs_")]
        for tcs_col in tcs_cols:
            bnst_col = str(tcs_col).lower().replace("tcs_", "other_bnst_", 1)
            # Find actual bnst column name matching case-insensitively
            actual_bnst_col = next((c for c in df.columns if str(c).lower() == bnst_col), None)
            if actual_bnst_col:
                tcs_vals = pd.to_numeric(df[tcs_col], errors="coerce").fillna(0)
                bnst_vals = pd.to_numeric(df[actual_bnst_col], errors="coerce").fillna(0)
                if not (tcs_vals <= bnst_vals).all():
                    raise ValueError(f"Data mapping error in ETL pipeline: {tcs_col} values exceed {actual_bnst_col}")

        # Write to SQLite
        df.to_sql(table_name, con, if_exists="replace", index=False)

        # Build column metadata
        col_metas = [
            ColumnMeta(
                name=col,
                dtype=str(df[col].dtype),
                has_spaces=" " in col,
            )
            for col in df.columns
        ]

        tables[table_name] = TableMeta(
            name=table_name,
            original_name=sheet_name,
            columns=col_metas,
            row_count=len(df),
        )
        logger.info("  Loaded table '%s' (%d rows, %d cols)", table_name, len(df), len(df.columns))

    if not tables:
        raise ValueError("No usable data found in the file (all sheets were empty).")

    _ensure_arcutis_data_indexes_sqlite(con, tables)

    _db_state = DatabaseState(
        con=con,
        tables=tables,
        file_name=path.name,
        file_path=str(path),
        loaded_at=datetime.now(timezone.utc).isoformat(),
    )

    logger.info("Database ready: %d tables loaded from '%s'", len(tables), path.name)
    return _db_state


def load_file(path: str | Path) -> DatabaseState:
    """
    Load an Excel (.xlsx, .xls) or CSV file into an in-memory SQLite database.

    Each sheet in Excel becomes a table.
    A single CSV file becomes one table named after the filename stem.

    On ``PermissionError`` (common with OneDrive-locked ``Backend\\src\\*.xlsx``), retries other
    candidates from ``DATA_FILE_PATH`` resolution (e.g. ``Desktop\\sql\\`` copy).
    """
    primary = Path(path).resolve()
    tried: set[Path] = set()
    order: list[Path] = [primary]
    try:
        from config import iter_workbook_candidate_paths

        ref = (os.getenv("DATA_FILE_PATH") or "").strip() or str(primary)
        for cand in iter_workbook_candidate_paths(ref):
            rp = cand.resolve()
            if rp not in tried:
                order.append(rp)
    except Exception:
        pass

    last_pe: PermissionError | None = None
    for cand in order:
        if cand in tried:
            continue
        tried.add(cand)
        try:
            return _load_file_impl(cand)
        except PermissionError as pe:
            last_pe = pe
            logger.warning(
                "Permission denied reading workbook %s — trying next candidate if any.",
                cand,
            )
            continue
    if last_pe is not None:
        raise last_pe
    raise FileNotFoundError(f"Data file not found or not readable: {primary}")


def execute_query(sql: str, limit: int = 500, *, apply_limit: bool = True) -> list[dict]:
    """
    Execute a validated SELECT query against the loaded in-memory SQLite database.

    Args:
        sql:   A sanitised SQL string (no trailing semicolon).
        limit: Hard cap on returned rows (protects against accidental full scans).
        apply_limit: When False, do not append a LIMIT clause (used for unlimited pipeline turns).

    Returns:
        List of row dicts with column names as keys.

    Raises:
        RuntimeError: If no data file has been loaded yet.
        sqlite3.Error: On any SQL execution error.
    """
    db = get_db()
    if db is None:
        raise RuntimeError(
            "No data file loaded. POST a file to /data/upload first."
        )

    # Enforce a hard row limit — append or replace LIMIT clause
    sql_stripped = sql.rstrip().rstrip(";")
    if apply_limit and not re.search(r"\bLIMIT\b", sql_stripped, re.IGNORECASE):
        sql_stripped = f"{sql_stripped} LIMIT {limit}"

    cursor = db.con.execute(sql_stripped)
    col_names = [d[0] for d in cursor.description]
    rows = [dict(zip(col_names, row)) for row in cursor.fetchall()]
    return rows
