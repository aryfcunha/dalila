"""One-time retroactive title repair (2026-06-11).

Fixes three classes of bad titles that shipped in published digests:
  1. Garbage placeholders fabricated from URL ids — ".cms", "1396974", … :
     re-derive a real title from the URL's wordiest path segment
     (gdelt._slug_title); if the URL has no words, blank the title so the
     item can never render again.
  2. Bad casing the per-word title-caser produced before today's fix:
     "ABU Dhabi" -> "Abu Dhabi", "Rsf" -> "RSF", "Irc" -> "IRC",
     "Dr Congo" -> "DR Congo", "Un's" -> "UN's".
  3. Anything the current clean_title() now rejects (e.g. ".cms" husks).

Then re-renders the digest HTML page for every date that contained an
affected item (same mechanism as scripts/retroclean_titles.py — a sanctioned
one-off maintenance pass over the otherwise-immutable digest pages), skipping
blank-title items at render time.

Run from the dalila/ directory:
  .venv/bin/python scripts/retrofix_titles_20260611.py
"""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from dalila import db
from dalila.db import clean_title
from dalila.ingestors.gdelt import _slug_title
from dalila.simhash import simhash64, to_hex

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

OUT_DIR = Path(__file__).parents[1] / "docs"
DIGESTS_DIR = OUT_DIR / "digests"

# Word-boundary casing repairs for titles already stored capitalised (the
# live cleaner only re-cases titles that OPEN in lowercase, so these stuck).
_CASING_FIXES = (
    (re.compile(r"\bABU Dhabi\b"), "Abu Dhabi"),
    (re.compile(r"\bRsf\b"), "RSF"),
    (re.compile(r"\bIrc\b"), "IRC"),
    (re.compile(r"\bDr\.? Congo\b"), "DR Congo"),
    (re.compile(r"\bUn's\b"), "UN's"),
    (re.compile(r"\bUnrwa\b"), "UNRWA"),
    (re.compile(r"\bMsf\b"), "MSF"),
)

_GARBAGE_RE = re.compile(r"^[\W\d_]*$|^\.?(cms|ece|shtml|amp)$", re.IGNORECASE)


def _fix_title(old: str, url: str | None) -> str:
    t = (old or "").strip()
    # Garbage placeholder? Re-derive from the URL.
    if _GARBAGE_RE.match(t) or len(re.findall(r"[A-Za-z]{2,}", t)) < 2:
        return _slug_title(url or "")
    for pat, repl in _CASING_FIXES:
        t = pat.sub(repl, t)
    t = clean_title(t)
    return t


def _slug_from_row(row):
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

    log.info("Loading all items...")
    with db.connect() as conn:
        rows = conn.execute("SELECT id, title, url FROM items ORDER BY id").fetchall()
        log.info("  %d items to check", len(rows))

        changed_ids: set[int] = set()
        n_rederived = n_blanked = n_recased = 0
        for row in rows:
            old = row["title"] or ""
            new = _fix_title(old, row["url"])
            if new == old:
                continue
            if not new:
                n_blanked += 1
            elif _GARBAGE_RE.match(old.strip()) or len(re.findall(r"[A-Za-z]{2,}", old)) < 2:
                n_rederived += 1
            else:
                n_recased += 1
            conn.execute(
                "UPDATE items SET title = ?, title_simhash = ? WHERE id = ?",
                (new, to_hex(simhash64(new)) if new else None, row["id"]),
            )
            changed_ids.add(row["id"])
        conn.commit()
    log.info("  changed=%d (re-derived=%d, blanked=%d, re-cased=%d)",
             len(changed_ids), n_rederived, n_blanked, n_recased)

    if not changed_ids:
        log.info("No titles changed — nothing to re-render.")
        return

    log.info("Finding affected digest pages...")
    with db.connect() as conn:
        digest_rows = conn.execute(
            """SELECT id, composed_at, date_label, item_ids_json
               FROM digests
               WHERE id IN (SELECT MAX(id) FROM digests GROUP BY date_label)
               ORDER BY composed_at DESC"""
        ).fetchall()

        affected = []
        for d in digest_rows:
            try:
                ids = set(int(x) for x in (json.loads(d["item_ids_json"] or "[]") or []))
            except Exception:
                continue
            if ids & changed_ids:
                affected.append(d)
    log.info("  %d digest pages to re-render", len(affected))

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
            # Blank-titled items (irreparable junk) must not render.
            items = [i for i in items if (i.get("title") or "").strip()]
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
