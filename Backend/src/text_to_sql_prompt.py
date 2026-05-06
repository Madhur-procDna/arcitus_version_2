"""Text-to-SQL prompt builders: ERD-grounded PostgreSQL with optional schema RAG.

Combines high-signal domain rules (commercial joins, periods, call metrics, NULL handling)
with RETRIEVAL + ERD_CONTEXT caps so prompts stay within model context limits.

When ``ERD.md`` describes the **Synthea** schema (``patients``, ``encounters``, ``claims``, …),
prompts also carry Synthea-specific rules (quoted identifiers, encounter vs claim costs, anti–Cartesian-join).

Includes a **Supplementary analyst rules** block (ratios, ``rep_activity`` vs ``call_plan`` CTEs,
anti-MAX averages, time windows, GROUP BY discipline), using ``pharma_schema.pharma_qualified_table``
for schema-qualified example names. Legacy Takeda tables (``patient``, ``drug``, ``hcp``, etc.) rules
still apply when those objects appear in context.
"""

from __future__ import annotations

import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


MAX_QUERY_CHARS = int(os.getenv("TEXT2SQL_MAX_QUERY_CHARS", "8000"))
MAX_SCHEMA_CHARS = int(os.getenv("TEXT2SQL_MAX_SCHEMA_CHARS", "120000"))
MAX_CONV_CHARS = int(os.getenv("TEXT2SQL_MAX_CONV_CHARS", "24000"))
MAX_RETRIEVAL_CHARS = int(os.getenv("SDA_SCHEMA_RAG_PROMPT_CAP", "32000"))
# Full ERD.md in the user message — safety valve only (raise cap if legitimate ERD growth).
MAX_ERD_CHARS = int(os.getenv("TEXT2SQL_MAX_ERD_CHARS", "250000"))
# Default LIMIT clause the model is asked to add when the pipeline does not want unlimited rows.
TEXT2SQL_LLM_LIMIT_HINT = int(os.getenv("TEXT2SQL_LLM_LIMIT_HINT", "200"))
# Default ERD next to this package (`src/ERD.md`). Override per-call or via `pharma_schema.erd_markdown_path()`.
ERD_PATH_DEFAULT = Path(__file__).resolve().parent / "ERD.md"

_ROLE_INJECTION_RE = re.compile(
    r"(?im)^\s*(system|assistant|user)\s*:\s*|\[\s*INST\s*\]|<\|im_start\|>",
)


@dataclass(frozen=True)
class PromptParts:
    system: str
    user: str


def _load_erd(erd_path: Path) -> str:
    if not erd_path.exists():
        raise FileNotFoundError(f"ERD not found at {erd_path}")
    return erd_path.read_text(encoding="utf-8")


def _sanitise_user_query(query: str) -> str:
    if not isinstance(query, str):
        raise ValueError("user_query must be a string.")
    cleaned = "".join(
        ch for ch in query
        if ch in ("\n", "\t") or unicodedata.category(ch) not in ("Cc", "Cs")
    )
    if _ROLE_INJECTION_RE.search(cleaned):
        raise ValueError("Disallowed role-injection pattern detected.")
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if not cleaned:
        raise ValueError("Sanitised query is empty.")
    if len(cleaned) > MAX_QUERY_CHARS:
        raise ValueError(
            f"Query exceeds max length of {MAX_QUERY_CHARS} chars (got {len(cleaned)})."
        )
    return cleaned


def _cap(text: Optional[str], max_chars: int, label: str) -> Optional[str]:
    if text is None:
        return None
    if len(text) > max_chars:
        print(f"[PromptBuilder] WARNING: {label} truncated.", file=sys.stderr)
        return text[:max_chars] + "\n... [truncated]"
    return text


def _qualified_table(table: str) -> str:
    """Schema-qualified name for prompt examples (matches live search_path)."""
    try:
        from pharma_schema import pharma_qualified_table

        return pharma_qualified_table(table)
    except Exception:
        return f"public.{table}"


