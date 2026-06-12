import sqlite3
import os
db_path = os.path.expanduser('~/dalila/dalila.db')
conn = sqlite3.connect(db_path)
cursor = conn.cursor()
cursor.execute('SELECT COUNT(*) FROM items WHERE ingested_at >= ? AND ingested_at <= ?', ('2026-05-13T00:00:00+00:00', '2026-05-14T00:00:00+00:00'))
print(cursor.fetchone()[0])
