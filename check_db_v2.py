import sqlite3

conn = sqlite3.connect('dalila.db')
conn.row_factory = sqlite3.Row

# Check total items
total_items = conn.execute('SELECT COUNT(*) as n FROM items').fetchone()['n']
classified = conn.execute('SELECT COUNT(*) as n FROM items WHERE classified_at IS NOT NULL').fetchone()['n']
with_country = conn.execute("SELECT COUNT(*) as n FROM items WHERE country_focus_json IS NOT NULL AND country_focus_json != '[]'").fetchone()['n']

print(f'Total items: {total_items}')
print(f'Classified: {classified}')
print(f'With country tags: {with_country}')
print()

# Check ingestion history
rows = conn.execute('''
    SELECT date(ingested_at) as day, COUNT(*) as n
    FROM items
    GROUP BY day
    ORDER BY day DESC
    LIMIT 20
''').fetchall()
print('Ingestion history (last 20 days):')
for r in rows:
    print(f'  {r["day"]}: {r["n"]} items')
print()

# Check digests
total_digests = conn.execute('SELECT COUNT(*) as n FROM digests').fetchone()['n']
digests = conn.execute('SELECT id, date_label, item_count, composed_at FROM digests ORDER BY id DESC LIMIT 15').fetchall()
print(f'Total digests in DB: {total_digests}')
print('Recent digests:')
for d in digests:
    print(f'  #{d["id"]} {d["date_label"]} - {d["item_count"]} items - {d["composed_at"]}')

print()
# Check earliest digest
earliest = conn.execute('SELECT id, date_label, item_count, composed_at FROM digests ORDER BY id ASC LIMIT 5').fetchall()
print('Earliest digests:')
for d in earliest:
    print(f'  #{d["id"]} {d["date_label"]} - {d["item_count"]} items - {d["composed_at"]}')

print()
# Check digests with item_count > 0 and item_ids_json has content
valid_digests = conn.execute("""
    SELECT COUNT(*) as n FROM digests
    WHERE item_count > 0
    AND item_ids_json IS NOT NULL
    AND item_ids_json != '[]'
""").fetchone()['n']
print(f'Valid digests (item_count>0 with IDs): {valid_digests}')

# check how many digests have actual items resolvable in items table
resolvable = 0
all_digests = conn.execute("SELECT id, date_label, item_ids_json FROM digests WHERE item_count > 0").fetchall()
import json
for d in all_digests:
    try:
        ids = json.loads(d['item_ids_json'] or '[]')
    except:
        ids = []
    if not ids:
        continue
    found = conn.execute(f"SELECT COUNT(*) as n FROM items WHERE id IN ({','.join(['?']*len(ids))})", ids).fetchone()['n']
    if found > 0:
        resolvable += 1

print(f'Digests with resolvable items in DB: {resolvable} / {len(all_digests)}')
conn.close()
