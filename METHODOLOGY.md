<!--
  This document is the source for the public Methodology page on the
  Dalila website (rendered into docs/methodology.html on every publish).

  Tone is for an external reader: a journalist, analyst, donor, or
  policymaker who wants to understand how Dalila is built and decide
  how much to trust it. Not for developers.

  When editing:
  - No file paths, environment variables, or code identifiers.
  - No model names from any specific vendor.
  - No mention of who built the brief or who reads it.
  - No engineering instructions, git workflow, or commit etiquette.
  - Numbers and thresholds stay (those are the substance).
  - When a calibration changes in code, update the rationale here too —
    the page should always reflect what is actually running.
-->

# Methodology

Dalila is built to skim a wide net of news and structured data, surface what is
relevant to the UAE&rsquo;s humanitarian and development agenda, and deliver one
tight morning brief. The judgments embedded in that pipeline &mdash; what to read,
what to skip, what counts as a real change in the world &mdash; are spelled out below.

## How news is collected

Dalila pulls from a curated set of free, public sources: state wire services,
UAE and Gulf press, global newswires, multilateral feeds (UN agencies and
development banks), donor agencies, specialist trade press, and structured
humanitarian and development databases. The full list lives on the About page.

| Decision | Value | Why |
|---|---|---|
| Polling cadence | **Every 30 minutes** | Catches a 24-hour news cycle with high timeliness; polling more aggressively yields diminishing returns and risks being throttled by anti-bot defences on publisher sites. |
| Real-time event feed sample | **300 items per slice** | Global event streams emit several thousand items at a time; sampling the most recent slice keeps coverage broad without overwhelming downstream analysis. |
| Historical backfill window | **Configurable per source** | Long-form archive pulls walk publisher sitemaps and event-data archives back to a chosen start date. Capped per source so any one publisher cannot dominate the corpus. |
| Conflict-data priority countries | **16** | A curated list covering all current major armed-conflict zones. Tightening the country filter keeps the structured event feed coherent with the rest of the brief. |

## What gets filtered out before reading

Most news on the global wire is not about humanitarian or development affairs.
A keyword-and-entity match runs against every item before any further analysis,
dropping roughly four out of every five. The remaining items reach the classifier.

| Decision | Value | Why |
|---|---|---|
| Pre-read filter | **Keyword and entity match** | Case-insensitive substring scan against the headline and excerpt. About 25 topic keywords (humanitarian, displacement, aid, donor, climate finance, named conflict zones) plus an entity watchlist. |
| Entity watchlist | **~250 names and aliases** | UAE leadership and institutions, peer foundations, multilateral bodies. Aliases cover common transliteration variants. |
| Source-level pass-through | **UAE state, entity, and press sources** | These have low daily volume and high signal density. Filtering them statistically discards real news, so they bypass the filter entirely. |

## How items are read and categorised

Surviving items go through a language model that assigns a category, a
UAE-relevance score from 0 to 1, a severity score, and structured tags
(entities mentioned, country focus, doctrine alignment, financial commitments,
bilateral meetings, and capital-instrument signals). Items are classified in
batches, with the same instructions and entity watchlist sent on every call so
that prompt caching engages efficiently.

| Decision | Value | Why |
|---|---|---|
| Categories | **Eight** | Humanitarian, aid commitments, reports and evidence, conferences and events, UAE foreign-policy signals, UAE leadership doctrine, UAE ecosystem moves, other. Designed to be mutually exclusive and to map onto the sections in the brief. |
| Batch size | **25 items per call** | Empirically the sweet spot for cost and throughput. Single-item calls waste overhead; larger batches do not improve quality and slow each call linearly. |
| Daily volume ceiling | **2,000 calls** | A soft circuit-breaker. Far above normal demand; exists so a runaway loop cannot escalate cost without manual intervention. |
| Instructions stability | **Identical on every call** | A static prompt is the precondition for the language model&rsquo;s internal caching. Any per-call variation (timestamps, IDs) would invalidate the cache and re-spend on every item. |

## Avoiding duplicates

The same story often appears multiple times &mdash; reposted across outlets, syndicated
from a wire, or carried by an aggregator. Three deduplication layers run before
anything reaches the brief.

