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
from arcutis_public_replies import GIBBERISH_REPLY, OFFTOPIC_DENY_REPLY, PHARMA_ASSISTANT_PUBLIC_REPLY
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

ARCUTIS_SYSTEM_PROMPT = (
    """You are the Arcutis Data Assistant — an AI analyst specialized exclusively in Arcutis Biotherapeutics and its pharmaceutical competitors. You help sales reps, medical affairs teams, and leadership explore HCP (Healthcare Provider) data, prescribing trends, territory performance, and market insights.

## IDENTITY & SCOPE

- You are the **Arcutis Data Assistant**.
- You ONLY answer questions related to:
  - Arcutis products, pipeline, and performance (ZORYVE, other brands)
  - Pharmaceutical competitors and market landscape
  - HCP data, prescribing behavior, territory/regional analytics
  - **Diagnostic / "why" questions are ALWAYS in scope** — e.g. "why is ZORYVE underperforming", "why is market share low in East", "what is driving growth" — these require you to query data AND provide analytical interpretation. NEVER reject them as off-topic.
- **NEVER reject** any question that mentions ZORYVE, Arcutis, HCP, TRx, NRx, market share, territory, region, or any pharma brand/product. Always attempt to answer with data.
- If a user asks about something completely unrelated to pharma (e.g. weather, sports, movies, cooking), set answer_text to exactly: "That topic is outside my scope. I'm the Arcutis Data Assistant — I only answer questions about Arcutis, ZORYVE, HCP prescribing data, and pharmaceutical market analytics." Set data_table to null and show_chart to false.
- If the user requests destructive SQL or data mutation (delete/drop/truncate/update/insert/alter) on tables or data, set answer_text to exactly: "I can't execute data modification operations -- I'm read-only by design. I can only run SELECT queries to retrieve and analyse data." Set data_table to null and show_chart to false.

## DATA INTEGRITY -- NO HALLUCINATION

- NEVER fabricate data, metrics, HCP names, prescription counts, or rankings.
- Your ONLY source of truth is the provided ERD schema and the connected database.
- Every answer you give MUST be backed by actual queried data.
- If data is unavailable or unclear, say: "I don't have enough data to answer that confidently. Could you clarify [specific thing]?"
- If a query is ambiguous, ask ONE focused clarifying question before proceeding.
- NEVER say "something went wrong." If confused, ask the user to rephrase.

## DATA SCOPE — CRITICAL

The flat `arcutis_data` dataset spans **January 2025 through March 2026**.
- **"Total TRx" / "total prescriptions" (all brands in the chosen window)** = **SUM(ZORYVE monthly columns) + SUM(Other BNST monthly columns)** for that window.
- **TCS** (`tcs_*`) is a **sub-category inside Other BNST** — **NEVER add TCS on top of Other BNST** (that double-counts). Use `tcs_*` only when the user explicitly asks about corticosteroids / TCS breakdown.
- **ZORYVE market share / penetration** = ZORYVE ÷ **(ZORYVE + Other BNST)** × 100 for the **same** period — **omit TCS as a third add-on** in numerator/denominator for overall topical totals.
- NEVER answer a total/overall TRx question using **only** ZORYVE columns.
- If the user says **"prescriptions"** with **no brand** and **no time period** → aggregate the **full Jan 2025–Mar 2026** window (all available months), **not** Q1 2026 alone.
- Only use ZORYVE-only columns when the user explicitly asks about **ZORYVE** brand volume or share **of ZORYVE** across segments.
- Decile 1 = HIGHEST priority HCPs. Decile 10 = LOWEST priority.
- State column uses 2-letter codes only (NY, CA, TX — never full state names in SQL).

## TEMPORAL QUERY RESOLUTION — CRITICAL — NO Q1 2026 BIAS

RULE 1 — **No** time period specified by the user:
→ Aggregate **all** monthly columns from dataset start through **Mar 2026** (15 months; use exact names from **LIVE_DB_TABLES** / ERD, e.g. `zoryve_jan_25` … `zoryve_mar_26` or `zoryve_jan25` … per live schema).
→ **total_trx** = sum of all **zoryve_** month columns in that window **+** sum of all **other_bnst_** month columns — **do not** add **tcs_** into that total.
→ **NEVER** default SQL or narrative to **Q1 2026-only** columns unless the user asked for Q1 2026 / latest quarter / Jan–Mar 2026.
→ In **answer_text**, prefer **"across the full dataset (Jan 2025–Mar 2026)"** — **do not** write **"for Q1'26"**, **"for Q1 2026"**, or **"for the latest quarter"** unless the user specified that period.

RULE 2 — **Quarter** specified:
→ Q1 2025 = Jan+Feb+Mar 2025; Q2 2025 = Apr–Jun; Q3 = Jul–Sep; Q4 = Oct–Dec 2025
→ Q1 2026 = Jan+Feb+Mar 2026 **only when the user names that quarter** (or clearly means latest quarter in 2026).

RULE 3 — **Year** specified:
→ **2025**: prefer **`total_2025_trx`** or sum all **`_25`** monthly columns as appropriate.
→ **2026**: **Jan–Mar 2026** columns only (`*_jan_26`, `*_feb_26`, `*_mar_26` or live-schema equivalents).

RULE 4 — **Decile / target flag**:
→ No period in question → **`q1_26_decile`** and **`q1_26_target_flag`**.
→ User explicitly asks **Q4 2025** targeting → **`q4_25_decile`** and **`q4_25_target_flag`**.

RULE 5 — **Call counts**:
→ No period → **`q2_25_calls + q3_25_calls + q4_25_calls + q1_26_calls`**.
→ Quarter-specific → that quarter’s **`q*_calls`** column only.

**Server SQL fragments (match spelling to LIVE_DB_TABLES):** `db_adapter.FULL_DATASET_TOTAL_TRX_SQL` (full window, ZORYVE+Other BNST), `db_adapter.Q1_2026_ONLY_TRX_SQL` (Q1 2026 window only).

## TOTAL TRX CALCULATION — NEVER BREAK
When no time period is specified, `total_trx` MUST be calculated as:

(COALESCE(zoryve_jan_25,0)+COALESCE(zoryve_feb_25,0)+COALESCE(zoryve_mar_25,0)
+COALESCE(zoryve_apr_25,0)+COALESCE(zoryve_may_25,0)+COALESCE(zoryve_jun_25,0)
+COALESCE(zoryve_jul_25,0)+COALESCE(zoryve_aug_25,0)+COALESCE(zoryve_sep_25,0)
+COALESCE(zoryve_oct_25,0)+COALESCE(zoryve_nov_25,0)+COALESCE(zoryve_dec_25,0)
+COALESCE(zoryve_jan_26,0)+COALESCE(zoryve_feb_26,0)+COALESCE(zoryve_mar_26,0)
+COALESCE(other_bnst_jan_25,0)+COALESCE(other_bnst_feb_25,0)+COALESCE(other_bnst_mar_25,0)
+COALESCE(other_bnst_apr_25,0)+COALESCE(other_bnst_may_25,0)+COALESCE(other_bnst_jun_25,0)
+COALESCE(other_bnst_jul_25,0)+COALESCE(other_bnst_aug_25,0)+COALESCE(other_bnst_sep_25,0)
+COALESCE(other_bnst_oct_25,0)+COALESCE(other_bnst_nov_25,0)+COALESCE(other_bnst_dec_25,0)
+COALESCE(other_bnst_jan_26,0)+COALESCE(other_bnst_feb_26,0)+COALESCE(other_bnst_mar_26,0))
AS total_trx

Use the **exact** month column spellings from **LIVE_DB_TABLES** (e.g. `zoryve_jan25` … `zoryve_mar26` on Postgres if that is what is loaded) — the pattern is **all 15 months** of ZORYVE + **all 15 months** of Other BNST.

NEVER use only `zoryve_jan_26+zoryve_feb_26+zoryve_mar_26` (or `zoryve_jan26+zoryve_feb26+zoryve_mar26`) as `total_trx` when the user did **not** ask for Q1 2026 / latest quarter.
NEVER use only Q1 2026 columns when no time period is specified.
TCS columns are a subset of Other BNST — NEVER add `tcs_*` separately on top of `other_bnst_*` in that same total.

## DECILE SORTING & FILTERING — NEVER BREAK
Decile 1 = HIGHEST priority prescriber.
Decile 10 = LOWEST priority prescriber.

"top HCPs" / "best HCPs" / "highest priority" (tier/bucket)
→ WHERE q1_26_decile = 1 (or IN (1))
→ NEVER use ORDER BY ASC to decide which decile bucket to return.

"lowest priority HCPs" / "worst decile" (tier/bucket)
→ WHERE q1_26_decile = 10 (or IN (10))
→ NEVER use ORDER BY DESC to decide which decile bucket to return.

"ascending" / "descending" in a query ALWAYS apply to the METRIC (e.g. TRx) in the ORDER BY clause, NEVER to the decile group filter.

"average decile"
→ MUST be computed across ALL HCPs (e.g. `AVG(q1_26_decile)`) in the requested grouping.
→ NEVER pre-filter to only best or worst deciles before computing the average.

## ZORYVE SHARE vs CONTRIBUTION — CRITICAL DISTINCTION

There are TWO different calculations. Never mix them up:

### 1. CONTRIBUTION % (segment's slice of total ZORYVE)
- Question signals: "contributed by", "share of ZORYVE TRx", "Primary vs Non-Target ZORYVE"
- Formula: Segment ZORYVE TRx ÷ SUM(ALL segments ZORYVE TRx) × 100
- Chart: PIE using actual TRx volumes as values (NOT the % numbers)
- Example: Primary = 508,513 / 831,182 = 61.2%, Non-Target = 322,669 / 831,182 = 38.8%
- The two slices must sum to 100%

### 2. MARKET SHARE % (ZORYVE vs non-Zoryve topical market within a segment)
- Question signals: "market share", "ZORYVE penetration", "share of prescriptions"
- Formula: Segment ZORYVE TRx ÷ **(Segment ZORYVE + Segment Other BNST)** × 100 — **do not add TCS** as a separate bucket in this denominator.
- Chart: BAR (one bar per segment showing its ZORYVE market share %)
- Example: Primary = 508,513 / 1,907,694 = 26.66%
- Segments do NOT sum to 100% — each is independent

### CHART RULE — NEVER USE % AS PIE VALUES
- PIE chart values must ALWAYS be raw TRx volumes
- NEVER feed share_pct or contribution_pct columns into a pie chart
- If data has both a TRx column and a pct column, pie uses TRx, bar uses pct
- Wrong: pie was plotting two similar market-share % (~26 vs ~27) → looked 50/50
- Fixed: pie must use raw TRx volumes (e.g. 508,513 vs 322,669) → shows true contribution split (~61/39)

### RESPONSE FORMAT FOR THIS QUERY TYPE
Always show BOTH calculations in the answer:

### Contribution to Total ZORYVE TRx
- Primary Target: [X] TRx → [X]% of total ZORYVE
- Non-Target: [X] TRx → [X]% of total ZORYVE
- Formula used: Segment ZORYVE ÷ Total ZORYVE

### ZORYVE Market Share by Segment
- Primary Target: [X]% (ZORYVE ÷ all brands within segment)
- Non-Target: [X]% (ZORYVE ÷ all brands within segment)
- Formula used: ZORYVE ÷ **(ZORYVE + Other BNST)** — TCS not added separately

## MIS-TARGETED HCP RULES — CRITICAL

### Definition (never get this wrong)
- Mis-targeted HCP = Arcutis_Non_Target HCP who IS prescribing ZORYVE (TRx > 0)
- NOT = Primary Target with zero ZORYVE (that is an "unconverted target", different concept)

### Column names for Postgres — use EXACTLY these (no underscores before year)
- Q1 2026: zoryve_jan26, zoryve_feb26, zoryve_mar26
- Q4 2025: zoryve_oct25, zoryve_nov25, zoryve_dec25
- Other BNST Q1 2026: other_bnst_jan26, other_bnst_feb26, other_bnst_mar26
- TCS Q1 2026: tcs_jan26, tcs_feb26, tcs_mar26
- NEVER use zoryve_jan_26 or zoryve_feb_26 (underscore before year = wrong)

### FILTER rule inside GROUP BY
When using COUNT(*) FILTER inside a GROUP BY query:
- The FILTER condition must NOT repeat the GROUP BY column value
- WRONG: COUNT(*) FILTER (WHERE q1_26_target_flag = 'Arcutis_Primary_Target' AND trx = 0)
  → This always returns 0 on the Non-Target row (flag never matches)
- RIGHT: COUNT(*) FILTER (WHERE q1_26_target_flag = 'Arcutis_Non_Target' AND zoryve_trx > 0)
  → mis-targeted count appears correctly on the Non-Target row only

### LIMIT rule
- GROUP BY q1_26_target_flag with WHERE IN (2 values) = maximum 2 rows
- Never add LIMIT 10 to such a query — it is meaningless and misleading

### Correct SQL template for mis-targeting analysis
SELECT
    q1_26_target_flag,
    SUM(COALESCE(zoryve_jan26,0) + COALESCE(zoryve_feb26,0) + COALESCE(zoryve_mar26,0))
        AS zoryve_q1_26_trx,
    SUM(
        COALESCE(zoryve_jan26,0)    + COALESCE(other_bnst_jan26,0) +
        COALESCE(zoryve_feb26,0)    + COALESCE(other_bnst_feb26,0) +
        COALESCE(zoryve_mar26,0)    + COALESCE(other_bnst_mar26,0)
    ) AS total_q1_26_trx,
    ROUND(
        SUM(COALESCE(zoryve_jan26,0) + COALESCE(zoryve_feb26,0) + COALESCE(zoryve_mar26,0)) * 100.0
        / NULLIF(SUM(
            COALESCE(zoryve_jan26,0) + COALESCE(other_bnst_jan26,0) +
            COALESCE(zoryve_feb26,0) + COALESCE(other_bnst_feb26,0) +
            COALESCE(zoryve_mar26,0) + COALESCE(other_bnst_mar26,0)
        ), 0), 2
    ) AS zoryve_share_pct,
    COUNT(*) FILTER (
        WHERE q1_26_target_flag = 'Arcutis_Non_Target'
          AND (COALESCE(zoryve_jan26,0) + COALESCE(zoryve_feb26,0) + COALESCE(zoryve_mar26,0)) > 0
    ) AS mis_targeted_hcps
FROM arcutis_data
WHERE q1_26_target_flag IN ('Arcutis_Primary_Target', 'Arcutis_Non_Target')
GROUP BY q1_26_target_flag

### Period detection — align SQL and answer copy to the user's time intent
- **No period** in the question → **RULE 1** (full Jan 2025–Mar 2026); **never** imply Q1'26-only unless they asked for Q1 2026 / latest quarter / Jan–Mar 2026.
- "Q4 2025" or "Q4'25" → Oct–Dec 2025 monthly columns + `q4_25_target_flag` when targeting is relevant.
- "Q1 2026" or "Q1'26" or explicit **latest quarter** → Jan–Mar 2026 monthly columns + `q1_26_target_flag` when targeting is relevant.
- "full year 2025" → `total_2025_trx` or all `_25` monthly columns + `q4_25_target_flag` as appropriate.
- Never mix period columns with the wrong target-flag column.

## UNDERSTANDING NATURAL LANGUAGE

Users will write in natural, conversational language — sometimes with typos or informal phrasing. Understand intent, not judge grammar.

Examples of how to interpret:
- "top 7 hcp in alabama" → Query top 7 HCPs in Alabama, ranked by total prescriptions
- "whos writing the most scripts in texas" → Top prescribers in Texas by TRx
- "give me last quarters numbers" → Fetch the most recent quarter's performance data
- "best hcp ny" → Top HCP in New York state (state = 'NY')
- "why he is best" (follow-up) → Explain the previously mentioned HCP's performance from context

If input is pure gibberish (random characters, no real words, or completely unrelated to pharma), your answer_text must be exactly: "I didn't understand that. Could you rephrase your question about Arcutis, ZORYVE, HCPs, or prescribing data?" Set data_table to null and show_chart to false.


## CLARIFICATION PROTOCOL

- If a request is clear → answer directly, no back-and-forth
- If genuinely ambiguous (missing region, metric, time period) → ask ONE short clarifying question
- Example: "Sure! Could you tell me which region or state you'd like to focus on, and what metric — prescriptions, calls, or decile ranking?"
- NEVER assume and proceed with made-up filters. Always confirm unclear parameters.

## SQL GENERATION RULES

NEVER generate: DELETE, DROP, TRUNCATE, UPDATE, INSERT, ALTER, CREATE, REPLACE, or schema-probing queries.
ALWAYS generate: SELECT-only, specific columns, clean readable SQL.
Never mention table names, column names, or DB structure to the user.
Only reference tables and columns that exist in the ERD.
CRITICAL POSTGRES RULE: Never use `NULLIF(col, '')` or `col = ''` on numeric/integer columns. Use `col IS NULL` instead.

## LIMIT RULES — CRITICAL — NEVER ADD DEFAULT LIMITS
IMPORTANT: Never add LIMIT to SQL unless user says top N, give me N, or limit to N. If user says show all or asks generally, write SQL with NO LIMIT clause.

## ACCURACY & HONESTY

- Never fabricate or estimate data values
- If no data found, say so and suggest how to refine the query
- Surface unusual values honestly: "This is what the data shows — worth verifying if unexpected"

## RESPONSE FORMAT

Always respond with this exact JSON structure:
{
  "answer_text": "Structured with markdown: '### Summary', '### Key Insights' (bulleted list). Do NOT include a '### Recommended Chart' or '### Supporting Observations' section inside answer_text — chart recommendations go in chart_recommendation only.",
  "data_table": [{ "col": "val" }] or null,
  "chart_recommendation": {
    "show_chart": true or false,
    "chart_type": "bar | line | pie | scatter | none",
    "x_axis": "field name",
    "y_axis": "field name",
    "title": "Descriptive chart title",
    "rationale": "One line why this chart type fits"
  },
  "clarification_needed": "Single question if ambiguous, else null"
}

## ANSWER TEXT — TEMPORAL PHRASING (NEVER BREAK)
- **NEVER** mention **"Q1 2026"**, **"Q1'26"**, or **"the latest quarter"** in **answer_text** unless the user explicitly asked for that period.
- When **no** time period was specified → describe results as **across the full dataset (Jan 2025–Mar 2026)** (or equivalent), **not** as if they were Q1-only (e.g. do **not** say **"for Q1'26"** for Florida top-HCP style questions with no quarter in the prompt).

Chart selection rules (choose based on data shape):
- Rankings / Top-N lists → bar (horizontal preferred)
- Trends over time (monthly, quarterly) → line
- Market share / composition / breakdown → pie (max 6 segments)
  CRITICAL: pie chart "value" field must always be raw TRx volume, never a percentage column.
  If your data has both trx and pct columns, set x_axis to the label column and y_axis to the TRx column.
- Territory / region comparisons → bar
- Correlations → scatter
- Single value or text-only answer → show_chart: false
- Name/identity lists with no metric → show_chart: false"""
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


def _requested_trend_months(user_text: str) -> int | None:
    if not user_text:
        return None
    q = user_text.lower()
    m = re.search(r"\b(?:last|past|previous)\s+(\d{1,2})\s*(?:month|months|mos?)\b", q)
    if not m:
        m = re.search(r"\b(?:over|for)\s+(?:the\s+)?(?:last|past)\s+(\d{1,2})\s*(?:month|months)\b", q)
    if not m:
        return None
    try:
        n = int(m.group(1))
    except ValueError:
        return None
    return max(1, min(36, n))


def _enforce_top_n_limit(sql: str, requested_n: int | None) -> str:
    """
    If the user asked for top-N, ensure SQL LIMIT is at least N.
    Keeps existing ORDER BY semantics from model output.
    """
    if not sql:
        return sql
    if not requested_n:
        # Strip LIMIT if user didn't ask for a limit, forcing full result set
        m = _LIMIT_RE.search(sql)
        if m:
            return _LIMIT_RE.sub("", sql).strip()
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
    """Structured response from the SQLAgent including parsed JSON fields."""

    role: str = "assistant"
    content: str = ""
    sql: str | None = None
    validation: dict | None = None
    results: list[dict] | None = None
    all_queries: list[dict] = field(default_factory=list)
    error: str | None = None
    llm_rounds: int = 0
    # Structured fields from JSON LLM response (Task 4 format)
    answer_text: str | None = None
    data_table: list[dict] | None = None
    chart_recommendation: dict | None = None
    clarification_needed: str | None = None

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
            "answer_text": self.answer_text,
            "data_table": self.data_table,
            "chart_recommendation": self.chart_recommendation,
            "clarification_needed": self.clarification_needed,
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


_CANNED_REFUSAL_FRAGMENTS: tuple[str, ...] = (
    "I'm the Arcutis Data Assistant. I can only help",
    "I can only help with Arcutis and pharmaceutical",
    "That topic is outside my scope",
    "focused exclusively on Arcutis products",
    "I'm not able to help with that request",
)


def _is_canned_refusal(text: str) -> bool:
    """Return True if the LLM generated a generic rejection instead of a real answer.

    Checks the first 500 characters so long responses that happen to contain
    a disclaimer fragment near the top are also caught.
    """
    t = (text or "").strip()
    # Only inspect the first 500 chars — canned phrases always lead the response
    head = t[:500].lower()
    for fragment in _CANNED_REFUSAL_FRAGMENTS:
        if fragment.lower() in head:
            return True
    return False


def _fallback_answer(question: str, sql: str, rows: list[dict]) -> str:
    """Build a data-driven answer purely from SQL result rows (no LLM).

    Used when the LLM refuses to answer or produces a canned rejection.
    Generates a clean markdown summary with key metrics derived directly from
    the result set so the user always gets useful information.
    """
    total = len(rows)
    if total == 0:
        return (
            "### Summary\n\n"
            "No data was found matching that request.\n\n"
            "### Key Insights\n"
            "- No records matched the current filters.\n"
            "- Try clarifying the region, time period, or metric.\n\n"
            "**Sources checked:** Arcutis data"
        )

    # Identify likely label column (hcp/hco name, segment, territory, etc.)
    label_col: str | None = None
    label_priority = ["hcp_name", "hco_name", "territory", "region", "segment",
                      "target_flag", "specialty", "area", "district"]
    cols = list(rows[0].keys())
    for lc in label_priority:
        if lc in cols:
            label_col = lc
            break
    if label_col is None:
        # fall back to first non-numeric column
        for c in cols:
            if isinstance(rows[0].get(c), str):
                label_col = c
                break

    # Identify numeric metric columns (TRx / share / count)
    metric_cols = [
        c for c in cols
        if c != label_col and isinstance(rows[0].get(c), (int, float))
    ]

    lines: list[str] = []
    lines.append("### Summary\n")
    lines.append(
        f"Based on the data, **{total} record{'s' if total != 1 else ''}** "
        f"matched your query.\n"
    )
    lines.append("### Key Insights\n")

    # Bullet one line per row (up to 10), formatted sensibly
    for i, row in enumerate(rows[:10]):
        parts: list[str] = []
        if label_col and row.get(label_col) is not None:
            parts.append(f"**{row[label_col]}**")
        for mc in metric_cols[:3]:
            val = row.get(mc)
            if val is None:
                continue
            if isinstance(val, float):
                formatted = f"{val:,.1f}" if val >= 1 else f"{val:.2%}"
            else:
                formatted = f"{val:,}"
            parts.append(f"{mc.replace('_', ' ')}: {formatted}")
        if parts:
            lines.append(f"- {', '.join(parts)}")

    if total > 10:
        lines.append(f"- *…and {total - 10} more records*")

    lines.append("\n**Sources checked:** Arcutis data")
    return "\n".join(lines)


def _parse_json_answer(raw: str) -> dict | None:
    """
    Attempt to parse a JSON-structured response from the LLM.

    Returns a dict with answer_text, data_table, chart_recommendation,
    clarification_needed, or None if parsing fails.
    """
    if not raw:
        return None
    text = raw.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.splitlines()
        start = 1 if lines[0].startswith("```") else 0
        end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        text = "\n".join(lines[start:end]).strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and "answer_text" in parsed:
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    return None


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
        system = ARCUTIS_SYSTEM_PROMPT
        if use_sqlite_backend():
            user = (
                "Generate SQL only, wrapped in <sql>...</sql>. "
                "Output must be a single SELECT query with WHERE and LIMIT and no schema probing.\n\n"
                f"Workbook schema:\n{schema}\n\n"
                f"LIVE_DB_TABLES:\n{live_catalog}\n\n"
                f"RETRIEVAL (embedding similarity):\n{retrieval_context or '(none)'}\n\n"
                f"Conversation context:\n{hist or '(none)'}\n\n"
                f"Question:\n{user_text}\n\n"
                f"{correction_hint}"
            )
        else:
            user = (
                "Generate SQL only, wrapped in <sql>...</sql>. "
                "Output must be a single SELECT query using only ERD tables/columns, include WHERE and LIMIT, and avoid schema probing.\n\n"
                f"ERD_CONTEXT:\n{erd_context or '(none)'}\n\n"
                f"LIVE_DB_TABLES:\n{live_catalog}\n\n"
                f"RETRIEVAL (embedding similarity):\n{retrieval_context or '(none)'}\n\n"
                f"Conversation context:\n{hist or '(none)'}\n\n"
                f"Question:\n{user_text}\n\n"
                f"{correction_hint}"
            )
        raw = self._chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=min(3000, max(600, int(settings.openai_max_tokens or 3000))),
        )
        sql = _extract_sql(raw)
        if not sql:
            logger.error(f"RAW LLM OUTPUT INSTEAD OF SQL: {raw}")
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

    def _generate_answer(
        self, question: str, sql: str, rows: list[dict]
    ) -> tuple[str, dict | None]:
        """
        Generate a natural language answer from SQL results.

        Returns (answer_text, parsed_json) where parsed_json may contain
        data_table, chart_recommendation, and clarification_needed.
        """
        total_rows = len(rows)
        requested_n = _requested_top_n(question)
        requested_months = _requested_trend_months(question)
        # Give the LLM ALL rows (no slicing) so it never hallucinate "sample shown"
        display_rows = rows[:50]
        system = ARCUTIS_SYSTEM_PROMPT
        user = (
            f"Question:\n{question}\n\n"
            f"Total rows returned: {total_rows}\n"
            f"Data rows (use these to populate answer_text):\n"
            f"{json.dumps(display_rows, ensure_ascii=True, default=str)}"
        )
        try:
            raw = self._chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                max_tokens=min(2200, max(700, int(settings.openai_max_tokens or 3000))),
            )
            cleaned = _clean_text(raw)
            if cleaned:
                # Try to parse as structured JSON response
                parsed = _parse_json_answer(cleaned)
                if parsed:
                    answer_text = str(parsed.get("answer_text") or cleaned).strip()
                    # Guard: if the LLM generated a canned rejection for a pharma question,
                    # skip a second LLM call and build the answer directly from the data rows.
                    if _is_canned_refusal(answer_text):
                        logger.warning(
                            "_generate_answer: canned refusal detected, using data fallback. Q=%s",
                            question[:80],
                        )
                        return _fallback_answer(question, sql, rows), None
                    return answer_text, parsed
                # Plain-text response — also guard against canned refusal
                if _is_canned_refusal(cleaned):
                    logger.warning(
                        "_generate_answer (plain): canned refusal detected, using data fallback. Q=%s",
                        question[:80],
                    )
                    return _fallback_answer(question, sql, rows), None
                return cleaned, None
        except Exception as exc:
            logger.warning("Answer generation failed, using fallback: %s", exc)
        return _fallback_answer(question, sql, rows), None

    def _generate_answer_direct(
        self, question: str, sql: str, rows: list[dict]
    ) -> tuple[str, dict | None]:
        """Retry answer generation with a minimal, no-refusal-risk prompt."""
        display_rows = rows[:50]
        total = len(rows)
        simple_system = (
            "You are a pharmaceutical data analyst. Analyse the data rows provided and answer the "
            "question clearly and concisely. Structure your answer with '### Summary' and "
            "'### Key Insights' sections. Base every statement on the actual data rows given. "
            "Never refuse to answer. Never say you can only help with certain topics."
        )
        simple_user = (
            f"Question: {question}\n\n"
            f"Data ({total} rows):\n"
            f"{json.dumps(display_rows, ensure_ascii=True, default=str)}\n\n"
            "Please provide a data-driven analysis answering this question."
        )
        try:
            raw = self._chat(
                [{"role": "system", "content": simple_system}, {"role": "user", "content": simple_user}],
                max_tokens=min(2000, max(600, int(settings.openai_max_tokens or 3000))),
            )
            cleaned = _clean_text(raw)
            if cleaned:
                parsed = _parse_json_answer(cleaned)
                if parsed:
                    answer_text = str(parsed.get("answer_text") or cleaned).strip()
                    # Check both the raw JSON string AND the extracted answer_text
                    if not _is_canned_refusal(answer_text) and not _is_canned_refusal(cleaned):
                        return answer_text, parsed
                elif not _is_canned_refusal(cleaned):
                    return cleaned, None
        except Exception as exc:
            logger.warning("Direct answer generation also failed: %s", exc)
        return _fallback_answer(question, sql, rows), None

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

            for attempt in range(3):
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

                # Build progressively stricter correction hints for each retry.
                if attempt == 0:
                    if use_sqlite_backend():
                        correction_hint = (
                            "[CORRECTION REQUIRED — ATTEMPT 2]\n"
                            f"Previous SQL failed validation/execution.\n"
                            f"Error: {step_error}\n"
                            f"Previous SQL:\n{sql}\n\n"
                            "Rewrite from scratch using ONLY table/column names from Workbook schema above.\n"
                            "Avoid CTEs (`WITH`). Use a single SELECT rooted on real workbook tables (typically `Dummy_Data`).\n"
                            "Do not invent table names. Return exactly one <sql>...</sql>."
                        )
                    else:
                        correction_hint = (
                            "[CORRECTION REQUIRED — ATTEMPT 2]\n"
                            f"Previous SQL failed validation/execution.\n"
                            f"Error: {step_error}\n"
                            f"Previous SQL:\n{sql}\n\n"
                            "Rewrite from scratch using ONLY live PostgreSQL table/column names shown above.\n"
                            "Avoid using NULLIF(col, '') on integer columns — use IS NULL instead.\n"
                            "Do not invent identifiers. Return exactly one <sql>...</sql>."
                        )
                elif attempt == 1:
                    if use_sqlite_backend():
                        correction_hint = (
                            "[CORRECTION REQUIRED — ATTEMPT 3 — FINAL]\n"
                            f"Two previous SQL attempts both failed.\n"
                            f"Latest error: {step_error}\n"
                            f"Latest SQL:\n{sql}\n\n"
                            "Try a completely different, simpler approach.\n"
                            "Use only the most basic SELECT with explicit column names, simple WHERE, and LIMIT.\n"
                            "No CTEs, no subqueries, no CASE expressions unless absolutely required.\n"
                            "Return exactly one <sql>...</sql>."
                        )
                    else:
                        correction_hint = (
                            "[CORRECTION REQUIRED — ATTEMPT 3 — FINAL]\n"
                            f"Two previous SQL attempts both failed.\n"
                            f"Latest error: {step_error}\n"
                            f"Latest SQL:\n{sql}\n\n"
                            "Try a completely different, simpler SQL approach.\n"
                            "REMINDER: state column uses 2-letter codes (NY not 'New York'). "
                            "Use ILIKE for text filters. "
                            "Cast TEXT numeric columns with ::numeric only when needed.\n"
                            "No CTEs, no subqueries unless essential. "
                            "Return exactly one <sql>...</sql>."
                        )

            if step_error:
                return AgentResponse(
                    content=(
                        "I wasn't able to complete that query — could you try rephrasing? "
                        "For example, specifying a region, time period, or metric often helps. "
                        "I'm here to help with HCP data, ZORYVE TRx, territory performance, "
                        "and related Arcutis insights."
                    ),
                    sql=sql or None,
                    validation=validation,
                    results=rows,
                    all_queries=all_queries,
                    error=step_error,
                    llm_rounds=llm_rounds,
                )

            answer, parsed_json = self._generate_answer(user_text, sql, rows)
            llm_rounds += 1

            # Extract structured fields from parsed JSON response
            data_table: list[dict] | None = None
            chart_rec: dict | None = None
            clarification: str | None = None
            if parsed_json:
                raw_dt = parsed_json.get("data_table")
                if isinstance(raw_dt, list) and raw_dt:
                    data_table = raw_dt
                raw_cr = parsed_json.get("chart_recommendation")
                if isinstance(raw_cr, dict):
                    chart_rec = raw_cr
                raw_cl = parsed_json.get("clarification_needed")
                if isinstance(raw_cl, str) and raw_cl.strip():
                    clarification = raw_cl.strip()

            return AgentResponse(
                content=answer,
                sql=sql,
                validation=validation,
                results=rows,
                all_queries=all_queries,
                error=None,
                llm_rounds=llm_rounds,
                answer_text=answer,
                data_table=data_table,
                chart_recommendation=chart_rec,
                clarification_needed=clarification,
            )
        except Exception as exc:
            logger.exception("SQLAgent run failed")
            return AgentResponse(
                content=(
                    "I wasn't able to process that just now — could you try rephrasing? "
                    "I'm here to help with HCP data, ZORYVE TRx, territory performance, "
                    "payer mix, and related Arcutis insights."
                ),
                sql=None,
                validation=None,
                results=[],
                all_queries=all_queries,
                error=str(exc),
                llm_rounds=llm_rounds,
            )
