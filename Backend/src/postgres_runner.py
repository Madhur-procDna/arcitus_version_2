

# """Postgres fetcher: credentials from environment only (see .env).

# Governance & reliability improvements over v1
# ---------------------------------------------
# 1. connect_timeout   – psycopg2.connect() now passes connect_timeout (default 10s).
#                        A hung DB will no longer block the pipeline indefinitely.
# 2. statement_timeout – SET LOCAL statement_timeout prevents runaway queries from
#                        holding a connection open for minutes.
# 3. Row cap           – fetchmany(MAX_ROWS) replaces fetchall() to prevent OOM on
#                        large result sets. Cap controlled by POSTGRES_MAX_ROWS env var
#                        (default 5000).
# 4. Retry             – Transient OperationalError / InterfaceError (connection reset,
#                        server restart) are retried with exponential backoff via
#                        postgres_retry() from retry_utils.
# 5. Error handling    – run_query() now wraps execution in try/except and surfaces
#                        clear RuntimeError messages instead of raw psycopg2 tracebacks.
# 6. Credential safety – _db_config() redacts the password in any __repr__ / log output
#                        via a SafeConfig wrapper so it cannot leak into LangSmith traces.
# """

# from __future__ import annotations

# import json
# import logging
# import os
# import re
# from collections import defaultdict
# from typing import Any, Dict, List, Optional, Tuple

# from env_loader import load_application_dotenv
# from pharma_schema import pharma_db_schema
# from retry_utils import postgres_retry

# logger = logging.getLogger(__name__)

# try:
#     import psycopg2
#     from psycopg2 import sql as pg_sql
#     from psycopg2.extras import RealDictCursor
# except Exception as e:  # pragma: no cover - optional dependency
#     psycopg2 = None  # type: ignore
#     RealDictCursor = None  # type: ignore
#     pg_sql = None  # type: ignore
#     _IMPORT_ERROR = e
# else:
#     _IMPORT_ERROR = None


# # ---------------------------------------------------------------------------
# # Configuration
# # ---------------------------------------------------------------------------


# def _set_session_search_path(cur: Any) -> None:
#     """Match unqualified SQL to the same schema as ``ERD.md`` / ``pharma_schema`` (default ``public``)."""
#     if pg_sql is None:
#         return
#     schema = pharma_db_schema()
#     cur.execute(pg_sql.SQL("SET LOCAL search_path TO {}, public").format(pg_sql.Identifier(schema)))


# def _programming_error_hint(exc: Exception) -> str:
#     raw = str(exc)
#     if "does not exist" in raw and "relation" in raw:
#         sch = pharma_db_schema()
#         return (
#             f"{raw}\n\n"
#             "Hint: The model generated SQL for tables in `ERD.md` (e.g. `patient`). "
#             f"This connection uses schema `{sch}` first on `search_path` (from SDA_PHARMA_SCHEMA / PGSCHEMA). "
#             "If your tables live elsewhere, set that env var to the correct schema. "
#             "If the database was never loaded, run `Backend/mock_data.py` (or your DDL) against `PGDATABASE`."
#         )
#     return raw


# DEFAULT_CONNECT_TIMEOUT = 10          # seconds — how long to wait for TCP connect
# # NL→SQL wide joins (many LEFT JOINs) often need >30s on first cold cache; override with POSTGRES_STATEMENT_TIMEOUT_MS.
# DEFAULT_STATEMENT_TIMEOUT_MS = 60000  # milliseconds — max query execution time (60s)
# DEFAULT_MAX_ROWS = 5000               # hard cap on rows returned per query


# def _env_int(key: str, default: int) -> int:
#     raw = os.getenv(key, "").strip()
#     try:
#         return int(raw) if raw else default
#     except ValueError:
#         return default


# def _get_env_var(*keys: str) -> str | None:
#     for key in keys:
#         value = os.getenv(key)
#         if value:
#             return value
#     return None


# # ---------------------------------------------------------------------------
# # Credential safety wrapper
# # ---------------------------------------------------------------------------

# class _SafeConfig(dict):
#     """Dict subclass that redacts 'password' in repr/str so it never leaks
#     into LangSmith trace payloads, log lines, or exception messages."""

