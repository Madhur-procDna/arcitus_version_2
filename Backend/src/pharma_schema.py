"""Schema grounding helper for both workbook/SQLite and Postgres modes.

- Workbook mode (``SDA_DATA_SOURCE=sqlite``, default): returns Arcutis workbook table
  names and SQL hints for NL→SQL grounding.
- Postgres mode: returns Synthea-oriented table/relationship grounding.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Tuple

from langsmith_config import traceable


def _is_workbook_sqlite_mode() -> bool:
    """Workbook mode is default unless explicitly set to postgres."""
    return (os.getenv("SDA_DATA_SOURCE") or "sqlite").strip().lower() != "postgres"


# Arcutis workbook: single flat table — one row per HCP, all metrics included.
_WORKBOOK_TABLES: Tuple[str, ...] = (
    "Dummy_Data",
)

# Single-table dataset — no cross-table relationships.
_WORKBOOK_RELATIONSHIPS: Tuple[Tuple[str, str, str, str], ...] = ()


def erd_markdown_path() -> Path:
    """Default ERD path is ``src/ERD.md`` with optional env override."""
    override = (os.getenv("SDA_ERD_PATH") or os.getenv("ERD_PATH") or "").strip()
    if override:
        p = Path(override).expanduser()
        if not p.is_absolute():
            p = (Path(__file__).resolve().parent.parent / p).resolve()
        return p
    return Path(__file__).resolve().parent / "ERD.md"


@traceable(name="SDA | read_erd_markdown", run_type="tool")
def read_erd_markdown(max_chars: int | None = None) -> str:
    """Return ERD markdown for prompt grounding; empty string if missing."""
    p = erd_markdown_path()
    if not p.is_file() or p.stat().st_size == 0:
        return ""
    text = p.read_text(encoding="utf-8", errors="replace").strip()
    if max_chars is not None and len(text) > max_chars:
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


# ── Core tables from ERD.md — 18 Synthea tables (lowercase Postgres identifiers) ──────
_ERD_BASE_TABLES: Tuple[str, ...] = (
    # ── Reference / Master ──────────────────────────────────────────────────────
    "patients",
    "organizations",
    "providers",
    "payers",
    # ── Transactional Hub ───────────────────────────────────────────────────────
    "encounters",
    # ── Financial ───────────────────────────────────────────────────────────────
    "claims",
    "claims_transactions",
    # ── Clinical ────────────────────────────────────────────────────────────────
    "conditions",
    "medications",
    "observations",
    "procedures",
    "allergies",
    "immunizations",
    "careplans",
    "devices",
    "supplies",
    "imaging_studies",
    # ── Payer ───────────────────────────────────────────────────────────────────
    "payer_transitions",
)

# ── Views (not in _ERD_BASE_TABLES; listed separately for documentation) ───────────────
_ERD_VIEWS: Tuple[str, ...] = (
    "patient_expenses",
    "encounter_costs",
)

# ── FK edges — (child_table, fk_column, parent_table, parent_column) ───────────────────
# Covers all 39 FK constraints defined in ERD.md.
# Column names match the double-quoted identifiers in the DDL.
_ERD_FK_EDGES: Tuple[Tuple[str, str, str, str], ...] = (
    # ── providers ──────────────────────────────────────────────────────────────
    ("providers",           "ORGANIZATION",   "organizations",  "Id"),   # NOT NULL

    # ── encounters (central hub — 4 required FKs) ──────────────────────────────
    ("encounters",          "PATIENT",        "patients",       "Id"),   # NOT NULL
    ("encounters",          "ORGANIZATION",   "organizations",  "Id"),   # NOT NULL
    ("encounters",          "PROVIDER",       "providers",      "Id"),   # NOT NULL
    ("encounters",          "PAYER",          "payers",         "Id"),   # NOT NULL

    # ── claims (3 required + 3 nullable FKs) ───────────────────────────────────
    ("claims",              "PATIENT",        "patients",       "Id"),   # NOT NULL
    ("claims",              "PROVIDER",       "providers",      "Id"),   # NOT NULL
    ("claims",              "PRIMARYPAYER",   "payers",         "Id"),   # NOT NULL
    ("claims",              "SECONDARYPAYER", "payers",         "Id"),   # nullable
    ("claims",              "ENCOUNTER_ID",   "encounters",     "Id"),   # nullable — financial→clinical link
    ("claims",              "ORGANIZATIONID", "organizations",  "Id"),   # nullable

    # ── claims_transactions (2 required + 2 nullable FKs) ──────────────────────
    ("claims_transactions", "CLAIMID",        "claims",         "Id"),   # NOT NULL
    ("claims_transactions", "PATIENTID",      "patients",       "Id"),   # NOT NULL
    ("claims_transactions", "PLACEOFSERVICE", "organizations",  "Id"),   # nullable
    ("claims_transactions", "PROVIDERID",     "providers",      "Id"),   # nullable

    # ── conditions ─────────────────────────────────────────────────────────────
    ("conditions",          "PATIENT",        "patients",       "Id"),   # NOT NULL
    ("conditions",          "ENCOUNTER",      "encounters",     "Id"),   # NOT NULL

    # ── medications ────────────────────────────────────────────────────────────
    ("medications",         "PATIENT",        "patients",       "Id"),   # NOT NULL
    ("medications",         "PAYER",          "payers",         "Id"),   # NOT NULL
    ("medications",         "ENCOUNTER",      "encounters",     "Id"),   # NOT NULL

    # ── observations ───────────────────────────────────────────────────────────
    ("observations",        "PATIENT",        "patients",       "Id"),   # NOT NULL
    ("observations",        "ENCOUNTER",      "encounters",     "Id"),   # NOT NULL

    # ── procedures ─────────────────────────────────────────────────────────────
    ("procedures",          "PATIENT",        "patients",       "Id"),   # NOT NULL
    ("procedures",          "ENCOUNTER",      "encounters",     "Id"),   # NOT NULL

    # ── allergies ──────────────────────────────────────────────────────────────
    ("allergies",           "PATIENT",        "patients",       "Id"),   # NOT NULL
    ("allergies",           "ENCOUNTER",      "encounters",     "Id"),   # NOT NULL

    # ── immunizations ──────────────────────────────────────────────────────────
    ("immunizations",       "PATIENT",        "patients",       "Id"),   # NOT NULL
    ("immunizations",       "ENCOUNTER",      "encounters",     "Id"),   # NOT NULL

    # ── careplans ──────────────────────────────────────────────────────────────
    ("careplans",           "PATIENT",        "patients",       "Id"),   # NOT NULL
    ("careplans",           "ENCOUNTER",      "encounters",     "Id"),   # NOT NULL

    # ── devices ────────────────────────────────────────────────────────────────
    ("devices",             "PATIENT",        "patients",       "Id"),   # NOT NULL
    ("devices",             "ENCOUNTER",      "encounters",     "Id"),   # NOT NULL

    # ── supplies ───────────────────────────────────────────────────────────────
    ("supplies",            "PATIENT",        "patients",       "Id"),   # NOT NULL
    ("supplies",            "ENCOUNTER",      "encounters",     "Id"),   # NOT NULL

    # ── imaging_studies ────────────────────────────────────────────────────────
    ("imaging_studies",     "PATIENT",        "patients",       "Id"),   # NOT NULL
    ("imaging_studies",     "ENCOUNTER",      "encounters",     "Id"),   # NOT NULL

    # ── payer_transitions (1 required + 1 nullable FK to payers) ──────────────
    ("payer_transitions",   "PATIENT",        "patients",       "Id"),   # NOT NULL
    ("payer_transitions",   "PAYER",          "payers",         "Id"),   # NOT NULL
    ("payer_transitions",   "SECONDARY_PAYER","payers",         "Id"),   # nullable
)


def get_all_tables() -> List[str]:
    """Tables documented in ``ERD.md`` (schema-qualified for prompts)."""
    if _is_workbook_sqlite_mode():
        return list(_WORKBOOK_TABLES)
    schema = _schema_name()
    return [f"{schema}.{t}" for t in _ERD_BASE_TABLES]


def pharma_relationships() -> List[Dict[str, str]]:
    """All 39 FK join edges from ERD.md (child.column → parent.column)."""
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
    """Comprehensive table blurbs for schema RAG / prompt grounding; aligned to ERD.md."""
    if _is_workbook_sqlite_mode():
        docs: List[Dict[str, str]] = [
            {
                "id": "Dummy_Data",
                "table": "Dummy_Data",
                "text": (
                    "Primary and only table in the Arcutis workbook (~39,932 rows, one per HCP). "
                    "Identity: 'NPI ID' (integer PK), 'HCP Name', 'City', 'State', 'Zip', "
                    "'Primary Specialty', 'Secondary Specialty', 'HCO Name'. "
                    "Geography: 'Base Territory' (city+state e.g. 'Minneapolis, MN'), 'Region' (14 regions), 'Area' (East/West). "
                    "Targeting: \"Q1'26 Target Flag\" and \"Q4'25 Target Flag\" — values: "
                    "'Arcutis_Primary_Target', 'Arcutis_Non_Target', 'Kowa_Target'. "
                    "Deciles: \"Q1'26 Decile\", \"Q4'25 Decile\" (1–10; 10=highest). "
                    "Rep calls (quarterly): \"Q2'25 Calls\", \"Q3'25 Calls\", \"Q4'25 Calls\", \"Q1'26 Calls\". "
                    "ZORYVE TRx monthly (Jan 2025–Mar 2026, 15 cols): \"ZORYVE_Jan'25\" … \"ZORYVE_Mar'26\". "
                    "Competitor TRx (Other BNST, 15 cols): \"Other BNST_Jan'25\" … \"Other BNST_Mar'26\". "
                    "Total Class Size TCS (15 cols): \"TCS_Jan'25\" … \"TCS_Mar'26\". "
                    "ZORYVE Share = ZORYVE / TCS. NO JOINs needed — this is the only table. "
                    "ALWAYS double-quote column names with spaces or apostrophes."
                ),
            },
        ]
        return docs

    schema = _schema_name()

    def fq(table: str) -> str:
        return f"{schema}.{table}"

    blurbs: Dict[str, str] = {
        # ── Reference / Master ──────────────────────────────────────────────────
        "patients": (
            'Patient master record ("Id" UUID PK). '
            'Columns: "BIRTHDATE" (NOT NULL), "DEATHDATE" (nullable), "SSN" VARCHAR(11), '
            '"FIRST" VARCHAR(100), "LAST" VARCHAR(100), "RACE" VARCHAR(50), '
            '"ETHNICITY" VARCHAR(50), "GENDER" CHAR(1), "BIRTHPLACE" VARCHAR(255), '
            '"ADDRESS" VARCHAR(255), "CITY" VARCHAR(100), "STATE" VARCHAR(50), '
            '"ZIP" VARCHAR(10), "LAT" NUMERIC(9,6), "LON" NUMERIC(9,6), '
            '"HEALTHCARE_EXPENSES" NUMERIC(14,2) DEFAULT 0, '
            '"HEALTHCARE_COVERAGE" NUMERIC(14,2) DEFAULT 0. '
            'Root anchor for every clinical, financial, and payer table. '
            'Always join on patients."Id" = child."PATIENT" (or "PATIENTID").'
        ),
        "organizations": (
            'Healthcare facilities ("Id" UUID PK). '
            'Columns: "NAME" VARCHAR(255), "ADDRESS", "CITY", "STATE", "ZIP", '
            '"LAT" NUMERIC(9,6), "LON" NUMERIC(9,6), "PHONE" VARCHAR(20), '
            '"REVENUE" NUMERIC(14,2), "UTILIZATION" INTEGER DEFAULT 0. '
            'Referenced by providers ("ORGANIZATION"), encounters ("ORGANIZATION"), '
            'claims ("ORGANIZATIONID" nullable), and claims_transactions ("PLACEOFSERVICE" nullable). '
            'Join: organizations."Id" = providers."ORGANIZATION".'
        ),
        "providers": (
            'Healthcare providers / clinicians ("Id" UUID PK). '
            'Columns: "ORGANIZATION" UUID NOT NULL FK → organizations."Id", '
            '"NAME" VARCHAR(255), "GENDER" CHAR(1), "SPECIALITY" VARCHAR(100), '
            '"ADDRESS", "CITY", "STATE", "ZIP", "LAT" NUMERIC(9,6), "LON" NUMERIC(9,6), '
            '"UTILIZATION" INTEGER DEFAULT 0. '
            'Specialties include General Practice, Cardiology, Orthopedics, Neurology, '
            'Oncology, Pediatrics, Gynecology, Dermatology, Psychiatry, Radiology, '
            'Emergency Medicine. '
            'Join to organizations: providers."ORGANIZATION" = organizations."Id". '
            'Join to encounters: encounters."PROVIDER" = providers."Id".'
        ),
        "payers": (
            'Insurance payers ("Id" UUID PK). '
            'Columns: "NAME" VARCHAR(255), "ADDRESS", "CITY", "STATE_HEADQUARTERED" VARCHAR(50), '
            '"ZIP", "PHONE" VARCHAR(20), '
            '"AMOUNT_COVERED" NUMERIC(16,2), "AMOUNT_UNCOVERED" NUMERIC(16,2), '
            '"REVENUE" NUMERIC(16,2), '
            '"COVERED_ENCOUNTERS" INTEGER, "UNCOVERED_ENCOUNTERS" INTEGER, '
            '"COVERED_MEDICATIONS" INTEGER, "UNCOVERED_MEDICATIONS" INTEGER, '
            '"COVERED_PROCEDURES" INTEGER, "UNCOVERED_PROCEDURES" INTEGER, '
            '"COVERED_IMMUNIZATIONS" INTEGER, "UNCOVERED_IMMUNIZATIONS" INTEGER, '
            '"UNIQUE_CUSTOMERS" INTEGER, "QOLS_AVG" NUMERIC(6,4), "MEMBER_MONTHS" INTEGER. '
            'Payer names include Medicare, Medicaid, Blue Cross Blue Shield, Aetna, '
            'UnitedHealthcare, Cigna, Humana, Kaiser Permanente, Anthem. '
            'Referenced by encounters, medications, claims (PRIMARYPAYER + SECONDARYPAYER), '
            'and payer_transitions (PAYER + SECONDARY_PAYER).'
        ),

        # ── Transactional Hub ────────────────────────────────────────────────────
        "encounters": (
            'Central clinical event hub ("Id" UUID PK). '
            'Indexes: idx_enc_patient ("PATIENT"), idx_enc_payer ("PAYER"), idx_enc_start ("START"). '
            'Columns: "START" TIMESTAMP NOT NULL, "STOP" TIMESTAMP, '
            '"PATIENT" UUID NOT NULL FK → patients."Id", '
            '"ORGANIZATION" UUID NOT NULL FK → organizations."Id", '
            '"PROVIDER" UUID NOT NULL FK → providers."Id", '
            '"PAYER" UUID NOT NULL FK → payers."Id", '
            '"ENCOUNTERCLASS" VARCHAR(50) — ambulatory/inpatient/emergency/urgentcare/wellness/outpatient, '
            '"CODE" VARCHAR(20) SNOMED-CT, "DESCRIPTION" VARCHAR(255), '
            '"BASE_ENCOUNTER_COST" NUMERIC(10,2), "TOTAL_CLAIM_COST" NUMERIC(10,2), '
            '"PAYER_COVERAGE" NUMERIC(10,2), "REASONCODE" VARCHAR(20), "REASONDESCRIPTION" VARCHAR(255). '
            'Every clinical table (conditions, medications, observations, procedures, allergies, '
            'immunizations, careplans, devices, supplies, imaging_studies) has "ENCOUNTER" FK → encounters."Id". '
            'Financial chain: encounters → claims (via claims."ENCOUNTER_ID") → claims_transactions.'
        ),

        # ── Financial ────────────────────────────────────────────────────────────
        "claims": (
            'Insurance claim headers ("Id" UUID PK — enhanced Synthea). '
            'Indexes: idx_claims_patient ("PATIENT"), idx_claims_encounter ("ENCOUNTER_ID"). '
            'Required FKs: "PATIENT" → patients."Id", "PROVIDER" → providers."Id", '
            '"PRIMARYPAYER" → payers."Id". '
            'Nullable FKs: "SECONDARYPAYER" → payers."Id", '
            '"ENCOUNTER_ID" → encounters."Id" (key financial→clinical bridge), '
            '"ORGANIZATIONID" → organizations."Id". '
            'Non-FK UUIDs (no DB constraint): "REFERREDID", "SUPERVISINGID", '
            '"SERVICING_PROVIDER", "SUPERVISINGPROVIDERID". '
            'Diagnosis columns: "DIAGNOSIS1" through "DIAGNOSIS8" VARCHAR(20) ICD-10 codes. '
            'Other columns: "DEPARTMENT" INTEGER, "CLAIMID" VARCHAR(50) (external string reference), '
            '"CURRENTILLNESSDATE" DATE, "SERVICEDATE" DATE, '
            '"STATUS1"/"STATUS2" VARCHAR(20), "OUTSTANDING1"/"OUTSTANDING2" NUMERIC(10,2), '
            '"LASTBILLEDDATE1"/"LASTBILLEDDATE2" DATE, '
            '"HEALTHCARECLAIMTYPEID1"/"HEALTHCARECLAIMTYPEID2" INTEGER. '
            'Join to transactions: claims_transactions."CLAIMID" = claims."Id".'
        ),
        "claims_transactions": (
            'Individual billing line items per claim ("ID" UUID PK). '
            'Index: idx_ct_claim ("CLAIMID"). '
            'Required FKs: "CLAIMID" → claims."Id", "PATIENTID" → patients."Id". '
            'Nullable FKs: "PLACEOFSERVICE" → organizations."Id", "PROVIDERID" → providers."Id". '
            'Non-FK UUID: "SUPERVISINGPROVIDERID", "APPOINTMENTID", "PATIENTINSURANCEID". '
            'Key financial columns: "TYPE" VARCHAR(50) CHARGE/PAYMENT/ADJUSTMENT/TRANSFER, '
            '"AMOUNT" NUMERIC(10,2), "METHOD" VARCHAR(50) CASH/CHECK/CC/INSURANCE, '
            '"PAYMENTS" NUMERIC(10,2), "ADJUSTMENTS" NUMERIC(10,2), '
            '"TRANSFERS" NUMERIC(10,2), "OUTSTANDING" NUMERIC(10,2), '
            '"UNITAMOUNT" NUMERIC(10,2), "UNITS" INTEGER. '
            'Billing reference columns: "CHARGEID" INTEGER, "PROCEDURECODE" VARCHAR(20) CPT, '
            '"MODIFIER1"/"MODIFIER2" VARCHAR(10), '
            '"DIAGNOSISREF1"–"DIAGNOSISREF4" INTEGER (pointer to claims.DIAGNOSIS#), '
            '"DEPARTMENTID" INTEGER, "FEESCHEDULEID" INTEGER, "TRANSFEROUTID" INTEGER, '
            '"TRANSFERTYPE" VARCHAR(20), "FROMDATE" DATE, "TODATE" DATE, '
            '"NOTES" TEXT, "LINENOTE" TEXT.'
        ),

        # ── Clinical ─────────────────────────────────────────────────────────────
        "conditions": (
            'Active and resolved diagnoses (no surrogate PK — composite key: "PATIENT"+"START"+"CODE"). '
            'Indexes: idx_cond_patient ("PATIENT"), idx_cond_encounter ("ENCOUNTER"). '
            'Columns: "START" DATE NOT NULL, "STOP" DATE (nullable = active condition), '
            '"PATIENT" UUID NOT NULL FK → patients."Id", '
            '"ENCOUNTER" UUID NOT NULL FK → encounters."Id", '
            '"CODE" VARCHAR(20) SNOMED-CT, "DESCRIPTION" VARCHAR(255). '
            'Use STOP IS NULL to find currently active conditions. '
            'Common codes: 44054006 Diabetes type 2, 59621000 Hypertension, 195967001 Asthma. '
            'Oncology codes: 363346000 Malignant neoplastic disease, 254837009 Breast cancer, '
            '93880001 Lung cancer, 109838007 Colon cancer, 126906006 Prostate cancer, '
            '372064008 Bladder tumor, 188340000 Melanoma, 447886005 Leukemia, '
            '109989006 Multiple myeloma, 415068001 Pancreatic carcinoma. '
            'For "patients eligible for oncology clinical trial", filter CODE IN those oncology SNOMEDs AND "STOP" IS NULL (active); '
            'do NOT use a clinical_trial table — that table does not exist in this Synthea database.'
        ),
        "medications": (
            'Medication orders (no surrogate PK — composite key: "PATIENT"+"START"+"CODE"). '
            'Indexes: idx_med_patient ("PATIENT"), idx_med_encounter ("ENCOUNTER"). '
            'Columns: "START" DATE NOT NULL, "STOP" DATE, '
            '"PATIENT" UUID NOT NULL FK → patients."Id", '
            '"PAYER" UUID NOT NULL FK → payers."Id", '
            '"ENCOUNTER" UUID NOT NULL FK → encounters."Id", '
            '"CODE" VARCHAR(20) RxNorm, "DESCRIPTION" VARCHAR(255), '
            '"BASE_COST" NUMERIC(10,2), "PAYER_COVERAGE" NUMERIC(10,2), '
            '"DISPENSES" INTEGER, "TOTALCOST" NUMERIC(10,2), '
            '"REASONCODE" VARCHAR(20), "REASONDESCRIPTION" VARCHAR(255). '
            'Common drugs: Lisinopril, Metformin, Atorvastatin, Albuterol, Omeprazole. '
            'Use STOP IS NULL for currently active medications. '
            'Payer coverage analysis: SUM("PAYER_COVERAGE") vs SUM("TOTALCOST").'
        ),
        "observations": (
            'Clinical measurements and lab results (no surrogate PK — composite: "PATIENT"+"DATE"+"CODE"). '
            'Indexes: idx_obs_patient ("PATIENT"), idx_obs_encounter ("ENCOUNTER"). '
            'Columns: "DATE" DATE NOT NULL, '
            '"PATIENT" UUID NOT NULL FK → patients."Id", '
            '"ENCOUNTER" UUID NOT NULL FK → encounters."Id", '
            '"CATEGORY" VARCHAR(50) — vital-signs/laboratory/survey, '
            '"CODE" VARCHAR(20) LOINC, "DESCRIPTION" VARCHAR(255), '
            '"VALUE" VARCHAR(255), "UNITS" VARCHAR(50), '
            '"TYPE" VARCHAR(20) — numeric/text/date. '
            'Common LOINC codes: 8302-2 Body Height, 29463-7 Body Weight, 39156-5 BMI, '
            '8867-4 Heart rate, 8480-6 Systolic BP, 8462-4 Diastolic BP, '
            '2093-3 Cholesterol, 4548-4 HbA1c, 2339-0 Glucose. '
            'Cast "VALUE" to NUMERIC for numeric analysis: "VALUE"::NUMERIC.'
        ),
        "procedures": (
            'Clinical procedures performed (no surrogate PK — composite: "PATIENT"+"START"+"CODE"). '
            'Indexes: idx_proc_patient ("PATIENT"), idx_proc_encounter ("ENCOUNTER"). '
            'Columns: "START" TIMESTAMP NOT NULL, "STOP" TIMESTAMP, '
            '"PATIENT" UUID NOT NULL FK → patients."Id", '
            '"ENCOUNTER" UUID NOT NULL FK → encounters."Id", '
            '"CODE" VARCHAR(20) SNOMED-CT, "DESCRIPTION" VARCHAR(255), '
            '"BASE_COST" NUMERIC(10,2), '
            '"REASONCODE" VARCHAR(20), "REASONDESCRIPTION" VARCHAR(255). '
            'Common procedures: Medication Reconciliation, Depression screening, '
            'Colonoscopy, Screening mammography, Physical examination.'
        ),
        "allergies": (
            'Patient allergy and intolerance records (no surrogate PK — composite: "PATIENT"+"CODE"). '
            'Index: idx_allergy_patient ("PATIENT"). '
            'Columns: "START" DATE NOT NULL, "STOP" DATE, '
            '"PATIENT" UUID NOT NULL FK → patients."Id", '
            '"ENCOUNTER" UUID NOT NULL FK → encounters."Id", '
            '"CODE" VARCHAR(20) SNOMED-CT, "SYSTEM" VARCHAR(20) SNOMED-CT/RxNorm, '
            '"DESCRIPTION" VARCHAR(255), "TYPE" VARCHAR(50) allergy/intolerance, '
            '"CATEGORY" VARCHAR(50) medication/food/environment, '
            '"REACTION1" VARCHAR(20) SNOMED code, "DESCRIPTION1" VARCHAR(255) reaction description, '
            '"SEVERITY1" VARCHAR(20) MILD/MODERATE/SEVERE, '
            '"REACTION2" VARCHAR(20), "DESCRIPTION2" VARCHAR(255), "SEVERITY2" VARCHAR(20). '
            'Common allergens: Penicillin (medication), Peanuts (food), Latex (environment).'
        ),
        "immunizations": (
            'Vaccination records (no surrogate PK — composite: "PATIENT"+"DATE"+"CODE"). '
            'Index: idx_immun_patient ("PATIENT"). '
            'Columns: "DATE" DATE NOT NULL, '
            '"PATIENT" UUID NOT NULL FK → patients."Id", '
            '"ENCOUNTER" UUID NOT NULL FK → encounters."Id", '
            '"CODE" INTEGER CVX vaccine code, "DESCRIPTION" VARCHAR(255), '
            '"BASE_COST" NUMERIC(10,2). '
            'Common CVX codes: 140 Influenza, 115 Tdap, 21 Varicella, 20 DTaP.'
        ),
        "careplans": (
            'Care plan assignments ("Id" UUID PK). '
            'Columns: "START" DATE NOT NULL, "STOP" DATE, '
            '"PATIENT" UUID NOT NULL FK → patients."Id", '
            '"ENCOUNTER" UUID NOT NULL FK → encounters."Id", '
            '"CODE" VARCHAR(20) SNOMED-CT, "DESCRIPTION" VARCHAR(255), '
            '"REASONCODE" VARCHAR(20), "REASONDESCRIPTION" VARCHAR(255). '
            'Use STOP IS NULL for currently active care plans.'
        ),
        "devices": (
            'Medical device assignments (no surrogate PK — composite: "PATIENT"+"START"+"CODE"). '
            'Columns: "START" DATE NOT NULL, "STOP" DATE, '
            '"PATIENT" UUID NOT NULL FK → patients."Id", '
            '"ENCOUNTER" UUID NOT NULL FK → encounters."Id", '
            '"CODE" VARCHAR(20) SNOMED-CT, "DESCRIPTION" VARCHAR(255), '
            '"UDI" VARCHAR(100) FDA Unique Device Identifier. '
            'Use STOP IS NULL for currently implanted / active devices.'
        ),
        "supplies": (
            'Medical supply dispensing records (no surrogate PK — composite: "PATIENT"+"DATE"+"CODE"). '
            'Columns: "DATE" DATE NOT NULL, '
            '"PATIENT" UUID NOT NULL FK → patients."Id", '
            '"ENCOUNTER" UUID NOT NULL FK → encounters."Id", '
            '"CODE" VARCHAR(20) SNOMED-CT, "DESCRIPTION" VARCHAR(255), '
            '"QUANTITY" INTEGER. '
            'Includes consumables such as bandages, syringes, test strips.'
        ),
        "imaging_studies": (
            'Radiology / imaging study records ("Id" UUID PK). '
            'Columns: "DATE" DATE NOT NULL, '
            '"PATIENT" UUID NOT NULL FK → patients."Id", '
            '"ENCOUNTER" UUID NOT NULL FK → encounters."Id", '
            '"SERIES_UID" VARCHAR(100) DICOM Series UID, '
            '"BODYSITE_CODE" VARCHAR(20) SNOMED-CT, "BODYSITE_DESCRIPTION" VARCHAR(255), '
            '"MODALITY_CODE" VARCHAR(10) — XRAY/CT/MR/US, '
            '"MODALITY_DESCRIPTION" VARCHAR(100), '
            '"INSTANCE_UID" VARCHAR(100) DICOM Instance UID, '
            '"SOP_CODE" VARCHAR(50) DICOM SOP Class, "SOP_DESCRIPTION" VARCHAR(255), '
            '"PROCEDURECODE" VARCHAR(20) SNOMED-CT procedure. '
            'Filter by "MODALITY_CODE" for modality-specific studies.'
        ),

        # ── Payer ────────────────────────────────────────────────────────────────
        "payer_transitions": (
            'Payer enrollment history per patient (no surrogate PK — composite: "PATIENT"+"START_YEAR"+"PAYER"). '
            'Columns: "PATIENT" UUID NOT NULL FK → patients."Id", '
            '"MEMBERID" VARCHAR(50) payer member ID, '
            '"START_YEAR" INTEGER, "END_YEAR" INTEGER, '
            '"PAYER" UUID NOT NULL FK → payers."Id", '
            '"SECONDARY_PAYER" UUID nullable FK → payers."Id", '
            '"PLAN_OWNERSHIP" VARCHAR(20) — Self/Employer/Government/Medicare/Medicaid, '
            '"OWNER_NAME" VARCHAR(255). '
            'Use to reconstruct a patient\'s insurance history over time. '
            'Join primary payer: payer_transitions."PAYER" = payers."Id". '
            'Join secondary payer: payer_transitions."SECONDARY_PAYER" = payers."Id" (LEFT JOIN).'
        ),
    }

    docs: List[Dict[str, str]] = []
    for table in _ERD_BASE_TABLES:
        tq = fq(table)
        text = blurbs.get(table, f"Table {tq} — see ERD.md for full column reference.")
        docs.append({"id": tq, "table": tq, "text": f"Table: {tq} — {text}"})
    return docs


def pharma_docs_and_relationships() -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """Table blurbs + FK edges for schema RAG. Aligned to ERD.md (Synthea Enhanced Schema)."""
    return pharma_table_docs(), pharma_relationships()