def _build_prompt(
    user_query: str,
    *,
    db_dialect: str,
    output_format: str,
    max_rows: Optional[int],
    physical_schema_context: Optional[str],
    erd_text: str,
    conversation_context: Optional[str],
    known_period_types: Optional[List[str]],
    anchor_sql: Optional[str] = None,
    retrieval_context: Optional[str] = None,
    live_db_tables_context: Optional[str] = None,
) -> PromptParts:
    r_table = _qualified_table("rep_activity")
    h_table = _qualified_table("call_plan")
    syn_e = _qualified_table("encounters")
    syn_p = _qualified_table("patients")
    syn_c = _qualified_table("claims")
    syn_ct = _qualified_table("claims_transactions")

    period_hint = ""
    if known_period_types:
        period_hint = (
            f"\n- **period_type** (when that column appears in ERD_CONTEXT): known values: "
            f"{', '.join(known_period_types)} — filter with **`IN (...)`**, not **`LIKE`**, for exact matches."
        )

    anchor_block = ""
    if anchor_sql and anchor_sql.strip():
        anchor_block = f"""
PREVIOUS SQL CONTEXT:
- The user may be asking a follow-up to the prior SQL below.
- Preserve the prior metric and table intent unless the new question clearly asks for a different one.
- Adjust filters, grouping, ordering, or joins only when the new question requires it.

```sql
{anchor_sql.strip()}
```
"""

    limit_instruction = (
        "- **Row caps (product rule):** Do not add a trailing LIMIT, OFFSET, or FETCH FIRST ... ONLY to implement "
        '"top N", "first N", "which N", or "show 20 ..." -- use ORDER BY (and window functions if needed) so the database '
        "returns the full ordered result set (the app controls preview length and CSV download). Only add a row cap when "
        'the user clearly asks for a SQL-level cap for sampling (e.g. "random 5 rows").'
        if max_rows is None
        else f"- Add LIMIT {max_rows} unless the user explicitly asks for all rows or a different limit."
    )

    sda_playbook = f"""
### Supplementary analyst rules (commercial & analytics — apply when relevant tables/columns appear in RETRIEVAL/ERD_CONTEXT)

**Ratio & shares:** For "% of total" use window framing, e.g. `SUM(metric) / NULLIF(SUM(SUM(metric)) OVER (), 0)`. For ratios, avoid **`WHERE`** clauses that silently drop denominator mass before aggregating; prefer **`CASE`** inside **`SUM`**. Wrap divisions with **`NULLIF(denominator, 0)`**. When rounding percentages or ratios, cast to **`numeric`** before **`ROUND`**, e.g. **`ROUND((100.0 * SUM(x) / NULLIF(SUM(SUM(x)) OVER (), 0))::numeric, 2)`**.

**`{r_table}` vs `{h_table}`:** Different grains. **`{h_table}`** is quarterly **planned_visits** per **sales_rep_id**, **hcp_id**, **plan_year**, **plan_quarter**. **`{r_table}`** is dated **activity_type** rows (VISIT, CALL, EMAIL, SAMPLE_DROP). For planned vs actual, align on **rep + HCP + calendar year/quarter** from **`{r_table}.activity_date`**, e.g. **`EXTRACT(YEAR FROM ra.activity_date) = cp.plan_year`** and **`EXTRACT(QUARTER FROM ra.activity_date) = cp.plan_quarter`**, plus **`ra.sales_rep_id = cp.sales_rep_id`** and **`ra.hcp_id = cp.hcp_id`**. Do **not** invent join keys unless they appear in ERD_CONTEXT.

**Call / email volumes on `{r_table}`:** Count rows or use **`COUNT(*) FILTER (WHERE activity_type = 'CALL')`** (or **EMAIL** / **VISIT**) per ERD_CONTEXT columns — do **not** assume legacy call-count column names.

**Planned vs actual:** Planned → **`{h_table}.planned_visits`**; actual → counts from **`{r_table}`** grouped at the same rep–HCP–quarter grain.

**NULL handling & determinism:** Use **`COALESCE(col, 0)`** inside **`SUM`/`AVG`** for additive numerics when appropriate — not for non-additive ratios. Avoid **`COALESCE` on dimensions inside `GROUP BY`** unless the user wants NULL labels merged. For rankings, consider **`WHERE rank_metric IS NOT NULL`**. Add **stable tie-break columns** in **`ORDER BY`** (e.g. **`hcp_id`**, **`patient_id`**, primary keys).

**"Above average" / vs peers:** Do **not** answer with **`MAX()`** alone; compare to **`(SELECT AVG(...))`** or **`AVG(...) OVER (...)`**. **Typed `COALESCE`:** branch types must match the column (numeric vs text); cast consistently (e.g. **`::text`**) when mixing.

**Percentiles / ordered-set aggregates (`PERCENTILE_CONT`, `PERCENTILE_DISC`, `MODE`):** Postgres **forbids nesting** another aggregate inside these (e.g. **`PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY SUM(x))`** is invalid). First **`GROUP BY`** entity (e.g. **patient_id**) in a **CTE** to get **`SUM(x) AS per_entity_total`**, then in an outer query use **`PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY per_entity_total)`** on that CTE, or **`WHERE per_entity_total >= (SELECT ... FROM agg_cte)`**.

**Time semantics on `{r_table}`:** Anchor relative windows to **`MAX(activity_date)`** on **`{r_table}`** (or explicit dates the user gives).{period_hint}

**Aggregation order:** Compute metrics at the **lowest grain** first (e.g. HCP × quarter), then windows/trends, **then** roll up to **region** via **`hcp.region_id`** or **`sales_rep.region_id`** as documented.

**Geography / region:** **`region.region_name`** — join **`hcp`** on **`hcp.region_id`**, or **`sales_rep`** on **`sales_rep.region_id`**, or **`market_share`** on **`market_share.region_id`**. **`drug_sale`** has **no** **`region_id`**; attribute revenue to region via **`drug_sale` → `hcp` → `region`**.

**Schema discipline:** Use only objects in **RETRIEVAL / SCHEMA_CONTEXT / ERD_CONTEXT** — do not reference outside schemas or invented tables.
"""

    live_guidance = ""
    if live_db_tables_context and live_db_tables_context.strip():
        live_guidance = (
            "\nWhen **LIVE_DB_TABLES** appears below, every **FROM** / **JOIN** must use a table name from that list "
            "(the live database). Use **ERD_CONTEXT** for columns and join logic when names align; if no listed table "
            "can answer the question, return exactly `-- ERROR:` with a short reason.\n"
        )

    system_prompt = f"""You are a meticulous Text-to-SQL expert.
You must ONLY use the provided ERD to understand tables, columns, keys, and relationships.
Never invent tables or columns. If the question cannot be answered with the ERD, say so.
{live_guidance}
Follow these rules:
- Use only columns and entities that exist in the ERD.
- Join tables exactly as described in the ERD relationships.
- Respect grain notes to avoid double counting.
- Prefer explicit column lists (no SELECT *).
- When using JOINs or CTEs, always qualify columns with table aliases to avoid ambiguity.
- Postgres is case-sensitive for quoted identifiers. If a table or column name uses uppercase or mixed case in the ERD,
  you MUST double-quote it in SQL.
- If names are all lowercase in the ERD, do not quote them.
- If SCHEMA_CONTEXT or PHYSICAL_SCHEMA is provided, use those table/column names exactly as shown,
  including required quotes and schema qualifiers.
- **`call_plan`** exists only when that name appears in RETRIEVAL / SCHEMA_CONTEXT / ERD_CONTEXT; join to **`rep_activity`**
  on **sales_rep_id**, **hcp_id**, and calendar **year** / **quarter** derived from **`activity_date`** vs **plan_year** / **plan_quarter**.
- When the user says **"state"** or **"states"**, prefer **`region.region_name`**: join facts to **`region`** via **`hcp.region_id`**,
  **`sales_rep.region_id`**, or **`market_share.region_id`** as documented.
- For relative windows on rep activity, anchor to **`MAX(activity_date)`** on **`{r_table}`** unless the user names explicit dates.
- For call-type counts, filter **`{r_table}.activity_type`** (e.g. CALL, EMAIL, VISIT) per ERD_CONTEXT.
- For averages in final outputs, round to 0 decimals and cast to integer, e.g.
  `CAST(ROUND(AVG(COALESCE(col, 0))::numeric, 0) AS INTEGER)` to avoid long decimals.
- **PostgreSQL `ROUND(x, n)`:** only accepts **`numeric`** for `x`. **`SUM`/`AVG` on FLOAT/DOUBLE columns yield `double precision`**, which has **no** `round(double precision, integer)` overload. Always cast first, e.g. **`ROUND((expr)::numeric, 2)`** or **`ROUND(expr::numeric, 0)`** — never `ROUND(expr, 2)` on raw float aggregates.
- For planned vs actual call comparisons (when both tables are in context):
  - Planned = **`call_plan.planned_visits`** by rep, HCP, year, quarter.
  - Actual = counts from **`rep_activity`** grouped to the same rep–HCP–quarter using **`activity_date`**.
- Do NOT add a LIMIT clause unless the user explicitly asks for a limit/top/bottom N.
- Null handling rules:
  - For aggregations (SUM, AVG), use COALESCE(column, 0) unless the metric is not additive.
  - For top/bottom rankings, exclude rows where the ranking metric is NULL.
- For rankings of entities (e.g., HCPs, territories, reps), exclude rows where the entity identifier/name is NULL.
- For filters on text/category columns, exclude NULLs unless the user explicitly asks to include them.
- If the output includes outlet state or city **and** an outlet table with those columns appears in context,
  exclude NULLs for those fields using the real column names from ERD_CONTEXT (do not assume `outlet_dim`).

Context in the user message (use together with the ERD):
- When **LIVE_DB_TABLES** is present, it **overrides ERD table names** for **FROM** / **JOIN** targets (the server must recognize every relation you use).
- When **RETRIEVAL** is present, treat it as the **primary** hint for which tables to use and how they join; use **ERD_CONTEXT**
  (and **SCHEMA_CONTEXT** when present) for column-level detail, keys, and grain.
- Use only tables and columns that appear in RETRIEVAL, SCHEMA_CONTEXT, ERD_CONTEXT, and **LIVE_DB_TABLES** (when provided).
- Produce one safe, executable, read-only **{db_dialect}** query.

Additional ERD-grounded rules (when those objects appear in context):

- **Drug catalog join path (Oncology / therapy area queries):**
  `drug` → JOIN `molecule` ON drug.molecule_id = molecule.molecule_id
  → JOIN `therapy_area` ON molecule.therapy_area_id = therapy_area.therapy_area_id
  → JOIN `manufacturer` ON drug.manufacturer_id = manufacturer.manufacturer_id
  Key columns: `drug.drug_name`, `drug.brand_name`, `manufacturer.manufacturer_name`, `therapy_area.therapy_name`.
  Filter example: `WHERE therapy_area.therapy_name = 'Oncology'`

- **Share / % of total revenue by therapy area (pie chart):** In a **CTE**, `GROUP BY therapy_area.therapy_name` and `SUM(ds.revenue) AS rev` from `drug_sale` → `drug` → `molecule` → `therapy_area`. Outer select: `100.0 * rev / NULLIF(SUM(rev) OVER (), 0) AS pct_of_total`. If you **`ROUND`** the percentage, use **`ROUND((100.0 * rev / NULLIF(SUM(rev) OVER (), 0))::numeric, 2)`** — never `ROUND(<double precision expr>, 2)` without **`::numeric`**.

- **YoY monthly line chart (two series):** Prefer **either** (a) **wide:** `month`, `SUM(CASE WHEN year=2024...) AS revenue_2024`, `SUM(CASE WHEN year=2025...) AS revenue_2025` grouped by month, **or** (b) **long:** aliases exactly **`year`**, **`month`** (or `calendar_month`), and **`revenue`** (or `total_revenue`) so the UI can pivot to two lines. Avoid only **`ORDER BY year, month`** as the sole shape without **`year` + `month` columns** in the SELECT list.

- **Drug / molecule / adverse events:** **`adverse_event`** has **adverse_event_id**, **patient_id**, **drug_id**, **event_date**, **reaction**, **severity**; **`drug`** has **drug_id**, **drug_name**, **brand_name**, **molecule_id**, **manufacturer_id**; **`molecule`** has **molecule_id**, **molecule_name**, **therapy_area_id**. Join **`adverse_event ae` → `drug d` ON ae.drug_id = d.drug_id** and optionally **`molecule m` ON d.molecule_id = m.molecule_id**. Do **not** join **`molecule`** on **`ae.drug_id`**. Only select columns that appear in ERD_CONTEXT or LIVE_DB_TABLES.

- **Patient / admission:** `patient` has **patient_id**, **gender**, **age**, **date_of_birth**. `admission` has **admission_id**, **patient_id** (FK), **admit_time**, **discharge_time**, **admission_type**, **is_hospital_death**. Join: `admission.patient_id = patient.patient_id`. For "patients with more than one admission": `GROUP BY patient_id HAVING COUNT(DISTINCT admission_id) > 1`.
- **High-cost cohorts / percentiles on claims:** Compute **`SUM(claim.total_cost)` per `patient_id`** in a **CTE** first; **`PERCENTILE_CONT`** / thresholds apply to **that CTE’s column**, never **`ORDER BY SUM(...)`** inside **`PERCENTILE_CONT`** (nested aggregates are invalid). Then join **`patient`**, filter with **`EXISTS` to `adverse_event`** when needed.

- **Severity / risk:** **`adverse_event.severity`** is VARCHAR — use **`CASE`** for numeric aggregates, not raw **`CAST`** to integer.

- **Journeys / care paths:** Chain **`patient` → `admission` (patient_id) → `prescription` / `diagnosis` / `adverse_event`** using only FK columns in ERD_CONTEXT. Do **not** return **`-- ERROR`** only because the question is broad.

- **Drug × region:** Use **`market_share`** (**drug_id**, **region_id**) or **`drug_sale` → `hcp` → `region`** per ERD. **Every** alias in **SELECT** must appear in **FROM** / **JOIN**.

- **Commercial / `rep_activity`:** Use **activity_date**, **activity_type**, **sales_rep_id**, **hcp_id**, **promotional_material_id** (nullable FK) as in ERD_CONTEXT. Join to `promotional_material` ON rep_activity.promotional_material_id = promotional_material.promotional_material_id when material details are needed.

- **Territory drill-down:** `territory` → `region` → (`hcp` or `sales_rep` or `market_share` or `channel_performance`). Join: `region.territory_id = territory.territory_id`. Use `territory.territory_name` for territory-level grouping, `region.region_name` for state-level.

- **Drug interactions:** `drug_interaction` has **drug_id** and **interacting_drug_id** (both FK → drug.drug_id), **interaction_type**, **severity**, **mechanism**, **clinical_effect**. Alias both drug joins: `JOIN drug d1 ON di.drug_id = d1.drug_id JOIN drug d2 ON di.interacting_drug_id = d2.drug_id`.

- **Clinical trials:** `clinical_trial` has **drug_id** FK, **trial_phase**, **trial_status**, **trial_result**, **enrollment_target**, **enrolled_count**, **nct_number**, **start_date**, **end_date**. Join to `drug` for drug name. Filter by trial_phase or trial_status as needed.

- **Drug shortages:** `drug_shortage` has **drug_id** FK, **shortage_start_date**, **shortage_end_date** (NULL = ongoing), **reason**, **severity_level**, **fda_shortage_status**. For ongoing shortages: `WHERE shortage_end_date IS NULL`.

- **Drug substitutions:** `drug_substitution` has **brand_drug_id** FK and **substitute_drug_id** FK (both → drug.drug_id), **substitution_type** (GENERIC/BIOSIMILAR/THERAPEUTIC_EQUIVALENT), **substitution_allowed**. Alias both drug joins separately.

- **Rep performance:** `rep_performance` has **sales_rep_id** FK, **performance_year**, **performance_quarter**, **sales_attainment_pct**, **call_coverage_pct**, **hcp_engagement_score**, **overall_score**, **rank_in_territory**. Join to `sales_rep` for rep name.

- **HCP segment history:** `hcp_segment_history` has **hcp_id** FK, **old_segment**, **new_segment**, **old_priority**, **new_priority**, **change_date**, **change_reason**. Join to `hcp` and `provider` for HCP name and specialty.

- **Step therapy:** `step_therapy` has **target_drug_id** FK, **required_drug_id** FK (both → drug.drug_id), **payer_id** FK, **step_number**, **min_duration_days**, **failure_criteria**. Always alias both drug joins: `JOIN drug td ON st.target_drug_id = td.drug_id JOIN drug rd ON st.required_drug_id = rd.drug_id`.

- **Specialty pharmacy:** `specialty_pharmacy` has **drug_id** FK, **pharmacy_name**, **pharmacy_type** (EXCLUSIVE/PREFERRED/OPEN_NETWORK), **distribution_channel**, **is_hub_pharmacy**, **enrollment_required**.

- **Patient assistance (PAP):** `patient_assistance` has **drug_id** FK, **patient_id** FK, **program_name**, **eligibility_status** (ENROLLED/PENDING/DENIED/EXPIRED), **income_threshold_pct**, **copay_assistance_amount**, **enrollment_date**, **expiry_date**.

- **Formulary tier changes:** `payer_formulary_tier_change` has **formulary_id** FK, **drug_id** FK, **payer_id** FK, **old_tier**, **new_tier**, **old_status**, **new_status**, **change_date**, **change_reason**, **is_manufacturer_negotiated**.

- **Competitive intelligence:** `competitive_intelligence` has **our_drug_id** FK and **competitor_drug_id** FK (both → drug.drug_id), **competitor_market_share_pct**, **competitor_trx_index**, **share_of_voice_pct**, **competitor_sov_pct**, **new_indication_flag**, **price_change_flag**, **measurement_date**. Alias both drug joins: `JOIN drug od ON ci.our_drug_id = od.drug_id JOIN drug cd ON ci.competitor_drug_id = cd.drug_id`.

- **Brand tracker:** `brand_tracker` has **drug_id** FK, **measurement_date**, **aided_awareness_pct**, **unaided_awareness_pct**, **consideration_pct**, **preference_pct**, **nps_score**, **share_of_voice_pct**, **hcp_survey_sample_size**. Join to `drug` for drug/brand name.

- **Channel performance:** `channel_performance` has **drug_id** FK, **region_id** FK, **channel** (RETAIL/SPECIALTY_PHARMACY/HOSPITAL/CLINIC/MAIL_ORDER/DIRECT), **period_date**, **units_sold**, **revenue**, **trx_count**, **channel_share_pct**. Join to `drug` and `region` for labels.

- **LIVE_DB_TABLES overrides ERD:** When **LIVE_DB_TABLES** lists actual column names for a table, those names are authoritative. Do not assume ERD column names if the live list differs.

- **Synthea Enhanced** (when **`patients`**, **`encounters`**, **`claims`**, **`claims_transactions`** appear in context):
  - **Quoted identifiers:** Use **`{syn_p}."Id"`**, **`{syn_e}."PATIENT"`**, **`{syn_e}."Id"`**, **`{syn_c}."ENCOUNTER_ID"`**, **`{syn_ct}."CLAIMID"`**, **`{syn_ct}."PAYMENTS"`** — every mixed-case column from the ERD must stay double-quoted.
  - **Billed total per visit** lives on **`{syn_e}."TOTAL_CLAIM_COST"`**, not on **`{syn_c}`**. Join **`{syn_c} c`** → **`{syn_e} e`** on **`c."ENCOUNTER_ID" = e."Id"`** when comparing payments to billed charges.
  - **Latest lab per patient (e.g. HbA1c for diabetics):** In **`observations`**, HbA1c is usually **`"CODE" = '4548-4'`**; use **`"DATE"`** and **`"VALUE"`** (cast **`"VALUE"::numeric`** when needed). One row per patient: **`DISTINCT ON (o."PATIENT") … ORDER BY o."PATIENT", o."DATE" DESC NULLS LAST`**, or a **`ROW_NUMBER() OVER (PARTITION BY o."PATIENT" ORDER BY o."DATE" DESC NULLS LAST)`** CTE. Restrict to diabetic patients with **`EXISTS` on `conditions`** (SNOMED for diabetes, e.g. type 2 **44054006**, from ERD). **Balance every `(`** — incomplete SQL is rejected.
  - **“One row per encounter” + condition + medication + …:** avoid a single SELECT with many **`LEFT JOIN`** one-to-many clinical tables (row explosion → **statement_timeout**). Prefer per-fact **`STRING_AGG`** subqueries grouped by **`"ENCOUNTER"`**, **`LATERAL … LIMIT 1`**, or **scalar subqueries** — **never** chain unconditional **`LEFT JOIN conditions`** + **`LEFT JOIN medications`** (+ other clinical facts) on **`encounters`** for this shape.
  - **MANDATORY reference shape** when the user wants **exactly one** condition description and **one** medication description **per encounter row** (plus provider, payer from encounter, `TOTAL_CLAIM_COST`): use the pattern below — **copy this join grain** (`FROM encounters` + join to `patients`, `providers`, `payers` only; facts via subselect):
```sql
SELECT
  p."Id" AS patient_id,
  p."LAST" AS patient_last_name,
  e."START" AS encounter_start,
  (SELECT c."DESCRIPTION" FROM conditions c
     WHERE c."ENCOUNTER" = e."Id" ORDER BY c."START" ASC NULLS LAST LIMIT 1) AS one_condition_description,
  (SELECT m."DESCRIPTION" FROM medications m
     WHERE m."ENCOUNTER" = e."Id" ORDER BY m."START" ASC NULLS LAST LIMIT 1) AS one_medication_description,
  pr."NAME" AS provider_name,
  py."NAME" AS payer_name,
  e."TOTAL_CLAIM_COST"
FROM encounters e
JOIN patients p ON p."Id" = e."PATIENT"
JOIN providers pr ON pr."Id" = e."PROVIDER"
JOIN payers py ON py."Id" = e."PAYER"
```
  - **“Payments exceed claim / encounter cost”:** aggregate **`{syn_ct}`** with **`SUM("PAYMENTS")`** grouped by **`"CLAIMID"`**, join to **`{syn_c}`**, then optionally to **`{syn_e}`** to compare against **`e."TOTAL_CLAIM_COST"`** (or compare payments to **`SUM("AMOUNT")`** for charge lines only if the user defines it that way).

{sda_playbook}
{limit_instruction}
- Prefer explicit joins on documented foreign-key columns when relationships are available.
- If a question can be answered from one table, do not add unnecessary joins.
- For counts of business entities, prefer the table at the natural grain requested by the user.
- Use clear aliases when joining multiple tables.
- **SQL alias / FROM scope (avoids "missing FROM-clause entry" errors):** In *each* `SELECT`, every `alias.column` (in `SELECT`, `WHERE`, `HAVING`, `ORDER BY` at that level) must use an `alias` that appears in *that same* `SELECT`'s `FROM` / `JOIN` **or** is a lateral/correlated column from an outer row. A `WITH` cte is only available after you write `FROM cte_name AS alias` (or `JOIN … AS alias`) in the `SELECT` that uses it. **Never** list `x.col` in the final `SELECT` if `x` is only an alias *inside* a subquery; join or re-alias the CTE in the outer query. Pick names that do not shadow each other.
- If aggregation is used, ensure every non-aggregated selected column is in GROUP BY.
- Return ONLY raw SQL (no markdown fences, no preamble).
- If the request cannot be answered from the provided schema, return exactly: -- ERROR: <short reason>

{anchor_block}
"""

    rag = _cap(retrieval_context, MAX_RETRIEVAL_CHARS, "retrieval_context")
    live_cap = _cap(live_db_tables_context, MAX_SCHEMA_CHARS, "live_db_tables_context")
    schema_blocks: List[str] = []
    if rag:
        schema_blocks.append(f"RETRIEVAL (embedding similarity — candidate tables & join paths):\n{rag}")
    # Live catalog before ERD so the model anchors on real relation names (ERD.md can be long).
    if live_cap:
        schema_blocks.append(f"LIVE_DB_TABLES:\n{live_cap}")
    if physical_schema_context:
        schema_blocks.append(f"SCHEMA_CONTEXT:\n{physical_schema_context}")
    if erd_text:
        schema_blocks.append(f"ERD_CONTEXT:\n{erd_text}")
    if not schema_blocks:
        schema_blocks.append("ERD_CONTEXT:\n(No ERD text loaded.)")
    schema_joined = "\n\n".join(schema_blocks)
    conv_block = f"CONVERSATION HISTORY:\n{conversation_context}\n" if conversation_context else ""

    user_prompt = f"""{conv_block}{schema_joined}

USER QUESTION:
{user_query}

Execution checklist:
1. Identify the smallest correct set of tables needed. When **LIVE_DB_TABLES** is present, **every** relation in FROM / JOIN must be a name from that list — never invent a table name.
2. When **LIVE_DB_TABLES** includes `table(col1, col2, ...)` column detail, use **only those column names** — they are authoritative and override ERD.
3. Use FK-based joins where possible.
4. Match the requested grain before aggregating.
5. Keep the SQL read-only and compatible with **{db_dialect}** (use that engine’s functions and types).
6. **Drug + therapy area + manufacturer queries:** Join `drug → molecule → therapy_area` and `drug → manufacturer`. Use `therapy_area.therapy_name` for filtering (not `therapy_area_name`). Use `drug.brand_name` and `manufacturer.manufacturer_name`.
7. **Admission / patient count queries:** Join `admission.patient_id = patient.patient_id`. For "more than one admission" use `GROUP BY patient_id HAVING COUNT(DISTINCT admission_id) > 1`.
8. For "journey" or clinical linkage questions, chain **`patient` → `admission` → clinical facts** (e.g. **`prescription`**, **`adverse_event`**) → **`drug`** / **`molecule`** using only FK columns in ERD_CONTEXT.
9. For **email / call** metrics by geography, use **`rep_activity`** with **`activity_type`** and join **`hcp` → `region`** (or **`sales_rep` → `region`**) unless ERD_CONTEXT documents other columns.
10. For **planned vs actual** calls, join **`call_plan`** to **`rep_activity`** on **sales_rep_id**, **hcp_id**, and **year/quarter** from **`activity_date`** vs **plan_year** / **plan_quarter** when both appear in ERD_CONTEXT.
11. If the schema does not support the request, emit the -- ERROR marker.
12. For ranking / \"top N\" / \"which N\" questions, prefer **no** trailing row **`LIMIT`** — order correctly and return all ranked rows, not only N.
13. When **both** **`{h_table}`** and **`{r_table}`** are required, aggregate each at **rep × HCP × year × quarter** in CTEs, then join on **sales_rep_id**, **hcp_id**, **plan_year** / **plan_quarter** vs **`EXTRACT` from `activity_date`**; for **% of plan**, **LEFT JOIN** plan CTE to activity CTE (not the reverse).
14. **CTEs and outer SELECT:** The final `SELECT` must `FROM` / `JOIN` every CTE (or subquery) whose columns it references, using a stable alias. Example: `WITH t AS (…) SELECT t.id FROM t` — not `SELECT t.id FROM other_table` unless `t` is joined. Same rule for Synthea double-quoted columns.

SQL:"""

    return PromptParts(system=system_prompt, user=user_prompt)


