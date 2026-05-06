# """One-shot script: introspect live Postgres → write a 100 % accurate ERD.md.

# Usage:
#     cd Backend/src
#     python generate_erd.py          # writes ERD.md next to this file
#     python generate_erd.py --dry    # prints to stdout, does not write
# """

# from __future__ import annotations

# import os
# import sys
# from collections import defaultdict
# from pathlib import Path
# from typing import Any, Dict, List, Set, Tuple

# # ── bootstrap env so PGHOST etc. are available ──────────────────────────
# sys.path.insert(0, str(Path(__file__).resolve().parent))
# from env_loader import load_application_dotenv

# load_application_dotenv()

# import psycopg2
# from psycopg2.extras import RealDictCursor

# # ── connection helper (mirrors postgres_runner._db_config) ──────────────

# def _connect():
#     def _env(*keys):
#         for k in keys:
#             v = os.getenv(k)
#             if v and v.strip():
#                 return v.strip()
#         return None

#     host = _env("PGHOST", "pg_host", "POSTGRES_HOST")
#     dbname = _env("PGDATABASE", "pg_dbname", "POSTGRES_DB")
#     user = _env("PGUSER", "pg_user", "POSTGRES_USER")
#     password = _env("PGPASSWORD", "pg_password", "POSTGRES_PASSWORD")
#     port = int(_env("PGPORT", "pg_port") or 5432)

#     missing = []
#     if not host:     missing.append("PGHOST")
#     if not dbname:   missing.append("PGDATABASE")
#     if not user:     missing.append("PGUSER")
#     if not password:  missing.append("PGPASSWORD")
#     if missing:
#         raise RuntimeError(f"Missing env vars: {', '.join(missing)}")

#     return psycopg2.connect(host=host, port=port, dbname=dbname,
#                             user=user, password=password, connect_timeout=15)


# def _schema_name() -> str:
#     for key in ("SDA_PHARMA_SCHEMA", "SDA_TAKEDA_SCHEMA", "PGSCHEMA", "pg_schema"):
#         v = (os.getenv(key) or "").strip()
#         if v:
#             return v
#     return "public"


# # ── information_schema queries ──────────────────────────────────────────

# def fetch_tables(cur, schema: str) -> List[str]:
#     cur.execute("""
#         SELECT table_name
#         FROM information_schema.tables
#         WHERE table_schema = %s AND table_type = 'BASE TABLE'
#         ORDER BY table_name;
#     """, (schema,))
#     return [r["table_name"] for r in cur.fetchall()]


# def fetch_columns(cur, schema: str) -> Dict[str, List[Dict[str, Any]]]:
#     cur.execute("""
#         SELECT table_name, column_name, data_type,
#                character_maximum_length, numeric_precision, numeric_scale,
#                is_nullable, column_default, ordinal_position
#         FROM information_schema.columns
#         WHERE table_schema = %s
#         ORDER BY table_name, ordinal_position;
#     """, (schema,))
#     cols: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
#     for r in cur.fetchall():
#         cols[r["table_name"]].append(dict(r))
#     return dict(cols)


# def fetch_primary_keys(cur, schema: str) -> Dict[str, List[str]]:
#     cur.execute("""
#         SELECT tc.table_name, kcu.column_name
#         FROM information_schema.table_constraints tc
#         JOIN information_schema.key_column_usage kcu
#             ON tc.constraint_name = kcu.constraint_name
#            AND tc.table_schema   = kcu.table_schema
#         WHERE tc.table_schema = %s AND tc.constraint_type = 'PRIMARY KEY'
#         ORDER BY tc.table_name, kcu.ordinal_position;
#     """, (schema,))
#     pks: Dict[str, List[str]] = defaultdict(list)
#     for r in cur.fetchall():
#         pks[r["table_name"]].append(r["column_name"])
#     return dict(pks)


