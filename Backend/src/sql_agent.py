from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from openai import AzureOpenAI

from config import settings
from data_loader import DatabaseState
from db_adapter import (
    live_table_names_prompt_text,
    run_query,
    sqlglot_dialect_for_backend,
    use_sqlite_backend,
)
from pharma_schema import read_erd_markdown
from sql_validate import SQLValidationError, validate_read_only_sql
from sql_validator import ValidationResult, validate_sql

logger = logging.getLogger(__name__)

_SQL_BLOCK_RE = re.compile(r"<sql>\s*(.*?)\s*</sql>", flags=re.IGNORECASE | re.DOTALL)
_DONE_RE = re.compile(r"</?done\s*/?>", flags=re.IGNORECASE)
_TOP_N_RE = re.compile(r"\btop\s+(\d{1,3})\b", flags=re.IGNORECASE)
_TOP_N_FLEX_RE = re.compile(r"\btop\b(?:\s+\w+){0,4}\s+(\d{1,3})\b", flags=re.IGNORECASE)
_LIMIT_RE = re.compile(r"\blimit\s+(\d+)\b", flags=re.IGNORECASE)
_METRIC_HINT_RE = re.compile(
    r"(growth|pct|percent|delta|change|trx|nrx|rank|score|value|amount|total|yoy|mom|qoq)",
    re.IGNORECASE,
)


def _scalar(v: object) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("%", "")
    try:
        return float(s)
    except ValueError:
        return None


def _is_blankish_label(v: object) -> bool:
    if v is None:
        return True
    s = str(v).strip().lower()
    if s in ("", "-", "--", "na", "n/a", "null", "none", "(blank)", "blank", "unknown"):
        return True
    if s.startswith("unnamed") or s.startswith("anonymous"):
        return True
    return False


def _pick_label_col(rows: list[dict]) -> str | None:
    if not rows:
        return None
    keys = list(rows[0].keys())
    if not keys:
        return None
    # Prefer non-metric textual columns for entity labels.
    for k in keys:
        if _METRIC_HINT_RE.search(k):
            continue
        v = rows[0].get(k)
        if _scalar(v) is None:
            return k
    for k in keys:
        if not _METRIC_HINT_RE.search(k):
            return k
    return keys[0]


def _drop_unfilled_entity_rows(rows: list[dict]) -> list[dict]:
    if not rows:
        return rows
    label_col = _pick_label_col(rows)
    if not label_col:
        return rows
    cleaned = [r for r in rows if not _is_blankish_label(r.get(label_col))]
    return cleaned if cleaned else rows


def _requested_top_n(user_text: str) -> int | None:
    if not user_text:
        return None
    m = _TOP_N_RE.search(user_text)
    if not m:
        m = _TOP_N_FLEX_RE.search(user_text)
    if not m:
        return None
    try:
        n = int(m.group(1))
    except ValueError:
        return None
    return max(1, min(500, n))


def _enforce_top_n_limit(sql: str, requested_n: int | None) -> str:
    """
    If the user asked for top-N, ensure SQL LIMIT is at least N.
    Keeps existing ORDER BY semantics from model output.
    """
    if not sql or not requested_n:
        return sql
    m = _LIMIT_RE.search(sql)
    if m:
        try:
            current = int(m.group(1))
        except ValueError:
            return sql
        if current >= requested_n:
            return sql
        return _LIMIT_RE.sub(f"LIMIT {requested_n}", sql, count=1)
    # If no LIMIT, append one.
    return sql.rstrip().rstrip(";") + f" LIMIT {requested_n}"


@dataclass
class AgentResponse:
    role: str = "assistant"
    content: str = ""
    sql: str | None = None
    validation: dict | None = None
    results: list[dict] | None = None
    all_queries: list[dict] = field(default_factory=list)
    error: str | None = None
    llm_rounds: int = 0

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "content": self.content,
            "sql": self.sql,
            "validation": self.validation,
            "results": self.results,
            "all_queries": self.all_queries,
            "error": self.error,
            "llm_rounds": self.llm_rounds,
        }