| Decision | Value | Why |
|---|---|---|
| Exact-duplicate guard | **Hash of URL plus title** | Catches the same article re-ingested through different paths. |
| Cross-outlet near-duplicate detection | **Title similarity within 12 bits** of a 64-bit fingerprint | Cross-outlet reposts of the same story typically differ by 6&ndash;10 bits in this fingerprint; unrelated stories typically differ by 28&ndash;40. Twelve sits in the sweet spot. |
| Cross-brief window | **Seven days** | An item used in Monday&rsquo;s brief should not reappear in Friday&rsquo;s. A week is roughly the half-life of a news cycle; a story discussed seven days later is usually a fresh angle, not a repost. |

## Forecast and early-warning indices

Alongside the news feeds, Dalila tracks forecast and severity indices from
established humanitarian-data producers. These appear in the brief&rsquo;s
**Foresight** section, under a strict rule: only changes are surfaced, never
states. A country that has been in crisis for months produces no item; a country
whose situation has measurably shifted does. On the first observation of any new
data series, a baseline is recorded silently so future readings have a comparison
point.

| Source | What it measures | Change threshold | Authority |
|---|---|---|---|
| **ACLED CAST** | Monthly conflict-escalation risk score, 0&ndash;10 | Movement of one point or more between observations | ACLED&rsquo;s own methodology defines one-point shifts as &ldquo;notable&rdquo;; stable-country month-over-month noise sits around 0.3. |
| **ACAPS INFORM** | Monthly crisis-severity score, 0&ndash;5 | Movement of half a point or more | The five named severity tiers sit at integer boundaries. Half a point is one tier-crossing&rsquo;s worth of movement. |
| **GDACS** | Real-time disaster alerts, Green / Orange / Red / Extreme | Any tier change, plus new alerts | The alert system is itself categorical; a tier transition is the meaningful event. |
| **WFP HungerMap LIVE** | People with insufficient food consumption, daily | Either a 10% shift backed by at least 100,000 people, or any absolute shift of 500,000 or more | The relative threshold flags real changes; the floor avoids noise in small populations; the absolute trigger catches large-population shifts that look small in percentage terms. |

When a metric does move, the brief reports the direction with a visual
indicator.

| Indicator | Meaning |
|---|---|
| 🔴 | Worsening: more violence, more hunger, higher severity, or an alert tier escalating. |
| 🟢 | Improving: lower numbers, or an alert clearing. |
| 🆕 | A country newly entering this tracking series after the baseline was already established. |
| 🟡 | A non-directional categorical change. |

## How the morning brief is assembled

Once items are read, classified, and deduplicated, the editor composes the
daily brief from the previous 24&nbsp;hours of qualifying content. Items below a
relevance threshold are excluded; section ordering is fixed; the foresight
section is built only from observations that crossed a change threshold.

| Decision | Value | Why |
|---|---|---|
| Delivery time | **06:30 Gulf Standard Time** | Reaches readers before the working day&rsquo;s first meeting. |
| Minimum relevance to qualify | **0.4 on the UAE-relevance scale** | Below this, items are about humanitarian topics but lack a UAE-specific angle. The brief is UAE-lensed, so they do not earn a slot. |
| Items per brief | **Up to 25** | A ceiling, not a target. The editor prunes harder in practice. |
| Foresight section cap | **Top 5 movers** by magnitude of change | Foresight items earn the spotlight only when their movement is genuinely large; surplus changes are dropped. |
| Empty-brief fallback | **&lt; 3 items above threshold** | Rather than publish a thin brief, Dalila says: &ldquo;Quiet news cycle. *N* items reviewed, none above threshold.&rdquo; |
| Target length | **800&ndash;1,500 words** | Long enough to give context, short enough to read with a coffee. |

## A note on what is *not* automated

Dalila&rsquo;s judgment is encoded in the rules above and in the entity watchlist
behind the prefilter; the language model classifying items, and the editor
composing the brief, operate strictly within those rules. There is no live
editorial discretion between ingestion and delivery: every brief is reproducible
from the same inputs and the same calibration. When a number or rule on this
page changes, the brief reflects it from the next cycle onward.
