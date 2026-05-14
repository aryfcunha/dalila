import sqlite3
import json
import os
import re
import yaml
from datetime import datetime
from pathlib import Path

# Connect to DB
conn = sqlite3.connect('c:/Users/pc14043/Documents/Dalila/dalila/dalila.db')
c = conn.cursor()

# 1. Retroactive "Opt:" removal
c.execute("SELECT id, title FROM items")
rows = c.fetchall()
updates_title = []
for r in rows:
    item_id, title = r
    new_title = re.sub(r'^Opt:\s*', '', title, flags=re.IGNORECASE)
    if new_title != title:
        updates_title.append((new_title, item_id))
if updates_title:
    c.executemany("UPDATE items SET title = ? WHERE id = ?", updates_title)
    print(f"Removed 'Opt:' from {len(updates_title)} titles.")

# 2. Re-populate digests table from HTML files
digests_dir = Path('c:/Users/pc14043/Documents/Dalila/dalila/docs/digests')
if digests_dir.exists():
    html_files = list(digests_dir.glob('*.html'))
    existing_digests = set(r[0].split('T')[0] for r in c.execute("SELECT composed_at FROM digests").fetchall())
    
    inserts_digest = []
    for f in html_files:
        date_str = f.stem # YYYY-MM-DD
        if date_str not in existing_digests:
            # We don't have the item IDs, but we can insert a dummy entry
            # so the archive page shows the date.
            # Use noon UTC for the time
            composed_at = f"{date_str}T12:00:00+00:00"
            inserts_digest.append((composed_at, "[]"))
    
    if inserts_digest:
        c.executemany("INSERT INTO digests (composed_at, item_ids_json) VALUES (?, ?)", inserts_digest)
        print(f"Re-instated {len(inserts_digest)} digests from HTML files.")

# 3. Backfill country_focus_json from entities_json
with open('c:/Users/pc14043/Documents/Dalila/dalila/countries.yaml', encoding='utf-8') as f:
    countries_data = yaml.safe_load(f)['countries']

name_to_code = {}
for code, data in countries_data.items():
    name_to_code[data['name'].lower()] = code
    for alias in data.get('aliases', []):
        name_to_code[alias.lower()] = code

c.execute("SELECT id, entities_json, country_focus_json FROM items WHERE entities_json IS NOT NULL AND entities_json != '[]'")
items = c.fetchall()
updates_country = []
for r in items:
    item_id, entities_json, current_focus = r
    try:
        entities = json.loads(entities_json)
        focus = set(json.loads(current_focus or "[]"))
        changed = False
        for ent in entities:
            name = ent['name'].lower()
            if name in name_to_code:
                code = name_to_code[name]
                if code not in focus:
                    focus.add(code)
                    changed = True
        if changed:
            updates_country.append((json.dumps(list(focus)), item_id))
    except:
        continue

if updates_country:
    c.executemany("UPDATE items SET country_focus_json = ? WHERE id = ?", updates_country)
    print(f"Backfilled country focus for {len(updates_country)} items.")

conn.commit()
conn.close()
