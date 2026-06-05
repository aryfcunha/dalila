"""One-time retroactive title cleanup.

Applies the current clean_title() to every item in the DB, updates rows
where the title changed, then re-renders the digest HTML pages for any date
that had at least one affected item.

Run from the dalila/ directory:
  .venv/Scripts/python scripts/retroclean_titles.py
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from dalila import db
from dalila.db import clean_title
from dalila.simhash import simhash64, to_hex

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

OUT_DIR = Path(__file__).parents[1] / "docs"
DIGESTS_DIR = OUT_DIR / "digests"


def _slug_from_row(row) -> str:
    try:
        composed = datetime.fromisoformat(row["composed_at"].replace("Z", "+00:00"))
    except Exception:
        composed = datetime.now(timezone.utc)
    slug_dt = composed
    label = (row["date_label"] or "").strip()
    if label:
        try:
            slug_dt = datetime.strptime(label, "%A %d %B %Y").replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return slug_dt.strftime("%Y-%m-%d"), slug_dt


def main() -> None:
    db.init_db()

    # ── Step 1: apply clean_title to every item ──────────────────────────────
    log.info("Loading all items...")
    with db.connect() as conn:
        rows = conn.execute("SELECT id, title FROM items ORDER BY id").fetchall()
        log.info("  %d items to check", len(rows))

        updates: list[tuple[str, str, int]] = []
        for row in rows:
            old = row["title"] or ""
            new = clean_title(old)
            if new != old:
                updates.append((new, to_hex(simhash64(new)), row["id"]))

        log.info("  %d titles need updating", len(updates))
        changed_ids: set[int] = set()
        for new_title, new_sh, item_id in updates:
            conn.execute(
                "UPDATE items SET title = ?, title_simhash = ? WHERE id = ?",
                (new_title, new_sh, item_id),
            )
            changed_ids.add(item_id)
        conn.commit()

    if not changed_ids:
        log.info("No titles changed — nothing to re-render.")
        return

    # ── Step 2: find affected digests ────────────────────────────────────────
    log.info("Finding affected digest pages...")
    with db.connect() as conn:
        digest_rows = conn.execute(
            """SELECT id, composed_at, date_label, item_ids_json
               FROM digests
               WHERE id IN (SELECT MAX(id) FROM digests GROUP BY date_label)
               ORDER BY composed_at DESC"""
        ).fetchall()

        affected: list[tuple] = []
        for d in digest_rows:
            try:
                ids = set(int(x) for x in (json.loads(d["item_ids_json"] or "[]") or []))
            except Exception:
                continue
            if ids & changed_ids:
                affected.append(d)

    log.info("  %d digest pages to re-render", len(affected))

    # ── Step 3: re-render affected digest pages ───────────────────────────────
    from dalila.pipeline import _items_by_ids, _dedupe_by_simhash
    from dalila.html_digest import render_digest

    rendered = 0
    with db.connect() as conn:
        for d in affected:
            slug, slug_dt = _slug_from_row(d)
            dest = DIGESTS_DIR / f"{slug}.html"

            try:
                item_ids = [int(x) for x in (json.loads(d["item_ids_json"] or "[]") or [])]
            except Exception:
                continue

            items = _items_by_ids(conn, item_ids)
            if not items:
                continue

            items = _dedupe_by_simhash(items, threshold=16)
            total = db.count_reviewed_24h(conn, as_of=slug_dt) or len(items)
            html_str = render_digest(items, when=slug_dt, total_ingested=total)
            dest.write_text(html_str, encoding="utf-8")
            rendered += 1
            log.info("  re-rendered %s", slug)

    log.info("Done: %d items updated, %d digest pages re-rendered.", len(changed_ids), rendered)


if __name__ == "__main__":
    main()
