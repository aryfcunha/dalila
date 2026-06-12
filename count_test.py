import sqlite3
conn = sqlite3.connect('dalila.db')
cursor = conn.cursor()
cursor.execute('SELECT COUNT(*) FROM items WHERE ingested_at >= ? AND ingested_at <= ?', ('2026-05-13T02:30:00+00:00', '2026-05-14T02:30:00+00:00'))
print(cursor.fetchone()[0])
