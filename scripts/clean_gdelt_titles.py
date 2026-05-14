"""Retroactively clean already-ingested GDELT titles.

GDELT items get their title synthesized from the URL slug because the
GKG feed doesn't carry article titles. Earlier versions of the synthesis
produced lowercase strings with trailing publisher tracking hashes like
"…defend country ef796d64". The new _clean_slug_title() helper strips
the hash and applies readable title-casing with acronym preservation.

This script walks every GDELT item, re-derives the title from the
stored URL, and updates the row if the new title differs. Doesn't
touch classification — just the display title.

Usage:
    python scripts/clean_gdelt_titles.py            # dry-run, samples 20
    python scripts/clean_gdelt_titles.py --apply    # actually update rows
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dalila import db
from dalila.ingestors.gdelt import _clean_slug_title


def _slug_from_url(url: str) -> str:
    """Mirror the slug extraction inside gdelt.py's _parse_slice."""
    if not url:
        return ""
    slug = url.split("/")[-1] or url
    for ext in (".html", ".htm", ".php", ".aspx", ".jsp"):
        if slug.lower().endswith(ext):
            slug = slug[: -len(ext)]
    return slug.replace("-", " ").replace("_", " ").strip()


def main(apply: bool) -> int:
    changes: list[tuple[int, str, str]] = []
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, title, url FROM items WHERE source_id = 'gdelt_v2'"
        ).fetchall()
        for r in rows:
            new_title = _clean_slug_title(_slug_from_url(r["url"] or ""))
            if new_title and new_title != (r["title"] or ""):
                changes.append((r["id"], r["title"] or "", new_title))

        print(f"Scanned {len(rows)} GDELT items.")
        print(f"Would update {len(changes)} titles.\n")
        for old_id, old, new in changes[:20]:
            print(f"  [{old_id}]")
            print(f"    old: {old}")
            print(f"    new: {new}")
        if len(changes) > 20:
            print(f"  …and {len(changes) - 20} more.")

        if not apply:
            print("\n(dry-run — pass --apply to commit)")
            return 0
        if not changes:
            print("Nothing to update.")
            return 0
        BATCH = 500
        for i in range(0, len(changes), BATCH):
            chunk = changes[i : i + BATCH]
            conn.executemany(
                "UPDATE items SET title = ? WHERE id = ?",
                [(new, rid) for rid, _, new in chunk],
            )
        conn.commit()
        print(f"\nUpdated {len(changes)} titles.")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true",
                   help="Actually update rows. Without this flag, dry-run only.")
    args = p.parse_args()
    sys.exit(main(apply=args.apply))
