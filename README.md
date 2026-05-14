# Dalila

Dalila is an autonomous **strategic foresight agent** that transforms global development and humanitarian data into actionable intelligence for UAE policy principals.

Unlike traditional news aggregators, Dalila uses **Log-Odds Volatility Scoring** to identify tail-risk signals across prediction markets (Manifold, Polymarket, Metaculus) and correlates them with real-time events from 2,000+ sources.

It replaces 30–60 minutes of fragmented morning newsletter reading with a single 800–1,500 word digest, delivered to your phone over Telegram every morning at 06:30 GST, with an accompanying public website that hosts the archive, an interactive country-mention map, and a published methodology.

It's designed for a single user (originally built for a principal at the UAE Presidential Court's Office for Development Affairs) but the code is general — anyone working on a regional humanitarian/development beat can fork it, swap the entity watchlist for their context, and have a working brief in under an hour.

> **Status**: running daily in production on a free-tier Google Cloud VM, with a companion website at `aryfcunha.github.io/dalila`. Spanning months of archived briefs, ~30 active news sources, 1 active forecast index (CAST), and **real-time prediction market monitoring** (Kalshi + Manifold).

## What makes Dalila different

- **No per-call API spend on the day-to-day path.** Routine LLM calls go through the `claude` CLI (Claude Code), shelling out as a subprocess. If you have a Pro/Max plan, classifier, editor, deep-dive, and doctrine calls are covered by your existing subscription — no Anthropic API key needed, no per-token billing to manage. A separate DeepSeek backend is available **opt-in** for one-shot historical backfills (`--backend deepseek`) when you need to chew through tens of thousands of items without burning your subscription quota.
- **UAE-specific by default, retargetable.** The entity watchlist (`entities.yaml`) and prompts (`prompts/`) encode a particular regional lens. Swap them and Dalila becomes a brief for any other beat (your country, your sector, your portfolio).
- **Foresight section.** Beyond news, Dalila tracks conflict-escalation and crisis-severity indices (ACLED CAST shipped; ACAPS INFORM, WFP HungerMap LIVE, GDACS in the pipeline). Each surfaces only **changes**, never states — Sudan being hungry every day produces zero items; Sudan getting hungrier produces one, tagged 🔴 (worsening) or 🟢 (improving) with the magnitude of the shift.
- **Doctrine tracker.** Beyond daily news, Dalila maintains a structured model of UAE foreign-aid doctrine — tracked positions on 35 topics with confidence scoring and an evolution log of every reinforcing/refining/contradicting statement. See [`doctrine_topics.yaml`](doctrine_topics.yaml) for the canonical vocabulary.
- **Market Signals.** Dalila monitors prediction markets (Kalshi, Manifold) for geopolitical and economic shifts. It uses **dynamic discovery** to find markets relevant to the current news cycle and surfaces "Market Signals" in the daily brief.
- **Curated news intake.** Around 30 RSS feeds plus structured data feeds (GDELT, ACLED, IATI, OCHA FTS, IDMC) and an outlet allowlist on GDELT that drops articles from low-circulation regional papers. The allowlist sits in [`trusted_outlets.yaml`](trusted_outlets.yaml) and is one-line editable.
- **Built free.** Ingestion is RSS + GDELT 2.0 + ACLED + sitemap scraping (no paid wire fees). LLM is Claude Code subscription. Hosting on Google Cloud free tier. Total monthly cost above an existing Claude plan: $0.

## How it works (one paragraph)

A Python pipeline ingests RSS feeds and structured data sources every 30 minutes. A cheap keyword + entity prefilter drops ~80% of items before the classifier sees them. Surviving items go to **Claude Haiku 4.5** (via the `claude` CLI) for categorisation, UAE-relevance scoring, severity, country focus, and entity tagging. At 06:30 GST, **Haiku** also composes the day's digest from items above a relevance threshold and broadcasts it to every subscribed Telegram chat. In parallel, forecast indices (currently ACLED CAST) and **prediction market deltas** (Kalshi/Manifold) are compared against baselines. When a large shift is detected, the pipeline automatically escalates to a **5-minute ingest loop** for 4 hours to capture breaking news. A separate 15-minute doctrine pass extracts UAE-position updates from leadership-flagged items. Daily, the static website regenerates from the SQLite DB.

