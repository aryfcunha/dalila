# Dalila

Dalila (دليلة — Arabic for *guide*) is a personal AI agent that delivers a daily morning brief on the **global humanitarian, development, and philanthropy ecosystem**, with sharp focus on the **United Arab Emirates' role within it**. It replaces 30–60 minutes of fragmented morning newsletter reading with a single 800–1,500 word digest, delivered to your phone over Telegram every morning at 06:30 GST.

It's designed for a single user (originally built for a principal at the UAE Presidential Court's Office for Development Affairs) but the code is general — anyone working on a regional humanitarian/development beat can fork it, swap the entity watchlist for their context, and have a working brief in under an hour.

> **Status**: MVP. Stable enough to run as a daily service. ~24 unit tests, deployable to free-tier Google Cloud, designed to live within the cost envelope of a single Claude Pro/Max subscription.

## What makes Dalila different

- **No per-call API spend.** All LLM calls go through the `claude` CLI (Claude Code), shelling out as a subprocess. If you have a Pro/Max plan, classifier and editor calls are covered by your existing subscription — no Anthropic API key needed, no per-token billing to manage.
- **UAE-specific by default, retargetable.** The entity watchlist (`entities.yaml`) and prompts (`prompts/`) encode a particular regional lens. Swap them and Dalila becomes a brief for any other beat (your country, your sector, your portfolio).
- **Doctrine tracker.** Beyond daily news, Dalila maintains a structured model of UAE foreign-aid doctrine — tracked positions on 35 topics with confidence scoring and an evolution log of every reinforcing/refining/contradicting statement. See [`doctrine_topics.yaml`](doctrine_topics.yaml) for the canonical vocabulary.
- **Built free.** Ingestion is RSS + GDELT 2.0 + ACLED + scrape (no paid wire fees). LLM is Claude Code subscription. Hosting on Google Cloud free tier. Total monthly cost above an existing Claude plan: $0.

## How it works (one paragraph)

A Python pipeline ingests RSS / GDELT 2.0 / ACLED / scraped sources every 30 minutes. A cheap keyword + entity prefilter drops ~80% of items before the classifier sees them. Surviving items go to **Claude Haiku 4.5** (via the `claude` CLI) for categorisation, UAE-relevance scoring, and entity tagging. At 06:30 GST, **Haiku** also composes the day's digest from items above a relevance threshold and broadcasts it to every subscribed Telegram chat. A separate 15-minute doctrine pass extracts position updates from UAE-leadership-flagged items into the `doctrine_facts` table. Storage is SQLite. Scheduling is APScheduler. No Postgres, no vector DB, no message queue — by design.

