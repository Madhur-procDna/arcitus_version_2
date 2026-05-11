# Arcutis Biotherapeutics — Data Assistant Schema

> Version: 3.1 | Flat Table Schema | Temporal-Aware | No Q1 2026 Bias

---

## Overview

The database consists of a **single flat table** named `arcutis_data` containing all HCP
(Healthcare Provider) metrics, demographic information, geographic assignments, and
prescription data for Arcutis. The dataset spans **January 2025 through March 2026**.

---

## 1. `arcutis_data` Table Definition

### HCP Identity

| Column | Type | Description |
|--------|------|-------------|
| `npi_id` | TEXT | National Provider Identifier (business key) |
| `hcp_name` | TEXT | Full name of the HCP |
| `primary_specialty` | TEXT | Primary specialty of the provider |
| `secondary_specialty` | TEXT | Secondary specialty of the provider |
| `hco_name` | TEXT | Health Care Organization name |

### Geography

| Column | Type | Description |
|--------|------|-------------|
| `city` | TEXT | City of the HCP |
| `state` | TEXT | **2-letter abbreviation ONLY** (e.g. 'FL', 'NY') |
| `zip` | TEXT | ZIP code |
| `base_territory` | TEXT | Base territory assignment |
| `region` | TEXT | Full region name (see valid values below) |
| `area` | TEXT | High-level area — ONLY `'East'` or `'West'` |

### Targeting & Decile Rankings

| Column | Type | Description |
|--------|------|-------------|
| `q1_26_decile` | BIGINT | Q1 2026 decile. **1 = HIGHEST priority. 10 = LOWEST priority.** |
| `q4_25_decile` | BIGINT | Q4 2025 decile. Same scale: 1 = highest, 10 = lowest. |
| `q1_26_target_flag` | TEXT | Q1 2026 targeting status |
| `q4_25_target_flag` | TEXT | Q4 2025 targeting status |

### Quarterly Call Counts

| Column | Type | Description |
|--------|------|-------------|
| `q2_25_calls` | BIGINT | Calls made in Q2 2025 |
| `q3_25_calls` | BIGINT | Calls made in Q3 2025 |
| `q4_25_calls` | BIGINT | Calls made in Q4 2025 |
| `q1_26_calls` | BIGINT | Calls made in Q1 2026 |

### Monthly TRx — ZORYVE (Jan 2025 – Mar 2026)

| Column | Type |
|--------|------|
| `zoryve_jan_25` | BIGINT |
| `zoryve_feb_25` | BIGINT |
| `zoryve_mar_25` | BIGINT |
| `zoryve_apr_25` | BIGINT |
| `zoryve_may_25` | BIGINT |
| `zoryve_jun_25` | BIGINT |
| `zoryve_jul_25` | BIGINT |
| `zoryve_aug_25` | BIGINT |
| `zoryve_sep_25` | BIGINT |
| `zoryve_oct_25` | BIGINT |
| `zoryve_nov_25` | BIGINT |
| `zoryve_dec_25` | BIGINT |
| `zoryve_jan_26` | BIGINT |
| `zoryve_feb_26` | BIGINT |
| `zoryve_mar_26` | BIGINT |

### Monthly TRx — Other BNST (Jan 2025 – Mar 2026)

> Other BNST = all non-Zoryve broad non-steroidal topicals. TCS is a sub-category within this group.

| Column | Type |
|--------|------|
| `other_bnst_jan_25` | BIGINT |
| `other_bnst_feb_25` | BIGINT |
| `other_bnst_mar_25` | BIGINT |
| `other_bnst_apr_25` | BIGINT |
| `other_bnst_may_25` | BIGINT |
| `other_bnst_jun_25` | BIGINT |
| `other_bnst_jul_25` | BIGINT |
| `other_bnst_aug_25` | BIGINT |
| `other_bnst_sep_25` | BIGINT |
| `other_bnst_oct_25` | BIGINT |
| `other_bnst_nov_25` | BIGINT |
| `other_bnst_dec_25` | BIGINT |
| `other_bnst_jan_26` | BIGINT |
| `other_bnst_feb_26` | BIGINT |
| `other_bnst_mar_26` | BIGINT |

### Monthly TRx — TCS / Topical Corticosteroids (Jan 2025 – Mar 2026)

> ⚠️ TCS is a **subset of Other BNST**. NEVER add TCS on top of Other BNST — that double counts.
> Use TCS columns ONLY when user specifically asks about corticosteroid breakdown.

