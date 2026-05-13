# Dalila — Calibration Methodology

Every numeric threshold, sampling rate, batch size, and model choice in this codebase encodes a decision. This document records each one, the rationale behind it, and where to change it. **Update this file when you tune a knob** — the goal is that a new collaborator (or future you) can audit "why is this number 25 and not 50?" without git-archaeology.

Organised by domain, ordered roughly by data-flow position (ingestion → prefilter → classify → dedupe → digest → cost).

---

## 1. Ingestion cadence and volume caps

| Parameter | Value | Rationale | File / Source |
|---|---|---|---|
| Live ingest interval | **30 min** | Balances news freshness against API politeness. Empirically, every-30m catches a 24h news cycle with ~98% timeliness; sub-15m yields diminishing returns and risks tripping Cloudflare / DataDome on RSS hosts. | `DALILA_INGEST_INTERVAL_MINUTES` env, default in `config.py` |
| GDELT live poll items/slice | **300** | GKG slices contain ~2-5k rows. We sample the most-recent 300 — enough that the prefilter (~80% drop) still surfaces 30-60 useful items/slice without bloating ingest time. | `GDELT_MAX_ITEMS_PER_POLL` in `gdelt.py` |
| GDELT backfill slice cadence | **60 min** (1 of 4 quarter-hour slices) | Full coverage would be 96 slices/day × 132 days = 12,672 zips for a Jan→May backfill. At 1/hour it's 3,168 — manageable in ~10 min at 8 workers. Lower (15m) for full coverage, higher (240m) for light sample. | `--gdelt-step` flag, default in `cli.py` |
| GDELT backfill items/slice cap | **60** | Down-samples each slice's rows so a single huge slice doesn't blow up the queue. | `items_per_slice` in `gdelt.iter_items_range` |
| GDELT backfill concurrency | **8** workers | Per-IP rate limits on `data.gdeltproject.org`; tested at 4/8/16 — 8 hits steady-state throughput without 429s. | `concurrency` in `gdelt.iter_items_range` |
| Sitemap backfill max URLs/source | **4000** | Per-source cap so WAM's ~10k-URL sitemap doesn't dominate a backfill. 4k × 3 sources ≈ 12k items, plenty for prefilter to chew on. Raise with `--max-per-source`. | `cli.py backfill --max-per-source` |
| Sitemap backfill concurrency | **12** workers | Higher than GDELT because each fetch is one HTML page (smaller than a zip), and Cloudflare/Arc tolerate ~10-15 parallel hits from a single IP before throttling. | `--concurrency` flag |
| Sitemap article inter-fetch sleep | **0s** (was 0.3s pre-parallelization) | When concurrent, the kernel's TCP/SSL setup overhead is the natural rate limiter; explicit sleep just under-utilises workers. | `article_sleep_s` in `sitemap.iter_items` |
| ACLED OAuth token TTL | **24h − 60s** safety margin | ACLED returns `expires_in: 86400`. We refresh 60s before expiry so a mid-request renewal can't happen. | `_get_access_token` in `acled.py` |
| ACLED priority countries | **16** | Curated to match `entities.yaml` priority list. Captures all current major conflict zones without bloating the API call. | `PRIORITY_COUNTRIES` in `acled.py` |

## 2. Prefilter

| Parameter | Value | Rationale | File / Source |
|---|---|---|---|
| Prefilter mechanism | **Keyword + entity substring match, case-insensitive** | Drops ~80% of global wire items before they hit a $-cost LLM call. Match against title+body. | `_prefilter_match` in `pipeline.py` |
| Source-level auto-pass tags | **`uae`, `state`, `entity`** | UAE state/entity sources are low-volume + high-signal by definition; filtering them statistically just throws out signal. | `run_ingest` in `pipeline.py` |
| Prefilter keyword list | **~25 terms** (UAE / humanitarian / displacement / donor / etc.) | Tuned 2026-05 to maximize recall of UAE-relevant items at ~80% global-wire drop rate. Each added keyword broadens recall but costs LLM calls. | `prefilter_keywords` in `sources.yaml` |
| Entity watchlist | **~250 aliases** across ~80 entities | Includes UAE leadership, institutions, peer foundations, multilateral bodies. Aliases catch transliteration variants. | `entities.yaml` |

## 3. Classification

