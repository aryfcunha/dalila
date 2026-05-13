"""Retroactively flag GDELT items from non-allowlisted outlets so they
stop reaching the digest and the archive.

Doesn't DELETE the items — that would discard the classification work
already paid for. Instead it flips `prefilter_passed` to 0, which means
they're excluded from digest composition (items_for_digest filters on
prefilter_passed=1) and from the country view (country_mention_counts
likewise).

Safe to re-run; only touches rows whose hostname is not in
trusted_outlets.yaml. Reports how many rows it flipped, broken down by
hostname so you can see whether anything important got caught up.

Usage:
    python scripts/prune_untrusted_outlets.py             # dry-run (default)
    python scripts/prune_untrusted_outlets.py --apply     # actually flip
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dalila import db
from dalila.ingestors.gdelt import _load_trusted_outlets, _is_trusted_url


def main(apply: bool) -> int:
    allowlist = _load_trusted_outlets()
    if not allowlist:
        print("Trusted-outlet allowlist is empty — refusing to prune.")
        return 2

    by_host: Counter[str] = Counter()
    to_flip: list[int] = []

    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, url FROM items
            WHERE source_id = 'gdelt_v2'
            AND prefilter_passed = 1
            """
        ).fetchall()
        for r in rows:
            url = (r["url"] or "").strip()
            if not url:
                continue
            if not _is_trusted_url(url, allowlist):
                host = (urlparse(url).hostname or "").lower()
                if host.startswith("www."):
                    host = host[4:]
                by_host[host] += 1
                to_flip.append(r["id"])

        print(f"Scanned {len(rows)} GDELT items.")
        print(f"Untrusted-outlet items currently passing prefilter: {len(to_flip)}\n")
        print("Top 30 hostnames that would be dropped:")
        for host, n in by_host.most_common(30):
            print(f"  {n:6d}  {host}")

        if not apply:
            print("\n(dry-run — pass --apply to flip prefilter_passed → 0)")
            return 0
        if not to_flip:
            print("\nNothing to do.")
            return 0

        # Batch the update
        BATCH = 500
        flipped = 0
        for i in range(0, len(to_flip), BATCH):
            chunk = to_flip[i : i + BATCH]
            placeholders = ",".join("?" * len(chunk))
            conn.execute(
                f"UPDATE items SET prefilter_passed = 0 WHERE id IN ({placeholders})",
                chunk,
            )
            flipped += len(chunk)
        conn.commit()
        print(f"\nFlipped prefilter_passed → 0 on {flipped} items.")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true",
                   help="Actually update rows. Without this flag, dry-run only.")
    args = p.parse_args()
    sys.exit(main(apply=args.apply))
