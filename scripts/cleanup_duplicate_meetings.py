#!/usr/bin/env python
"""Optional prod cleanup: collapse duplicate bilateral_meetings rows.

CONTEXT: N outlets covering the same meeting create N near-identical rows. The
render path (db.recent_bilateral_meetings) already de-duplicates at DISPLAY time
as of the 2026-06-25 fix, so the website/bot look correct WITHOUT this script.
This is optional storage hygiene only — run it if you also want the table itself
deduplicated.

SAFE BY DEFAULT: prints a SELECT-style preview and changes NOTHING. It deletes
only when you pass --apply, and even then:
  - keeps the lowest id() per canonical key (the earliest-ingested row),
  - NEVER touches rows where BOTH principals are blank (unidentified = distinct),
  - runs in a single transaction you can inspect the count of first.

Canonical key matches the render dedup: normalized uae_principal +
foreign_principal + foreign_country + meeting DATE (when_iso[:10]); rows with a
null when_iso attach to the same-principals dated bucket if one exists.

USAGE:
    python scripts/cleanup_duplicate_meetings.py            # preview only
    python scripts/cleanup_duplicate_meetings.py --apply    # actually delete
    python scripts/cleanup_duplicate_meetings.py --db /path/to/dalila.db
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if SRC.exists():
    sys.path.insert(0, str(SRC))

from dalila import db  # noqa: E402  (reuse the exact same normalization)


def _key(row):
    uae = db._norm_principal(row["uae_principal"])
    foreign = db._norm_principal(row["foreign_principal"])
    if not uae and not foreign:
        return None  # blank-both → never merge
    return (uae, foreign, db._norm_principal(row["foreign_country"]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="Actually delete (default: preview only)")
    ap.add_argument("--db", default=None, help="Path to dalila.db (default: configured DALILA_DB_PATH)")
    args = ap.parse_args()
    if args.db:
        import os
        os.environ["DALILA_DB_PATH"] = args.db
        from dalila import config
        config.get_config.cache_clear()

    with db.connect() as conn:
        rows = conn.execute(
            """SELECT id, uae_principal, foreign_principal, foreign_country, when_iso
               FROM bilateral_meetings ORDER BY id ASC"""
        ).fetchall()

        # Group: dated rows by principals+day; null-date rows attach to the
        # principals' dated bucket if any, else their own.
        groups: dict = {}
        principals_to_key: dict = {}
        for r in rows:
            pk = _key(r)
            if pk is None:
                continue  # blank-both: leave alone
            day = (r["when_iso"] or "")[:10]
            if day:
                k = pk + (day,)
                principals_to_key.setdefault(pk, k)
            else:
                k = principals_to_key.get(pk) or (pk + ("",))
                principals_to_key.setdefault(pk, k)
            groups.setdefault(k, []).append(r["id"])

        dup_groups = {k: ids for k, ids in groups.items() if len(ids) > 1}
        to_delete = [i for ids in dup_groups.values() for i in sorted(ids)[1:]]  # keep MIN(id)

        total = len(rows)
        print(f"bilateral_meetings rows:        {total}")
        print(f"canonical meetings (deduped):   {len(groups) + sum(1 for r in rows if _key(r) is None)}")
        print(f"duplicate groups (>1 row):      {len(dup_groups)}")
        print(f"rows that WOULD be deleted:      {len(to_delete)}  (keeping earliest id per meeting)")
        print(f"blank-both rows (left untouched):{sum(1 for r in rows if _key(r) is None)}")

        # show a few example groups
        print("\nExample duplicate groups (up to 8):")
        for k, ids in list(dup_groups.items())[:8]:
            ex = conn.execute(
                "SELECT uae_principal, foreign_principal, foreign_country, when_iso "
                "FROM bilateral_meetings WHERE id = ?", (sorted(ids)[0],)
            ).fetchone()
            label = f"{ex['uae_principal']} <-> {ex['foreign_principal']} ({ex['foreign_country']}) {ex['when_iso'] or '(no date)'}"
            print(f"  x{len(ids):<3} {label}  -> keep id={sorted(ids)[0]}, delete {len(ids)-1}")

        if not args.apply:
            print("\nPREVIEW ONLY — nothing changed. Re-run with --apply to delete the above.")
            return 0

        if not to_delete:
            print("\nNothing to delete.")
            return 0

        placeholders = ",".join("?" * len(to_delete))
        cur = conn.execute(
            f"DELETE FROM bilateral_meetings WHERE id IN ({placeholders})", to_delete
        )
        conn.commit()
        print(f"\nDELETED {cur.rowcount} duplicate meeting rows. Kept one per meeting.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