```
   ┌─── ingestors ────┐         ┌─── classifier (Haiku) ──────┐
   │ RSS, GDELT, ACLED│  ───►   │ category, uae_relevance,    │
   │ IDMC, scrape     │         │ entities, doctrine_relation │
   └──────────────────┘         └────────────┬────────────────┘
                                             │
                ┌────────────────────────────┴───────────────┐
                ▼                                            ▼
        ┌─── editor (Haiku) ────┐               ┌─── doctrine (Haiku) ──┐
        │ daily digest (06:30)  │               │ position_summary,     │
        │ + /more deep-dives    │               │ evolution_log,        │
        └───────────┬───────────┘               │ confidence            │
                    ▼                           └───────────────────────┘
            Telegram broadcast
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

## Day-to-day commands

```bash
dalila init                          # bootstrap or apply pending migrations
dalila check                         # env / CLI / token sanity check
dalila status                        # operator snapshot (queue depth, top entities)
dalila verify-sources                # probe every enabled source — selector-rot canary
dalila ingest                        # one ingest pass across enabled sources
dalila classify --limit 50           # classify pending items (default batch=25)
dalila doctrine --limit 20           # extract doctrine facts from new UAE statements
dalila digest                        # compose + print today's digest (no Telegram)
dalila bot                           # production: bot + scheduler (long-running)
dalila set-name                      # push canonical bot name + descriptions to Telegram
pytest -v                            # smoke tests (no external deps)
```

## Telegram commands (user-facing)

| Command | What it does |
|---|---|
| `/start` | Subscribe to the daily digest |
| `/digest` | Show the latest daily digest (read-only). `/digest fresh` to recompose. |
| `/more <topic>` | Deep-dive synthesis on a topic over the last 30 days |
| `/doctrine` | List tracked UAE doctrine positions; `/doctrine <topic>` for one topic's evolution log |
| `/help` | Command list |
| `/stop` | Unsubscribe |
| *(hidden)* `/status` | Operator diagnostic — pipeline queue, classifier latency, etc. |

## Repository layout

```
sources.yaml          ← source registry (RSS, scrape, GDELT, ACLED)
entities.yaml         ← entity watchlist (UAE focus, fed to the classifier)
doctrine_topics.yaml  ← 35 canonical doctrine slugs (seeded vocabulary)
prompts/
  classifier.md       ← Haiku prompt — per-item categorisation
  editor.md           ← Haiku prompt — daily digest composition
  deepdive.md         ← Haiku prompt — /more synthesis
  doctrine.md         ← Haiku prompt — doctrine fact extraction
migrations/
  001_initial.sql       ← base schema
  002_dedup_simhash.sql ← title SimHash for near-duplicate detection
  003_doctrine.sql      ← doctrine_facts table + processing flag
src/dalila/
  cli.py              ← entry point (`dalila <cmd>`)
  config.py           ← env + YAML loading
  db.py               ← SQLite helpers
  llm.py              ← subprocess wrapper around `claude` CLI
  classifier.py       ← Haiku-based per-item classifier
  editor.py           ← daily digest + deep-dive composers
  doctrine.py         ← doctrine extraction pass
  pipeline.py         ← ingest → prefilter → classify → digest orchestrator
  bot.py              ← Telegram handlers + identity sync
  scheduler.py        ← APScheduler jobs
  simhash.py          ← 64-bit SimHash for near-duplicate clustering
  ingestors/
    rss.py, scrape.py, gdelt.py, acled.py
tests/
  test_smoke.py       ← 24 pytest smoke tests (DB, prefilter, SimHash, doctrine validation)
deploy/
  install-on-vm.sh    ← one-line bootstrap for a fresh Ubuntu/Debian VM
  dalila@.service     ← systemd template unit
  README.md           ← Google Cloud free-tier walkthrough
scripts/
  install-service.ps1 ← Windows NSSM service install (if hosting on Windows)
