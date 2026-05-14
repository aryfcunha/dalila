"""For each country in CAPITALS, check whether the capital coord falls
inside the LARGEST polygon ring. If not, compute the centroid of the
largest ring and emit it as a corrected anchor.

This catches the Abu Dhabi problem — UAE is a MultiPolygon where the
capital sits on a small offshore island ring, while the visible
"mainland of UAE" is a different ring. Rendering the dot at the capital
coord makes it look "off" because it lands on a tiny island the user
doesn't perceive as UAE proper.

Output: a JS-ready dict literal of corrected anchors to paste into
ANCHORS_OVERRIDES in html_digest.py.
"""

from __future__ import annotations

import json
import math
import re
import sys
import urllib.request
from pathlib import Path

ATLAS_URL = "https://cdn.jsdelivr.net/npm/world-atlas@2/countries-50m.json"
HTML_DIGEST = Path(__file__).resolve().parents[1] / "src" / "dalila" / "html_digest.py"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_capitals import (
    decode_arcs, extract_const_map, feature_rings, point_in_ring,
)


def polygon_area(ring: list[tuple[float, float]]) -> float:
    """Shoelace formula, absolute area."""
    n, s = len(ring), 0.0
    for i in range(n):
        x1, y1 = ring[i]; x2, y2 = ring[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def polygon_centroid(ring: list[tuple[float, float]]) -> tuple[float, float]:
    """Area-weighted centroid (shoelace)."""
    n, a, cx, cy = len(ring), 0.0, 0.0, 0.0
    for i in range(n):
        x1, y1 = ring[i]; x2, y2 = ring[(i + 1) % n]
        cross = x1 * y2 - x2 * y1
        a += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    a /= 2
    if abs(a) < 1e-12:
        return ring[0]
    return cx / (6 * a), cy / (6 * a)


def main():
    src = HTML_DIGEST.read_text(encoding="utf-8")
    capitals = extract_const_map(src, "CAPITALS")
    n3_to_a2 = extract_const_map(src, "N3_TO_A2")

    topo = json.loads(urllib.request.urlopen(ATLAS_URL, timeout=30).read())
    arcs = decode_arcs(topo)

    by_iso: dict[str, dict] = {}
    SKIP = {"Ashmore and Cartier Is."}
    for g in topo["objects"]["countries"]["geometries"]:
        iso = n3_to_a2.get(str(g.get("id") or "").zfill(3))
        if not iso:
            continue
        name = (g.get("properties") or {}).get("name") or ""
        if name in SKIP:
            continue
        by_iso[iso] = {"name": name, "rings": feature_rings(g, arcs)}

    overrides: list[tuple[str, str, list[float], list[float], str]] = []
    capital_on_island = 0
    for iso, coord in capitals.items():
        entry = by_iso.get(iso)
        if not entry or not entry["rings"]:
            continue
        rings = entry["rings"]
        lon, lat = float(coord[0]), float(coord[1])
        # Largest ring by area
        largest = max(rings, key=polygon_area)
        cap_in_largest = point_in_ring(lon, lat, largest)
        if cap_in_largest:
            continue
        # Capital is in a smaller ring (or outside all rings — coastal slop).
        # Use the largest ring's centroid as the mainland anchor.
        cx, cy = polygon_centroid(largest)
        # Sanity: centroid should be inside the largest ring (convex-or-not).
        # If not, fall back to picking the largest ring's vertex closest to
        # the capital coord — guarantees on-land.
        if not point_in_ring(cx, cy, largest):
            cx, cy = min(largest, key=lambda p: (p[0] - lon) ** 2 + (p[1] - lat) ** 2)
        overrides.append((iso, entry["name"], [lon, lat], [round(cx, 2), round(cy, 2)], "→ mainland centroid"))
        capital_on_island += 1

    print(f"\n=== Capitals NOT inside largest polygon ring: {capital_on_island} ===")
    for iso, name, cap, anchor, note in sorted(overrides):
        print(f"  {iso} {name:30s}  capital={tuple(cap)}  anchor={tuple(anchor)}  {note}")

    # Emit JS literal — paste this into html_digest.py as ANCHORS_OVERRIDES.
    print()
    print("=== JS literal to paste as ANCHORS_OVERRIDES ===")
    print("  const ANCHORS_OVERRIDES = {")
    for iso, _, _, anchor, _ in sorted(overrides):
        print(f'    "{iso}":[{anchor[0]},{anchor[1]}],')
    print("  };")


if __name__ == "__main__":
    main()