def _build_workbook_sqlite_prompt(
    user_query: str,
    *,
    db_dialect: str,
    output_format: str,
    max_rows: Optional[int],
    physical_schema_context: Optional[str],
    conversation_context: Optional[str],
    anchor_sql: Optional[str] = None,
    retrieval_context: Optional[str] = None,
    live_db_tables_context: Optional[str] = None,
) -> PromptParts:
    """SQLite over loaded Excel/CSV — no ``ERD.md`` / ``pharma_schema`` context."""
    limit_instruction = (
        "- **Row caps:** Do not add a trailing LIMIT, OFFSET, or FETCH FIRST ... ONLY to implement "
        '"top N", "first N", "which N", or "show 20 ..." — use ORDER BY (and window functions if needed) so the database '
        "returns the full ordered result set (the app controls preview length). Only add a row cap when "
        'the user clearly asks for a SQL-level cap for sampling (e.g. "random 5 rows").'
        if max_rows is None
        else f"- Add LIMIT {max_rows} unless the user explicitly asks for all rows or a different limit."
    )
    anchor_block = ""
    if anchor_sql and anchor_sql.strip():
        anchor_block = f"""
PREVIOUS SQL CONTEXT:
- The user may be asking a follow-up to the prior SQL below.
- Preserve the prior metric and table intent unless the new question clearly asks for a different one.
- Adjust filters, grouping, ordering, or joins only when the new question requires it.

```sql
{anchor_sql.strip()}
```
"""

    system_prompt = f"""You are a meticulous Text-to-SQL assistant for **{db_dialect}** against a **loaded workbook**
(tabs from Excel/CSV are SQLite tables). This deployment does **not** use a separate enterprise ERD file.

Rules:
- Use **only** tables and columns that appear in **LIVE_DB_TABLES** and optional **RETRIEVAL** (same loaded file).
- Do not invent table or column names from other schemas or generic examples.
- Prefer explicit column lists (no **SELECT ***).
- Qualify columns with table aliases when you use **JOIN**s.
- Read-only: output a single **SELECT** or **WITH … SELECT** statement.
- **SQLite:** double-quote identifiers that are reserved words or need quoting; otherwise use names exactly as listed.
- If the request cannot be answered from the provided catalog, return exactly: `-- ERROR:` followed by a short reason.

{limit_instruction}
- Return ONLY raw SQL (no markdown fences, no preamble).
{anchor_block}
"""

    rag = _cap(retrieval_context, MAX_RETRIEVAL_CHARS, "retrieval_context")
    live_cap = _cap(live_db_tables_context, MAX_SCHEMA_CHARS, "live_db_tables_context")
    phys = _cap(physical_schema_context, MAX_SCHEMA_CHARS, "physical_schema_context")
    conv_block = f"CONVERSATION HISTORY:\n{conversation_context}\n" if conversation_context else ""

    schema_blocks: List[str] = []
    if rag:
        schema_blocks.append(f"RETRIEVAL (embedding similarity over loaded tables):\n{rag}")
    if live_cap:
        schema_blocks.append(f"LIVE_DB_TABLES:\n{live_cap}")
    if phys and str(phys).strip():
        schema_blocks.append(f"SCHEMA_CONTEXT:\n{phys}")
    if not schema_blocks:
        schema_blocks.append(
            "LIVE_DB_TABLES:\n(No catalog in this request — every **FROM** / **JOIN** target must still "
            "exist in the database the server has loaded.)"
        )
    schema_joined = "\n\n".join(schema_blocks)

    user_prompt = f"""{conv_block}{schema_joined}

USER QUESTION:
{user_query}

Execution checklist:
1. **FROM** / **JOIN** must use only table names listed in **LIVE_DB_TABLES** (when present).
2. Column names must match **LIVE_DB_TABLES** detail or **RETRIEVAL**; never assume extra columns.
3. Infer joins only when a shared key column exists in both tables (e.g. same **NPI**, **ZIP**, **Month** name).
4. Match the grain the user asked for before aggregating.
5. Keep SQL compatible with **{db_dialect}** (SQLite types and functions).
6. For ranking / \"top N\" / \"which N\", prefer **no** trailing **LIMIT** unless the user asked for a cap — order correctly.
7. **Ratios / division in SQLite:** use **`CAST(... AS REAL)`** or **`1.0 *`** so you never rely on integer-only division (which truncates to 0).
8. **Categorical WHERE:** do not assume values like `'Yes'` / `'Accepted'` — match the workbook’s real literals (often **`'Y'`**, **`'Full'`**, **`'Positive Engagement'`**, etc.); use **DISTINCT** on the column when unsure.
9. **BR-004 / specialty crediting review:** Filter or list by **Specialty** vs **IC_Credit** / **Creditable** policy (e.g. Internal Medicine vs Infectious Disease) — **not** by **`ORDER BY` call count** unless the question asks for activity.
10. **target-flag audit (`VALID` / `REVIEW` / `INVALID`):** **INVALID** for **retired** or **merged-away non-survivor** **HCP_ID** when DCR/survivor columns say credit belongs on another ID; include **Dummy_Data** **TRx** or **Units** (exact column names) joined per **HCP_ID**/**NPI** when the question is a compliance audit of credited volume.
11. **Virtual calls after Digital DNC opt-out:** Join calls to **Marketing_Opt** (or equivalent) on **HCP_ID**/**NPI**, filter **Channel** to virtual, **`call_date` > `opt_out_date`** (use schema’s exact date column names).
12. **Traceability:** Do not **SELECT** or alias a column you cannot source from a table in **LIVE_DB_TABLES** (e.g. invented **Risk_Score**). Prefer **`COUNT`/`CASE`** from real columns.
13. **ZIP vs territory leakage:** Misalignment = **same HCP**, territory from **ZIP→Alignment** **≠** territory on master/Dummy_Data — not “count of ZIPs per territory” in Alignment alone.
14. **Multi-step questions:** If the user asks for several derived steps, use **CTEs** or **multiple subqueries** in one statement when possible so the deliverable is not “step 1 only.”

SQL:"""

    _ = output_format  # same contract as Postgres path; model still emits SQL only
    return PromptParts(system=system_prompt, user=user_prompt)


