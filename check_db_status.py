import sqlite3
import json

conn = sqlite3.connect('c:/Users/pc14043/Documents/Dalila/dalila/dalila.db')
c = conn.cursor()

# Check digests
c.execute("SELECT id, composed_at FROM digests")
digests = c.fetchall()
print(f"Digests in DB: {len(digests)}")
for d in digests:
    print(d)

# Check items with country focus
c.execute("SELECT id, country_focus_json FROM items WHERE country_focus_json IS NOT NULL AND country_focus_json != '[]' LIMIT 10")
items = c.fetchall()
print(f"Items with country focus (sample): {len(items)}")
for it in items:
    print(it)

# Check items with entities
c.execute("SELECT id, entities_json FROM items WHERE entities_json IS NOT NULL AND entities_json != '[]' LIMIT 10")
items_e = c.fetchall()
print(f"Items with entities (sample): {len(items_e)}")
for it in items_e:
    print(it)