# def fetch_foreign_keys(cur, schema: str) -> List[Dict[str, str]]:
#     cur.execute("""
#         SELECT
#             kcu.table_name       AS child_table,
#             kcu.column_name      AS child_column,
#             ccu.table_name       AS parent_table,
#             ccu.column_name      AS parent_column,
#             tc.constraint_name
#         FROM information_schema.table_constraints tc
#         JOIN information_schema.key_column_usage kcu
#             ON tc.constraint_name = kcu.constraint_name
#            AND tc.table_schema   = kcu.table_schema
#         JOIN information_schema.constraint_column_usage ccu
#             ON tc.constraint_name = ccu.constraint_name
#            AND tc.table_schema   = ccu.table_schema
#         WHERE tc.table_schema = %s AND tc.constraint_type = 'FOREIGN KEY'
#         ORDER BY kcu.table_name, kcu.column_name;
#     """, (schema,))
#     return [dict(r) for r in cur.fetchall()]


# # ── domain classification ───────────────────────────────────────────────

# DOMAIN_MAP = {
#     # Domain 1 — Reference + Drug catalog (ERD.md)
#     "dim_date": 1,
#     "therapy_area": 1,
#     "molecule": 1,
#     "manufacturer": 1,
#     "drug": 1,
#     "drug_class": 1,
#     "drug_subclass": 1,
#     "drug_formulation": 1,
#     "drug_indication": 1,
#     "drug_target": 1,
#     "drug_price": 1,
#     "drug_patent": 1,
#     "drug_lifecycle": 1,
#     "drug_competitor": 1,
#     "drug_packaging": 1,
#     "drug_approval": 1,
#     "rebate_program": 1,
#     # Domain 2 — Clinical / Patient
#     "patient": 2,
#     "provider": 2,
#     "admission": 2,
#     "icu_stay": 2,
#     "lab_event": 2,
#     "diagnosis": 2,
#     "procedure_event": 2,
#     "prescription": 2,
#     "adverse_event": 2,
#     "patient_demographic": 2,
#     "comorbidity": 2,
#     "treatment_pathway": 2,
#     "adherence": 2,
#     "persistence": 2,
#     "patient_outcome": 2,
#     # Domain 3 — Commercial / Sales
#     "region": 3,
#     "hcp": 3,
#     "hcp_affiliation": 3,
#     "sales_rep": 3,
#     "rep_activity": 3,
#     "call_outcome": 3,
#     "call_plan": 3,
#     "sales_target": 3,
#     "sales_incentive": 3,
#     "drug_sale": 3,
#     "rx_summary": 3,
#     # Domain 4 — Payer, access, analytics
#     "payer": 4,
#     "formulary": 4,
#     "formulary_history": 4,
#     "claim": 4,
#     "claim_line": 4,
#     "reimbursement": 4,
#     "prior_authorization": 4,
#     "copay": 4,
#     "coverage_limit": 4,
#     "payer_contract": 4,
#     "market_share": 4,
#     "forecast": 4,
#     "kpi_metric": 4,
# }

# DOMAIN_TITLES = {
#     1: "Domain 1 - Reference & Drug Catalog",
#     2: "Domain 2 - Clinical & Patient",
#     3: "Domain 3 - Commercial & Sales",
#     4: "Domain 4 - Payer, Access & Analytics",
#     0: "Other Tables",
# }


# # ── markdown generation ─────────────────────────────────────────────────

# def _pg_type_display(col: Dict[str, Any]) -> str:
#     dt = col["data_type"].upper()
#     if dt == "CHARACTER VARYING":
#         ml = col.get("character_maximum_length")
#         return f"VARCHAR({ml})" if ml else "VARCHAR"
#     if dt == "NUMERIC":
#         p, s = col.get("numeric_precision"), col.get("numeric_scale")
#         if p:
#             return f"NUMERIC({p},{s or 0})"
#         return "NUMERIC"
#     if dt in ("INTEGER", "BIGINT", "SMALLINT", "BOOLEAN", "TEXT", "DATE",
#               "TIMESTAMP WITHOUT TIME ZONE", "TIMESTAMP WITH TIME ZONE",
#               "DOUBLE PRECISION", "REAL", "BYTEA", "JSON", "JSONB", "UUID"):
#         return dt.replace("WITHOUT TIME ZONE", "").replace("WITH TIME ZONE", "TZ").strip()
#     return dt