def build_geography_rewrite_prompt(
    anchor_sql: str,
    new_place_instruction: str,
    *,
    max_rows: int | None = None,
) -> PromptParts:
    limit_instruction = (
        "- Do **not** add a trailing LIMIT / FETCH unless the user explicitly asked for a capped sample; preserve the prior query's row shape when possible."
        if max_rows is None
        else f"- Add LIMIT {max_rows} only if the rewritten query needs a row cap and none exists already."
    )
    system = f"""You rewrite one PostgreSQL query for a follow-up request.

Rules:
- Keep the prior query's core metric and intent unless the new instruction clearly changes them.
- Change only the filters, grouping, or ordering needed to satisfy the follow-up.
- Output only one SQL statement.
{limit_instruction}
"""

    user = f"""FOLLOW-UP REQUEST:
{new_place_instruction}

PRIOR SQL:
```sql
{anchor_sql.strip()}
```

Return ONLY the rewritten SQL."""
    return PromptParts(system=system, user=user)


def build_text_to_sql_prompt(
    user_query: str,
    *,
    erd_path: Optional[Path] = ERD_PATH_DEFAULT,
    db_dialect: str = "PostgreSQL",
    output_format: str = "SQL_ONLY",
    max_rows: Optional[int] = None,
    physical_schema_context: Optional[str] = None,
    known_period_types: Optional[List[str]] = None,
    conversation_context: Optional[str] = None,
    anchor_sql: Optional[str] = None,
    retrieval_context: Optional[str] = None,
    live_db_tables_context: Optional[str] = None,
    workbook_sqlite_mode: bool = False,
) -> PromptParts:
    sanitised_query = _sanitise_user_query(user_query)
    sanitised_query = _cap(sanitised_query, MAX_QUERY_CHARS, "user_query") or sanitised_query
    schema_ctx = _cap(physical_schema_context, MAX_SCHEMA_CHARS, "physical_schema_context")
    live_ctx = _cap(live_db_tables_context, MAX_SCHEMA_CHARS, "live_db_tables_context")
    conv_ctx = _cap(conversation_context, MAX_CONV_CHARS, "conversation_context")

    if workbook_sqlite_mode:
        return _build_workbook_sqlite_prompt(
            sanitised_query,
            db_dialect=db_dialect,
            output_format=output_format,
            max_rows=max_rows,
            physical_schema_context=schema_ctx,
            conversation_context=conv_ctx,
            anchor_sql=anchor_sql,
            retrieval_context=retrieval_context,
            live_db_tables_context=live_ctx,
        )

    erd_text = ""
    if erd_path:
        # When a full physical schema dump is provided alone, skip ERD to save tokens (legacy).
        if schema_ctx and not (retrieval_context and str(retrieval_context).strip()):
            erd_text = ""
        else:
            erd_text = _load_erd(erd_path)
            erd_text = _cap(erd_text, MAX_ERD_CHARS, "erd_context") or ""

    return _build_prompt(
        sanitised_query,
        db_dialect=db_dialect,
        output_format=output_format,
        max_rows=max_rows,
        physical_schema_context=schema_ctx,
        erd_text=erd_text,
        conversation_context=conv_ctx,
        known_period_types=known_period_types,
        anchor_sql=anchor_sql,
        retrieval_context=retrieval_context,
        live_db_tables_context=live_ctx,
    )