| Column | Type |
|--------|------|
| `tcs_jan_25` | BIGINT |
| `tcs_feb_25` | BIGINT |
| `tcs_mar_25` | BIGINT |
| `tcs_apr_25` | BIGINT |
| `tcs_may_25` | BIGINT |
| `tcs_jun_25` | BIGINT |
| `tcs_jul_25` | BIGINT |
| `tcs_aug_25` | BIGINT |
| `tcs_sep_25` | BIGINT |
| `tcs_oct_25` | BIGINT |
| `tcs_nov_25` | BIGINT |
| `tcs_dec_25` | BIGINT |
| `tcs_jan_26` | BIGINT |
| `tcs_feb_26` | BIGINT |
| `tcs_mar_26` | BIGINT |

### Annual & Payer Totals

| Column | Type | Description |
|--------|------|-------------|
| `total_2025_trx` | BIGINT | Full-year 2025 TRx across ALL brands — use for 2025 annual totals |
| `commercial_2025_trx` | BIGINT | 2025 commercial channel TRx |
| `medicare_2025` | BIGINT | 2025 Medicare TRx |
| `medicaid_2025` | BIGINT | 2025 Medicaid TRx |
| `commecial` | DOUBLE | Commercial payer mix % (0–100) |
| `medicare` | DOUBLE | Medicare payer mix % (0–100) |
| `medicaid` | DOUBLE | Medicaid payer mix % (0–100) |
| `united_health` | BIGINT | United Health payer volume |
| `cvs_health` | BIGINT | CVS Health payer volume |
| `centene_corp` | BIGINT | Centene Corp payer volume |
| `humana` | BIGINT | Humana payer volume |
| `elevance_health` | BIGINT | Elevance Health payer volume |

---

## 2. Product Categories & Market Share (CRITICAL)

There are **two market categories** for all TRx and market share calculations:

| Category | Column Prefix | Description |
|----------|--------------|-------------|
| **ZORYVE** | `zoryve_` | Arcutis product — the focus brand |
| **Other BNST** | `other_bnst_` | All non-Zoryve topicals including TCS |
| **TCS** | `tcs_` | ⚠️ Sub-category of Other BNST — breakdown use only |

### Total TRx Formula

Total TRx (any period) = ZORYVE + Other BNST

DO NOT add TCS — it is already inside Other BNST

### ZORYVE Market Share Formula

ZORYVE market share % =
SUM(zoryve columns for period) /
NULLIF(SUM(zoryve columns + other_bnst columns for same period), 0) * 100

### Full Dataset Total TRx Per HCP

(zoryve_jan_25 + zoryve_feb_25 + ... + zoryve_mar_26) +
(other_bnst_jan_25 + other_bnst_feb_25 + ... + other_bnst_mar_26)

### When to Use TCS Columns

| User asks | Action |
|-----------|--------|
| "total TRx" or "total prescriptions" | ZORYVE + Other BNST only — ignore tcs_ |
| "market share" | ZORYVE + Other BNST only — ignore tcs_ |
| "corticosteroid volume" or "TCS breakdown" | Use tcs_ columns |
| "what portion of Other BNST is TCS" | Use tcs_ / other_bnst_ ratio |

### NEVER

- NEVER add TCS + Other BNST together in a total — that double counts
- NEVER answer "total prescriptions" using only ZORYVE columns
- NEVER compute market share with ZORYVE as both numerator and denominator

### TCS User Request Override Rule (CRITICAL)

If the user explicitly asks to "include TCS" or "show TCS" alongside Other BNST in a total:

- NEVER add TCS as a third category to the total
- ALWAYS respond with: **Total TRx = ZORYVE + Other BNST only**
- Show TCS as a **SEPARATE breakdown column** labeled **"TCS (subset of Other BNST)"** — never in the sum
- Always include this warning in the response:

> TCS is already included within Other BNST. Adding it separately would double-count those prescriptions.

---

## 3. Temporal Query Resolution (CRITICAL — NO Q1 2026 BIAS)

> The dataset spans Jan 2025 – Mar 2026.
> NEVER default to Q1 2026 when no time period is specified.
> No time period = aggregate the FULL dataset across all available months.

### Resolution Rules by User Intent

| User says | Columns to use |
|-----------|---------------|
| Nothing / no period specified | ALL months: jan_25 through mar_26 (15 months) |
| "full dataset" / "all time" | ALL months: jan_25 through mar_26 |
| "2025" / "last year" | `total_2025_trx` OR sum all _25 columns |
| "2026" / "this year" | jan_26 + feb_26 + mar_26 columns |
| "Q1 2025" | jan_25 + feb_25 + mar_25 |
| "Q2 2025" | apr_25 + may_25 + jun_25 |
| "Q3 2025" | jul_25 + aug_25 + sep_25 |
| "Q4 2025" | oct_25 + nov_25 + dec_25 |
| "Q1 2026" / "latest quarter" | jan_26 + feb_26 + mar_26 |
| Specific month e.g. "February 2026" | exact column e.g. `_feb_26` |