# def _role(col_name: str, pk_cols: List[str],
#           fk_lookup: Dict[str, Tuple[str, str]]) -> str:
#     parts = []
#     if col_name in pk_cols:
#         parts.append("**PK**")
#     if col_name in fk_lookup:
#         pt, pc = fk_lookup[col_name]
#         parts.append(f"**FK -> {pt}.{pc}**")
#     return ", ".join(parts) if parts else ""


# def generate_markdown(
#     tables: List[str],
#     columns: Dict[str, List[Dict[str, Any]]],
#     pks: Dict[str, List[str]],
#     fks: List[Dict[str, str]],
#     schema: str,
# ) -> str:

#     fk_by_child: Dict[str, Dict[str, Tuple[str, str]]] = defaultdict(dict)
#     for fk in fks:
#         fk_by_child[fk["child_table"]][fk["child_column"]] = (
#             fk["parent_table"], fk["parent_column"]
#         )

#     domains: Dict[int, List[str]] = defaultdict(list)
#     for t in tables:
#         d = DOMAIN_MAP.get(t, 0)
#         domains[d].append(t)

#     lines: List[str] = []
#     L = lines.append

#     L("# Entity Relationship Diagram (ERD)")
#     L("### Pharma-Focused Data Model - auto-generated from live database")
#     L("")
#     L("> **This file is the single source of truth for NL-to-SQL.**")
#     L("> Every table, column, type, PK, and FK below was pulled from")
#     L(f"> `information_schema` in schema `{schema}`.")
#     L("> Re-generate with: `python generate_erd.py`")
#     L("")
#     L("---")
#     L("")

#     # ── Table of Contents ──
#     L("## Table of Contents")
#     L("")
#     for d_id in sorted(domains.keys()):
#         if d_id == 0 and not domains[0]:
#             continue
#         title = DOMAIN_TITLES.get(d_id, f"Domain {d_id}")
#         anchor = title.lower().replace(" ", "-").replace("—", "").replace("&", "").replace("  ", "-")
#         L(f"- [{title}](#{anchor})")
#     L("- [Key Relationships](#key-relationships)")
#     L("- [ER Diagram (Mermaid)](#er-diagram-mermaid)")
#     L("- [Schema Tree View](#schema-tree-view)")
#     L("")
#     L("---")
#     L("")

#     # ── Per-domain table sections ──
#     for d_id in sorted(domains.keys()):
#         if d_id == 0 and not domains[0]:
#             continue
#         title = DOMAIN_TITLES.get(d_id, f"Domain {d_id}")
#         L(f"## {title}")
#         L("")

#         for tbl in domains[d_id]:
#             tbl_upper = tbl.upper()
#             pk_cols = pks.get(tbl, [])
#             fk_map = fk_by_child.get(tbl, {})
#             tbl_cols = columns.get(tbl, [])

#             L(f"### `{tbl_upper}`")
#             L("")
#             L(f"| # | Column | Type | Nullable | Role |")
#             L(f"|---|--------|------|----------|------|")
#             for i, c in enumerate(tbl_cols, 1):
#                 name = c["column_name"]
#                 dtype = _pg_type_display(c)
#                 nullable = "YES" if c["is_nullable"] == "YES" else "NO"
#                 role = _role(name, pk_cols, fk_map)
#                 L(f"| {i} | `{name}` | {dtype} | {nullable} | {role} |")
#             L("")

#     # ── Key Relationships ──
#     L("---")
#     L("")
#     L("## Key Relationships")
#     L("")
#     if fks:
#         L("| Parent Table | Parent Column | Child Table | Child Column | Constraint |")
#         L("|-------------|---------------|-------------|--------------|------------|")
#         for fk in fks:
#             L(f"| `{fk['parent_table']}` | `{fk['parent_column']}` "
#               f"| `{fk['child_table']}` | `{fk['child_column']}` "
#               f"| {fk['constraint_name']} |")
#     else:
#         L("> No foreign-key constraints found in the database.")
#         L("> If your tables use implicit joins (matching column names without formal FK constraints),")
#         L("> add the logical relationships manually below.")
#     L("")

#     # ── Mermaid ER diagram ──
#     L("---")
#     L("")
#     L("## ER Diagram (Mermaid)")
#     L("")
#     L("```mermaid")
#     L("erDiagram")
#     L("")

