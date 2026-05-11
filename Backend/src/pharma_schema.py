"""Schema grounding helpers aligned to Arcutis ERD."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Tuple

from langsmith_config import traceable


def _is_workbook_sqlite_mode() -> bool:
    """Workbook mode is default unless explicitly set to postgres."""
    return (os.getenv("SDA_DATA_SOURCE") or "sqlite").strip().lower() != "postgres"


def _is_arcutis_pg_mode() -> bool:
    """Arcutis ERD mode uses normalized relational tables in Postgres."""
    if _is_workbook_sqlite_mode():
        return False
    override = (os.getenv("SDA_ARCUTIS_PG") or "").strip().lower()
    if override in ("1", "true", "yes", "on"):
        return True
    if override in ("0", "false", "no", "off"):
        return False
    return True


_WORKBOOK_TABLES: Tuple[str, ...] = (
    "Dummy_Data",
)

# Single-table dataset — no cross-table relationships.
_WORKBOOK_RELATIONSHIPS: Tuple[Tuple[str, str, str, str], ...] = ()


def erd_markdown_path() -> Path:
    """ERD path: env override -> selected source -> fallback."""
    override = (os.getenv("SDA_ERD_PATH") or os.getenv("ERD_PATH") or "").strip()
    if override:
        p = Path(override).expanduser()
        if not p.is_absolute():
            p = (Path(__file__).resolve().parent.parent / p).resolve()
        return p
    here = Path(__file__).resolve().parent
    arcutis_erd = here / "arcutis_erd_md.md"
    if arcutis_erd.is_file():
        return arcutis_erd
    return here / "Arcutis_ERD.md"


# Flat-table Arcutis: TCS ⊆ Other BNST — hard rules merged into every ERD read (see Query 7).
ARCUTIS_TCS_ENFORCEMENT_MD = """## TCS vs Other BNST — CRITICAL (pharma_schema)

- **Invariant:** For any HCP and period, **TCS TRx MUST NOT exceed Other BNST TRx** (`SUM(tcs_*) ≤ SUM(other_bnst_*)`). If violated in output, rewrite the query/narrative.
- **Presentation:** Always show TCS **inside** Other BNST, e.g. **"Other BNST: X TRx (Y of which are TCS)"** — never as a separate add-on bucket in the headline total.
- **Total prescriptions:** **ZORYVE + Other BNST only** — **never add TCS** to that sum.
- **Every response that mentions TCS and totals** MUST include:

> TCS is already included within Other BNST. Adding it separately would double-count those prescriptions.

Market structure for arcutis_data table:
- ZORYVE = Arcutis branded product columns (zoryve_mmm_yy)
- other_bnst = ALL competitor products grouped (other_bnst_mmm_yy)
- TCS = Topical Corticosteroids (tcs_mmm_yy), largest part of other_bnst
- Total market = ZORYVE + other_bnst (do NOT add TCS separately,
  TCS is already included inside other_bnst)
- ZORYVE market share = zoryve / (zoryve + other_bnst) * 100

SWITCH OPPORTUNITY RULES:
- HCPs with high TCS volume but low ZORYVE share are switch targets
- For switch opportunity queries ALWAYS use zoryve_share_pct < 30
  as the threshold, NEVER < 10
- Reason: average ZORYVE share in dataset is 36.78%,
  so < 30 captures genuinely below-average adopters
- < 10 threshold returns 0 rows and is too restrictive

ZORYVE SHARE THRESHOLDS TO USE:
- Low ZORYVE share = < 30% (use this for switch opportunity queries)
- Medium ZORYVE share = 30% to 60%
- High ZORYVE share = > 60%
"""


ARCUTIS_DECILE_ENFORCEMENT_MD = """## Decile Language Hard Rules — CRITICAL (pharma_schema)

- "best" HCPs → `WHERE decile = 1` (or `WHERE q1_26_decile = 1`)
- "worst" HCPs → `WHERE decile = 10` (or `WHERE q1_26_decile = 10`)
- "ascending/descending" → applies to `ORDER BY` metric only
- NEVER use ascending/descending to determine decile group.
"""


ARCUTIS_METRIC_CALCULATION_GUARDRAILS_MD = """## Call-Aligned Response Metric Rules - CRITICAL (pharma_schema)

- Rule 1: `avg_monthly_zoryve_trx_per_hcp` MUST divide by both `COUNT(npi_id)` and the month count.
- Rule 2: For call-response analyses, calls start at Q2 2025, so ZORYVE TRx MUST use Apr 2025-Mar 2026 only.
- Rule 3: Call-aligned average monthly ZORYVE TRx uses 12 months, never 15 months.
- Rule 4: `inadequate_response_hcps` applies ONLY to the 4-6 call bucket; never include 7+ calls.
- Rule 5: TCS is a subset of Other BNST. Total TRx = ZORYVE + Other BNST ONLY; never add TCS as a third bucket.

