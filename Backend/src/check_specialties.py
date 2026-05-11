import psycopg2

conn = psycopg2.connect(
    host='smartpp-dev.cm6vbnnsye5g.us-east-1.rds.amazonaws.com',
    port=5432, database='Arcutis_final_db',
    user='postgres', password='Proc#1234'
)
cur = conn.cursor()

sql = """
SELECT hcp_name, city, state, region, primary_specialty, total_2025_trx
FROM public.arcutis_data
WHERE (primary_specialty ILIKE '%nurse practitioner%' OR secondary_specialty ILIKE '%nurse practitioner%')
  AND (state = 'NY' OR region = 'New York')
ORDER BY total_2025_trx DESC NULLS LAST
LIMIT 10
"""
cur.execute(sql)
rows = cur.fetchall()
print(f'Rows found: {len(rows)}')
for r in rows:
    print(r)
conn.close()