```

## Extending for your context

Dalila is structured so you should rarely touch the Python code:

- **Add a news source**: edit `sources.yaml` — give it an `id`, `kind` (rss/scrape/gdelt/acled), `url`, and `quality` (1–5).
- **Add an entity to track**: edit `entities.yaml` — name + aliases. The classifier sees the full file on every call.
- **Change classifier behaviour**: edit `prompts/classifier.md`. Reload by restarting `dalila bot`.
- **Change digest format**: edit `prompts/editor.md`.
- **Add a doctrine topic**: edit `doctrine_topics.yaml` — slug + description.
- **Add a Telegram command**: edit `src/dalila/bot.py` (register a `CommandHandler`), update `USER_MENU_COMMANDS` if you want it in the `/` autocomplete.
- **Schema change**: drop a new `migrations/00X_*.sql` — it runs automatically on the next `dalila init`.

## Design choices worth knowing

- **CLI > API.** Every LLM call is `subprocess.run(["claude", ...])`. This trades fine-grained features (structured outputs, prompt cache introspection) for cost: a single Pro/Max subscription covers all calls. See `src/dalila/llm.py`.
- **Haiku for everything.** Original spec used Haiku for the classifier and Sonnet for the editor. MVP uses Haiku for both so they share one model, one cache, one rate-limit pool. Flip the editor back to Sonnet by changing one line in `editor.py::compose_digest`.
- **Prefilter before classifying.** The classifier is the most expensive call. `pipeline._prefilter_match` drops ~80% of items by keyword + entity match before paying for an LLM call. UAE state/entity sources auto-pass.
- **Batch classifier calls.** Single-item classifier calls cost ~33s each on Windows (dominated by CLI process spawn). Batch=25 drops to ~2.5s per item — see `benchmark_batch.py`. Going beyond 30 hits diminishing returns.
- **Rate-limit-aware scheduler.** When Claude's CLI returns "you've hit your limit — resets 5:30pm (Asia/Dubai)", the scheduler parses the exact reset time and pauses until then. No flat back-off guessing.
- **SimHash for cross-outlet dedup.** Same story from Reuters + BBC clusters at ~9 bits Hamming distance. Threshold of 12 catches reposts without merging unrelated stories. See `simhash.py`.
- **No vector DB.** URL+title hash for exact dedup, 64-bit SimHash for fuzzy dedup. If you start seeing duplicate clusters, that's where to invest.

## Roadmap

Already shipped: classifier, editor, daily digest, Telegram bot, `/more` deep-dive, doctrine tracker with 35-topic seed vocabulary, near-duplicate dedup, rate-limit-aware scheduler, source-verification command, free-tier GCP deployment kit.

Not yet built (open issues / contributions welcome):

- **Playwright ingestor**. JS-rendered sources (WAM, MoFA UAE, ERC, UAE Aid Agency) currently can't be scraped — the static HTML has no article cards. A `renderer: playwright` flag in `sources.yaml` + a code path that loads pages headless would unblock these.
- **Vector-based dedup**. SimHash works at the title level; cluster-aware deduplication on body text would catch more reposts.
- **Financial-commitment tracker**. Pull AED/USD figures from items into a structured ledger (fund pledges, MoU values, multi-year commitments).
- **Breaking-news alerts**. Currently the digest is daily. The concept note specs a magnitude/velocity/relevance trigger for push alerts.
- **Sentiment trajectories**. Once we have 8–12 weeks of accumulated data, track tone of foreign coverage of UAE over time.
- **Multi-user with personal entity watchlists**. Right now `/start` subscribes you to the same digest as everyone else. Per-user customisation would let one bot serve multiple beats.

## Testing

```bash
pip install pytest
pytest -v
```

Tests are scaffold-level — they don't call out to Claude or Telegram. Coverage includes DB round-trips, prefilter logic, SimHash clustering, doctrine validation, and rate-limit parsing. For end-to-end, run `dalila ingest && dalila classify && dalila digest` against the live system.

## Contributing

PRs welcome. Before sending one:
- `pytest` passes
- If you add a new prompt, drop it in `prompts/` as a separate `.md` file rather than inlining a string
- If you add a schema change, write it as `migrations/00X_*.sql` rather than mutating `001_initial.sql`
- Keep dependencies minimal — the appeal of Dalila is that it fits in 1 GB RAM on free-tier compute

For larger changes, open an issue first to align on direction. The single-user / single-host architecture is deliberate; multi-tenant features need discussion before code.

## License

MIT.

## Acknowledgements

- Built on the [Claude Code](https://docs.claude.com/en/docs/agents-and-tools/claude-code) CLI by Anthropic.
- News ingestion uses [GDELT 2.0](https://www.gdeltproject.org/), [ReliefWeb](https://reliefweb.int/), [ACLED](https://acleddata.com/), and the public RSS feeds of NYT, WaPo, BBC, Al Jazeera, Gulf News, and The National.
- Doctrine seed vocabulary derived from public UAE foreign-policy discourse and the UAE Foreign Aid Policy 2026 framework.
- Named after [دليلة](https://en.wikipedia.org/wiki/Delilah_(name)) (Dalila / Delilah) — Arabic for *guide*.
