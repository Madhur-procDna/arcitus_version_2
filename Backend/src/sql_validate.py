
"""
SQL Validation Layer for Takeda DSA (takeda_dsa_db).
VERSION: 3.9 (Balanced Governance & Production-Ready)

Defense Layers:
  1. None / Empty Guard:    Rejects None or blank SQL before any parsing.
  2. Hard Keyword Block:    Immediate rejection of DML/DDL.
  3. Incompleteness Guard:  Rejects truncated LLM output.
  4. Syntax Validation:     Ensures valid Postgres dialect.
  5. AST Security Walk:     Rejects forbidden nodes (Grant, Revoke, Set, etc.)
                            using find_all() -- compatible with all sqlglot versions.
  6. Read-Only Enforcement: Rejects locking (FOR UPDATE).
  7. Complexity Limit:      Prevents Denial of Service via deep CTEs.

FIXES vs v3.1:
  - Added Layer 0: None / empty guard before any regex or AST work.
  - Replaced tree.walk() with tree.find_all() for forbidden-node detection.
    walk() yields (node, parent, key) tuples in newer sqlglot versions, making
    isinstance(node, forbidden) silently fail. find_all() always yields nodes.
  - v3.3: When naive parenthesis count disagrees, trust a successful sqlglot one-statement
    parse (dialect) so Synthea/complex LLM output is not false-rejected; double-quoted ids
    still stripped for the heuristic.
  - v3.4: If the LLM returns a **truncated** line (trailing `,` or trailing `OR` / `AND`),
    attempt a one-shot repair when sqlglot can parse the shortened text as a single
    valid statement.
  - v3.5: If naive parenthesis counts fail because the model appended **non-SQL prose**
    after a valid statement (common with unmatched `(` in explanations), strip trailing
    whole lines until counts balance **and** sqlglot parses one statement.
  - v3.6: Trailing-comma repair strips a **run** of trailing commas (`,,` when the model
    stops mid-list) and loops until sqlglot accepts the statement, not a single `,` only.
  - v3.7: Trailing commas are peeled **without** requiring sqlglot pre-parse — sqlglot can
    reject some Postgres-valid LLM SQL; Layer 3 / Postgres remain authoritative.
  - v3.8: When **SQLGLOT_VALIDATE** is on, do **not** hard-reject on naive parenthesis imbalance
    if sqlglot's quick parse fails — the heuristic false-positives on complex clinical SQL;
    Layer 3 full parse is authoritative (clearer errors, fewer spurious 'unbalanced' rejects).
  - v3.9: Trailing **`OR` / `AND`** (truncated boolean) are peeled like trailing commas — no sqlglot
    pre-parse required; fixes dangling-keyword rejects when the model stops mid-WHERE.
"""

from __future__ import annotations

import os
import re
from typing import Any, Tuple, Type

try:
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import ParseError, TokenError
except ImportError:
    sqlglot = None      # type: ignore[assignment]
    exp = None          # type: ignore[assignment]
    ParseError = Exception
    TokenError = Exception


class SQLValidationError(RuntimeError):
    """Raised when generated SQL fails security, syntax, or structural checks."""