def _clean_text(text: str) -> str:
    cleaned = (text or "").strip()
    return _DONE_RE.sub("", cleaned).strip()


def _extract_sql(text: str) -> str | None:
    if not text:
        return None
    m = _SQL_BLOCK_RE.search(text)
    if m:
        return m.group(1).strip().rstrip(";")
    s = text.strip()
    if s.upper().startswith(("SELECT ", "WITH ")):
        return s.rstrip(";")
    return None


def _schema_block(db_state: Optional[DatabaseState]) -> str:
    if db_state is None or not db_state.tables:
        return "No workbook schema available."
    lines: list[str] = []
    for tname in sorted(db_state.tables.keys()):
        meta = db_state.tables[tname]
        cols = ", ".join(meta.column_names)
        lines.append(f"- {meta.name} ({meta.row_count} rows): {cols}")
    return "\n".join(lines)

def _live_db_tables_context(db_state: Optional[DatabaseState]) -> str:
    """Compact table/column catalog injected into SQL generation prompt."""
    if db_state is None or not db_state.tables:
        return live_table_names_prompt_text(max_list_items=260, max_chars=12000) or "No live DB tables loaded."
    lines: list[str] = []
    for tname in sorted(db_state.tables.keys()):
        meta = db_state.tables[tname]
        cols = ", ".join(meta.column_names)
        lines.append(f"{meta.name}({cols})")
    return "\n".join(lines)


def _fallback_answer(question: str, sql: str, rows: list[dict]) -> str:
    total = len(rows)
    if total == 0:
        return (
            "Summary\n"
            f"The query for '{question}' returned no results.\n\n"
            "Key Insights\n"
            "- No matching records were found for the current filters.\n"
            "- Try adjusting the territory, product, time period, or status criteria.\n\n"
            "Sources checked: Arcitus data"
        )
    return (
        "Summary\n"
        f"The query returned {total} record{'s' if total != 1 else ''} from the Arcitus workbook.\n\n"
        "Key Insights\n"
        f"- {total} rows matched the question criteria.\n\n"
        "Sources checked: Arcitus data"
    )


