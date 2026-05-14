import sqlite3, json

conn = sqlite3.connect('dalila.db')
conn.row_factory = sqlite3.Row

# Full ingestion history
rows = conn.execute('''
    SELECT date(ingested_at) as day, COUNT(*) as n
    FROM items
    GROUP BY day
    ORDER BY day ASC
''').fetchall()
print('Full ingestion history:')
for r in rows:
    print(f"  {r['day']}: {r['n']} items")

print()
id_range = conn.execute('SELECT MIN(id) as mn, MAX(id) as mx, COUNT(*) as n FROM items').fetchone()
print(f"ID range: {id_range['mn']} to {id_range['mx']} ({id_range['n']} rows)")
print()

# Gap analysis
ids = [r[0] for r in conn.execute('SELECT id FROM items ORDER BY id').fetchall()]
if ids:
    expected = set(range(ids[0], ids[-1]+1))
    actual = set(ids)
    missing = expected - actual
    print(f'Missing IDs in range [{ids[0]}, {ids[-1]}]: {len(missing)} gaps')
    if ids[-1] - ids[0] < 30000:
        # sample to check for large contiguous deletions
        # find runs of missing
        missing_sorted = sorted(missing)
        if missing_sorted:
            run_start = missing_sorted[0]
            prev = missing_sorted[0]
            runs = []
            for m in missing_sorted[1:]:
                if m != prev + 1:
                    runs.append((run_start, prev))
                    run_start = m
                prev = m
            runs.append((run_start, prev))
            print(f'Missing runs ({len(runs)} total):')
            for s, e in runs[:20]:
                print(f'  {s}-{e} ({e-s+1} IDs)')

# Also check sources table to see how many sources have items
print()
srcs = conn.execute('''
    SELECT s.name, COUNT(i.id) as n
    FROM sources s
    LEFT JOIN items i ON i.source_id = s.id
    GROUP BY s.id
    ORDER BY n DESC
    LIMIT 20
''').fetchall()
print('Items per source (top 20):')
for s in srcs:
    print(f"  {s['name']}: {s['n']}")

conn.close()
