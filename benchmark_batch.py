"""Empirical batch-size benchmark.

Times classifier calls at different batch sizes on disjoint samples drawn from
the live unclassified queue. Reports wall-clock per item, per-call duration
breakdown, and success rate.

Usage:
    .venv/Scripts/python.exe benchmark_batch.py

Result is printed to stdout AND appended to benchmark_results.txt.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

from dalila import db
from dalila.pipeline import run_classify


# Tests to run: (label, batch_size, total_items)
TESTS = [
    ("size=1",  1,  5),    # baseline — no batching
    ("size=5",  5,  5),    # one batch of 5
    ("size=10", 10, 10),   # one batch of 10
    ("size=20", 20, 20),   # one batch of 20
    ("size=30", 30, 30),   # push the upper end
]


def queue_depth() -> int:
    with db.connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM items WHERE classified_at IS NULL AND prefilter_passed = 1"
        ).fetchone()["n"]


def llm_calls_in_window(start_iso: str, end_iso: str) -> list[sqlite3.Row]:
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT model, purpose, duration_ms, success, error
               FROM llm_call_log
               WHERE called_at >= ? AND called_at < ?
               ORDER BY called_at""",
            (start_iso, end_iso),
        ).fetchall()
    return list(rows)


def fmt_ms(ms: int | float) -> str:
    return f"{ms/1000:6.2f}s"


def run_one(label: str, batch_size: int, total: int) -> dict:
    """Run one timed classify. Returns {label, wall_s, items_classified, ...}."""
    from datetime import datetime, timezone

    print(f"\n--- {label:8s}  total={total:3d}  batch_size={batch_size} ---", flush=True)
    print(f"queue depth before: {queue_depth()}")

    start_iso = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    try:
        result = run_classify(limit=total, batch_size=batch_size)
    except Exception as exc:
        wall = time.monotonic() - t0
        print(f"  RAISED {type(exc).__name__}: {exc}")
        return {"label": label, "wall_s": wall, "result": None, "exc": str(exc)}

    wall = time.monotonic() - t0
    end_iso = datetime.now(timezone.utc).isoformat()
    print(f"  result: {result}")
    print(f"  wall:   {wall:.2f}s   per-item: {wall/max(1, result['classified']):.2f}s")

    calls = llm_calls_in_window(start_iso, end_iso)
    print(f"  llm calls in window: {len(calls)}")
    for c in calls:
        marker = " OK" if c["success"] else "ERR"
        err = (c["error"] or "")[:80]
        print(f"    [{marker}]  {fmt_ms(c['duration_ms'])}  {err}")

    return {
        "label": label,
        "batch_size": batch_size,
        "total_requested": total,
        "wall_s": wall,
        "result": result,
        "calls": [dict(r) for r in calls],
    }


def main() -> int:
    out_lines: list[str] = []
    def echo(s: str = ""):
        print(s, flush=True)
        out_lines.append(s)

    echo("=" * 70)
    echo("Dalila — batch-size benchmark")
    echo("=" * 70)
    echo(f"Queue depth at start: {queue_depth()}")

    rows = []
    for label, bsize, total in TESTS:
        out = run_one(label, bsize, total)
        rows.append(out)
        # If we got rate-limited mid-test, abort the rest
        if out.get("result") and out["result"].get("rate_limited"):
            echo("\nRATE LIMITED mid-benchmark — aborting remaining tests.")
            break

    echo("\n" + "=" * 70)
    echo("SUMMARY  (lower per-item-s is better)")
    echo("=" * 70)
    echo(f"{'test':10s}  {'classified':>10s}  {'wall (s)':>10s}  {'per-item (s)':>14s}  {'calls':>5s}  {'avg-call (s)':>14s}")
    for r in rows:
        if r.get("result") is None:
            echo(f"{r['label']:10s}  (raised: {r.get('exc','?')})")
            continue
        c = r["result"]["classified"]
        wall = r["wall_s"]
        per_item = wall / c if c else float("nan")
        calls = r["calls"]
        avg_call = sum(x["duration_ms"] for x in calls) / max(1, len(calls)) / 1000
        echo(f"{r['label']:10s}  {c:10d}  {wall:10.2f}  {per_item:14.2f}  {len(calls):5d}  {avg_call:14.2f}")

    # Append to file
    Path("benchmark_results.txt").write_text("\n".join(out_lines), encoding="utf-8")
    print("\nResults written to benchmark_results.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
