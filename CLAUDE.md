# CLAUDE.md — orientation for Claude Code working in this repo

You are an LLM coding agent. This file is the handoff brief. Read it before making changes. It encodes load-bearing decisions other sessions made and the rationale for them. If a section here contradicts a quick observation from the code, the doc is probably right (the code might have been mid-refactor) — but check, and update this file when the truth changes.

## What Dalila is

A single-user (initially) personal AI agent that delivers a daily 800–1500 word brief on humanitarian / development / philanthropy news, with a UAE-specific lens, over Telegram every morning at 06:30 GST. Designed to live within the cost envelope of a single Claude Pro/Max subscription.

A companion **static website** (`docs/`) regenerates from the live DB on demand and hosts: today's brief, the full archive (with progressive "Load more"), an interactive world map of country mentions and co-mentions, a public methodology page, and an about/sources page.

The full concept lives in `../dalila-concept-note.md` and `../dalila-sources.md` (the latter scopes which sources are in/out). The UAE Foreign Aid Policy 2026 (used to derive doctrine_topics.yaml) lives at `~/Documents/Foreign Aid Policy/` outside this repo and is internal-only — never commit it or extracts of it (the .gitignore blocks accidents).

## Architecture in one screen

```
  sources.yaml  ─►  ingestors/ ─►  prefilter ─►  items table (sqlite)
  trusted_outlets   (rss, gdelt,    (keyword +     │
                     acled, scrape,  entity match,  │
                     sitemap, cast,  ~80% drop +    │
                     iati, fts,      GDELT outlet   │
                     idmc, gmail)    allowlist)     ▼
                                            ┌─── classifier ──────────┐
                                            │  category, entities,    │
                                 ┌──────────│  uae_relevance,         │
                                 │          │  severity, country,     │
                                 │          │  doctrine_relation,     │
                                 │          │  policy_sector,         │
                                 │          │  financial_commitments, │
                                 │          │  bilateral_meetings,    │
                                 │          │  capital_signals        │
                                 │          │  (batch=25, Haiku/CLI   │
                                 │          │   live + DeepSeek/REST  │
                                 │          │   opt-in for backfill)  │
                                 │          └─────────────────────────┘
                                 ▼                     │
                  ┌─── editor (Haiku) ─────────┐       ▼
                  │ daily digest 06:30 GST     │  ┌─── doctrine (Haiku) ───┐
                  │ + /more deep-dives         │  │ doctrine_facts table:  │
                  │ + Foresight section        │  │ topic, position,       │
                  │   (🔴/🟢/🆕/🟡 deltas only)│  │ evolution_log,         │
                  └────────────────┬───────────┘  │ confidence; every 15m  │
                                   │              │ on flagged items       │
                                   ▼              └────────────────────────┘
                      ┌─── outputs ─────────────────────────────┐
                      │ Telegram broadcast (every /start chat)  │
                      │ +                                       │
                      │ static website regenerate on demand:    │
                      │   /index   /archive   /countries        │
                      │   /methodology   /about   /digests/...  │
                      └─────────────────────────────────────────┘
```

Forecast indices live on a parallel scaffold:

```
  forecast/index sources (CAST shipped; INFORM/HungerMap/GDACS planned)
        │
        ▼
  ingestor reads upstream → for each (source, country, metric):
        record_observation(value)
        │
        ├── first ever sighting → silent baseline (no item)
        ├── value within threshold → silent (record only)
        └── value crossed threshold → emit RawItem with 🔴/🟢/🆕/🟡 title
                │
                ▼ same downstream path as news items (prefilter auto-pass
                  via "entity" tag, classifier, editor's Foresight section)
```

Storage: one SQLite file (`dalila.db`). Scheduling: APScheduler in-process. No Redis, no Postgres, no message queue — by design. The cost lever and complexity ceiling are both held by "every routine LLM call is a `claude` CLI subprocess".

## Critical invariants — DO NOT VIOLATE