#     def __repr__(self) -> str:
#         safe = {k: ("***" if k == "password" else v) for k, v in self.items()}
#         return f"_SafeConfig({safe!r})"

#     def __str__(self) -> str:
#         return self.__repr__()


# def _db_config() -> _SafeConfig:
#     load_application_dotenv()
#     host = _get_env_var("PGHOST", "pg_host", "POSTGRES_HOST")
#     dbname = _get_env_var("PGDATABASE", "pg_dbname", "POSTGRES_DB")
#     user = _get_env_var("PGUSER", "pg_user", "POSTGRES_USER")
#     password = _get_env_var("PGPASSWORD", "pg_password", "POSTGRES_PASSWORD")

#     missing: list[str] = []
#     if not host or (isinstance(host, str) and not host.strip()):
#         missing.append("PGHOST (or POSTGRES_HOST)")
#     if not dbname or (isinstance(dbname, str) and not dbname.strip()):
#         missing.append("PGDATABASE (or POSTGRES_DB)")
#     if not user or (isinstance(user, str) and not user.strip()):
#         missing.append("PGUSER (or POSTGRES_USER)")
#     if password is None or (isinstance(password, str) and not password.strip()):
#         missing.append("PGPASSWORD (or POSTGRES_PASSWORD)")
#     if missing:
#         raise RuntimeError(
#             "Postgres env incomplete — empty or unset: "
#             + ", ".join(missing)
#             + ". Set them in DSA/src/.env (loaded before repo-root .env). "
#             "Note: PGDATABASE must not be left blank after '='."
#         )

#     connect_timeout = _env_int("POSTGRES_CONNECT_TIMEOUT", DEFAULT_CONNECT_TIMEOUT)

#     return _SafeConfig(
#         host=host,
#         port=int(_get_env_var("PGPORT", "pg_port") or 5432),
#         dbname=dbname,
#         user=user,
#         password=password,
#         connect_timeout=connect_timeout,
#     )


# def _require_driver() -> None:
#     if psycopg2 is None:
#         raise RuntimeError(
#             "psycopg2 is not installed. Install it with: pip install psycopg2-binary"
#         ) from _IMPORT_ERROR


# def _live_table_catalog_disabled() -> bool:
#     return (os.getenv("SDA_LIVE_TABLE_NAMES") or "1").strip().lower() in (
#         "0",
#         "false",
#         "no",
#         "off",
#     )


# def _table_columns_lower(cur: Any, schema: str, table: str) -> set[str]:
#     cur.execute(
#         """
#         SELECT column_name
#         FROM information_schema.columns
#         WHERE table_schema = %s AND table_name = %s
#         """,
#         (schema, table),
#     )
#     return {str(r[0]).lower() for r in cur.fetchall()}