### Quarter → Column Mapping (all three prefixes follow same pattern)

| Quarter | Months | Example (ZORYVE) |
|---------|--------|-----------------|
| Q1 2025 | Jan + Feb + Mar 2025 | `zoryve_jan_25 + zoryve_feb_25 + zoryve_mar_25` |
| Q2 2025 | Apr + May + Jun 2025 | `zoryve_apr_25 + zoryve_may_25 + zoryve_jun_25` |
| Q3 2025 | Jul + Aug + Sep 2025 | `zoryve_jul_25 + zoryve_aug_25 + zoryve_sep_25` |
| Q4 2025 | Oct + Nov + Dec 2025 | `zoryve_oct_25 + zoryve_nov_25 + zoryve_dec_25` |
| Q1 2026 | Jan + Feb + Mar 2026 | `zoryve_jan_26 + zoryve_feb_26 + zoryve_mar_26` |

### Call Count → Column Mapping

| Period | Column |
|--------|--------|
| Q2 2025 | `q2_25_calls` |
| Q3 2025 | `q3_25_calls` |
| Q4 2025 | `q4_25_calls` |
| Q1 2026 | `q1_26_calls` |
| No period specified | `q2_25_calls + q3_25_calls + q4_25_calls + q1_26_calls` |

### Decile & Target Flag → Column Mapping

| User intent | Decile column | Target flag column |
|-------------|-------------|-------------------|
| Current / no period specified | `q1_26_decile` | `q1_26_target_flag` |
| Q4 2025 specifically | `q4_25_decile` | `q4_25_target_flag` |

---

## 4. SQL Query Rules

- ALWAYS query the `arcutis_data` table — never invent other tables
- SELECT-only — NEVER: DELETE, DROP, TRUNCATE, UPDATE, INSERT, ALTER, CREATE, REPLACE
- Never expose table names, column names, or DB structure to the user in responses
- Never generate schema-probing queries (SHOW TABLES, DESCRIBE, INFORMATION_SCHEMA, PRAGMA)
- Always use specific columns — no SELECT *
- Always include WHERE conditions and LIMIT clauses
- Use parameterized inputs — no string concatenation
- Do not cast BIGINT or DOUBLE columns — they are already correct types
- When aggregating: group by `region`, `area`, `primary_specialty`, or `q1_26_target_flag`

---

## 5. Geographic Rules (CRITICAL)

- `state` stores **2-letter abbreviations ONLY** — NEVER use full state names in WHERE clauses
- `region` stores full region names — NEVER use abbreviations
- `area` stores ONLY `'East'` or `'West'`

### State + Region Combined Lookup Pattern

```sql
-- When user says "in Florida":
WHERE (state = 'FL' OR region = 'Florida')

-- When user says "in New York":
WHERE (state = 'NY' OR region = 'New York')

-- When user says "in Texas":
WHERE (state = 'TX' OR region ILIKE '%Texas%')
```

### State Name → Abbreviation Conversion

| User says | SQL value |
|-----------|-----------|
| New York | `NY` |
| California | `CA` |
| Texas | `TX` |
| Florida | `FL` |
| Illinois | `IL` |
| Ohio | `OH` |
| Pennsylvania | `PA` |
| Georgia | `GA` |
| North Carolina | `NC` |
| New Jersey | `NJ` |
| Massachusetts | `MA` |
| Michigan | `MI` |
| Virginia | `VA` |
| Washington | `WA` |
| Colorado | `CO` |
| Tennessee | `TN` |
| Arizona | `AZ` |
| Minnesota | `MN` |
| Maryland | `MD` |
| Connecticut | `CT` |

### All Valid `state` Values

AK, AL, AR, AZ, CA, CO, CT, DC, DE, FL, GA, HI, IA, ID, IL, IN, KS, KY, LA,
MA, MD, ME, MI, MN, MO, MS, MT, NC, ND, NE, NH, NJ, NM, NV, NY, OH, OK, OR,
PA, RI, SC, SD, TN, TX, UT, VA, VT, WA, WI, WV, WY

### All Valid `region` Values

Florida, Great Lakes, Gulf Coast, Mid-Atlantic, Mid-South, Midwest, Mountain,
New England, New York, Northwest, South Atlantic, Southeast, Southwest, Texas North

### Mountain region — valid `state` values ONLY

When filtering **Mountain** (by `region = 'Mountain'` or equivalent), **ONLY** these states apply:

