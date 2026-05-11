import sys
import json
from sql_agent import SQLAgent
from data_loader import get_db

agent = SQLAgent()
db = get_db()

history = [
  {"role": "user", "content": "Rank Regions and Territories by ZORYVE TRx growth from Q4'25 to Q1'26. Where are we accelerating vs stalling?"},
  {"role": "assistant", "content": "Across Q4 2025 to Q1 2026, ZORYVE prescription growth varied widely by region and territory..."}
]

q = "Find HCPs with high TCS volume but low ZORYVE share - these are switch opportunities. Rank by absolute TCS and show the top 7"

try:
  sql = agent._generate_sql(q, db, history=history)
  print("SUCCESS:")
  print(sql)
except Exception as e:
  print("ERROR:", e)
