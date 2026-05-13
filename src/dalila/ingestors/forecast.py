"""Shared change-detection scaffold for forecast / index ingestors.

The contract: each forecast source (CAST, INFORM, HungerMap, GDACS) calls
`record_observation()` for every (country, metric) reading it gets back from
its upstream API. The helper compares against the last persisted snapshot
and returns a `ForecastChange` if the delta crosses the per-source
threshold — or None if the reading is unchanged / within noise.

The brief's "FORESIGHT" section is composed only from items that came out of
this helper, so by construction it never repeats "Sudan is still hungry"
month-over-month. Only actual movement reaches the user.

Each ingestor turns the returned ForecastChange into a RawItem with a
pre-formatted, emoji-tagged title like:
  "🔴 [CAST May 2026] Burkina Faso escalation risk 8.4/10 (↑ from 7.1)"
The classifier still runs on these items but the prefilter auto-passes
them because the source row is tagged `forecast` (high-signal source policy).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger(__name__)


@dataclass
class ForecastChange:
    """A meaningful movement on a tracked metric."""
    country_iso2: str
    metric_key: str
    value_now: float | None
    value_prev: float | None
    text_now: str | None         # for categorical metrics (e.g. GDACS 'red' / 'orange')
    text_prev: str | None
    direction: str               # 'up' | 'down' | 'new' | 'cleared' | 'change'
    delta: float | None          # value_now - value_prev (None for first-ever or categorical)
    observed_at: datetime
    notes: str | None = None

    @property
    def emoji(self) -> str:
        """🔴 = worsening, 🟢 = improving, ⚪ = new tracking entry without a baseline.

        For most humanitarian/conflict metrics, going UP is bad (more violence,
        more hunger, higher severity). Sources that invert this semantic
        (e.g. a "stability score" where up = good) should override at the
        ingestor level by flipping `direction` before passing the change to
        `format_title`.
        """
        if self.direction == "up":     return "🔴"
        if self.direction == "down":   return "🟢"
        if self.direction == "new":    return "⚪"
        if self.direction == "cleared":return "🟢"
        return "🟡"


def record_observation(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    country_iso2: str,
    metric_key: str,
    value: float | None = None,
    text: str | None = None,
    observed_at: datetime,
    threshold_abs: float | None = None,
    threshold_rel: float | None = None,
    threshold_text_change: bool = False,
    notes: str | None = None,
) -> ForecastChange | None:
    """Persist a new observation and return a ForecastChange iff it's meaningful.

    Returns None when:
      - This is the first-ever observation for (source, country, metric) AND
        the caller didn't ask for "new entries are interesting too" (i.e. they
        only care about movements, not baselines).
      - The numeric delta is below `threshold_abs` AND below `threshold_rel`
        (relative threshold expressed as a fraction, e.g. 0.05 = 5%).
      - For categorical metrics: the text is unchanged.

    Returns a ForecastChange when:
      - Numeric value moved by at least one of the thresholds.
      - Categorical value changed (only relevant when threshold_text_change=True).
      - This is the first observation AND threshold_abs/threshold_rel are not
        both set (callers like GDACS treat any first sighting as "new alert").

    Side effect: the snapshot row is upserted regardless of whether a change
    is returned — so future calls compare against the latest value.
    """
    iso = (country_iso2 or "").strip().upper()
    if len(iso) != 2 or not iso.isalpha():
        log.debug("forecast: skipping observation with bad iso=%r", country_iso2)
        return None

    row = conn.execute(
        "SELECT value_num, value_text, observed_at, notes FROM forecast_snapshots "
        "WHERE source_id=? AND country_iso2=? AND metric_key=?",
        (source_id, iso, metric_key),
    ).fetchone()

    prev_num = row["value_num"] if row else None
    prev_text = row["value_text"] if row else None
    is_first = row is None

    change: ForecastChange | None = None

    if value is not None:
        if is_first:
            # First sighting — only surface if caller didn't set both thresholds
            # (i.e. didn't say "I only care about movements").
            if threshold_abs is None and threshold_rel is None:
                change = ForecastChange(
                    country_iso2=iso, metric_key=metric_key,
                    value_now=value, value_prev=None,
                    text_now=text, text_prev=None,
                    direction="new", delta=None,
                    observed_at=observed_at, notes=notes,
                )
        else:
            delta = value - (prev_num or 0)
            abs_ok = threshold_abs is not None and abs(delta) >= threshold_abs
            rel_ok = (
                threshold_rel is not None
                and prev_num
                and abs(delta) / max(abs(prev_num), 1e-9) >= threshold_rel
            )
            if abs_ok or rel_ok:
                direction = "up" if delta > 0 else "down"
                change = ForecastChange(
                    country_iso2=iso, metric_key=metric_key,
                    value_now=value, value_prev=prev_num,
                    text_now=text, text_prev=prev_text,
                    direction=direction, delta=delta,
                    observed_at=observed_at, notes=notes,
                )
    elif threshold_text_change and text is not None and text != prev_text:
        direction = "change"
        if prev_text and not text:
            direction = "cleared"
        elif not prev_text and text:
            direction = "new"
        change = ForecastChange(
            country_iso2=iso, metric_key=metric_key,
            value_now=None, value_prev=None,
            text_now=text, text_prev=prev_text,
            direction=direction, delta=None,
            observed_at=observed_at, notes=notes,
        )

    # Always upsert the snapshot, regardless of whether the change is surfaced.
    conn.execute(
        """
        INSERT INTO forecast_snapshots(source_id, country_iso2, metric_key,
                                       value_num, value_text, observed_at, recorded_at, notes)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id, country_iso2, metric_key) DO UPDATE SET
            value_num   = excluded.value_num,
            value_text  = excluded.value_text,
            observed_at = excluded.observed_at,
            recorded_at = excluded.recorded_at,
            notes       = excluded.notes
        """,
        (source_id, iso, metric_key, value, text,
         observed_at.isoformat(), datetime.now(timezone.utc).isoformat(),
         notes),
    )
    return change


def format_title(
    change: ForecastChange,
    *,
    source_label: str,        # e.g. "CAST May 2026"
    country_name: str,
    metric_label: str,        # e.g. "escalation risk", "crisis severity"
    units: str = "",          # e.g. "/10", "%", "M people"
) -> str:
    """Build the human-readable, emoji-tagged item title.

    Examples (the produced strings):
      🔴 [CAST May 2026] Burkina Faso escalation risk 8.4/10 (↑ from 7.1)
      🟢 [INFORM May 2026] Somalia crisis severity 4.1/5 (↓ from 4.5)
      ⚪ [GDACS] New red alert: Cyclone Bahir — Mozambique
    """
    arrow = "↑" if change.direction == "up" else "↓" if change.direction == "down" else ""
    parts: list[str] = [change.emoji, f"[{source_label}]", country_name, metric_label]
    if change.value_now is not None:
        parts.append(f"{change.value_now:.1f}{units}")
        if change.value_prev is not None:
            parts.append(f"({arrow} from {change.value_prev:.1f})")
        elif change.direction == "new":
            parts.append("(new tracking entry)")
    elif change.text_now is not None:
        parts.append(change.text_now)
        if change.text_prev:
            parts.append(f"({arrow or '→'} from {change.text_prev})")
    return " ".join(p for p in parts if p)