# def _legacy_column_hint_lines(cur: Any, schema: str, name_lower: set[str]) -> List[str]:
#     """Detect legacy Takeda-style PK/FK names so text-to-SQL matches the live DB."""
#     lines: List[str] = []
#     if "sales_rep" in name_lower:
#         c = _table_columns_lower(cur, schema, "sales_rep")
#         if "rep_id" in c and "sales_rep_id" not in c:
#             lines.append(
#                 "`sales_rep`: primary key is **`rep_id`** (not `sales_rep_id`); rep label **`rep_name`**; "
#                 "join **`region`** on **`sales_rep.region_id = region.region_id`**."
#             )
#     if "rep_activity" in name_lower:
#         c = _table_columns_lower(cur, schema, "rep_activity")
#         if "rep_id" in c and "sales_rep_id" not in c:
#             lines.append(
#                 "`rep_activity`: use **`rep_id`** (not `sales_rep_id`) to join **`sales_rep`** on **`rep_activity.rep_id = sales_rep.rep_id`**."
#             )
#     if "call_plan" in name_lower:
#         c = _table_columns_lower(cur, schema, "call_plan")
#         if "rep_id" in c and "sales_rep_id" not in c:
#             lines.append(
#                 "`call_plan`: use **`rep_id`** (not `sales_rep_id`) to join **`sales_rep`** when that is the live FK column."
#             )
#     if "adverse_events" in name_lower:
#         c = _table_columns_lower(cur, schema, "adverse_events")
#         if "event_id" in c and "adverse_event_id" not in c:
#             tail = ""
#             if "subject_id" in c and "patient_id" not in c:
#                 tail = " Patient FK is **`subject_id`** (join **`patients`** on **`subject_id`**, not `patient_id`)."
#             lines.append(
#                 "`adverse_events`: primary key **`event_id`** (not `adverse_event_id`); keep **`drug_id`** for joins to the drug catalog."
#                 + tail
#             )
#     if "adverse_event" in name_lower and "adverse_events" not in name_lower:
#         c = _table_columns_lower(cur, schema, "adverse_event")
#         if "event_id" in c and "adverse_event_id" not in c:
#             lines.append(
#                 "`adverse_event`: this database uses **`event_id`** as the row id (not `adverse_event_id`); keep **`drug_id`** / **`patient_id`** if present."
#             )
#     if "drug_master" in name_lower:
#         c = _table_columns_lower(cur, schema, "drug_master")
#         if "brand_name" not in c and "drug_name" in c:
#             lines.append(
#                 "`drug_master`: no **`brand_name`** column here — use **`drug_name`** (and **`drug_id`**, **`molecule_id`**) as in this table."
#             )
#     if "patients" in name_lower:
#         c = _table_columns_lower(cur, schema, "patients")
#         if "subject_id" in c and "patient_id" not in c:
#             lines.append(
#                 "`patients`: primary key **`subject_id`** (not `patient_id`). Join **`admissions`** on **`patients.subject_id = admissions.subject_id`**."
#             )
#     if "admissions" in name_lower:
#         c = _table_columns_lower(cur, schema, "admissions")
#         if "hadm_id" in c and "admission_id" not in c:
#             join_p = ""
#             if "subject_id" in c:
#                 join_p = " Count rows with **`COUNT(hadm_id)`** or **`COUNT(*)`** grouped by **`subject_id`** for “how many admissions per patient”."
#             lines.append(
#                 "`admissions`: primary key **`hadm_id`** (not `admission_id`); patient link is **`subject_id`** (not `patient_id`)."
#                 + join_p
#             )
#     if "patient" in name_lower and "patients" not in name_lower:
#         c = _table_columns_lower(cur, schema, "patient")
#         if "subject_id" in c and "patient_id" not in c:
#             lines.append(
#                 "`patient`: uses **`subject_id`** instead of `patient_id` — use **`subject_id`** in joins and aggregates."
#             )
#     if "admission" in name_lower and "admissions" not in name_lower:
#         c = _table_columns_lower(cur, schema, "admission")
#         if "hadm_id" in c and "admission_id" not in c:
#             lines.append(
#                 "`admission`: uses **`hadm_id`** instead of `admission_id` (and possibly **`subject_id`** instead of `patient_id`)."
#             )
#     return lines


# def _synthea_schema_hint_lines(cur: Any, schema: str, name_lower: set[str]) -> List[str]:
#     """When the live DB is the Synthea Enhanced schema (ERD.md), steer NL→SQL away from timeouts / empty results."""
#     if "patients" not in name_lower or "encounters" not in name_lower:
#         return []
#     try:
#         pc = _table_columns_lower(cur, schema, "patients")
#     except Exception:
#         return []
#     # Synthea `patients` has HEALTHCARE_EXPENSES; MIMIC-style `patients` does not.
#     if "healthcare_expenses" not in pc:
#         return []
#     lines: List[str] = [
#         "Synthea (`patients`, `encounters`, `claims`, …): **double-quote** DDL column names — "
#         '`"Id"`, `"PATIENT"`, `"ENCOUNTER"`, `"START"`, `"STOP"`, `"TOTAL_CLAIM_COST"`, `"PAYER"`, '
#         '`"PRIMARYPAYER"`, `"ENCOUNTER_ID"`, `"CLAIMID"`, `"PAYMENTS"`, `"AMOUNT"`. '
#         "Unquoted identifiers are folded to lowercase and will **fail**.",
#         '`claims` has **no** encounter billed-total column. Use **`encounters."TOTAL_CLAIM_COST"`** '
#         'for billed amount per visit; join **`claims."ENCOUNTER_ID" = encounters."Id"`** (nullable FK).',
#         "**Wide encounter + many clinical facts:** chaining several **`LEFT JOIN`** one-to-many tables "
#         "(`conditions`, `medications`, …) on the same encounter **multiplies rows** (Cartesian product) "
#         "and often **times out**. Prefer **`STRING_AGG` / sub-SELECT grouped by `\"ENCOUNTER\"`**, "
#         "**`LATERAL … LIMIT 1`**, or **separate queries** — not many unconditional LEFT JOINs.",
#         "**Payments vs billed:** `SUM(claims_transactions.\"PAYMENTS\")` per **`\"CLAIMID\"`** vs "
#         '**`encounters."TOTAL_CLAIM_COST"`** (via claims→encounter) is a valid comparison when the user '
#         'asks whether payments exceed billed; **`claims`** alone has no total-cost column.',
#     ]
#     return lines