```
   ┌─── ingestors ────────┐         ┌─── classifier (Haiku) ──────┐
   │ RSS feeds,           │  ───►   │ category, uae_relevance,    │
   │ GDELT, ACLED,        │         │ severity, country_focus,    │
   │ IATI, OCHA FTS,      │         │ entities, doctrine_relation │
   │ sitemap scraping     │         └────────────┬────────────────┘
   └──────────────────────┘                      │
                                                 ▼
   ┌─── forecast scaffold ───┐  ┌─── editor (Haiku) ──────────────────┐
   │ ACLED CAST (monthly),   │─►│ daily digest 06:30 GST              │
   │ INFORM/GDACS/HungerMap  │  │ + /more deep-dives                  │
   │ (planned); change-only  │  │ + Foresight section (🔴/🟢/🆕/🟡)   │
   │ rule, baselines silent  │  └─────────────────┬───────────────────┘
   └─────────────────────────┘                    │
                                                  ▼
   ┌─── doctrine (Haiku) ───┐         ┌─── outputs ──────────────────┐
   │ position_summary,       │        │ Telegram broadcast           │
   │ evolution_log,          │        │ +                            │
   │ confidence; every 15m   │        │ static website:              │
   │ on flagged items        │        │   /index   /archive          │
   └─────────────────────────┘        │   /countries  (world map)    │
                                      │   /methodology  /about       │
                                      └──────────────────────────────┘
```

## Quickstart (local)