#     fk_edges_seen: Set[str] = set()
#     for fk in fks:
#         edge = f"    {fk['parent_table'].upper()} ||--o{{ {fk['child_table'].upper()} : \"{fk['child_column']}\""
#         if edge not in fk_edges_seen:
#             fk_edges_seen.add(edge)
#             L(edge)
#     L("")

#     for tbl in tables:
#         tbl_upper = tbl.upper()
#         pk_cols = pks.get(tbl, [])
#         fk_map = fk_by_child.get(tbl, {})
#         tbl_cols = columns.get(tbl, [])
#         L(f"    {tbl_upper} {{")
#         for c in tbl_cols:
#             name = c["column_name"]
#             dtype = _pg_type_display(c).replace(" ", "_").lower()
#             marker = ""
#             if name in pk_cols:
#                 marker = " PK"
#             elif name in fk_map:
#                 marker = " FK"
#             L(f"        {dtype} {name}{marker}")
#         L("    }")

#     L("```")
#     L("")

#     # ── Schema Tree View ──
#     L("---")
#     L("")
#     L("## Schema Tree View")
#     L("")

#     parents_of: Dict[str, List[str]] = defaultdict(list)
#     children_of: Dict[str, List[str]] = defaultdict(list)
#     for fk in fks:
#         if fk["child_table"] not in children_of[fk["parent_table"]]:
#             children_of[fk["parent_table"]].append(fk["child_table"])
#         if fk["parent_table"] not in parents_of[fk["child_table"]]:
#             parents_of[fk["child_table"]].append(fk["parent_table"])

#     roots = [t for t in tables if not parents_of.get(t)]
#     visited: Set[str] = set()

#     L("```")
#     def _tree(node: str, indent: int):
#         if node in visited:
#             L(f"{'  ' * indent}└── {node.upper()} (see above)")
#             return
#         visited.add(node)
#         L(f"{'  ' * indent}{'└── ' if indent > 0 else ''}{node.upper()}")
#         for child in children_of.get(node, []):
#             _tree(child, indent + 1)

#     for root in roots:
#         _tree(root, 0)
#         L("")

#     standalone = [t for t in tables if t not in visited]
#     for t in standalone:
#         L(f"{t.upper()} (standalone - no FK relationships)")
#         L("")
#     L("```")
#     L("")