# _COLUMN_DETAIL_TABLES: frozenset[str] = frozenset(
#     {
#         # Drug domain
#         "drug", "drug_master", "molecule", "molecule_master",
#         "manufacturer", "therapy_area", "drug_class", "drug_subclass",
#         "drug_formulation", "drug_indication", "drug_price", "drug_approval",
#         "drug_lifecycle", "drug_competitor", "drug_patent", "rebate_program",
#         "drug_interaction", "clinical_trial", "territory", "region",
#         # Clinical
#         "patient", "patients", "admission", "admissions", "icu_stay",
#         "lab_event", "diagnosis", "procedure_event", "prescription", "prescriptions",
#         "adverse_event", "adverse_events", "comorbidity",
#         "treatment_pathway", "adherence", "persistence", "patient_outcome",
#         "patient_demographic",
#         # Commercial
#         "provider", "hcp", "hcp_affiliation", "sales_rep",
#         "rep_activity", "call_outcome", "call_plan", "sales_target",
#         "sales_incentive", "drug_sale", "rx_summary",
#         # Payer/analytics
#         "payer", "formulary", "formulary_history", "claim", "claim_line",
#         "reimbursement", "prior_authorization", "copay", "coverage_limit",
#         "payer_contract", "market_share", "forecast", "kpi_metric",
#         # Reference
#         "region", "dim_date",
#         # Synthea Enhanced (ERD.md / synthea_db) — quoted mixed-case columns in SQL
#         "organizations",
#         "providers",
#         "payers",
#         "encounters",
#         "claims",
#         "claims_transactions",
#         "conditions",
#         "medications",
#         "observations",
#         "procedures",
#         "allergies",
#         "immunizations",
#         "careplans",
#         "devices",
#         "supplies",
#         "imaging_studies",
#         "payer_transitions",
#     }
# )


# def _fetch_live_catalog(max_names: int) -> tuple[List[str], str]:
#     """Return ``(table_names, column_detail_paragraph)`` for prompts.

#     The column paragraph lists ``table(col1, col2, ...)`` for every table in
#     ``_COLUMN_DETAIL_TABLES`` that actually exists, so the model never has to
#     guess column names.
#     """
#     if _live_table_catalog_disabled() or psycopg2 is None:
#         return [], ""
#     try:
#         config = _db_config()
#     except RuntimeError:
#         return [], ""
#     schema = pharma_db_schema()
#     try:
#         with psycopg2.connect(**config) as conn:
#             with conn.cursor() as cur:
#                 cur.execute(
#                     """
#                     SELECT table_name
#                     FROM information_schema.tables
#                     WHERE table_schema = %s AND table_type = 'BASE TABLE'
#                     ORDER BY table_name
#                     LIMIT %s;
#                     """,
#                     (schema, max_names),
#                 )
#                 names = [row[0] for row in cur.fetchall()]
#                 nm = {str(n).lower() for n in names}

#                 # ── Fetch column names for key tables ──────────────────────
#                 detail_tables = [n for n in names if n.lower() in _COLUMN_DETAIL_TABLES]
#                 cols_map: Dict[str, List[str]] = defaultdict(list)
#                 if detail_tables:
#                     cur.execute(
#                         """
#                         SELECT table_name, column_name
#                         FROM information_schema.columns
#                         WHERE table_schema = %s
#                           AND table_name = ANY(%s)
#                         ORDER BY table_name, ordinal_position;
#                         """,
#                         (schema, detail_tables),
#                     )
#                     for row in cur.fetchall():
#                         cols_map[row[0]].append(row[1])

