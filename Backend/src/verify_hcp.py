import psycopg2
conn = psycopg2.connect(host='smartpp-dev.cm6vbnnsye5g.us-east-1.rds.amazonaws.com', port=5432, database='Arcutis_final_db', user='postgres', password='Proc#1234')
cur = conn.cursor()
cur.execute("SELECT hcp_name, elevance_health FROM public.arcutis_data WHERE hcp_name ILIKE '%Kaitlynd Hanna%'")
print(cur.fetchone())
conn.close()
