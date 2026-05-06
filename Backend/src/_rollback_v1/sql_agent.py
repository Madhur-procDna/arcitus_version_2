from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from openai import AzureOpenAI

from config import settings
from data_loader import DatabaseState, execute_query
from db_adapter import sqlglot_dialect_for_backend
from sql_validate import SQLValidationError, validate_read_only_sql
from sql_validator import ValidationResult, validate_sql

logger = logging.getLogger(__name__)

_SQL_BLOCK_RE = re.compile(r"<sql>\s*(.*?)\s*</sql>", flags=re.IGNORECASE | re.DOTALL)
_DONE_RE = re.compile(r"</?done\s*/?>", flags=re.IGNORECASE)


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
        return "No live DB tables loaded."
    lines: list[str] = []
    for tname in sorted(db_state.tables.keys()):
        meta = db_state.tables[tname]
        cols = ", ".join(meta.column_names)
        lines.append(f"{meta.name}({cols})")
    return "\n".join(lines)


def _fallback_answer(question: str, sql: str, rows: list[dict]) -> str:
    total = len(rows)
    lines = [
        f"We ran a workbook query for: **{question}**.",
        "",
        "What we verified",
        f"- SQL executed: `{sql}`",
        f"- Total rows returned: **{total}**.",
        "",
    ]
    if total == 0:
        lines += [
            "Key Findings",
            "- No matching rows were found for the current filters.",
            "- Try adjusting territory, product, time period, or status filters.",
            "",
            "Detailed Analysis",
            "The query executed successfully but returned zero rows.",
        ]
    else:
        lines += [
            "Key Findings",
            f"- Query returned **{total}** rows from the Arcitus workbook.",
            "",
            "Detailed Analysis",
            "The query completed; row values are listed below in plain language (one line per row).",
        ]
    lines += ["", "Sources checked: Arcitus data"]
    return "\n".join(lines)


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
        user = (
            f"Workbook schema:\n{schema}\n\n"
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
            rows = execute_query(vr.sanitized or sql, limit=settings.query_row_limit)
            return vr.to_dict(), rows, None
        except Exception as exc:
            return vr.to_dict(), [], f"SQL execution error: {exc}"

    def _generate_answer(self, question: str, sql: str, rows: list[dict]) -> str:
        rows_preview = rows[:120]
        system = (
            "You are a helpful analytics assistant for the Arcitus workbook.\n"
            "Answer in clear, natural, conversational language — avoid robotic phrasing.\n"
            "Use the sections: What we verified, Key Findings, Detailed Analysis.\n"
            "Rules:\n"
            "- Never show SQL, SELECT/FROM/WHERE/LIMIT keywords, or technical query logic.\n"
            "- Never use markdown tables, pipe tables, or grid layouts.\n"
            "- In Detailed Analysis, write highlights and interpretation only. "
            "  The system will automatically append every result row as a plain-language bullet — do not repeat row data yourself.\n"
            "- For top-N requests, mention the count prominently (e.g. 'Here are the top 10 HCPs...').\n"
            "- Keep the tone clear and direct, like explaining to a non-technical business user.\n"
            "Final line must start exactly with: Sources checked:"
        )
        user = (
            f"Question:\n{question}\n\n"
            f"SQL used:\n{sql}\n\n"
            f"Total rows: {len(rows)}\n"
            f"Rows JSON:\n{json.dumps(rows_preview, ensure_ascii=True)}"
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
                    correction_hint = (
                        "[CORRECTION REQUIRED]\n"
                        f"Previous SQL failed validation/execution.\n"
                        f"Error: {step_error}\n"
                        f"Previous SQL:\n{sql}\n\n"
                        "Rewrite from scratch using ONLY table/column names from Workbook schema above.\n"
                        "Avoid CTEs (`WITH`). Use a single SELECT rooted on real workbook tables (typically `Dummy_Data`).\n"
                        "Do not invent table names. Return exactly one <sql>...</sql>."
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
                    "Check Azure/OpenAI settings and workbook availability."
                ),
                sql=None,
                validation=None,
                results=[],
                all_queries=all_queries,
                error=str(exc),
                llm_rounds=llm_rounds,
            )