Correct call-aligned SQL fragments:

```sql
ROUND(
  SUM(
    COALESCE(zoryve_apr_25,0)+COALESCE(zoryve_may_25,0)+COALESCE(zoryve_jun_25,0)+
    COALESCE(zoryve_jul_25,0)+COALESCE(zoryve_aug_25,0)+COALESCE(zoryve_sep_25,0)+
    COALESCE(zoryve_oct_25,0)+COALESCE(zoryve_nov_25,0)+COALESCE(zoryve_dec_25,0)+
    COALESCE(zoryve_jan_26,0)+COALESCE(zoryve_feb_26,0)+COALESCE(zoryve_mar_26,0)
  ) / NULLIF(COUNT(npi_id), 0) / 12.0,
  2
) AS avg_monthly_zoryve_trx_per_hcp

COUNT(*) FILTER (
  WHERE (
    COALESCE(q2_25_calls,0)+COALESCE(q3_25_calls,0)+
    COALESCE(q4_25_calls,0)+COALESCE(q1_26_calls,0)
  ) BETWEEN 4 AND 6
  AND (
    COALESCE(zoryve_apr_25,0)+COALESCE(zoryve_may_25,0)+COALESCE(zoryve_jun_25,0)+
    COALESCE(zoryve_jul_25,0)+COALESCE(zoryve_aug_25,0)+COALESCE(zoryve_sep_25,0)+
    COALESCE(zoryve_oct_25,0)+COALESCE(zoryve_nov_25,0)+COALESCE(zoryve_dec_25,0)+
    COALESCE(zoryve_jan_26,0)+COALESCE(zoryve_feb_26,0)+COALESCE(zoryve_mar_26,0)
  ) / 12.0 < 5
) AS inadequate_response_hcps
```
"""


_PRE_CALL_ZORYVE_COLS = ("zoryve_jan_25", "zoryve_feb_25", "zoryve_mar_25")
_CALL_ALIGNED_ZORYVE_COLS = (
    "zoryve_apr_25", "zoryve_may_25", "zoryve_jun_25",
    "zoryve_jul_25", "zoryve_aug_25", "zoryve_sep_25",
    "zoryve_oct_25", "zoryve_nov_25", "zoryve_dec_25",
    "zoryve_jan_26", "zoryve_feb_26", "zoryve_mar_26",
)
_CALL_COUNT_COLS = ("q2_25_calls", "q3_25_calls", "q4_25_calls", "q1_26_calls")


def validate_arcutis_metric_sql(sql: str | None) -> list[str]:
    """Return call-response metric guardrail violations for generated Arcutis SQL."""
    if not sql:
        return []
    compact = " ".join(str(sql).lower().split())
    violations: list[str] = []
    has_call_context = any(col in compact for col in _CALL_COUNT_COLS)
    has_zoryve_metric = "zoryve" in compact and (
        "avg_monthly" in compact or "inadequate_response" in compact
    )

    # Fix 1: average monthly ZORYVE response must be per HCP and per aligned month.
    if "avg_monthly_zoryve" in compact:
        if re.search(r"sum\s*\(\s*total_trx_all_hcps\s*\)\s*/\s*15(?:\.0)?", compact):
            violations.append(
                "avg_monthly_zoryve must not use bucket total / 15; use ZORYVE SUM / COUNT(npi_id) / 12.0."
            )
        if not re.search(r"count\s*\(\s*(?:distinct\s+)?npi_id\s*\)", compact):
            violations.append("avg_monthly_zoryve must divide by COUNT(npi_id).")
        if "/ 12.0" not in compact and "/12.0" not in compact:
            violations.append("avg_monthly_zoryve must divide by 12.0 call-aligned months.")
        if "/ 15.0" in compact or "/15.0" in compact:
            violations.append("avg_monthly_zoryve must not divide by 15.0 when calls are involved.")

    # Fix 2: inadequate response must be scoped strictly to the 4-6 call bucket.
    if "inadequate_response" in compact:
        if not re.search(r"between\s+4\s+and\s+6", compact):
            violations.append("inadequate_response_hcps must use total calls BETWEEN 4 AND 6.")
        if re.search(r"(?:>=|>)\s*4", compact) and not re.search(r"between\s+4\s+and\s+6", compact):
            violations.append("inadequate_response_hcps must not include 7+ call HCPs.")

    # Fix 3: call-aligned ZORYVE calculations exclude pre-call Jan-Mar 2025 months.
    if has_call_context and has_zoryve_metric:
        used_pre_call = [col for col in _PRE_CALL_ZORYVE_COLS if col in compact]
        if used_pre_call:
            violations.append(
                "call-aligned ZORYVE TRx must exclude pre-call months: " + ", ".join(used_pre_call)
            )
        missing_aligned = [col for col in _CALL_ALIGNED_ZORYVE_COLS if col not in compact]
        if missing_aligned and "avg_monthly_zoryve" in compact:
            violations.append(
                "avg_monthly_zoryve must include all Apr 2025-Mar 2026 ZORYVE month columns."
            )

    # Fix 4/5: TCS is a subset; do not add TCS to Other BNST in total TRx formulas.
    adds_tcs_to_prior_expr = any(
        "other_bnst" in compact[max(0, m.start() - 400):m.start()]
        for m in re.finditer(r"\+\s*\(?\s*(?:sum\s*\()?\s*(?:coalesce\s*\()?\s*tcs_", compact)
    )
    adds_other_bnst_to_prior_tcs_expr = False
    for m in re.finditer(r"\+\s*\(?\s*(?:sum\s*\()?\s*(?:coalesce\s*\()?\s*other_bnst", compact):
        prior = compact[max(0, m.start() - 400):m.start()]
        tcs_pos = prior.rfind("tcs_")
        # A valid query may select TCS separately and later compute ZORYVE + Other BNST.
        # Only block the reverse additive formula when TCS is the immediate formula context.
        if tcs_pos >= 0 and "zoryve" not in prior[tcs_pos:]:
            adds_other_bnst_to_prior_tcs_expr = True
            break
    if (
        "other_bnst" in compact
        and "tcs_" in compact
        and (adds_tcs_to_prior_expr or adds_other_bnst_to_prior_tcs_expr)
    ):
        violations.append("Total TRx must be ZORYVE + Other BNST only; do not add TCS to Other BNST.")

    return violations


def arcutis_tcs_enforcement_block() -> str:
    """Standalone TCS rules for prompts (same body as merged into ``read_erd_markdown``)."""
    return ARCUTIS_TCS_ENFORCEMENT_MD.strip()


def arcutis_metric_calculation_guardrails_block() -> str:
    """Standalone call-aligned metric rules for prompts and live catalog context."""
    return ARCUTIS_METRIC_CALCULATION_GUARDRAILS_MD.strip()

ARCUTIS_LIMIT_ENFORCEMENT_MD = """
IMPORTANT: Never add LIMIT to SQL unless user says top N, give me N, or limit to N. If user says show all or asks generally, write SQL with NO LIMIT clause.
"""


@traceable(name="SDA | read_erd_markdown", run_type="tool")
def read_erd_markdown(max_chars: int | None = None) -> str:
    """Return ERD markdown for prompt grounding; prepends immutable TCS and Decile rules; empty if missing."""
    p = erd_markdown_path()
    plug = (
        f"{ARCUTIS_TCS_ENFORCEMENT_MD.rstrip()}\n\n"
        f"{ARCUTIS_DECILE_ENFORCEMENT_MD.rstrip()}\n\n"
        f"{ARCUTIS_METRIC_CALCULATION_GUARDRAILS_MD.rstrip()}\n\n"
        f"{ARCUTIS_LIMIT_ENFORCEMENT_MD.strip()}"
    )
    if not p.is_file() or p.stat().st_size == 0:
        return plug if plug else ""
    body = p.read_text(encoding="utf-8", errors="replace").strip()
    text = f"{plug}\n\n{body}" if plug else body
    text = text.strip()
    if max_chars is not None and len(text) > max_chars:
        # Keep tail (main ERD) when truncating: drop from the start after plug if needed.
        if len(plug) < max_chars and text.startswith(plug):
            remainder = max_chars - len(plug) - len("\n\n")
            tail = body[:remainder] + ("\n... [truncated]" if len(body) > remainder else "")
            return f"{plug}\n\n{tail}".strip()
        return text[:max_chars] + "\n... [truncated]"
    return text


def pharma_only_mode() -> bool:
    """Restrict prompts to this schema (``SDA_PHARMA_ONLY`` env flag)."""
    raw = (os.getenv("SDA_PHARMA_ONLY") or os.getenv("SDA_TAKEDA_ONLY") or "true").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _schema_name() -> str:
    for key in ("SDA_PHARMA_SCHEMA", "SDA_TAKEDA_SCHEMA", "PGSCHEMA", "pg_schema"):
        value = (os.getenv(key) or "").strip()
        if value:
            return value
    return "public"


def pharma_db_schema() -> str:
    """Postgres schema holding ERD tables — used for ``search_path`` and qualified names."""
    return _schema_name()


def pharma_qualified_table(table: str) -> str:
    """Return schema-qualified table name for SQL snippets."""
    schema = _schema_name()
    return f'"{schema}"."{table}"'


_ERD_BASE_TABLES: Tuple[str, ...] = (
    "dim_geography",
    "dim_hco",
    "dim_territory",
    "dim_hcp",
    "dim_product_category",
    "dim_quarter",
    "fact_hcp_targeting",
    "fact_rx_monthly",
    "fact_calls",
    "fact_hcp_annual_rx",
    "fact_payer_mix",
)

_ERD_FK_EDGES: Tuple[Tuple[str, str, str, str], ...] = (
    ("dim_hcp", "geo_id", "dim_geography", "geo_id"),
    ("dim_hcp", "hco_id", "dim_hco", "hco_id"),
    ("dim_hcp", "territory_id", "dim_territory", "territory_id"),
    ("fact_hcp_targeting", "hcp_id", "dim_hcp", "hcp_id"),
    ("fact_hcp_targeting", "quarter_id", "dim_quarter", "quarter_id"),
    ("fact_rx_monthly", "hcp_id", "dim_hcp", "hcp_id"),
    ("fact_rx_monthly", "category_id", "dim_product_category", "category_id"),
    ("fact_calls", "hcp_id", "dim_hcp", "hcp_id"),
    ("fact_calls", "quarter_id", "dim_quarter", "quarter_id"),
    ("fact_hcp_annual_rx", "hcp_id", "dim_hcp", "hcp_id"),
    ("fact_payer_mix", "hcp_id", "dim_hcp", "hcp_id"),
)


def get_all_tables() -> List[str]:
    """Tables documented in selected Arcutis ERD."""
    if _is_workbook_sqlite_mode():
        return list(_WORKBOOK_TABLES)
    schema = _schema_name()
    return [f"{schema}.{t}" for t in _ERD_BASE_TABLES]


def pharma_relationships() -> List[Dict[str, str]]:
    """FK join edges from Arcutis ERD (child.column -> parent.column)."""
    if _is_workbook_sqlite_mode():
        return [
            {"left": f"{child}.{ccol}", "right": f"{parent}.{pcol}"}
            for child, ccol, parent, pcol in _WORKBOOK_RELATIONSHIPS
        ]
    schema = _schema_name()

    def fqn(table: str, column: str) -> str:
        return f"{schema}.{table}.{column}"

    return [
        {"left": fqn(child, ccol), "right": fqn(parent, pcol)}
        for child, ccol, parent, pcol in _ERD_FK_EDGES
    ]


def pharma_table_docs() -> List[Dict[str, str]]:
    """Table blurbs for schema RAG and prompt grounding."""
    if _is_workbook_sqlite_mode():
        return [{"id": "Dummy_Data", "table": "Dummy_Data", "text": "Legacy workbook table."}]

    schema = _schema_name()

    def fq(table: str) -> str:
        return f"{schema}.{table}"

    blurbs: Dict[str, str] = {
        "dim_geography": "Normalized city/state/zip location dimension.",
        "dim_hco": "Health care organization master with soft-delete activity flag.",
        "dim_territory": "Territory hierarchy dimension (base_territory, region, area).",
        "dim_hcp": "HCP master with NPI business key and references to geography/HCO/territory.",
        "dim_product_category": "Product category dimension with only ZORYVE and BNST codes.",
        "dim_quarter": "Quarter dimension for standardized quarter references.",
        "fact_hcp_targeting": "Quarter-level HCP targeting fact (decile and target flag).",
        "fact_rx_monthly": "Monthly TRx fact by HCP, product category, and subtype.",
        "fact_calls": "Quarterly call count fact by HCP.",
        "fact_hcp_annual_rx": "Annual TRx fact with payer channel totals and percentages.",
        "fact_payer_mix": "Payer-level annual value fact by HCP.",
    }

    docs: List[Dict[str, str]] = []
    for table in _ERD_BASE_TABLES:
        tq = fq(table)
        text = blurbs.get(table, f"Table {tq} — see arcutis_erd_md.md for full column reference.")
        docs.append({"id": tq, "table": tq, "text": f"Table: {tq} — {text}"})
    return docs


def pharma_docs_and_relationships() -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """Table blurbs + FK edges for schema RAG. Aligned to ERD.md (Synthea Enhanced Schema)."""
    return pharma_table_docs(), pharma_relationships()