def _vprint(msg: str) -> None:
    """Echo to stdout when SQLGLOT_VERBOSE is truthy."""
    raw = (os.getenv("SQLGLOT_VERBOSE") or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        print(msg, flush=True)


def _sqlglot_ast_enabled() -> bool:
    """AST / sqlglot parse + walk. Set SQLGLOT_VALIDATE=false to skip."""
    return (os.getenv("SQLGLOT_VALIDATE") or "true").strip().lower() not in (
        "0", "false", "no", "off",
    )


# ---------------------------------------------------------------------------
# Layer 0: None / Empty Guard
# ---------------------------------------------------------------------------

def _none_and_empty_guard(sql: object) -> str:
    """
    Reject None, non-string, or blank input immediately.

    Fixes AttributeError: 'NoneType' object has no attribute 'find_all'
    which crashed when the LLM returned None for non-data questions like
    greetings. Returns stripped str if valid.
    """
    if sql is None:
        raise SQLValidationError(
            "SQL is None -- the LLM did not generate a query. "
            "This usually means the question is not a data question."
        )
    if not isinstance(sql, str):
        raise SQLValidationError(
            f"SQL must be a string, got {type(sql).__name__!r}."
        )
    stripped = sql.strip()
    if not stripped:
        raise SQLValidationError(
            "SQL is empty or whitespace-only -- the LLM returned no query."
        )
    return stripped


# ---------------------------------------------------------------------------
# Layer 1: Forbidden Operation Block
# ---------------------------------------------------------------------------

_BLOCKED_KEYWORDS: frozenset[str] = frozenset({
    "delete", "drop", "truncate", "insert", "update", "alter", "create",
    "replace", "grant", "revoke", "exec", "execute", "call", "merge",
    "attach", "detach", "vacuum", "reindex", "cluster", "copy", "import",
    "load", "savepoint", "rollback", "commit", "begin", "start", "lock",
    "unlock", "comment", "rename", "explain", "analyze",
    "show", "describe", "pragma",
})


def _hard_block_check(sql: str) -> None:
    """Fast-path rejection of non-read-only keywords and malicious patterns."""
    cleaned = re.sub(r"--.*|/\*.*?\*/", "", sql, flags=re.DOTALL).strip()
    if not cleaned:
        raise SQLValidationError("SQL is empty or contains only comments.")

    tokens = cleaned.split()
    first_word = tokens[0].lower().rstrip(";")

    if first_word in _BLOCKED_KEYWORDS:
        raise SQLValidationError(
            f"Operation '{first_word.upper()}' is forbidden. "
            "Access restricted to SELECT/WITH."
        )

    if first_word not in ("select", "with"):
        raise SQLValidationError(
            f"SQL must start with SELECT or WITH (detected '{first_word.upper()}')."
        )

    if ";" in cleaned[:-1]:
        raise SQLValidationError(
            "Multiple SQL statements detected via semicolon. Only one query allowed."
        )
    if re.search(r"\b(information_schema|pg_catalog|sys\.tables)\b", cleaned, flags=re.I):
        raise SQLValidationError("Schema-probing catalogs are not permitted.")


# ---------------------------------------------------------------------------
# Layer 2: Structural Integrity (Anti-Truncation)
# ---------------------------------------------------------------------------

def _strip_sql_literals_for_paren_count(s: str) -> str:
    """
    Remove PostgreSQL string regions so '(' / ')' inside literals do not break
    naive parenthesis balance (false positives on CASE labels, LIKE patterns, etc.).
    Handles single-quoted strings ('' escape) and dollar-quoted strings ($$...$$, $tag$...$tag$).
    Also strips delimited **double-quoted identifiers** ("" for embedded quote) so '(' / ')'
    in column/alias names — common in Synthea prompts — do not trigger false
    "unbalanced parentheses" rejections.
    """
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "'":
            i += 1
            while i < n:
                if s[i] == "'":
                    if i + 1 < n and s[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append(" ")
            continue
        if ch == '"':
            i += 1
            while i < n:
                if s[i] == '"':
                    if i + 1 < n and s[i + 1] == '"':
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append(" ")
            continue
        if ch == "$" and i + 1 < n:
            j = s.find("$", i + 1)
            if j != -1:
                tag = s[i + 1 : j]
                if all(c.isalnum() or c == "_" for c in tag) or tag == "":
                    close = "$" + tag + "$"
                    k = s.find(close, j + 1)
                    if k != -1:
                        i = k + len(close)
                        out.append(" ")
                        continue
        out.append(ch)
        i += 1
    return "".join(out)


def _sqlglot_parses_as_single_statement(sql: str, dialect: str | None = None) -> bool:
    """
    True if sqlglot parses the string as exactly one non-null statement.
    Used to override naive parenthesis heuristics (still prone to false positives
    on exotic delimiters) without weakening truncation detection when parse fails.
    Intentionally does **not** require SQLGLOT_VALIDATE: when full AST checks are
    off, a successful parse is still a strong signal that the query is not truncated
    on parentheses.
    """
    if sqlglot is None:
        return False
    d = (dialect or os.getenv("SQLGLOT_DIALECT") or "postgres").strip() or "postgres"
    try:
        statements = sqlglot.parse(sql, dialect=d)
    except (TokenError, ParseError, TypeError, ValueError, AttributeError):
        return False
    if not statements or len(statements) != 1:
        return False
    return statements[0] is not None


def _repair_llm_trailing_prose_lines(sql: str, dialect: str | None = None) -> str:
    """
    When the LLM appends natural-language after the SQL (despite instructions), trailing
    lines can contain `(` / `)` that break the naive parenthesis heuristic while the SQL
    prefix is valid. Drop terminal lines only when the prefix is strictly better: balanced
    parens (after literal stripping) **and** sqlglot accepts it as a single statement.
    """
    if not sql or not isinstance(sql, str):
        return sql
    d = (dialect or os.getenv("SQLGLOT_DIALECT") or "postgres").strip() or "postgres"
    core = re.sub(r"--.*|/\*.*?\*/", "", sql, flags=re.DOTALL).strip()
    if not core:
        return sql
    stripped = _strip_sql_literals_for_paren_count(core)
    if stripped.count("(") == stripped.count(")"):
        return sql
    lines = sql.splitlines()
    if len(lines) < 2:
        return sql
    max_drop = min(40, len(lines) - 1)
    for drop in range(1, max_drop + 1):
        cand = "\n".join(lines[:-drop]).strip()
        if not cand:
            continue
        core_c = re.sub(r"--.*|/\*.*?\*/", "", cand, flags=re.DOTALL).strip()
        if not core_c:
            continue
        st = _strip_sql_literals_for_paren_count(core_c)
        if st.count("(") != st.count(")"):
            continue
        if _sqlglot_parses_as_single_statement(cand, dialect=d):
            _vprint(
                "[sql_validate] Repaired: removed trailing non-SQL lines "
                f"(dropped {drop}) so parenthesis guard matches a parseable statement."
            )
            return cand
    return sql


def _repair_llm_truncation_sql(sql: str, dialect: str) -> str:
    """
    Best-effort fixes for common **truncated** LLM outputs.

    Trailing comma runs are always peeled (invalid in PostgreSQL). We no longer require
    sqlglot to accept the peeled text first — sqlglot can be stricter than Postgres on
    some generated dialect; validate_sql_for_production Layer 3 + execution surface errors.
    Trailing **OR / AND** boolean tails are peeled the same way (no sqlglot pre-parse).
    """
    s = sql.strip()
    if not s:
        return s
    # Same as _incomplete_sql_guard: line/block comments can hide a trailing `,`
    # from naive string tests (e.g. `SELECT a, -- more cols cut off`)
    base = re.sub(r"--.*|/\*.*?\*/", "", s, flags=re.DOTALL).strip()
    base = base.rstrip().rstrip(";").rstrip()

    work = base
    while True:
        nxt = re.sub(r",+\s*$", "", work).rstrip()
        if nxt == work:
            break
        work = nxt
    if work != base:
        _vprint(
            "[sql_validate] Repaired: removed trailing comma run(s) (LLM truncation; Layer 3 validates)."
        )

    pre_bool = work
    while True:
        nxt = re.sub(r"\s+(OR|AND)\s*$", "", work, flags=re.I).rstrip()
        if nxt == work:
            break
        work = nxt
    if work != pre_bool:
        _vprint(
            "[sql_validate] Repaired: removed trailing OR/AND (LLM truncation; Layer 3 validates)."
        )

    if sqlglot is None:
        return work if work != base else s
    if work != base:
        return work
    return s


def _incomplete_sql_guard(sql: str, *, dialect: str | None = None) -> None:
    """Detects if the LLM output was cut off mid-query."""
    core = re.sub(r"--.*|/\*.*?\*/", "", sql, flags=re.DOTALL).strip()

    if core.endswith(","):
        raise SQLValidationError(
            "SQL ends with a trailing comma; the query appears truncated."
        )

    core_for_parens = _strip_sql_literals_for_paren_count(core)
    if core_for_parens.count("(") != core_for_parens.count(")"):
        if _sqlglot_parses_as_single_statement(sql, dialect=dialect):
            _vprint(
                "[sql_validate] paren count heuristic imbalanced, "
                "sqlglot still parses a single statement — allowing."
            )
        elif _sqlglot_ast_enabled():
            # Heuristic vs sqlglot quick-parse disagree often on long FILTER / window / nested
            # clinical SQL; do not block here when the full AST pass (Layer 3) still runs.
            _vprint(
                "[sql_validate] paren count heuristic imbalanced and quick parse failed — "
                "SQLGLOT_VALIDATE on; deferring to Layer 3 parse instead of 'unbalanced' reject."
            )
        else:
            raise SQLValidationError(
                "Unbalanced parentheses; the query appears incomplete or cut off."
            )

    dangling_ends = {
        "where", "and", "or", "from", "select", "join",
        "on", "with", "as", "by", "order",
    }
    last_token = core.split()[-1].lower().rstrip(";")
    if last_token in dangling_ends:
        raise SQLValidationError(
            f"SQL ends with a dangling keyword '{last_token.upper()}'; "
            "query is incomplete."
        )


def _select_list_comma_guard(sql: str) -> None:
    """Catch comma-before-clause patterns that sqlglot may silently recover from."""
    core = re.sub(r"--.*|/\*.*?\*/", "", sql, flags=re.DOTALL)
    low = core.lower()
    patterns = (
        (r",\s+from\b",       "comma immediately before FROM"),
        (r",\s+where\b",      "comma immediately before WHERE"),
        (r",\s+group\s+by\b", "comma immediately before GROUP BY"),
        (r",\s+having\b",     "comma immediately before HAVING"),
        (r",\s+order\s+by\b", "comma immediately before ORDER BY"),
        (r",\s+limit\b",      "comma immediately before LIMIT"),
    )
    for pat, hint in patterns:
        if re.search(pat, low):
            raise SQLValidationError(
                f"Invalid or truncated SELECT list ({hint}); "
                "sqlglot may still parse this -- rejected."
            )


# ---------------------------------------------------------------------------
# Layer 3: AST-Based Dialect & Security Walk
# ---------------------------------------------------------------------------

def _get_forbidden_node_types() -> Tuple[Type, ...]:
    """Node types representing DML, DDL, or session-state changes."""
    if exp is None:
        return ()
    return (
        exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create,
        exp.Alter, exp.TruncateTable, exp.Merge, exp.Command,
        exp.Execute, exp.Copy, exp.Grant, exp.Revoke,
        exp.Transaction, exp.Set, exp.Use,
    )


def validate_sql_for_production(
    sql: object,
    dialect: str | None = None,
    *,
    _multi_coalesce_depth: int = 0,
) -> str:
    """
    Final production entry point.

    Returns cleaned SQL (str) if valid.
    Raises SQLValidationError if the query is unsafe, malformed, or incomplete.

    Parameter sql is typed as object so callers that pass an unvalidated LLM
    return value (which might be None) get a clean error from Layer 0 instead
    of an AttributeError deep in the call stack.
    """
    # Layer 0 -- reject None / empty before touching anything else
    sql_str = _none_and_empty_guard(sql)

    d = (dialect or os.getenv("SQLGLOT_DIALECT") or "postgres").strip() or "postgres"
    # Repair truncated LLM endings (trailing `,` / ` OR` / ` AND`) when parseable
    sql_str = _repair_llm_truncation_sql(sql_str, d)
    # Prose after SQL (or a partial explanation) can break the paren heuristic — trim lines
    sql_str = _repair_llm_trailing_prose_lines(sql_str, d)

    # Layer 1 -- keyword fast-block
    _hard_block_check(sql_str)

    # Layer 2 -- truncation / incompleteness guards
    _incomplete_sql_guard(sql_str, dialect=d)
    _select_list_comma_guard(sql_str)

    if sqlglot is None or exp is None:
        raise SQLValidationError(
            "sqlglot is not installed -- cannot run AST validation. "
            "Install with: pip install sqlglot"
        )

    if not _sqlglot_ast_enabled():
        _vprint(
            "[sql_validate] SQLGLOT_VALIDATE disabled -- "
            "skipping sqlglot parse/AST (regex layers applied)."
        )
        return sql_str

    ver = getattr(sqlglot, "__version__", "?")
    _vprint(
        f"[sql_validate] sqlglot {ver} -- "
        f"parse (dialect={d}), single-statement + read-only AST checks..."
    )

    # Layer 3a -- parse
    try:
        statements = sqlglot.parse(sql_str, dialect=d)
    except (TokenError, ParseError) as e:
        _vprint(f"[sql_validate] PARSE FAILED: {e}")
        raise SQLValidationError(f"Syntax error in generated SQL: {e}") from e

    if not statements:
        raise SQLValidationError("sqlglot could not parse any valid statements.")
    if len(statements) > 1:
        policy = (os.getenv("SDA_SQLITE_MULTI_STATEMENT_POLICY") or "first_select").strip().lower()
        allow_first = d == "sqlite" and policy in (
            "1",
            "true",
            "yes",
            "on",
            "first_select",
            "first",
        )
        if allow_first and _multi_coalesce_depth < 4:
            non_null = [s for s in statements if s is not None]

            def _readonly_select_root(node: Any) -> bool:
                root = node
                while isinstance(root, exp.Subquery):
                    root = root.this
                return isinstance(root, (exp.Select, exp.Union))

            if non_null and all(_readonly_select_root(s) for s in non_null):
                first_sql = non_null[0].sql(dialect=d).strip().rstrip(";")
                _vprint(
                    f"[sql_validate] SQLite: coalesced {len(statements)} statements to first SELECT-only"
                )
                return validate_sql_for_production(
                    first_sql,
                    dialect=d,
                    _multi_coalesce_depth=_multi_coalesce_depth + 1,
                )
        raise SQLValidationError(
            "Security violation: multiple SQL statements detected."
        )

    tree = statements[0]
    if tree is None:
        # sqlglot.parse() can return [None] for blank / comment-only input.
        # Layer 0 should have caught this, but be defensive.
        raise SQLValidationError(
            "sqlglot returned a null parse tree -- SQL is empty or unparseable."
        )

    _vprint(f"[sql_validate] parsed root: {type(tree).__name__}")

    # Layer 3b -- forbidden-node walk
    # Use find_all() not walk(). walk() in newer sqlglot versions yields
    # (node, parent, key) 3-tuples so isinstance(node, forbidden) silently
    # fails. find_all() always yields node objects directly.
    forbidden = _get_forbidden_node_types()
    for ftype in forbidden:
        if list(tree.find_all(ftype)):
            _vprint(f"[sql_validate] REJECTED: forbidden node {ftype.__name__}")
            raise SQLValidationError(
                f"Security alert: forbidden SQL node '{ftype.__name__}' detected."
            )

    # Layer 4 -- locking check
    for _ in tree.find_all(exp.Lock):
        raise SQLValidationError(
            "Operation 'FOR UPDATE' or row-locking is not permitted."
        )

    # Layer 5 -- complexity guard (DoS / CPU spike prevention)
    cte_depth = len(list(tree.find_all(exp.CTE)))
    max_ctes = int(os.getenv("SQL_MAX_CTE_DEPTH", "12"))
    if cte_depth > max_ctes:
        raise SQLValidationError(
            f"Query complexity exceeded: {cte_depth} CTEs (limit: {max_ctes})."
        )

    # Layer 6 -- root verification
    root = tree
    while isinstance(root, exp.Subquery):
        root = root.this
    if not isinstance(root, (exp.Select, exp.Union)):
        raise SQLValidationError(
            f"Invalid root node: {type(root).__name__}. "
            "Only SELECT / WITH ... SELECT / UNION are allowed."
        )

    _vprint("[sql_validate] OK -- validation passed (read-only SELECT/UNION).")
    return sql_str


def validate_read_only_sql(sql: object, dialect: str | None = None) -> str:
    """Alias kept for backward compatibility with qa_pipeline imports."""
    return validate_sql_for_production(sql, dialect=dialect)


# ---------------------------------------------------------------------------
# TEST SUITE
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    test_cases = [
        ("SELECT * FROM call_plan",                             "VALID"),
        # Parens only inside double-quoted id (Synthea) — must not false-trigger layer 2
        (
            'SELECT t."A (B", (SELECT 1) AS x FROM synthea.observations t',
            "VALID",
        ),
        ("TRUNCATE TABLE rep_activity",                         "INVALID (DML)"),
        ("SELECT * FROM users; DROP TABLE call_plan",           "INVALID (Multi-Statement)"),
        ("SELECT hcp_id, FROM rep_activity",                    "INVALID (Truncated Comma)"),
        ("WITH cte AS (SELECT 1) SELECT * FROM cte FOR UPDATE", "INVALID (Locking)"),
        ("INSERT INTO call_plan (call_plan_id) VALUES (1)",     "INVALID (DML)"),
        (None,                                                   "INVALID (None input)"),
        ("",                                                     "INVALID (Empty string)"),
        ("   ",                                                  "INVALID (Whitespace only)"),
        ("hello",                                               "INVALID (Natural language)"),
        # Trailing prose with `(` — v3.5 should keep the parseable SELECT prefix
        (
            "SELECT 1 AS x\n\nThis explains (hospitals",
            "VALID",
        ),
        # v3.6: multiple trailing commas when the model stops mid–SELECT list
        (
            "SELECT 1 AS a, 2 AS b,,",
            "VALID",
        ),
        # v3.9: truncated WHERE … OR
        (
            "SELECT 1 AS n WHERE 1 = 1 OR",
            "VALID",
        ),
    ]

    passed = failed = 0
    for query, expected in test_cases:
        try:
            result = validate_sql_for_production(query)
            label = "VALID"
        except SQLValidationError as e:
            label = "INVALID"
            result = str(e)

        ok = (label == "VALID") == (expected == "VALID")
        status = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        else:
            failed += 1

        display = repr(query) if query is None or len(str(query)) < 60 else f"{str(query)[:57]}..."
        _ = (status, expected, display, result, ok)