#     # ── Appendix ──
#     L("---")
#     L("")
#     L("## Appendix - Common Query Patterns")
#     L("")
#     L("### Example 1 - Adverse Events per Drug (with names)")
#     L("")
#     L("```sql")
#     L("SELECT d.drug_id, d.drug_name, m.molecule_name,")
#     L("       COUNT(ae.adverse_event_id) AS adverse_event_count")
#     L("FROM adverse_event ae")
#     L("JOIN drug d ON ae.drug_id = d.drug_id")
#     L("JOIN molecule m ON d.molecule_id = m.molecule_id")
#     L("GROUP BY d.drug_id, d.drug_name, m.molecule_name")
#     L("ORDER BY adverse_event_count DESC;")
#     L("```")
#     L("")
#     L("### Example 2 - Drug Sales by Region")
#     L("")
#     L("```sql")
#     L("SELECT r.region_name, d.brand_name,")
#     L("       SUM(ds.quantity) AS total_units,")
#     L("       SUM(ds.revenue)  AS total_revenue")
#     L("FROM drug_sale ds")
#     L("JOIN drug d ON ds.drug_id = d.drug_id")
#     L("JOIN hcp h ON ds.hcp_id = h.hcp_id")
#     L("JOIN region r ON h.region_id = r.region_id")
#     L("GROUP BY r.region_name, d.brand_name")
#     L("ORDER BY total_revenue DESC;")
#     L("```")
#     L("")
#     L("### Example 3 - Rep Activity with HCP and Region")
#     L("")
#     L("```sql")
#     L("SELECT sr.rep_name, r.region_name, ra.activity_date, ra.activity_type")
#     L("FROM rep_activity ra")
#     L("JOIN sales_rep sr ON ra.sales_rep_id = sr.sales_rep_id")
#     L("JOIN region r ON sr.region_id = r.region_id")
#     L("ORDER BY ra.activity_date DESC;")
#     L("```")
#     L("")
#     L("### Example 4 - Market Share by Drug and Region")
#     L("")
#     L("```sql")
#     L("SELECT d.brand_name, r.region_name, ms.period_date, ms.market_share_percent")
#     L("FROM market_share ms")
#     L("JOIN drug d ON ms.drug_id = d.drug_id")
#     L("JOIN region r ON ms.region_id = r.region_id")
#     L("ORDER BY ms.period_date DESC, ms.market_share_percent DESC;")
#     L("```")
#     L("")
#     L("### Example 5 - Patient Journey (Admission to Diagnosis to Prescriptions)")
#     L("")
#     L("```sql")
#     L("SELECT p.patient_id, p.gender, p.age,")
#     L("       a.admission_id, a.admit_time, a.discharge_time,")
#     L("       dg.icd_code, dg.diagnosis_desc,")
#     L("       pr.drug_id, d.drug_name, pr.dose")
#     L("FROM patient p")
#     L("JOIN admission a ON p.patient_id = a.patient_id")
#     L("LEFT JOIN diagnosis dg ON a.admission_id = dg.admission_id")
#     L("LEFT JOIN prescription pr ON a.admission_id = pr.admission_id")
#     L("LEFT JOIN drug d ON pr.drug_id = d.drug_id")
#     L("WHERE p.patient_id = 1")
#     L("ORDER BY a.admit_time, dg.diagnosis_id;")
#     L("```")
#     L("")
#     L("### Important Notes")
#     L("")
#     L("| Topic | Guidance |")
#     L("|-------|----------|")
#     L("| **Region for commercial queries** | Join `hcp.region_id` or `sales_rep.region_id` to `region`; for `drug_sale` use `drug_sale` → `hcp` → `region` |")
#     L("| **Drug names** | Use `drug.drug_name` / `drug.brand_name`; join `molecule` on `drug.molecule_id` for ingredient |")
#     L("| **Molecule names** | `molecule.molecule_name` via `drug.molecule_id` |")
#     L("| **Severity** | `adverse_event.severity` is VARCHAR — use `CASE` for numeric scoring |")
#     L("| **Rep to Region** | `rep_activity` has no region column; join `sales_rep` on `sales_rep_id` then `region` |")
#     L("| **Prescriptions** | `prescription` links `admission_id`, `patient_id`, `provider_id`, `drug_id` |")
#     L("")

#     return "\n".join(lines)


# # ── main ────────────────────────────────────────────────────────────────

# def main():
#     dry_run = "--dry" in sys.argv

#     schema = _schema_name()
#     print(f"[generate_erd] Connecting to database, schema = '{schema}' ...")

#     conn = _connect()
#     try:
#         with conn.cursor(cursor_factory=RealDictCursor) as cur:
#             print("[generate_erd] Fetching tables ...")
#             tables = fetch_tables(cur, schema)
#             print(f"[generate_erd]   Found {len(tables)} tables: {tables}")

#             print("[generate_erd] Fetching columns ...")
#             columns = fetch_columns(cur, schema)
#             total_cols = sum(len(v) for v in columns.values())
#             print(f"[generate_erd]   Found {total_cols} columns across {len(columns)} tables")

#             print("[generate_erd] Fetching primary keys ...")
#             pks = fetch_primary_keys(cur, schema)
#             print(f"[generate_erd]   Found PKs for {len(pks)} tables")

#             print("[generate_erd] Fetching foreign keys ...")
#             fks = fetch_foreign_keys(cur, schema)
#             print(f"[generate_erd]   Found {len(fks)} FK constraints")
#     finally:
#         conn.close()

#     md = generate_markdown(tables, columns, pks, fks, schema)

#     if dry_run:
#         print("\n" + "=" * 60)
#         print("DRY RUN — would write the following ERD.md:")
#         print("=" * 60 + "\n")
#         print(md)
#     else:
#         out_path = Path(__file__).resolve().parent / "ERD.md"
#         out_path.write_text(md, encoding="utf-8")
#         print(f"\n[generate_erd] Wrote {len(md)} chars to {out_path}")
#         print("[generate_erd] Done. ERD.md is now the source of truth.")


# if __name__ == "__main__":
#     main()