#                 # ── Legacy column / join-key hints ──────────────────────────
#                 hint_lines = _legacy_column_hint_lines(cur, schema, nm)
#                 hint_lines.extend(_synthea_schema_hint_lines(cur, schema, nm))

#                 hints = ""
#                 if cols_map:
#                     col_lines = "\n".join(
#                         f"  {tbl}({', '.join(cols)})"
#                         for tbl, cols in sorted(cols_map.items())
#                     )
#                     hints = (
#                         "\n**Live table columns (authoritative — use these exact names in SQL):**\n"
#                         + col_lines
#                     )
#                 if hint_lines:
#                     hints += (
#                         "\n**Live join-key overrides (use instead of ERD when they conflict):**\n"
#                         + "\n".join(f"- {h}" for h in hint_lines)
#                     )
#                 return names, hints
#     except Exception as exc:
#         logger.warning("[postgres_runner] _fetch_live_catalog: %s", exc)
#         return [], ""


# def fetch_live_base_table_names(*, max_names: int = 800) -> List[str]:
#     """Base-table names in ``pharma_db_schema()`` from ``information_schema`` (empty if disabled or on error)."""
#     names, _ = _fetch_live_catalog(max_names)
#     return names


# def get_table_columns_hint(table_name: str) -> str:
#     """Return a human-readable column list for *table_name* from the live DB.

#     Used for error-recovery: when Postgres rejects a column reference, call this to
#     get the real column names and inject them into a retry prompt.

#     Returns an empty string if the table is not found or the connection fails.
#     """
#     if psycopg2 is None:
#         return ""
#     try:
#         config = _db_config()
#     except RuntimeError:
#         return ""
#     schema = pharma_db_schema()
#     try:
#         with psycopg2.connect(**config) as conn:
#             with conn.cursor() as cur:
#                 cols = _table_columns_lower(cur, schema, table_name)
#                 if not cols:
#                     cur.execute(
#                         "SELECT column_name FROM information_schema.columns "
#                         "WHERE table_name = %s ORDER BY ordinal_position",
#                         (table_name,),
#                     )
#                     cols = {str(r[0]).lower() for r in cur.fetchall()}
#                 if cols:
#                     return (
#                         f"Table `{table_name}` actual columns in schema `{schema}`: "
#                         + ", ".join(sorted(cols))
#                     )
#                 return f"Table `{table_name}` was not found in schema `{schema}`."
#     except Exception as exc:
#         logger.warning("[postgres_runner] get_table_columns_hint(%s): %s", table_name, exc)
#         return ""


# def extract_failing_identifiers(error_msg: str) -> List[str]:
#     """Parse a Postgres error string and return candidate table/column identifiers.

#     Works for messages like:
#     - ``column "brand_name" does not exist``
#     - ``relation "drug_master" does not exist``
#     - ``ERROR:  column x.foo does not exist``
#     """
#     found: List[str] = []
#     for m in re.finditer(r'"([^"]+)"', error_msg):
#         token = m.group(1)
#         if token and not token.startswith("$"):
#             found.append(token)
#     return found


# # When the ERD uses a newer name but the connected DB still has legacy Takeda-style tables.
# _ERD_TO_LEGACY_TABLE: Tuple[Tuple[str, str], ...] = (
#     ("drug", "drug_master"),
#     ("molecule", "molecule_master"),
#     ("patient", "patients"),
#     ("admission", "admissions"),
#     ("adverse_event", "adverse_events"),
#     ("prescription", "prescriptions"),
# )


# def live_table_names_prompt_text(
#     *,
#     max_list_items: int = 260,
#     max_chars: int = 14000,
# ) -> Optional[str]:
#     """
#     Paragraph for text-to-SQL: real ``FROM``/``JOIN`` identifiers on this server.