`AZ`, `CO`, `ID`, `MT`, `NM`, `NV`, `UT`, `WY`

**Midwest-only (never Mountain):** `KS`, `MO`, `IA` — do not include these in a Mountain state list.

### Valid `area` Values

East, West

### Geography-Only Query Rule

If the user asks to "list" or "show" HCPs filtered by **geography ONLY** (state, region, area, city):

- Do **NOT** apply any decile filter unless explicitly requested
- Do **NOT** apply any target flag filter unless explicitly requested
- Default sort: **ORDER BY total TRx DESC**
- **LIMIT rules (CRITICAL):**
  - IMPORTANT: Never add LIMIT to SQL unless user says top N, give me N, or limit to N. If user says show all or asks generally, write SQL with NO LIMIT clause.
- Only add decile/target filters if the user says **"top priority"**, **"targeted"**, **"decile X"**, etc.

---

## 6. Targeting & Decile Rules (CRITICAL)

### Valid Target Flag Values

Arcutis_Primary_Target
Kowa_Target
Arcutis_Non_Target

- "targeted HCPs" or "primary targets" → `q1_26_target_flag = 'Arcutis_Primary_Target'`
- "non-targets" → `q1_26_target_flag = 'Arcutis_Non_Target'`

### Decile priority (numeric meaning)

- **Decile 1** = HIGHEST priority (best targeting tier)
- **Decile 10** = LOWEST priority (worst targeting tier)

### "Best" vs "worst" HCPs (by targeting tier)

Use an explicit **tier filter** — not sort direction on the decile column:

| User intent | SQL filter (current period: `q1_26_decile`) |
|-------------|---------------------------------------------|
| **"best HCPs"** / **"highest priority"** (tier) | `WHERE q1_26_decile = 1` (or `IN (1)` if listing that bucket only) |
| **"worst HCPs"** / **"lowest priority"** (tier) | `WHERE q1_26_decile = 10` (or `IN (10)` for that bucket only) |

If the user asks for **multiple deciles** (e.g. "deciles 1–3"), use `WHERE q1_26_decile BETWEEN 1 AND 3` or `IN (1,2,3)` — still **not** determined by the words "ascending" / "descending".

### "Ascending" / "descending" → **metric** order only

The words **ascending** and **descending** in a user query apply to the **output metric being ranked** (e.g. TRx, call count, script count) — **NOT** to which decile group to return, and **NOT** as a substitute for `ORDER BY q1_26_decile ASC|DESC` to mean "best vs worst tier."

| User phrasing | Interpretation |
|---------------|----------------|
| "sort ascending" / "lowest to highest" **(on TRx or volume)** | `ORDER BY <metric> ASC` |
| "sort descending" / "highest to lowest" **(on TRx or volume)** | `ORDER BY <metric> DESC` |

**NEVER** use "ascending" or "descending" alone to decide **which decile values** to filter — only explicit wording like "decile 10", "worst tier", "best priority" does that.

---

## 7. Specialty Rules (CRITICAL)

- ALWAYS use `ILIKE` for specialty filters — values exist in UPPER and Title case
- Search BOTH columns unless user specifies one:
  `WHERE primary_specialty ILIKE '%X%' OR secondary_specialty ILIKE '%X%'`
- NEVER use AND across primary + secondary unless user explicitly says both
- `REGISTERED NURSE` appears only as secondary specialty — never primary
- When grouping: use `UPPER(primary_specialty)` to collapse case variants

### Top Primary Specialties (match with `ILIKE '%keyword%'`)

DERMATOLOGY              (14,882)
PHYSICIAN ASSISTANT      (8,862)
NURSE PRACTITIONER       (5,931)
FAMILY MEDICINE          (2,765)
INTERNAL MEDICINE        (1,983)
ALLERGY & IMMUNOLOGY     (1,627)
PEDIATRICS               (1,038)
RHEUMATOLOGY             (491)
DERMATOLOGIC SURGERY     (231)
DERMATOPATHOLOGY         (230)
PROCEDURAL DERMATOLOGY   (188)
PEDIATRIC DERMATOLOGY    (186)
OBSTETRICS & GYNECOLOGY  (109)
EMERGENCY MEDICINE       (103)
GENERAL PRACTICE         (93)
PODIATRIST               (77)
CARDIOVASCULAR DISEASE   (56)
PHARMACIST               (47)
HOSPITALIST              (43)
PLASTIC SURGERY          (42)
DENTIST                  (42)
GENERAL SURGERY          (36)
OPHTHALMOLOGY            (34)
GASTROENTEROLOGY         (33)