- **Day-to-day LLM access is via the `claude` CLI, NOT the Anthropic API SDK.** Do not add `anthropic` to dependencies. Do not write `client.messages.create(...)`. The user is on a Pro/Max plan; per-token API spend on the live path defeats the architecture.
- **DeepSeek has three roles; Claude is still the default for live calls.** `src/dalila/deepseek.py` is the explicit *backfill backend* (`--backend deepseek` on `classify` / `backfill-digests`). On the live path `llm._run_claude` also reaches for DeepSeek in two situations:
    1. *Manual override* — `DALILA_LLM_BACKEND=deepseek` routes **every** live call to DeepSeek (use when the Claude subscription is disabled/unavailable).
    2. *Automatic fallback* (added 2026-06-25) — on a Claude **capacity error** (quota / rate-limit / overload, matched by `llm._FALLBACK_SIGNALS`) the call transparently retries on DeepSeek so the daily brief still ships. Gated by `DALILA_DEEPSEEK_FALLBACK` (default **ON** when `DEEPSEEK_API_KEY` is set; `=0` forces Claude-only). A shared module-level cooldown (`DALILA_DEEPSEEK_FALLBACK_COOLDOWN_MINUTES`, default 30) routes straight to DeepSeek after a capacity error instead of re-spawning a throttled CLI every tick. **Only capacity-class errors fall back** — auth / bad-request / prompt errors still raise `LLMError` so they stay visible, and the scheduler's rate-limit back-off still engages when the fallback is disabled.

  Don't make DeepSeek the *default* live backend without conversation — it's the safety net, not the primary.