class SQLAgent:
    def __init__(self) -> None:
        timeout_raw = (os.getenv("AZURE_OPENAI_HTTP_TIMEOUT_SEC") or "120").strip()
        try:
            timeout_sec = max(30.0, float(timeout_raw))
        except ValueError:
            timeout_sec = 120.0
        self._client = AzureOpenAI(
            api_key=settings.azure_openai_api_key,
            azure_endpoint=settings.azure_openai_endpoint,
            api_version=settings.azure_openai_api_version,
            timeout=timeout_sec,
            max_retries=2,
        )

    def _chat(self, messages: list[dict[str, str]], *, max_tokens: int) -> str:
        resp = self._client.chat.completions.create(
            model=settings.azure_openai_deployment,
            messages=messages,
            temperature=0,
            max_tokens=max_tokens,
        )
        return (resp.choices[0].message.content or "").strip()

    def _generate_sql(
        self,
        user_text: str,
        db_state: Optional[DatabaseState],
        history: list[dict[str, Any]] | None,
        correction_hint: str = "",
    ) -> str:
        schema = _schema_block(db_state)
        erd_context = ""
        if not use_sqlite_backend():
            try:
                erd_context = (read_erd_markdown(max_chars=32000) or "").strip()
            except Exception:
                erd_context = ""
        live_catalog = _live_db_tables_context(db_state)
        retrieval_context = ""
        try:
            # Semantic similarity over ERD chunks; helps table/column inference when
            # user wording does not exactly match schema terms.
            from schema_rag import retrieval_context_for_nl_question

            retrieval_context = (retrieval_context_for_nl_question(user_text) or "").strip()
        except Exception:
            retrieval_context = ""
        hist = ""
        if history:
            last_msgs = history[-6:]
            hist = "\n".join(f"{m.get('role', 'user')}: {m.get('content', '')}" for m in last_msgs)
        if use_sqlite_backend():
            system = (
                "You are an Arcetus workbook SQL generator.\n"
                "Return exactly one read-only SQLite query inside <sql>...</sql>.\n"
                "Rules:\n"
                "- Only SELECT or WITH statements.\n"
                "- Prefer plain SELECT without CTE; avoid WITH unless absolutely required.\n"
                "- For this workbook, use `Dummy_Data` as the primary/only source table unless schema clearly shows another table.\n"
                "- Use only provided table/column names.\n"
                "- Double-quote column names containing spaces.\n"
                "- If user wording does not exactly match table/column names, use semantic similarity to map intent to the closest real columns from LIVE_DB_TABLES / RETRIEVAL.\n"
                "- Add LIMIT 500 if no limit is present.\n"
                "- Do not include explanation text.\n"
            )
        else:
            system = (
                "You are a PostgreSQL SQL generator.\n"
                "Return exactly one read-only PostgreSQL query inside <sql>...</sql>.\n"
                "Rules:\n"
                "- Only SELECT or WITH statements.\n"
                "- Use only table/column names present in LIVE_DB_TABLES / RETRIEVAL.\n"
                "- For mixed-case identifiers, use double quotes exactly as listed.\n"
                "- Prefer explicit JOIN conditions using known FK paths.\n"
                "- For numeric calculations on text-like columns, cast safely: COALESCE(NULLIF(col, '')::numeric, 0).\n"
                "- Do NOT use COALESCE(col, 0)::numeric when col can be text.\n"
                "- If the user asks for 'contribution' or 'share contributed' by segment, compute contribution against TOTAL ZORYVE TRx across all returned segments (SUM(segment_zoryve_trx) / SUM(all_segment_zoryve_trx)).\n"
                "- For contribution asks, return a contribution metric alias like contribution_pct (or contribution_share_pct), not segment share vs TCS.\n"
                "- Use segment share vs TCS only when user explicitly asks for segment-level share/penetration versus class size.\n"
                "- Add LIMIT 500 if no limit is present.\n"
                "- Do not include explanation text.\n"
            )
        if use_sqlite_backend():
            user = (
                f"Workbook schema:\n{schema}\n\n"
                f"LIVE_DB_TABLES:\n{live_catalog}\n\n"
                f"RETRIEVAL (embedding similarity):\n{retrieval_context or '(none)'}\n\n"
                f"Conversation context:\n{hist or '(none)'}\n\n"
                f"Question:\n{user_text}\n\n"
                f"{correction_hint}"
            )
        else:
            user = (
                f"ERD_CONTEXT:\n{erd_context or '(none)'}\n\n"
                f"LIVE_DB_TABLES:\n{live_catalog}\n\n"
                f"RETRIEVAL (embedding similarity):\n{retrieval_context or '(none)'}\n\n"
                f"Conversation context:\n{hist or '(none)'}\n\n"
                f"Question:\n{user_text}\n\n"
                f"{correction_hint}"
            )
        raw = self._chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=min(1800, max(600, int(settings.openai_max_tokens or 1500))),
        )
        sql = _extract_sql(raw)
        if not sql:
            raise RuntimeError("Model did not return SQL in <sql> tags.")
        sql = _enforce_top_n_limit(sql, _requested_top_n(user_text))
        return sql

    def _validate_and_execute(
        self, sql: str, db_state: Optional[DatabaseState]
    ) -> tuple[dict, list[dict], str | None]:
        vr: ValidationResult = validate_sql(
            sql, db_state if settings.sql_validation_enabled else None
        )
        if vr.valid:
            try:
                readonly_sql = validate_read_only_sql(
                    vr.sanitized or sql, dialect=sqlglot_dialect_for_backend()
                )
                vr = ValidationResult(
                    valid=True,
                    errors=[],
                    warnings=vr.warnings,
                    sanitized=readonly_sql,
                )
            except SQLValidationError as exc:
                vr = ValidationResult(
                    valid=False,
                    errors=[str(exc)],
                    warnings=vr.warnings,
                    sanitized=vr.sanitized,
                )
        if not vr.valid:
            return vr.to_dict(), [], "SQL validation failed: " + "; ".join(vr.errors)
        try:
            rows = run_query(vr.sanitized or sql, max_rows=settings.query_row_limit)
            return vr.to_dict(), rows, None
        except Exception as exc:
            return vr.to_dict(), [], f"SQL execution error: {exc}"

    def _generate_answer(self, question: str, sql: str, rows: list[dict]) -> str:
        total_rows = len(rows)
        requested_n = _requested_top_n(question)
        display_limit = requested_n if requested_n else 10
        # Give the LLM requested top-N rows (or 10 by default) for bullet narrative.
        display_rows = rows[:display_limit]
        system = (
            "You are a helpful analytics assistant for the Arcitus workbook.\n"
            "Always respond in clear, natural, human-like language. Never sound robotic.\n\n"
            "STRICT FORMATTING RULES — follow every one:\n"
            "1. Never use markdown tables, pipe tables, or any grid layout.\n"
            "2. Never show SQL code, SELECT/FROM/WHERE/LIMIT keywords, or query logic.\n"
            "3. Do NOT dump raw field-value pairs like 'Region: Florida · Territory: Miami'. "
            "   Instead, write each result as a readable sentence or named bullet.\n"
            "4. Do NOT repeat the same data in multiple sections.\n\n"
            "RESPONSE STRUCTURE — use exactly these sections in order:\n"
            "Summary\n"
            "  One or two sentences giving the key takeaway.\n\n"
            "Key Insights\n"
            "  Put each insight on its OWN LINE as a markdown bullet starting with '- '.\n"
            "  Do not join insights with middots (·), bullets (•), or semicolons on one line.\n\n"
            "Top Results\n"
            "  List up to 10 items; each MUST be its OWN LINE beginning with '- ' (markdown).\n"
            "  Each line is ONE readable sentence describing the territory/entity.\n"
            "  Format: '[Name/Place — context] — metric / change (+X%)' "
            "(not 'Region: … · Territory: …').\n"
            "  Example line: '- Miami, Florida — grew from 15 to 23 TRx (+53.3%)'\n"
            "  Never put all ranked rows on one long line separated by middots.\n"
            "  If no ranked/list data is present, omit this section entirely.\n\n"
            "Supporting Observations\n"
            "  Any notable patterns, stalling markets, or context worth calling out.\n"
            "  Skip this section if nothing meaningful to add.\n\n"
            "PERCENTAGE SAFETY RULES:\n"
            "- Never add/total percentages across groups with different denominators.\n"
            "- If user asks for an overall share, compute it from DB totals: SUM(numerator) / SUM(denominator).\n"
            "- If each row already has share %, treat it as per-group ratio and do not present their sum.\n\n"
            "RELATIONSHIP ANALYSIS RULES (for questions like call frequency vs TRx):\n"
            "- Distinguish correlation from causation: use 'association' or 'directional relationship';\n"
            "  do not claim one variable causes another unless explicitly proven.\n"
            "- If data is bucketed/aggregated, do not make entity-level conclusions.\n"
            "- For non-ranked relationship outputs, do not use 'Top Results'; prefer labels like\n"
            "  'Call Frequency Buckets', 'Segment Performance', or 'Distribution Overview'.\n"
            "- Explicitly mention limitations when granular/entity data is not available.\n"
            "- Highlight threshold effects, plateau/diminishing returns, and obvious anomalies.\n\n"
            "SCOPE CONTROL RULES:\n"
            "- If the user asks about one region only (e.g., 'why underperforming in East'),\n"
            "  keep analysis strictly in that region's internal drivers.\n"
            "- Do NOT compare against other regions unless user explicitly asks for comparison.\n"
            "- Use causal-safe language (association/possible contributors), not proven causation.\n\n"
            "Final line must start exactly with: Sources checked:"
        )
        user = (
            f"Question:\n{question}\n\n"
            f"Total rows returned: {total_rows}\n"
            f"Top {len(display_rows)} rows (use these to write the Top Results bullets):\n"
            f"{json.dumps(display_rows, ensure_ascii=True, default=str)}"
        )
        try:
            raw = self._chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                max_tokens=min(2200, max(700, int(settings.openai_max_tokens or 1500))),
            )
            cleaned = _clean_text(raw)
            if cleaned:
                return cleaned
        except Exception as exc:
            logger.warning("Answer generation failed, using fallback: %s", exc)
        return _fallback_answer(question, sql, rows)

    def run(
        self,
        user_text: str,
        history: list[dict[str, Any]] | None = None,
        db_state: Optional[DatabaseState] = None,
    ) -> AgentResponse:
        if not (user_text or "").strip():
            return AgentResponse(content="Please enter a question.")

        all_queries: list[dict] = []
        llm_rounds = 0

        try:
            sql = ""
            validation: dict | None = None
            rows: list[dict] = []
            step_error: str | None = None
            correction_hint = ""

            for attempt in range(2):
                sql = self._generate_sql(
                    user_text, db_state, history, correction_hint=correction_hint
                )
                llm_rounds += 1

                validation, rows, step_error = self._validate_and_execute(sql, db_state)
                rows = _drop_unfilled_entity_rows(rows)
                all_queries.append(
                    {
                        "sql": sql,
                        "results": rows,
                        "validation": validation,
                        "error": step_error,
                    }
                )

                if not step_error:
                    break

                # Retry once with strict correction context (unknown table/column, bad SQL shape, etc.).
                if attempt == 0:
                    if use_sqlite_backend():
                        correction_hint = (
                            "[CORRECTION REQUIRED]\n"
                            f"Previous SQL failed validation/execution.\n"
                            f"Error: {step_error}\n"
                            f"Previous SQL:\n{sql}\n\n"
                            "Rewrite from scratch using ONLY table/column names from Workbook schema above.\n"
                            "Avoid CTEs (`WITH`). Use a single SELECT rooted on real workbook tables (typically `Dummy_Data`).\n"
                            "Do not invent table names. Return exactly one <sql>...</sql>."
                        )
                    else:
                        correction_hint = (
                            "[CORRECTION REQUIRED]\n"
                            f"Previous SQL failed validation/execution.\n"
                            f"Error: {step_error}\n"
                            f"Previous SQL:\n{sql}\n\n"
                            "Rewrite from scratch using ONLY live PostgreSQL table/column names shown above.\n"
                            "For text numeric fields, use COALESCE(NULLIF(col, '')::numeric, 0).\n"
                            "Do not invent identifiers. Return exactly one <sql>...</sql>."
                        )

            if step_error:
                return AgentResponse(
                    content="I could not complete the query safely. Please refine your question.",
                    sql=sql or None,
                    validation=validation,
                    results=rows,
                    all_queries=all_queries,
                    error=step_error,
                    llm_rounds=llm_rounds,
                )

            answer = self._generate_answer(user_text, sql, rows)
            llm_rounds += 1

            return AgentResponse(
                content=answer,
                sql=sql,
                validation=validation,
                results=rows,
                all_queries=all_queries,
                error=None,
                llm_rounds=llm_rounds,
            )
        except Exception as exc:
            logger.exception("SQLAgent run failed")
            return AgentResponse(
                content=(
                    "I could not process this question right now. "
                    "Check Azure/OpenAI settings and database availability."
                ),
                sql=None,
                validation=None,
                results=[],
                all_queries=all_queries,
                error=str(exc),
                llm_rounds=llm_rounds,
            )