def build_intent_classification_prompt(user_query: str, *, workbook_sqlite_mode: bool = False) -> str:
    if workbook_sqlite_mode:
        return f"""Analyze the user's input and classify it into one of two categories:
1. DATA_QUERY: The user wants information from the **loaded spreadsheet / SQLite** data — counts, lists, filters, joins, rankings, trends, or any question about sheet/table names and columns that could be answered with SQL over the current file.

   **Always DATA_QUERY** (not CHAT) when the message asks about **any** of: **ZORYVE** TRx, **TCS** / total class, **Other BNST** / competitor volume, **call frequency** / calls, **target flag** / targeting, **decile**, **region** / territory / area, **HCP** prescribers, **ZORYVE share**, **trend** / growth / MoM / QoQ, **switch opportunities**.

2. CHAT: Only brief social or assistant messages such as hello, thanks, goodbye, help, who are you, or what can you do, with no request for data from the file.

User Input: "{user_query}"

Respond with ONLY the word "DATA_QUERY" or "CHAT"."""
    return f"""Analyze the user's input and classify it into one of two categories:
1. DATA_QUERY: The user wants information from the current database or ERD. This includes counts, lists, filters, joins, rankings, patient journeys, payer or formulary questions, schema questions, SQL-style questions, or any question about tables in **ERD.md** (e.g. patient, admission, drug, molecule, therapy_area, prescription, adverse_event, claim, formulary, hcp, sales_rep, rep_activity, call_plan, drug_sale, market_share, payer, territory, region, manufacturer, drug_interaction, drug_shortage, clinical_trial, drug_substitution, promotional_material, rep_performance, hcp_segment_history, step_therapy, specialty_pharmacy, patient_assistance, payer_formulary_tier_change, competitive_intelligence, brand_tracker, channel_performance, forecast, kpi_metric, dim_date).

   IMPORTANT — the following are always DATA_QUERY (answered from the database, never from general knowledge):
   - Any question about days, weekends, holidays, or calendar counts for a specific year (e.g. "how many weekend days in 2024?", "how many holidays in 2023?") — answered from the **dim_date** table.
   - Any question referencing a specific calendar year (2020–2026) alongside words like days, weekends, weekend days, holidays, working days, quarters.
   - A bare year number like "2025" or "2023" used as a follow-up to a calendar/date question.

2. CHAT: Only brief social or assistant messages such as hello, thanks, goodbye, help, who are you, or what can you do, with no request for database information.

User Input: "{user_query}"

Respond with ONLY the word "DATA_QUERY" or "CHAT"."""


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Build Text-to-SQL prompts (ERD + optional RAG).")
    parser.add_argument("--erd", default=str(ERD_PATH_DEFAULT), help="Path to ERD markdown")
    parser.add_argument("--dialect", default="PostgreSQL", help="SQL dialect label in the system prompt")
    parser.add_argument("--format", default="SQL_ONLY", help="Output format label")
    parser.add_argument(
        "--max-rows",
        type=int,
        default=-1,
        help="Row limit hint for the model (default -1 = no LIMIT hint; set e.g. 200 to ask the model for LIMIT 200)",
    )
    args = parser.parse_args()
    erd_p = Path(args.erd)
    max_rows: int | None = None if args.max_rows < 0 else args.max_rows
    user_q = input("Enter question: ").strip()
    if user_q:
        prompt = build_text_to_sql_prompt(
            user_q,
            erd_path=erd_p,
            db_dialect=args.dialect,
            output_format=args.format,
            max_rows=max_rows,
        )
        print(f"\nSYSTEM:\n{prompt.system}\n\nUSER:\n{prompt.user}")
