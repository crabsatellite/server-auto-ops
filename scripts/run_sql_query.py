"""Run an ad-hoc SQL query on VPS. Usage: python run_sql_query.py <database> <query>"""
import sys
from ecs_control import get_status
from deploy_sql import run_sql

if len(sys.argv) < 3:
    print("Usage: python run_sql_query.py <database> <query>")
    sys.exit(1)

db = sys.argv[1]
query = sys.argv[2]

status = get_status()
if status != 'Running':
    print(f'VPS is {status}, cannot run query')
    sys.exit(1)

print(f'Database: {db}')
print(f'Query: {query}')
print('---')
result = run_sql(db, query=query, timeout=120)
print(result)