| Parameter | Value | Rationale | File / Source |
|---|---|---|---|
| Live classifier model | **Haiku 4.5** (`claude-haiku-4-5`) via Claude CLI | Shares Pro/Max subscription rate pool with editor + doctrine. Pinning to one Haiku-tier model lets cache hit rate compound. | `llm.HAIKU` constant |
| Backfill classifier model | **DeepSeek v4-flash** (`deepseek-v4-flash`) via REST | ~1/20 the cost of Haiku-via-API, ~28s/batch vs Haiku CLI's 56s, no contention with shared subscription pool. **Opt-in only** via `--backend deepseek`. Day-to-day stays Haiku per CLAUDE.md invariant. | `dalila.deepseek.classify_batch` |
| Classifier batch size | **25** items | Empirically tuned on Linux/Windows + Claude Code 2.1: single-item ~33s; size-25/30 ~2.5s/item (13× speedup). Past 30, Haiku's output tokens scale linearly with batch size with no latency benefit. Below 10, CLI spawn overhead dominates. | `batch_size` in `cli.py classify` |
| DeepSeek classify workers (backfill) | **5** | Each call ~28s, output-token-bound. Five concurrent calls = ~5× throughput. DeepSeek's published rate limit is 1M tokens/min on chat — 5 × 5k output tokens = 25k tokens/min, well under. | `--workers` flag, recommended in `cli.py` |
| Daily classifier call cap | **2000** calls/day | Soft cost circuit-breaker. At 25 items/call = 50k items/day capacity. Live ingest's actual daily volume is ~200-500 items so this never triggers in normal operation; it exists to bound a runaway loop. | `DALILA_DAILY_CLASSIFIER_CALL_CAP` env |
| Editor model | **Haiku 4.5** | Originally specced Sonnet; pinned to Haiku for cache pool reasons. Reversible — change `model=llm.HAIKU` → `llm.SONNET` in `editor.compose_digest`. | `editor.py` |
| Deep-dive model | **Haiku 4.5** | Same pool reason. Deep-dives are user-triggered + rare; could justify Sonnet if quality complaints arise. | `editor.py` |
| Doctrine model | **Haiku 4.5** | Doctrine extraction runs every 15min only on items the classifier flagged for it; volume is 0-5 items/run typically. Cheap, fits in pool. | `doctrine.py` |
| Classifier system prompt | **Static** (instructions + entity watchlist, no per-call variables) | Stable system prompt is the precondition for Claude Code's prompt cache. Interpolating timestamps / request IDs would defeat caching and re-spend on every call. | `prompts/classifier.md` |

## 4. Deduplication

| Parameter | Value | Rationale | File / Source |
|---|---|---|---|
| URL-hash dedup | **`hash(url || title)`** UNIQUE constraint | Catches exact re-ingests. Title fallback handles RSS feeds that change the URL between polls. | `items.url_hash` schema |
| SimHash title threshold | **12 bits Hamming distance** on 64-bit hash | Empirically: cross-outlet reposts are ~6-10 bits apart, unrelated stories are ~28-40 bits apart. 12 is the sweet-spot middle. | `is_near_duplicate` in `simhash.py` |
| Cross-digest dedup window | **7 days** | An item used in Monday's brief shouldn't reappear in Friday's. Seven days is the half-life of a news cycle — story X mentioned a week later is fresh angle, not stale repost. | `dedupe_against_recent_digests` in `pipeline.run_compose_digest` |
| Forecast snapshot grain | **`(source, country, metric)`** unique | One row per data series. Allows a single forecast feed to track multiple metrics per country (e.g. HungerMap could track both food_insecure_count and price_inflation). | `forecast_snapshots` schema |

## 5. Forecast & alert thresholds (the "change-only" rule)

Each forecast/index source only emits an item when the metric crosses a threshold. The first-ever run for a source records baselines silently — no items. Subsequent runs surface deltas only.

| Source | Threshold | Rationale | Authority |
|---|---|---|---|
| **ACLED CAST** (conflict risk) | **Δ ≥ 1.0** on 0–10 scale | ACLED's own CAST methodology doc calls Δ≥1.0 "notable"; stable-country month-to-month noise is ~0.3, so 0.5 would over-trigger. | ACLED CAST methodology (2024) |
| **ACAPS INFORM** (crisis severity) | **Δ ≥ 0.5** on 0–5 scale | ACAPS tier boundaries (Very Low / Low / Medium / High / Very High) sit at 1/2/3/4. 0.5 = one tier-crossing's worth of movement. | ACAPS INFORM technical handbook |
| **GDACS** (disaster alerts) | **Any tier change** (Green ↔ Orange ↔ Red ↔ Extreme) | GDACS's own alert system is the tier transition — no useful numeric threshold exists. | GDACS doctrine |
| **WFP HungerMap LIVE** | **(≥10% relative AND ≥100k absolute) OR ≥500k absolute** | WFP's methodology flags ≥10% week-over-week as significant. The 100k floor avoids noise in small populations (10% of 50k = 5k is meaningless). The 500k absolute "always notable" catches large-population shifts that look small in percentage terms. | WFP HungerMap LIVE methodology |
| **New tracking entry** | Any first observation **after** baseline established | Fires only when a country newly enters a forecast feed (e.g. ACLED adds Country X to CAST coverage). Uses 🆕 emoji. **Never** fires on the first-ever run of a source — that's pure baseline. | `is_baseline_run` in `forecast.py` |

**Emoji semantics in the brief's `🔭 Foresight` section:**

