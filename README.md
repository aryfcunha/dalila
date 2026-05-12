# Dalila

Daily intelligence digest on the global humanitarian / development / philanthropy ecosystem, with sharp focus on the UAE's role. Delivered every morning at 06:30 GST via Telegram.

MVP — free sources only, single-builder, designed to run on a Pro/Max Claude Code subscription instead of paying for the Anthropic API.

## Architecture (one paragraph)

A Python pipeline ingests RSS, GDELT, ACLED, and scraped pages every 30 minutes; a cheap keyword + entity prefilter drops ~80% of items before they reach the classifier; surviving items go to **Haiku 4.5** (via the `claude` CLI) for categorisation, UAE-relevance scoring, and entity tagging; at 06:30 GST, **Haiku** also composes the digest from items above threshold (MVP runs both stages on one model to share cache/rate limits — flip the editor to Sonnet 4.6 by changing one line in `editor.py`). The Telegram bot broadcasts to every subscribed chat. SQLite for storage. APScheduler for cron.

## Setup

### Prerequisites

1. **Python 3.12+**
2. **Claude Code** installed and logged in. Verify with `claude --version`. Dalila shells out to this for every LLM call — Pro/Max plan covers it.
3. **A Telegram bot token.** Chat with [@BotFather](https://t.me/BotFather) → `/newbot` → save the token.

### Install

```bash
cd dalila
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate    # macOS / Linux
pip install -e .
```

### Configure

```bash
copy .env.example .env          # Windows
# cp .env.example .env         # macOS / Linux
```

Edit `.env` and fill in `TELEGRAM_BOT_TOKEN`. ACLED creds are optional — leave blank to skip ACLED ingestion.

### Initialise the DB

```bash
dalila init
dalila check
```

`dalila check` verifies the `claude` CLI is on PATH, the Telegram token is set, etc. Fix anything red before going further.

## Day-to-day commands

```bash
dalila ingest              # one ingest pass across all enabled sources
dalila classify --limit 50 # classify up to 50 pending items
dalila digest              # compose and print today's digest (no Telegram delivery)
dalila bot                 # start the bot with the scheduler — long-running
```

The first three are useful for development and testing. In production you run `dalila bot` once and it does everything on its own.

## What's in here

```
sources.yaml          ← source registry (RSS, scrape, GDELT, ACLED)
entities.yaml         ← UAE entity watchlist (fed to the classifier)
prompts/
  classifier.md       ← Haiku 4.5 prompt
  editor.md           ← Sonnet 4.6 prompt
migrations/
  001_initial.sql     ← SQLite schema
src/dalila/
  cli.py              ← entry point (`dalila <cmd>`)
  config.py           ← env + YAML loading
  db.py               ← SQLite helpers
  llm.py              ← subprocess wrapper around `claude` CLI
  classifier.py       ← Haiku-based per-item classifier
  editor.py           ← Sonnet-based daily digest composer
  pipeline.py         ← ingest → prefilter → classify → digest orchestrator
  bot.py              ← Telegram bot (start/stop/digest/link N commands)
  scheduler.py        ← APScheduler jobs
  ingestors/
    rss.py, scrape.py, gdelt.py, acled.py
tests/
  test_smoke.py       ← pytest smoke tests (no external deps)
```

## Sources in MVP

Per Ary's narrowed scope:

- **UAE state/wire**: WAM, MoFA
- **UAE local press**: The National, Gulf News
- **UAE ecosystem**: ERC, Khalifa Foundation, Erth Zayed Philanthropies, UAE Aid Agency (some disabled pending URL verification)
- **Global wires**: Reuters, NYT (World/Africa/Middle East), Washington Post, BBC World, Al Jazeera
- **Specialist**: Devex, ReliefWeb, The New Humanitarian
- **Real-time events**: GDELT 2.0, ACLED, IDMC

All free. Full registry in [sources.yaml](sources.yaml).

## Known limitations / next steps

- Scrape selectors are placeholders; each scrape source needs verification against the live site on first run.
- "Erth Zayed Philanthropies" source URL is TBD — set in sources.yaml when Ary provides it.
- No vector dedup (pgvector) — URL+title hash only. If you start seeing duplicate clusters in the digest, that's where to invest.
- No two-way `more on <topic>` deep-dive yet (Phase 2 feature in the concept note).
- No doctrine tracker yet — every UAE leadership item is classified, but cross-statement comparison is Phase 2.
- Cost guardrail is daily call count, not dollars, because the CLI doesn't expose token usage cleanly.

## Testing

```bash
pip install pytest
pytest -v
```

Tests are scaffold-level (DB, prefilter, JSON parsing). They don't call out to Claude or Telegram. For end-to-end, run `dalila ingest && dalila classify && dalila digest` against the live system.