Prerequisites: **Python 3.11+**, **Claude Code** installed and logged in (`claude --version` should work), a **Telegram bot token** from [@BotFather](https://t.me/BotFather).

```bash
git clone https://github.com/<you>/dalila.git
cd dalila
python -m venv .venv
.venv\Scripts\activate              # Windows
# source .venv/bin/activate          # macOS / Linux
pip install -e .

cp .env.example .env                 # then edit: TELEGRAM_BOT_TOKEN=...
dalila init                          # bootstrap DB, apply migrations
dalila check                         # verify env (claude CLI, token, etc.)
dalila bot                           # start the bot + scheduler (long-running)
```

In Telegram, message your bot `/start`. You'll be subscribed and the next daily digest will land at 06:30 GST.

## Production: free Google Cloud deployment

Running `dalila bot` in a terminal only works while the terminal is open. For a real 24/7 deployment, the recommended path is the **Google Cloud always-free e2-micro tier**. Walkthrough lives at **[`deploy/README.md`](deploy/README.md)** — covers project creation, region constraints, the bootstrap script, systemd setup, and bandwidth math. End-to-end takes ~15 min and bills $0/month.

The static website is published to `docs/` and served by GitHub Pages or any static host (Caddy/nginx on the same VM also works).

## Day-to-day commands

```bash
# Setup / health
dalila init                          # bootstrap or apply pending migrations
dalila check                         # env / CLI / token / ACLED creds sanity check
dalila status                        # operator snapshot (queue depth, top entities)
dalila verify-sources                # probe every enabled source — selector-rot canary

# Live pipeline
dalila ingest                        # one ingest pass across enabled sources
dalila classify --limit 50           # classify pending items via Claude CLI (batch=25)
dalila doctrine --limit 20           # extract doctrine facts from new UAE statements
dalila digest                        # compose + print today's digest (no Telegram)

# Production
dalila bot                           # bot + scheduler (long-running)
dalila set-name                      # push canonical bot name + descriptions to Telegram

# Historical backfill (opt-in DeepSeek backend for cost-bounded bulk classify)
dalila backfill --since 2026-01-01           # walk sitemaps + GDELT/ACLED ranges
dalila classify --backend deepseek --workers 5 --limit 20000
dalila backfill-digests --days 135           # compose one brief per past day

# Website
dalila publish-site                  # regenerate static HTML into docs/

# Tests
pytest -v                            # smoke tests (no external deps)
```

See [`METHODOLOGY.md`](METHODOLOGY.md) for the public-facing description of how every step makes its judgments, and the calibrated thresholds behind each.

## Telegram commands (user-facing)

| Command | What it does |
|---|---|
| `/start` | Subscribe to the daily digest |
| `/digest` | Show the latest daily digest (read-only). `/digest fresh` to recompose. |
| `/more <topic>` | Deep-dive synthesis on a topic over the last 30 days |
| `/doctrine` | List tracked UAE doctrine positions |
| `/commitments` | Recent UAE financial commitments and pledges, structured |
| `/meetings` | Recent UAE bilateral meetings, structured |
| `/region [name]` | Items aggregated by region |
| `/country <name>` | Recent news mentioning a specific country |
| `/help` | Command list |
| `/stop` | Unsubscribe |
| *(hidden)* `/status` | Operator diagnostic — pipeline queue, classifier latency, etc. |

Daily digest arrives ~06:30 GST. Powered by Strategic Foresight Engine (Log-Odds Volatility).

## Web companion

`dalila publish-site` regenerates a self-contained static website into `docs/`:

| Page | What's there |
|---|---|
| `/` (Home) | The latest morning brief in full |
| `/archive` | Every past brief, oldest-on-load is the most recent 20 with a *Load more* control |
| `/countries` | Interactive world map (Natural Earth projection, d3-geo) coloured by mention count; click a country to see co-mention arcs to other countries, with a 7D / 30D / 90D / ALL window selector |
| `/methodology` | The calibration document — every numeric threshold, every editorial rule, every change-detection threshold, with rationale |
| `/about` | Project description, source list, subscribe link |

The site is regenerated by reading the live DB; no separate data store. Run `dalila publish-site` whenever you want it fresh — daily after the morning brief is a natural cadence.

## Repository layout

```
sources.yaml          ← source registry (RSS, scrape, GDELT, ACLED, IATI, FTS, …)
entities.yaml         ← entity watchlist (UAE focus, fed to the classifier)
doctrine_topics.yaml  ← 35 canonical doctrine slugs (seeded vocabulary)
trusted_outlets.yaml  ← GDELT outlet allowlist (~185 reputable publishers)
prediction_markets.yaml ← settings for dynamic market discovery
METHODOLOGY.md        ← public methodology — source for the website page
prompts/
  classifier.md       ← Haiku prompt — per-item categorisation
  editor.md           ← Haiku prompt — daily digest + Foresight composition
  deepdive.md         ← Haiku prompt — /more synthesis
  doctrine.md         ← Haiku prompt — doctrine fact extraction
migrations/
  001_initial.sql               ← base schema
  002_dedup_simhash.sql         ← title SimHash for near-duplicate detection
  003_doctrine.sql              ← doctrine_facts table + processing flag
  004_richer_classification.sql ← policy_sector, country_focus, capital_signals,
                                  graduation, financial_commitments,
                                  bilateral_meetings
  005_forecast_snapshots.sql    ← per (source, country, metric) baseline
                                  table for the foresight scaffold
006_prediction_markets.sql    ← snapshots and history for market deltas
src/dalila/
  cli.py              ← entry point (`dalila <cmd>`)
  config.py           ← env + YAML loading
  db.py               ← SQLite helpers
  llm.py              ← subprocess wrapper around `claude` CLI
  deepseek.py         ← OPT-IN: DeepSeek REST backend for backfill classify
  classifier.py       ← per-item classifier
  editor.py           ← daily digest + deep-dive composers
  doctrine.py         ← doctrine extraction pass
  pipeline.py         ← orchestrator: ingest → prefilter → classify → digest,
                        backfill, publish-site
  bot.py              ← Telegram handlers + identity sync
  scheduler.py        ← APScheduler jobs
  simhash.py          ← 64-bit SimHash for near-duplicate clustering
  html_digest.py      ← static-site renderer (Home, Archive, Countries,
                        Methodology, About, individual digest pages)
  ingestors/
    rss.py, scrape.py, gdelt.py, acled.py, iati.py, fts.py, idmc.py, gmail.py
    sitemap.py        ← historical backfill walker for publisher sitemaps
    forecast.py       ← shared change-detection scaffold for forecast indices
    cast.py           ← ACLED CAST (monthly conflict-escalation forecast)
    prediction_markets.py ← dynamic Manifold/Kalshi discovery + poll
    base.py           ← ingestor dispatch by `kind`
tests/
  test_smoke.py       ← pytest smoke tests
deploy/
  install-on-vm.sh    ← one-line bootstrap for a fresh Ubuntu/Debian VM
  dalila@.service     ← systemd template unit
  README.md           ← Google Cloud free-tier walkthrough
scripts/
  verify_capitals.py            ← audits capital-coordinate accuracy on the map
  prune_untrusted_outlets.py    ← retroactively drop GDELT items from
                                   non-allowlisted outlets
  install-service.ps1           ← Windows NSSM service install (Windows host)
```

## Extending for your context

Dalila is structured so you should rarely touch the Python code:

- **Add a news source**: edit `sources.yaml` — give it an `id`, `kind` (`rss` / `scrape` / `gdelt` / `acled` / `iati` / `fts` / `cast` / `gmail`), `url`, and `quality` (1–5).
- **Add a trusted outlet for GDELT intake**: append one line to `trusted_outlets.yaml`. Suffix-matching means a single bare domain covers all subdomains.
- **Add an entity to track**: edit `entities.yaml` — name + aliases. The classifier sees the full file on every call.
- **Change classifier behaviour**: edit `prompts/classifier.md`. Reload by restarting `dalila bot`.
- **Change digest format**: edit `prompts/editor.md`.
- **Add a doctrine topic**: edit `doctrine_topics.yaml` — slug + description.
- **Add a Telegram command**: edit `src/dalila/bot.py` (register a `CommandHandler`), update `USER_MENU_COMMANDS` if you want it in the `/` autocomplete.
- **Add a forecast index source** (INFORM, HungerMap, etc.): add an ingestor in `src/dalila/ingestors/`, register the `kind` in `ingestors/base.py`, declare its threshold in code with a `record_observation(...)` call, and add a row to `sources.yaml`. The shared `forecast.py` scaffold handles baselines + change-detection emoji.
- **Schema change**: drop a new `migrations/00X_*.sql` — it runs automatically on the next `dalila init`.

## Design choices worth knowing

- **CLI > API for the live path.** Every routine LLM call is `subprocess.run(["claude", ...])`. This trades fine-grained features (structured outputs, parallel API calls) for cost: a single Pro/Max subscription covers all calls. See `src/dalila/llm.py`. DeepSeek is allowed *only* on the backfill path, opt-in via `--backend deepseek`.
- **Haiku for everything.** Original spec used Haiku for the classifier and Sonnet for the editor. MVP uses Haiku for both so they share one model, one cache, one rate-limit pool. Flip the editor back to Sonnet by changing one line in `editor.py::compose_digest`.
- **Prefilter before classifying.** The classifier is the most expensive call. `pipeline._prefilter_match` drops ~80% of items by keyword + entity match before paying for an LLM call. UAE state/entity sources auto-pass.
- **Outlet allowlist on GDELT.** GDELT indexes the entire news web. Without filtering, a local regional paper's article would compete on equal footing with Reuters. `trusted_outlets.yaml` narrows GDELT intake to ~185 outlets with national circulation or specialist authority.
- **Foresight = changes only.** Every forecast/index ingestor records observations into a `forecast_snapshots` table keyed by `(source, country, metric)`. An item only reaches the brief when the new observation crosses a per-source threshold from the prior reading. The first run of any new source is silent baseline-establishment, no items emitted.
- **Batch classifier calls.** Single-item classifier calls cost ~33s each (dominated by CLI process spawn). Batch=25 drops to ~2.5s per item — see `benchmark_batch.py`. Going beyond 30 hits diminishing returns.
- **Rate-limit-aware scheduler.** When Claude's CLI returns "you've hit your limit — resets 5:30pm (Asia/Dubai)", the scheduler parses the exact reset time and pauses until then. No flat back-off guessing.
- **SimHash for cross-outlet dedup.** Same story from Reuters + BBC clusters at ~9 bits Hamming distance. Threshold of 12 catches reposts without merging unrelated stories. See `simhash.py`.
- **No vector DB.** URL+title hash for exact dedup, 64-bit SimHash for fuzzy dedup. If you start seeing duplicate clusters, that's where to invest.

## Roadmap

Already shipped: classifier, editor, daily digest, Telegram bot, `/more` deep-dive, doctrine tracker with 35-topic seed vocabulary, near-duplicate dedup, rate-limit-aware scheduler, source-verification command, free-tier GCP deployment kit, **historical backfill** (sitemap walkers for WAM/Gulf News/The National, GDELT and ACLED date ranges), **DeepSeek backend** for cost-bounded backfill classify, **forecast scaffold** with the change-only rule, **ACLED CAST** ingestor, **prediction markets** (dynamic discovery + Kalshi/Manifold integration), **Breaking-news alerts** (5m dynamic polling loop), **static website** (home, archive, country map, methodology, about), **GDELT outlet allowlist**, **policy-aligned classification** (policy_sector, country_focus, capital_signals, financial_commitments, bilateral_meetings).

Not yet built (open issues / contributions welcome):

- **Phase 2 of foresight indices**: ACAPS INFORM (crisis severity, monthly), GDACS (real-time disaster alerts), WFP HungerMap LIVE (daily food-insecurity counts). The shared `forecast.py` scaffold is ready; each is a ~100-line ingestor plus one row in `sources.yaml`.
- **Playwright ingestor**. JS-rendered sources (WAM, MoFA UAE, ERC, UAE Aid Agency, Erth Zayed Philanthropies) currently can't be live-ingested — the static HTML has no article cards. The historical-backfill sitemap walker works for WAM since the sitemap is static XML; live polling still needs a headless browser.
- **Vector-based dedup**. SimHash works at the title level; cluster-aware deduplication on body text would catch more reposts.
- **Sentiment trajectories**. Once enough weeks of accumulated data, track tone of foreign coverage of UAE over time.
- **Multi-user with personal entity watchlists**. Right now `/start` subscribes you to the same digest as everyone else. Per-user customisation would let one bot serve multiple beats.

## Testing

```bash
pip install pytest
pytest -v
```

Tests are scaffold-level — they don't call out to Claude or Telegram. Coverage includes DB round-trips, prefilter logic, SimHash clustering, doctrine validation, rate-limit parsing, and the forecast change-detection helper. For end-to-end, run `dalila ingest && dalila classify && dalila digest` against the live system. A separate `scripts/verify_capitals.py` audits the world map's capital-city coordinates against the rendered country polygons.

## Contributing

PRs welcome. Before sending one:
- `pytest` passes
- If you add a new prompt, drop it in `prompts/` as a separate `.md` file rather than inlining a string
- If you add a schema change, write it as `migrations/00X_*.sql` rather than mutating `001_initial.sql`
- If you tune a numeric threshold or change a model choice, update [`METHODOLOGY.md`](METHODOLOGY.md) in the same commit
- Keep dependencies minimal — the appeal of Dalila is that it fits in 1 GB RAM on free-tier compute

For larger changes, open an issue first to align on direction. The single-user / single-host architecture is deliberate; multi-tenant features need discussion before code.

## License

MIT.

## Acknowledgements

- Built on the [Claude Code](https://docs.claude.com/en/docs/agents-and-tools/claude-code) CLI by Anthropic.
- News ingestion uses public RSS feeds from The National, Gulf News, NYT, WaPo, BBC, Al Jazeera, ReliefWeb, UN News, OCHA, UNHCR, WFP, UNICEF, WHO, IOM, OHCHR, World Bank, IMF, AfDB, USAID, FCDO, BMZ, AFD, Sida, Norad, JICA, plus specialist outlets including Devex, Alliance Magazine, Climate Home News, ODI, CGD, SSIR, Philanthropy News Digest, Semafor, France 24, Deutsche Welle, SCMP, Foreign Policy, and Foreign Affairs.
- Structured-data ingestion uses [GDELT 2.0](https://www.gdeltproject.org/), [ACLED](https://acleddata.com/), the [IATI Standard Datastore](https://iatistandard.org/), [OCHA Financial Tracking Service](https://fts.unocha.org/), and [IDMC](https://www.internal-displacement.org/).
- Forecast indices: [ACLED CAST](https://acleddata.com/conflict-alert-system/), with ACAPS INFORM, WFP HungerMap LIVE, and GDACS in the pipeline.
- World map built with [d3-geo](https://d3js.org/d3-geo) and [world-atlas](https://github.com/topojson/world-atlas) (Natural Earth 50m).
- Doctrine seed vocabulary derived from public UAE foreign-policy discourse and the UAE Foreign Aid Policy 2026 framework.
- Named after [دليلة](https://en.wikipedia.org/wiki/Delilah_(name)) (Dalila / Delilah) — Arabic for *guide*.