#     Disable with ``SDA_LIVE_TABLE_NAMES=0``. Returns None when disabled, on failure, or when
#     the schema has no base tables (caller keeps ERD-only grounding).
#     """
#     names, col_hints = _fetch_live_catalog(max_names=max_list_items + 400)
#     if not names:
#         return None
#     schema = pharma_db_schema()
#     name_lower = {str(n).lower() for n in names}
#     legacy_lines: List[str] = []
#     for erd_t, legacy_t in _ERD_TO_LEGACY_TABLE:
#         if erd_t not in name_lower and legacy_t in name_lower:
#             legacy_lines.append(
#                 f"- ERD table **`{erd_t}`** is not present; use **`{legacy_t}`** instead for the same role in SQL."
#             )
#     suffix = ""
#     display = names
#     if len(names) > max_list_items:
#         display = names[:max_list_items]
#         suffix = f"\n… and {len(names) - max_list_items} more tables in schema `{schema}` (not shown)."
#     body = ", ".join(display)
#     text = (
#         f"Schema `{schema}` — these **BASE TABLE** names exist in the live database. "
#         f"**Every** relation in **FROM** / **JOIN** must match one of these names (case-insensitive); "
#         f"do **not** emit `drug`, `patient`, `molecule`, etc. unless that identifier appears in the list below. "
#         f"Use **ERD_CONTEXT** for columns and join logic when the live name matches; otherwise follow the mapping lines. "
#         f"If no table can answer the question, return `-- ERROR:`.\n"
#         f"{body}{suffix}"
#     )
#     if col_hints:
#         text += col_hints
#     if legacy_lines:
#         text += "\n**Live / ERD name mapping (mandatory):**\n" + "\n".join(legacy_lines)
#     if len(text) > max_chars:
#         text = text[:max_chars] + "\n… [truncated]"
#     return text


# # ---------------------------------------------------------------------------
# # Core query runner (with retry)
# # ---------------------------------------------------------------------------

# @postgres_retry
# def _execute_query(sql: str, max_rows: int | None) -> List[Dict[str, Any]]:
#     """Inner function wrapped by postgres_retry for transient error handling.

#     Kept separate from run_query() so the retry decorator only wraps the
#     network/execution call, not the driver availability check or config load.
#     """
#     config = _db_config()
#     statement_timeout_ms = _env_int(
#         "POSTGRES_STATEMENT_TIMEOUT_MS", DEFAULT_STATEMENT_TIMEOUT_MS
#     )

#     with psycopg2.connect(**config) as conn:
#         with conn.cursor(cursor_factory=RealDictCursor) as cur:
#             _set_session_search_path(cur)
#             # Postgres-side query timeout — independent of Python connect_timeout.
#             cur.execute(f"SET LOCAL statement_timeout = {statement_timeout_ms};")
#             cur.execute(sql)
#             if max_rows is None:
#                 rows = cur.fetchall()
#             else:
#                 rows = cur.fetchmany(max_rows)
#                 if len(rows) == max_rows:
#                     logger.warning(
#                         "[postgres_runner] Row cap reached (%d rows). "
#                         "Results may be truncated. Increase POSTGRES_MAX_ROWS if needed.",
#                         max_rows,
#                     )
#             return [dict(r) for r in rows]


# def run_query(sql: str, max_rows: int | None = None, unlimited: bool = False) -> List[Dict[str, Any]]:
#     """Execute a read-only SQL statement and return rows as list of dicts.

#     Governance guarantees
#     ---------------------
#     - connect_timeout:    TCP connect times out after POSTGRES_CONNECT_TIMEOUT seconds (default 10).
#     - statement_timeout:  Query execution capped at POSTGRES_STATEMENT_TIMEOUT_MS ms (default 60000).
#     - row cap:            At most POSTGRES_MAX_ROWS rows returned by default (default 5000).
#     - unlimited:          If unlimited=True, fetch all rows without applying the row cap.
#     - retry:              Transient OperationalError / InterfaceError retried with
#                           exponential backoff (see retry_utils.postgres_retry).
#     - error surfacing:    All psycopg2 exceptions caught and re-raised as RuntimeError
#                           with human-readable messages; callers get consistent types.

#     Args:
#         sql: A validated read-only SQL statement (should have passed sql_validate).
#         max_rows: Optional explicit row cap; if None, uses POSTGRES_MAX_ROWS.
#         unlimited: When True, fetch all rows without a row cap.

#     Returns:
#         List of row dicts. May be empty.

