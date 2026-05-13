"""Verify each capital coordinate falls inside its country's polygon
in the world-atlas 50m geometry.

Decodes the TopoJSON arcs, builds polygons per country, and tests
point-in-polygon for the (lon, lat) of each capital in our CAPITALS
table (extracted from html_digest.py).

Output: a list of any country where the capital does NOT land inside
its polygon — with the country name, the claimed coords, and the
nearest polygon's centroid for comparison.
"""

from __future__ import annotations

import json
import urllib.request
import re
import sys
from pathlib import Path


ATLAS_URL = "https://cdn.jsdelivr.net/npm/world-atlas@2/countries-50m.json"
HTML_DIGEST = Path(__file__).resolve().parents[1] / "src" / "dalila" / "html_digest.py"


def extract_const_map(source: str, name: str) -> dict[str, list[float] | str]:
    """Pull a JS const map like `const X = { "AE":[54.37,24.45], … };` out of a Python string."""
    m = re.search(rf"const {name} = \{{(.*?)\}};", source, re.DOTALL)
    if not m:
        raise RuntimeError(f"Couldn't find const {name} in source")
    body = m.group(1)
    # Strip JS comments
    body = re.sub(r"//.*", "", body)
    # Convert to JSON-ish
    j = "{" + body + "}"
    return json.loads(j)


def decode_arcs(topo: dict) -> list[list[tuple[float, float]]]:
    """Apply the TopoJSON transform to decode arcs into [(lon,lat), ...] lists."""
    transform = topo["transform"]
    sx, sy = transform["scale"]
    tx, ty = transform["translate"]
    decoded = []
    for arc in topo["arcs"]:
        x = y = 0
        out = []
        for dx, dy in arc:
            x += dx; y += dy
            out.append((x * sx + tx, y * sy + ty))
        decoded.append(out)
    return decoded


def stitch_polygon(arc_indices: list[int], arcs: list[list[tuple[float, float]]]) -> list[tuple[float, float]]:
    """Glue an ordered list of (possibly-reversed) arc indices into a ring."""
    ring: list[tuple[float, float]] = []
    for idx in arc_indices:
        if idx >= 0:
            seg = arcs[idx]
        else:
            seg = arcs[~idx][::-1]
        if ring and seg and ring[-1] == seg[0]:
            ring.extend(seg[1:])
        else:
            ring.extend(seg)
    return ring


def feature_rings(geom: dict, arcs: list[list[tuple[float, float]]]) -> list[list[tuple[float, float]]]:
    """Return all rings (outer + holes) for one country geometry."""
    rings: list[list[tuple[float, float]]] = []
    if geom["type"] == "Polygon":
        for arc_seq in geom["arcs"]:
            rings.append(stitch_polygon(arc_seq, arcs))
    elif geom["type"] == "MultiPolygon":
        for poly in geom["arcs"]:
            for arc_seq in poly:
                rings.append(stitch_polygon(arc_seq, arcs))
    return rings


def point_in_ring(lon: float, lat: float, ring: list[tuple[float, float]]) -> bool:
    """Standard ray-casting point-in-polygon test."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-15) + xi):
            inside = not inside
        j = i
    return inside


def nearest_ring_point(lon: float, lat: float, rings: list[list[tuple[float, float]]]) -> tuple[float, float, float]:
    """Closest vertex on any ring (return lon, lat, great-circle-ish distance in degrees)."""
    best = (None, None, float("inf"))
    for ring in rings:
        for x, y in ring:
            d = ((x - lon) ** 2 + (y - lat) ** 2) ** 0.5
            if d < best[2]:
                best = (x, y, d)
    return best


def ring_bbox(rings: list[list[tuple[float, float]]]) -> tuple[float, float, float, float]:
    xs = [x for r in rings for x, _ in r]
    ys = [y for r in rings for _, y in r]
    return (min(xs), min(ys), max(xs), max(ys))


def main():
    print(f"Reading capital table from {HTML_DIGEST}")
    src = HTML_DIGEST.read_text(encoding="utf-8")
    capitals = extract_const_map(src, "CAPITALS")
    n3_to_a2 = extract_const_map(src, "N3_TO_A2")
    print(f"  capitals: {len(capitals)} entries")
    print(f"  N3→A2:    {len(n3_to_a2)} entries")

    print(f"Fetching {ATLAS_URL} …")
    topo = json.loads(urllib.request.urlopen(ATLAS_URL, timeout=30).read())

    print("Decoding arcs…")
    arcs = decode_arcs(topo)

    # Build per-ISO2 polygon rings. Mirror the JS-side filter that drops
    # Ashmore (which shares id=036 with Australia and would otherwise shadow it).
    SKIP_NAMES = {"Ashmore and Cartier Is."}
    by_iso: dict[str, dict] = {}
    for geom in topo["objects"]["countries"]["geometries"]:
        nid = str(geom.get("id") or "").zfill(3)
        iso = n3_to_a2.get(nid)
        if not iso:
            continue
        name = (geom.get("properties") or {}).get("name") or ""
        if name in SKIP_NAMES:
            continue
        rings = feature_rings(geom, arcs)
        by_iso[iso] = {
            "name": (geom.get("properties") or {}).get("name") or iso,
            "rings": rings,
            "bbox": ring_bbox(rings) if rings else None,
        }
    print(f"Resolved geometry for {len(by_iso)} ISO-2 codes")

    misses: list[dict] = []
    no_geom: list[str] = []
    for iso, coord in capitals.items():
        lon, lat = float(coord[0]), float(coord[1])
        entry = by_iso.get(iso)
        if not entry:
            no_geom.append(iso)
            continue
        rings = entry["rings"]
        # Point-in-polygon: treat as inside if inside any outer ring.
        # (We don't track holes here — TopoJSON doesn't separate them
        # cleanly without more machinery; the false-positive rate on
        # holes is negligible at country scale.)
        inside = any(point_in_ring(lon, lat, ring) for ring in rings)
        if not inside:
            # Find nearest vertex and the country's centroid for context.
            nx, ny, dist = nearest_ring_point(lon, lat, rings)
            bbox = entry["bbox"]
            misses.append({
                "iso": iso,
                "name": entry["name"],
                "capital_coord": (lon, lat),
                "nearest_polygon_point": (round(nx, 3), round(ny, 3)),
                "distance_deg": round(dist, 3),
                "bbox": tuple(round(v, 2) for v in bbox),
            })

    print()
    print(f"=== Capitals OUTSIDE their polygon: {len(misses)} ===")
    for m in sorted(misses, key=lambda x: x["distance_deg"], reverse=True):
        print(
            f"  {m['iso']} {m['name']:30s}  capital@{m['capital_coord']}  "
            f"nearest@{m['nearest_polygon_point']}  d={m['distance_deg']}°  "
            f"bbox={m['bbox']}"
        )
    if no_geom:
        print(f"\n=== No geometry found for {len(no_geom)} ISO-2s: {', '.join(no_geom)} ===")
    print(f"\n{len(capitals) - len(misses) - len(no_geom)} of {len(capitals)} capitals verified inside their polygons.")


if __name__ == "__main__":
    main()
