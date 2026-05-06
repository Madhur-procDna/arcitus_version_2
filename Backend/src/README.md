<!-- # Takeda SDA — `src` overview

This folder implements a **natural language interface** to the Takeda commercial PostgreSQL database, focused on **`rep_activity`** and **`hcp_call_plan`**.

## Core purpose

| Capability | Role |
|------------|------|
| **NL → SQL** | Converts questions into validated, read-only PostgreSQL using **`ERD.md`** as the LLM schema ground truth (`text_to_sql_prompt.py`, `qa_pipeline.py`). |
| **Intent routing** | Treats **data questions** vs **brief greetings** vs **off-topic** chat; general knowledge is not answered (`build_intent_classification_prompt`, `_allowed_smalltalk_response`, `_TAKEDA_OFF_TOPIC_REPLY`). |
| **Contextual memory** | `ConversationBuffer` (optionally Redis-backed) for follow-ups such as geography-only rewrites (`build_geography_rewrite_prompt`, `_effective_sql_question_for_llm`). |
| **Summarization** | LLM narrative with **table-first** formatting and optional advisories for duplicate-name metrics and **data quality** flags (`summarize_results`). |

## Major implementation notes

- **Type safety:** Mixed-type `COALESCE` (e.g. **tier**) — use `CAST(tier AS TEXT)` / `::text` where text and numeric collide (prompt rule in `text_to_sql_prompt.py`).
- **Time semantics:** Calendar-year style filters via **`period_end_date`** / `EXTRACT(YEAR FROM …)` so totals align with reporting expectations.
- **Serialization:** JSON for result payloads uses **`default=str`** so `Decimal` and dates do not crash the pipeline.
- **Join / fan-out:** Prefer **aggregate per table in CTEs, then join** — section **1b** in the text-to-SQL system prompt. Duplicate **HCP display names** use an **`hcp_call_plan`-only** fast path when matched (`_hcp_duplicate_names_sql`).
- **Out-of-schema questions:** Database errors such as **missing columns** are mapped to a short **schema-scope** explanation (`_friendly_schema_or_db_error`).

## Main entry points

- `qa_pipeline.py` — Orchestration: intent → SQL generation → `run_query` → `summarize_results`.
- `api_server.py` — HTTP API for the frontend (same pipeline).
- `postgres_runner.py` — Query execution.
- `sql_validate.py` — Read-only and safety checks on generated SQL.

## Known limitations (see also code comments)

- **Data quality:** Blank or placeholder names in results are flagged as **possible anomalies** in summarization, not as ground truth.
- **Complex joins:** The model must follow **aggregate-first** patterns; prompts enforce this, but unusual questions may still need iteration.
- **Schema gaps:** Questions that sound on-brand but reference **non-existent columns** should surface the friendly schema message, not a raw Postgres error string.

For environment setup and CLI/API run instructions, see **`../README.md`** at the Backend root. -->
