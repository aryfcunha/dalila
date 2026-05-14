import sqlite3
import datetime

conn = sqlite3.connect('dalila.db')
c = conn.execute('''
    SELECT published_at, title, source_id, uae_relevance, severity
    FROM items
    WHERE is_breaking_candidate = 1 AND published_at >= "2026-01-01"
    ORDER BY published_at ASC
''')
print("--- Breaking News Backtest Since Start of Year ---")
for row in c:
    print(f"[{row[0]}] {row[2]} (Rel: {row[3]}, Sev: {row[4]}) - {row[1]}")