- **Free sources only by default.** No paid X API, no NewsAPI subscription, no Devex paid tier. Ask before adding a paid source. ACLED is free for non-commercial use (requires OAuth registration); CAST piggybacks on those same creds.
- **Models for live path**: Haiku 4.5 (`llm.HAIKU`) for classifier, editor, deep-dive, and doctrine. Originally specced Sonnet for the editor; pinned to Haiku-only to share one model/cache/rate-limit pool. To upgrade the editor back to Sonnet, change `model=llm.HAIKU` to `model=llm.SONNET` in `editor.py::compose_digest`.
- **Classifier system prompt is `instructions + entity watchlist`, sent identically every call.** This lets Claude Code's internal cache engage. Don't interpolate timestamps, request IDs, or per-call variables into the system prompt — that defeats caching.
- **Prefilter before classifying.** `pipeline._prefilter_match` drops ~80% of items based on keyword + entity match. Do not bypass it. UAE state/entity sources auto-pass (low volume, high signal — filtering doesn't help). Forecast sources (`tags: [forecast, ..., entity]`) also auto-pass.
- **GDELT outlet allowlist is non-negotiable.** GDELT indexes the whole news web. `trusted_outlets.yaml` keeps roughly 185 vetted publishers; without it, regional papers swamp the queue. Both `fetch()` and `_parse_slice()` in `gdelt.py` apply the filter via `_is_trusted_url(...)`.
- **Forecast change-only rule.** Every forecast/index ingestor uses `forecast.record_observation(...)` — only emits items when the metric moves past a per-source threshold. First ever run of any new forecast source is *silent baseline establishment*, no items emitted. Honor this; the public methodology page promises it.
- **Migrations are forward-only, numbered, idempotent.** Drop new files as `migrations/00X_*.sql`; `db.init_db()` applies any not in `schema_migrations`. Never edit a migration that's been deployed.
- **Doctrine topic slugs are kebab-case.** The validator in `doctrine.py` enforces `^[a-z][a-z0-9-]{1,48}$`. Bad slugs are rejected as noop. Prefer slugs from `doctrine_topics.yaml`; only invent new ones for genuinely novel positions.
- **METHODOLOGY.md is public.** It's rendered into `docs/methodology.html` by `render_methodology()` on every publish. Tone is for external readers (journalists, analysts, donors) — no file paths, no model names, no engineering-process language. There's a maintainer comment at the top of the file (stripped by the renderer) detailing the contract. Numerical thresholds and rules stay; they're the substance.

## File ownership (who edits what)

| To change… | Edit |
|---|---|
| Add a news source | `sources.yaml` |
| Add a forecast/index source | `sources.yaml` + new ingestor in `src/dalila/ingestors/` + register `kind` in `ingestors/base.py` + use `forecast.record_observation(...)` for the change rule |
| Curate GDELT outlets | `trusted_outlets.yaml` (suffix-matching, one bare domain per line) |
| Add a new entity to track | `entities.yaml` |
| Add a doctrine topic to the seed vocabulary | `doctrine_topics.yaml` |
| Change classifier behaviour | `prompts/classifier.md` |
| Change digest format / Foresight rules | `prompts/editor.md` |
| Change deep-dive synthesis style | `prompts/deepdive.md` |
| Change doctrine extraction rules | `prompts/doctrine.md` |
| Change the public methodology page | `METHODOLOGY.md` (preserve maintainer-comment block at top) |
| Add a CLI subcommand | `src/dalila/cli.py` |
| Add a Telegram command | `src/dalila/bot.py` (also `USER_MENU_COMMANDS` if user-facing) |
| Add a new ingestor kind | new file in `src/dalila/ingestors/` + dispatch in `ingestors/base.py` |
| Change static-site rendering | `src/dalila/html_digest.py` |
| Schema change | new `migrations/00X_*.sql` — runs automatically via `db.init_db()` |
| Near-duplicate threshold | `is_near_duplicate(threshold=…)` in `src/dalila/simhash.py` |
| Forecast change thresholds | constants in the per-source ingestor (e.g. `CAST_DELTA_THRESHOLD` in `cast.py`) — also update `METHODOLOGY.md` |
| Rate-limit fallback duration | `_RATE_LIMIT_BACKOFF` in `src/dalila/scheduler.py` |
| Bot display name / `/` menu | `BOT_DISPLAY_NAME` and `USER_MENU_COMMANDS` in `src/dalila/bot.py` |

## What was built when (recent shipping log)

- **Prediction-market history backfill** (2026-06-25): `dalila backfill-markets [--days N] [--source manifold|kalshi] [--max-markets N]` recovers the 2026-05-14→06-25 history gap (the url-column crash, fixed by migration 007, froze `prediction_market_history`) so 30m/24h/1w deltas populate immediately instead of re-accumulating over a week. Pulls FREE, no-auth historical series for markets already in `prediction_market_snapshots`: Manifold via `/v0/bets?contractId=…` (`probAfter`+`createdTime`, paged newest-first via `before=`; slug→contractId resolved first), Kalshi via `/series/{s}/markets/{ticker}/candlesticks?period_interval=60` (`*_dollars` close; series = ticker prefix before first `-`). In `ingestors/prediction_markets.py` (`backfill_market_history` + `_backfill_manifold_market`/`_backfill_kalshi_market`/`_kalshi_candle_prob`). Idempotent (skips existing `(market_id, source, recorded_at)`); deltas stored NULL, recomputed on read. Live-verified from the UAE host. This is historical-gap recovery only — the ongoing crash was already fixed by 007.
- **Countries-page publish hang fix** (2026-06-25): the countries render looped per-country (~190×) calling `items_for_country` + `country_cooccurrence`, each a full scan + per-row `json.loads` of the ~300k-row items window — ~2N+2 passes. At scale this hung `run_publish_site`, and because the bot (`scheduler._publish_site_hook`) and cron both call it **in-process**, the hang wedged the publisher and silently staled the whole site. Fix: `db.country_aggregates` does it in **one** pass (verified ~265× faster, output identical on current data); the legacy four functions stay for the `/country` bot command + tests. Migration `008_country_index.sql` adds a minor partial index. **Resilience:** countries now renders LAST and **out-of-process** via a new `dalila publish-countries` subcommand invoked with a hard `DALILA_COUNTRIES_TIMEOUT_SECS` (default 180s) timeout — a subprocess so a future hang is OS-killable (a thread would leak on the 1GB VM); `DALILA_PUBLISH_SKIP_COUNTRIES=1` is an operator kill-switch. Markets render moved before countries (it's cheap, items-table-independent). Also hardened `db.init_db()` to treat "duplicate column / already exists" migration errors as already-applied (the un-recorded 007 `ADD COLUMN url` otherwise makes init_db raise on every call, bricking bot boot + publish + all CLI).
- **Original MVP**: bot + scheduler + ingestors + classifier + editor.
- **Cross-platform CLI launch, batched classifier, smart rate-limit back-off**: early hardening.
- **`verify-sources`**: canary that probes every enabled source.
- **SimHash dedup**: 64-bit title hash, threshold 12. `schema_migrations` introduced.
- **/more deep-dive**: bot command + `prompts/deepdive.md`.
- **Doctrine tracker**: `doctrine_facts` table, processing flag, 35-topic seed vocabulary.
- **Bot identity + `/` menu + read-only /digest**: `Dalila | دليلة`, `set_my_commands`.
- **GCP free-tier deploy kit**: `deploy/install-on-vm.sh`, systemd template.
- **Policy-aligned classification** (migration 004): `policy_sector`, `country_focus`, `capital_signals`, `graduation_signal`, `financial_commitments`, `bilateral_meetings`.
- **Static website**: `render_index/archive/digest/countries/about/methodology` + masthead + footer + `run_publish_site()`. Country view started as a tile cartogram, evolved into a D3 + topojson Natural Earth world map.
- **Historical backfill**: `sitemap.py` walker (WAM monthly sitemap, The National Arc sitemap-index, Gulf News sitemap), GDELT and ACLED date-range iterators, `dalila backfill --since YYYY-MM-DD` CLI.
- **DeepSeek backend** (`deepseek.py`): opt-in REST classifier for cost-bounded backfill; same `classify_batch` signature as the Claude path so it's a drop-in. Concurrent workers supported via `--workers N`.
- **ACLED OAuth migration**: ACLED moved to OAuth 2.0 in 2026 (username + password → 24h Bearer token). `_get_access_token` in `acled.py` handles it with in-process caching and thread safety.
- **Forecast scaffold** (migration 005, `forecast.py`): `forecast_snapshots` table keyed by `(source, country, metric)`; `record_observation()` returns a `ForecastChange` only when the value moves past a threshold; `is_baseline_run()` for first-run silence.
- **ACLED CAST ingestor**: monthly conflict-escalation forecast, 0–10 scale, Δ ≥ 1.0 threshold. Re-uses ACLED OAuth.
- **METHODOLOGY.md + methodology page**: public-facing methodology rendered into `docs/methodology.html` by the inline `_md_to_html` converter (strips HTML comments so the maintainer-guidance block at the top doesn't leak).
- **GDELT outlet allowlist** (`trusted_outlets.yaml`): ~185 publishers; one-line edits, suffix-matched. `scripts/prune_untrusted_outlets.py` for retroactive cleanup.
- **Country-view world map fix**: countries rendered as Natural-Earth-projected SVG paths with sqrt-scaled amber heatmap. Co-mention arcs anchored at projected capital coordinates (CAPITALS table inline in the JS). Architectural bug fix: arcs + dots + labels render INSIDE the main map SVG (not a separate overlay) so they share the exact same coordinate system — a previous separate-overlay design had a 48px vertical mismatch.
- **Archive progressive disclosure**: 20 briefs visible, "Load more" reveals next 20. Server-side renders the full list under `.hidden` class so it remains crawlable.
- **Publish-site dedup**: when multiple digests exist for the same `date_label` (e.g. an old pre-prune brief and a fresh post-prune one), the most recent by id wins. Archive cap bumped 60 → 365.
- **Daily Digest Scheduling Bug Fix & Background Ingestion Hardening** (2026-05-18):
    - Resolved a critical scheduling flaw where `_ingest_job` and `_markets_job` had `next_run_time=None` passed to the scheduler, pausing background polling indefinitely.
    - Fixed the resulting race condition where the bot's daily digest ran at `02:30 UTC` before the system cron job (`02:40 UTC`), which historically led to empty digests and skipped broadcasts on weekends.
    - Removed `next_run_time=None` to ensure active background polling and classification runs continuously 24/7, keeping the 24-hour brief window consistently populated.
- **Prediction Market Expansion & Automation** (2026-05-15):
    - Added **30-minute probability deltas** for real-time volatility tracking.
    - Expanded indicator dashboard to **9 items** with standardized grid heights.
    - Restored/Re-instated the **"Build" page** (`build.html`) in masthead/footer.
    - Integrated **Market Signals into Telegram brief** (Markdown composer fix).
    - Hardened automation: `dalila run-pipeline` command + VM cron adjusted to 02:40 UTC (06:40 GST).
- **Restart-resilient scheduling + retroactive backfill + daily publish** (2026-06-22):
    - **Root cause of missing daily briefs**: the `AsyncIOScheduler` was built with no `job_defaults`, so APScheduler's default `misfire_grace_time` of **1 second** applied. The digest is one daily cron at 06:30 GST; under systemd `Restart=always` any restart straddling that instant by >1s made APScheduler silently skip the day's digest forever. Fixed via `job_defaults` (`coalesce=True, max_instances=1, misfire_grace_time=300`) in `cli.py`, a **6h misfire grace + coalesce** on the digest cron, and a one-shot **startup catch-up** (`scheduler.run_digest_if_missing`) ~90s after boot that composes today's brief if it's past digest_time and missing (idempotent via `db.digest_exists_for_label`, never double-broadcasts).
    - **Root cause of bursty/irregular market-signal commits**: the bot pushed to `origin/main` but never pulled — only the `sync_site.sh` cron rebases — so once origin advanced, the bot's pushes were rejected (non-fast-forward) and piled up locally. Fixed with a shared `pipeline._git_commit_and_push` that pulls `--rebase --autostash` and retries once on rejection; used by both the market-signal and publish-site push paths.
    - **Retroactive backlog fill**: `pipeline.run_publish_backfilled_pages()` writes `docs/digests/YYYY-MM-DD.html` for any persisted digest whose page is **missing** on disk (it never rewrites an existing page, so the immutability invariant still holds — this is the one sanctioned way to add *new* past pages). `run_backfill_digests(..., only_missing=True)` skips days that already have a brief (no LLM call) while still feeding their item_ids into the dedup set. `pipeline.run_backfill_and_publish()` ties compose → write-pages → regenerate-site → push together and is shared by the CLI and the bot. CLI: **`dalila backfill-digests --only-missing --publish --days N`** composes the gaps, writes their pages, regenerates index/archive, and (when `DALILA_SITE_GIT_PUSH=1`) commits + pushes — the one command to recover a backlog on the VM.
    - **Automatic boot backfill**: env `DALILA_BACKFILL_DAYS_ON_BOOT` (config field `backfill_days_on_boot`, default 0=off). When > 0, the `boot_backfill` scheduler job (one-shot ~2 min after start) runs `run_backfill_and_publish(only_missing=True)` so a freshly deployed/restarted VM fills its backlog onto the website **without broadcasting** the historical briefs to Telegram. Idempotent across restarts — already-present days cost no LLM call.
    - **Guaranteed daily site publish**: new `publish_site_daily` scheduler job runs `_publish_site_hook` 30 min after digest_time. The digest job publishes too, but it returns *before* publishing on low-news days (`digest_id==0`), so this guarantees the website is regenerated + pushed every day regardless.
- **Unattended-for-months hardening** (2026-06-23):
    - **Root cause of the stale website** (Telegram briefs kept arriving but only one `Publish site` commit ever landed): the publish path raised before reaching the push. `_publish_site_hook` did `return` on any render exception, and `run_publish_site`'s Step-1 digest/index discovery hit the DB *unwrapped* (unlike the countries/markets/about sections). One `database is locked` on the resource-starved VM aborted the whole render → no commit. The markets path survived only because it writes one file with no heavy DB work. Fixed: `_publish_site_hook` now logs and **falls through to push** whatever was written; `run_publish_site`'s Step 1 is wrapped and falls back to the on-disk archive list (no DB) so `index.html` still rebuilds.
    - **Self-healing git**: `_git_clear_interrupted_state()` runs at the top of every `_git_commit_and_push` and aborts any stale `rebase`/`merge` left by a process killed mid-retry. A half-finished rebase otherwise wedges every later git command with "you have unstaged changes" *permanently* — the single biggest threat to months-unattended operation, since no operator is around to `git rebase --abort`.
    - **Nightly maintenance job** (04:00 GST, `scheduler._maintenance_job` → `db.maintain_database()`): WAL `TRUNCATE` + `PRAGMA optimize`, plus a `journalctl`-visible warning when the DB volume passes 85% full. Between-batch checkpoints already exist; this guarantees WAL drift can't accumulate over months and surfaces disk pressure before it wedges the `claude` CLI.
    - **One-shot VM hardening** (`deploy/harden-vm.sh`, run once): caps journald at 200 MB, guarantees 2 GB swap, rotates `~/dalila/logs/*.log`, and installs the weekly `reshallow_repo.sh` cron. Documented in `deploy/README.md` §6b. The only thing it can't do is resize the boot disk (needs gcloud/console) — the nightly disk warning tells you when that's due.
- **Multi-source market discovery — broaden the live-mover pool** (2026-06-25):
    - **Root cause the pool was ~100% Manifold**: `discover_markets()` only ever called `_search_manifold`. `_search_polymarket` and `_search_metaforecast` existed but were **dead code** (never invoked), and there was **no Kalshi discovery path at all** (`_refresh_kalshi_prob` only refreshes a known ticker). `discover_markets` is now genuinely multi-source: Manifold + Polymarket + Metaforecast per-seed, plus a curated-series Kalshi sweep, merged **round-robin** so the pool keeps a cross-source mix instead of collapsing to the most-liquid source. Each source is independent (one blocked/down/empty never starves the others); network-fragile sources get a one-shot reachability pre-flight (`_reachable`) so a block costs one failure, not one-per-seed.
    - **Source realities** (probed live): **Polymarket** Gamma API is geo/network-blocked from the UAE dev host (TLS connection reset; github reachable) — wired in but degrades to nothing when unreachable; should contribute on a non-blocked VM. **Metaforecast** GraphQL `searchQuestions` is 500-ing upstream (Metaculus native API 403s) — wired in, degrades gracefully. **Kalshi** is reachable and liquid: but (a) it migrated fields to `*_dollars`(0–1)/`*_fp`, so the old `_refresh_kalshi_prob` reading `yes_bid`/`volume` always got `None` — fixed via `_kalshi_prob`/`_kalshi_open_interest`; (b) it has no full-text search and its open feed is swamped by elections + a World-Cup-2026 flood, so we pull by **curated series ticker** (`discovery.kalshi_series` in `prediction_markets.yaml`: KXWTI oil, KXFED, KXFEDDECISION, KXCPI, KXISRAELPM, …) and gate on **open interest** (`volume` is absent on the series endpoint). Kalshi's macro/oil series (Fed decision ~1M OI, WTI, CPI) are genuine daily movers and on-lens for OPEC/UAE.
    - Verified end-to-end: live DB pool went from 23 Manifold-only → a 13 Manifold + 12 Kalshi mix in `prediction_market_snapshots`; `tests/` 32 passed. Kalshi markets only surface as *movers* after a second poll establishes a baseline to diff against — the broad pool is tracked in the DB, the site surfaces movers via `live_only`.
    - **⚠️ Data-loss caveat — the live-only website filter was never committed.** Despite the 2026-06-18 shipping-log entry, `git log -S "live_only"`/`-S "tracked_count"` find nothing: `get_market_signals(live_only=…)`, `tracked_market_count`, `_max_abs_move`, `render_markets(tracked_count=…)` and the pipeline call sites lived **only as uncommitted working-tree changes**, which a mid-session `git reset --hard origin/main` + branch checkout (see reflog) wiped. They were reconstructed in this change. **Commit market work promptly** — the scheduler's auto-push only stages `markets.html`, so source changes to `prediction_markets.py`/`pipeline.py`/`html_digest.py` sit uncommitted and a stray reset will eat them again.

## Style / conventions

- Python 3.11+ (was 3.12; loosened so the codebase runs on Debian bookworm without external PPAs).
- Use the union syntax (`str | None`, not `Optional[str]`).
- Dataclasses for value objects (`models.py`). No Pydantic.
- Logging via `logging.getLogger(__name__)`, not `print()` (except in CLI handlers).
- DB access is always through helpers in `db.py` — don't write inline SQL in pipeline/bot/etc.
- All env config flows through `config.get_config()`. Don't read `os.getenv` elsewhere.
- Prompts live in `prompts/*.md` as version-controlled markdown, not inline strings.
- If you tune a numeric threshold, update the matching row in `METHODOLOGY.md` in the same commit.

## Classifier batch size (tuned 2026-05-12)

Default `batch_size = 25` was set after empirical measurement on Windows + Claude Code 2.1.126. Single-item calls ~33s/item (dominated by CLI spawn + auth overhead); size-25/30 batches drop to ~2.5s per item — ~13× speedup. Going beyond 30 hits diminishing returns because Haiku's output tokens scale linearly with batch size. Going below 10 leaves spawn overhead on the table.

DeepSeek path is output-token-bound (~28s/batch regardless of CLI spawn), so concurrent workers help; recommended `--workers 5`.

Benchmark script: `benchmark_batch.py`. Re-run if you switch hosts or upgrade Claude Code.

## Source blockers (audited 2026-05-12)

Some sources from the original spec are disabled for live ingest in `sources.yaml`:

- **WAM, MoFA UAE, ERC, UAE Aid Agency** — JS-rendered pages; static HTML has no article cards. Need a headless browser (Playwright) for live polling. The historical backfill via sitemap walker DOES work for WAM (the sitemap XML is static).
- **Reuters** — `feeds.reuters.com` retired ~2020; no free RSS exists. Reuters wire copy reaches Dalila via aggregator redistribution and via the GDELT pipeline (reuters.com is on the trusted-outlet allowlist).
- **Devex, The New Humanitarian** — anti-bot protection (DataDome / Cloudflare). Disabled for now.
- **Erth Zayed Philanthropies** — splash page only. EZP mentions reach Dalila via the entity watchlist instead.

When v0.2 adds Playwright, the fix is in `src/dalila/ingestors/scrape.py`: add a `renderer: playwright` option, branch on it in `fetch()`, and call Playwright's sync API.

## Common operations

```bash
dalila init                              # bootstrap DB (applies pending migrations)
dalila check                             # verify env (claude CLI, ACLED OAuth, etc.)
dalila verify-sources                    # probe every enabled source

# Live pipeline
dalila ingest                            # one ingest pass
dalila classify --limit 50               # classify pending items (Claude/CLI)
dalila doctrine --limit 20               # extract doctrine facts
dalila run-pipeline                      # full pass (ingest + classify + doctrine)
dalila digest                            # compose + print today's digest
dalila bot                               # bot + scheduler (production)
dalila set-name                          # push bot identity + commands to Telegram

# Historical backfill
dalila backfill --since 2026-01-01                          # all sources
dalila backfill --since 2026-01-01 --source gdelt_v2        # one source
dalila classify --backend deepseek --workers 5 --limit 20000
dalila backfill-digests --days 135                          # composes daily briefs
                                                            # for each past day
dalila backfill-markets --days 14                           # Manifold + Kalshi history
dalila backfill-markets --source manifold --max-markets 5   # one platform, capped

# Website
dalila publish-site                      # regenerate docs/*.html

# Tests
pytest -v
```

## Scheduler jobs (when `dalila bot` runs)

| Job | Cadence | Notes |
|---|---|---|
| ingest    | every `ingest_interval_minutes` (default 30m) | replace_existing |
| classify  | every 5m | pauses exactly until rate-limit window resets (parsed from CLI error) |
| doctrine  | every 15m | independent rate-limit back-off; cheap when queue is empty |
| digest    | daily at `digest_time` GST | also runs `classify(200)` first to flush backlog |
| maintenance | daily at 04:00 GST | WAL `TRUNCATE` + `PRAGMA optimize` + disk-usage warning (>85%) via `db.maintain_database()`; keeps the single SQLite file healthy unattended |

The CAST ingestor (and any future monthly forecast ingestors) is currently invoked via the normal `dalila ingest` path; the change-only rule makes it cheap to run on every cycle even though the upstream data only updates monthly. If that becomes too chatty, gate on observation date in the ingestor itself.

## Deployment paths

- **Local development**: `dalila bot` in a terminal. Dies when you close the window. Fine for iteration.
- **Windows production**: `scripts/install-service.ps1` installs an NSSM service. Needs Admin elevation, scoop-installed NSSM.
- **Cloud production**: `deploy/install-on-vm.sh` + `deploy/dalila@.service` on a free-tier Google Cloud e2-micro. Full walkthrough in `deploy/README.md`. Survives reboot/disconnect, $0/month.
- **Static site hosting**: `docs/` is generated by `dalila publish-site`. Serve via GitHub Pages (commit + push `docs/`) or via Caddy/nginx pointed at the VM directory. The methodology page is regenerated from `METHODOLOGY.md` on every publish-site call.

## What NOT to do

- Don't add interactive Claude features (web search, computer use, file ops) to the classifier / editor / doctrine prompts. All are one-shot, no-tool-use calls. `--max-turns 1` is set in `llm._run_claude` for this reason.
- Don't store secrets in `entities.yaml`, `sources.yaml`, `doctrine_topics.yaml`, or `trusted_outlets.yaml` — they're committed to git.
- Don't commit `.env`, `*.db`, `*.docx` (policy docs are gitignored).
- Don't reach for the Anthropic SDK on the live path.
- Don't add `/status` to the user-facing menu — it's a hidden operator command. Keep the handler but exclude from `USER_MENU_COMMANDS` and `HELP_TEXT`.
- Don't make `/digest` recompose by default. It returns the persisted daily digest; only `/digest fresh` triggers recomposition. This was a deliberate cost choice.
- Don't render arc endpoints / dots into a separate SVG layer from the country paths. They MUST share the same SVG element so they share the same `viewBox → pixel` transform. The current implementation uses a `<g class="arcs-layer">` inside the main map SVG.
- Don't make CAST or any future forecast ingestor emit items on the first run for a source. The baseline-establishment behaviour is part of the public methodology contract.
- Don't pass `next_run_time=None` when adding interval/cron jobs in `scheduler.attach_jobs` unless you explicitly intend to register them in a paused state. In APScheduler 3.x, this prevents them from ever executing. Omit it to let APScheduler schedule the first run automatically after the first interval has elapsed.
- **`docs/digests/*.html` files are immutable once written.** `run_publish_site` only writes today's digest from the DB; all past digest pages are left exactly as they are on disk. The archive list is built by scanning existing files, not by re-querying the DB. Never add logic that re-renders old digest pages — it produces massive git diffs for zero user-visible benefit and defeats the purpose of the skip-if-exists invariant. When committing after `publish-site`, only `docs/index.html`, `docs/archive.html`, and the other root-level pages (+ today's digest) should show up as changed.

## Glossary

- **Classifier / editor / doctrine / deep-dive**: the four routine LLM call sites, each with its own prompt file.
- **Pre-filter**: cheap pre-LLM keyword+entity match (`pipeline._prefilter_match`).
- **doctrine_relation**: the classifier's tag on a single item — one of `reinforcing | refining | evolving | contradicting | new | null`. Drives whether the item gets sent to the doctrine pass.
- **doctrine_facts**: structured rows representing tracked UAE positions on topics. One row per topic. Evolution log appended on every update.
- **Foresight**: brief section composed only from `RawItems` produced by a forecast/index ingestor that crossed a change threshold. Uses 🔴 (worsening), 🟢 (improving), 🆕 (newly tracked after baseline), 🟡 (categorical change).
- **forecast_snapshots**: per-`(source, country, metric)` table that holds the last observed value. The change-detection helper compares each new observation against this row.
- **CAPITALS (in html_digest.py)**: ISO-2 → [lon, lat] lookup of national capitals, used as the anchor point for co-mention arcs on the country map. ~170 entries; suffix-matched against country features by numeric id (`N3_TO_A2`).
- **SimHash threshold**: 12 bits Hamming distance on 64-bit title hashes — empirically catches cross-outlet reposts (~6-10 bits apart) without merging unrelated stories (~28-40 bits apart).
- **Trusted outlet**: a domain on `trusted_outlets.yaml`. GDELT items whose URL hostname does not match (suffix-wise) are dropped at ingestion.
- **IHPC** vs **IHC**: easy to confuse and used to be aliased together in entities.yaml. **IHPC** = International Humanitarian and Philanthropic Council (policy coordination). **IHC** = International Humanitarian City Dubai (logistics warehousing). Now separate entities.
