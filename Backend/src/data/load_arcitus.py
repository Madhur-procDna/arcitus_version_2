"""
Arcutis Data Ingestion Script
Ingests data from Arcutis_Data.xlsx into PostgreSQL (Arcutis_final_db)
Sheet: Articus_data | Rows: ~39,932
"""

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
import numpy as np

# ──────────────────────────────────────────────
# DB CONFIG — fill in host and password
# ──────────────────────────────────────────────
DB_CONFIG = {
    "dbname":   "Arcutis_final_db",
    "host":     "smartpp-dev.cm6vbnnsye5g.us-east-1.rds.amazonaws.com",          # <-- fill in
    "port":     5432,
    "user":     "postgres",
    "password": "Proc#1234",          # <-- fill in
}

EXCEL_PATH  = "Arcutis_Data.xlsx"
SHEET_NAME  = "Articus_data"
TABLE_NAME  = "arcutis_data"
BATCH_SIZE  = 500           # rows per INSERT batch


# ──────────────────────────────────────────────
# LOAD & CLEAN DATA
# ──────────────────────────────────────────────
def load_data(path: str) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name=SHEET_NAME, header=1)

    # Drop the unnamed index column (column A in Excel)
    df = df.drop(columns=["Unnamed: 0"], errors="ignore")

    # Sanitize column names → lowercase, spaces → underscores, strip special chars
    df.columns = (
        df.columns
        .str.strip()
        .str.lower()
        .str.replace(r"['\s/]", "_", regex=True)
        .str.replace(r"[^a-z0-9_]", "", regex=True)
        .str.replace(r"_+", "_", regex=True)
        .str.strip("_")
    )

    # Replace pandas NA / numpy NaN with None (PostgreSQL NULL)
    df = df.where(pd.notnull(df), None)

    print(f"✓ Loaded {len(df):,} rows × {len(df.columns)} columns")
    return df


# ──────────────────────────────────────────────
# CREATE TABLE
# ──────────────────────────────────────────────
def pg_type(series: pd.Series) -> str:
    """Map a pandas Series dtype to a PostgreSQL type."""
    if pd.api.types.is_integer_dtype(series):
        return "BIGINT"
    if pd.api.types.is_float_dtype(series):
        return "DOUBLE PRECISION"
    return "TEXT"


def create_table(conn, df: pd.DataFrame):
    col_defs = []
    for col in df.columns:
        col_defs.append(f'    "{col}" {pg_type(df[col])}')

    ddl = (
        f'DROP TABLE IF EXISTS "{TABLE_NAME}";\n'
        f'CREATE TABLE "{TABLE_NAME}" (\n'
        + ",\n".join(col_defs)
        + "\n);"
    )

    with conn.cursor() as cur:
        cur.execute(ddl)
    conn.commit()
    print(f"✓ Table '{TABLE_NAME}' created")


# ──────────────────────────────────────────────
# INGEST DATA IN BATCHES
# ──────────────────────────────────────────────
def ingest(conn, df: pd.DataFrame):
    columns = [f'"{c}"' for c in df.columns]
    insert_sql = f'INSERT INTO "{TABLE_NAME}" ({", ".join(columns)}) VALUES %s'

    total = len(df)
    inserted = 0

    with conn.cursor() as cur:
        for start in range(0, total, BATCH_SIZE):
            batch = df.iloc[start : start + BATCH_SIZE]

            # Convert each row to a plain Python tuple (None for NaN)
            rows = [
                tuple(
                    None if (v is None or (isinstance(v, float) and np.isnan(v)))
                    else v
                    for v in row
                )
                for row in batch.itertuples(index=False, name=None)
            ]

            execute_values(cur, insert_sql, rows)
            conn.commit()

            inserted += len(rows)
            pct = inserted / total * 100
            print(f"  Inserted {inserted:,}/{total:,} rows ({pct:.1f}%)", end="\r")

    print(f"\n✓ Ingestion complete — {inserted:,} rows written to '{TABLE_NAME}'")


# ──────────────────────────────────────────────
# VERIFY
# ──────────────────────────────────────────────
def verify(conn):
    with conn.cursor() as cur:
        cur.execute(f'SELECT COUNT(*) FROM "{TABLE_NAME}"')
        count = cur.fetchone()[0]
    print(f"✓ Verification: {count:,} rows in database")
    return count


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    if not DB_CONFIG["host"] or not DB_CONFIG["password"]:
        raise ValueError("Please fill in 'host' and 'password' in DB_CONFIG before running.")

    print("── Loading Excel data ──────────────────────")
    df = load_data(EXCEL_PATH)

    print("\n── Connecting to PostgreSQL ────────────────")
    conn = psycopg2.connect(**DB_CONFIG)
    print("✓ Connected")

    try:
        print("\n── Creating table ──────────────────────────")
        create_table(conn, df)

        print("\n── Ingesting data ──────────────────────────")
        ingest(conn, df)

        print("\n── Verifying ───────────────────────────────")
        verify(conn)

    finally:
        conn.close()
        print("✓ Connection closed")


if __name__ == "__main__":
    main()