| Emoji | Meaning |
|---|---|
| 🔴 | Metric worsened (↑ violence, ↑ hunger, ↑ severity, alert tier escalated) |
| 🟢 | Metric improved (↓ violence, ↓ hunger, ↓ severity, alert cleared) |
| 🆕 | Country newly entered tracking *after* baseline was already established for this source |
| 🟡 | Non-directional categorical change |

## 6. Digest assembly

| Parameter | Value | Rationale | File / Source |
|---|---|---|---|
| Daily digest send time | **06:30 GST** | Ary's preference; aligns with the start of his working day before the markets and meetings cycle. | `DALILA_DIGEST_TIME` env, default `06:30` |
| Digest minimum relevance score | **0.4** (uae_relevance ≥ 0.4) | Empirically, scores below 0.4 are about Sudan/Yemen/etc. without any UAE-specific angle; the brief is UAE-lensed so they don't earn a slot. | `min_relevance` in `pipeline.run_compose_digest` |
| Digest max items | **25** | Telegram message-length friendly (under ~4k chars). Editor prunes harder in practice; 25 is the ceiling, not the target. | `max_items` in `pipeline.run_compose_digest` |
| Foresight section cap | **Top 5 movers** by absolute delta | Keeps the section tight. If >5 movers exist, prefer those with `uae_relevance ≥ 0.4` first, then by magnitude. | `prompts/editor.md` rule 5a |
| Empty-digest fallback threshold | **<3 items** above relevance | Better to send "Quiet news cycle. N items reviewed, none above threshold." than a one-bullet brief. | `run_compose_digest` |
| Brief word target | **800–1500 words** | Read-on-phone-with-coffee length. Below 800 feels thin; above 1500 risks fatigue. | `prompts/editor.md` |

## 7. Rate limits & error handling

| Parameter | Value | Rationale | File / Source |
|---|---|---|---|
| Claude rate-limit backoff (fallback) | **30 min** | When the CLI error doesn't carry a parseable reset time, sleep 30m and retry. Long enough to clear most transient buckets without idling all day. | `_RATE_LIMIT_BACKOFF` in `scheduler.py` |
| Claude rate-limit exact-reset parsing | **regex on CLI stderr** | Parses "resets 5:30pm (Asia/Dubai)" → exact UTC datetime → sleeps precisely until then. | `parse_rate_limit_reset` in `pipeline.py` |
| DeepSeek HTTP retry semantics | **No auto-retry** (raises `LLMError`) | DeepSeek rate limits are generous; the right retry is the *next* tick of the scheduler, not an in-call loop. 429 / 5xx mapped to `"rate limit / transient"` so pipeline catches them. | `_call_chat` in `deepseek.py` |
| GDELT slice timeout | **25s read, 10s connect** | GKG zips are 1-3MB CDN-served; anything slower is effectively dead. Tight timeouts prevent worker starvation. | `iter_items_range` in `gdelt.py` |

## 8. Scheduler cadence (when `dalila bot` runs)

| Job | Cadence | Notes |
|---|---|---|
| ingest    | every `ingest_interval_minutes` (default 30m) | replace_existing |
| classify  | every 5m | pauses exactly until rate-limit window resets (parsed from CLI error) |
| doctrine  | every 15m | independent rate-limit back-off; cheap when queue is empty |
| digest    | daily at `digest_time` GST | also runs `classify(200)` first to flush backlog |
| **(planned)** CAST | once per month, day 5 | catches the first-week-of-month update window |
| **(planned)** INFORM | once per month | aligns with ACAPS publishing cadence |
| **(planned)** HungerMap | daily | granularity is daily; snapshot dedup makes that cheap |
| **(planned)** GDACS | every 30m | matches the live ingest pulse |

## 9. Cost envelope

| Lever | Designed cost | Why |
|---|---|---|
| Single Claude Pro/Max subscription | **$200/mo fixed** | All Haiku calls share this pool. No per-token API spend in the day-to-day path. |
| DeepSeek for backfill only | **<$5/backfill** | One-shot historical pulls only. 13k items × ~$0.0001/call ≈ $1.30. |
| SQLite single-file storage | **$0** | No Postgres, no Redis, no managed DB. |
| Free-tier GCP e2-micro VM | **$0/mo** | 24/7 host. Survives reboot via systemd. |
| External data | **$0** | Free sources only per CLAUDE.md invariant. No paid X / NewsAPI / Devex tier. |

---

## Editing this file

When you tune a knob:

1. **Change the value in code.**
2. **Update the row in this table** — value, rationale, and (if the rationale is new) the empirical evidence or doc citation that backs it.
3. **Commit both together** so `git log -- METHODOLOGY.md` is a chronological record of how the calibration evolved.

Don't add rows for *defaults you didn't choose* (e.g. Python's hash function, sqlite's row size). Only document the parameters where a different value would change behaviour and someone might reasonably want to change.