#     Raises:
#         RuntimeError: On driver missing, config error, or DB execution failure.
#     """
#     _require_driver()
#     if unlimited:
#         return _execute_query(sql, None)
#     if max_rows is None:
#         max_rows = _env_int("POSTGRES_MAX_ROWS", DEFAULT_MAX_ROWS)

#     try:
#         return _execute_query(sql, max_rows)
#     except RuntimeError:
#         # Re-raise RuntimeErrors from _db_config() and _require_driver() as-is
#         raise
#     except psycopg2.ProgrammingError as exc:
#         # Bad SQL syntax / unknown column — not retriable, surface clearly
#         raise RuntimeError(
#             f"Postgres query error (bad SQL): {_programming_error_hint(exc)}"
#         ) from exc
#     except psycopg2.DataError as exc:
#         # Type mismatch, out-of-range — not retriable
#         raise RuntimeError(f"Postgres data error: {exc}") from exc
#     except psycopg2.extensions.QueryCanceledError as exc:
#         # statement_timeout fired on Postgres side
#         raise RuntimeError(
#             f"Postgres query timed out after "
#             f"{_env_int('POSTGRES_STATEMENT_TIMEOUT_MS', DEFAULT_STATEMENT_TIMEOUT_MS)}ms. "
#             "Simplify the query or increase POSTGRES_STATEMENT_TIMEOUT_MS."
#         ) from exc
#     except Exception as exc:
#         raise RuntimeError(f"Postgres execution failed: {exc}") from exc


# # ---------------------------------------------------------------------------
# # Table-output variant (used by CLI / non-pipeline callers)
# # ---------------------------------------------------------------------------

# @postgres_retry
# def _execute_query_with_columns(
#     sql: str, max_rows: int | None
# ) -> Tuple[List[str], List[Tuple[Any, ...]]]:
#     config = _db_config()
#     statement_timeout_ms = _env_int(
#         "POSTGRES_STATEMENT_TIMEOUT_MS", DEFAULT_STATEMENT_TIMEOUT_MS
#     )
#     with psycopg2.connect(**config) as conn:
#         with conn.cursor() as cur:
#             _set_session_search_path(cur)
#             cur.execute(f"SET LOCAL statement_timeout = {statement_timeout_ms};")
#             cur.execute(sql)
#             columns = [desc[0] for desc in cur.description]
#             if max_rows is None:
#                 rows = cur.fetchall()
#             else:
#                 rows = cur.fetchmany(max_rows)
#             return columns, list(rows)


# def run_query_with_columns(
#     sql: str, max_rows: int | None = None, unlimited: bool = False
# ) -> Tuple[List[str], List[Tuple[Any, ...]]]:
#     """Execute SQL and return (columns, rows) for table-like output.

#     Applies the same governance guarantees as run_query().
#     """
#     _require_driver()
#     if unlimited:
#         return _execute_query_with_columns(sql, None)
#     if max_rows is None:
#         max_rows = _env_int("POSTGRES_MAX_ROWS", DEFAULT_MAX_ROWS)

#     try:
#         return _execute_query_with_columns(sql, max_rows)
#     except RuntimeError:
#         raise
#     except psycopg2.ProgrammingError as exc:
#         raise RuntimeError(
#             f"Postgres query error (bad SQL): {_programming_error_hint(exc)}"
#         ) from exc
#     except psycopg2.DataError as exc:
#         raise RuntimeError(f"Postgres data error: {exc}") from exc
#     except psycopg2.extensions.QueryCanceledError as exc:
#         raise RuntimeError(
#             f"Postgres query timed out after "
#             f"{_env_int('POSTGRES_STATEMENT_TIMEOUT_MS', DEFAULT_STATEMENT_TIMEOUT_MS)}ms."
#         ) from exc
#     except Exception as exc:
#         raise RuntimeError(f"Postgres execution failed: {exc}") from exc


# # ---------------------------------------------------------------------------
# # CLI entry point
# # ---------------------------------------------------------------------------

# def main() -> None:
#     sql = input("Enter SQL to run: ").strip()
#     if not sql:
#         raise SystemExit("No SQL provided.")

#     rows = run_query(sql)
#     print(json.dumps(rows, indent=2, default=str))


# if __name__ == "__main__":
#     main()

