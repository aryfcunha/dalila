# Dalila

Dalila is an autonomous **daily news briefing tool** with advanced strategic foresight capabilities. It transforms global development and humanitarian data into actionable intelligence for UAE policy principals.

It replaces 30–60 minutes of fragmented morning newsletter reading with a single 800–1,500 word digest, delivered to your phone over Telegram every morning at 06:30 GST, with an accompanying public website that hosts the archive, an interactive country-mention map, and a published methodology.

Unlike traditional news aggregators, Dalila is powered by a **Strategic Foresight Engine** that uses Log-Odds Volatility Scoring to identify "mathematical surprises" across prediction markets (Polymarket, Metaculus, Manifold) and correlates them with real-time events from 2,000+ sources.

It's designed for a single user (originally built for a principal at the UAE Presidential Court's Office for Development Affairs) but the code is general — anyone working on a regional humanitarian/development beat can fork it, swap the entity watchlist for their context, and have a working brief in under an hour.

> **Status**: running daily in production on a free-tier Google Cloud VM. Spanning months of archived briefs, ~30 active news sources, 1 active forecast index (ACLED CAST), and **real-time prediction market monitoring** (Manifold, Polymarket, Metaculus). Now features a robust **DeepSeek API fallback** for 100% uptime when CLI tools are unavailable.

## What makes Dalila different

- **Daily Brief First.** The primary output is a human-readable, policy-ready morning brief. Everything else—the indices, the markets, the doctrine—exists to make that brief more insightful.
- **Strategic Foresight Capabilities.** Beyond news, Dalila tracks conflict-escalation and crisis-severity indices (ACLED CAST shipped; ACAPS INFORM, WFP HungerMap LIVE, GDACS in the pipeline). Each surfaces only **changes**, never states — Sudan being hungry every day produces zero items; Sudan getting hungrier produces one, tagged 🔴 (worsening) or 🟢 (improving) with the magnitude of the shift.
- **Market Surprise Intelligence.** Dalila monitors prediction markets for geopolitical and economic shifts. It uses **Log-Odds Shift** scoring to filter out noise and highlight only the most significant movements in the 24-hour and 7-day windows.
- **No per-call API spend on the day-to-day path.** Routine LLM calls go through the `claude` CLI (Claude Code), shelling out as a subprocess. If you have a Pro/Max plan, your daily briefings are covered by your existing subscription. A resilient **DeepSeek API fallback** ensures the pipeline never breaks if the CLI environment changes.
- **Doctrine tracker.** Beyond daily news, Dalila maintains a structured model of UAE foreign-aid doctrine — tracked positions on 35 topics with confidence scoring and an evolution log of every reinforcing/refining/contradicting statement.
- **Curated news intake.** Around 30 RSS feeds plus structured data feeds (GDELT, ACLED, IATI, OCHA FTS, IDMC) and an outlet allowlist on GDELT that drops articles from low-circulation regional papers.

## How it works (one paragraph)

A Python pipeline ingests RSS feeds and structured data sources every 30 minutes. A cheap keyword + entity prefilter drops ~80% of items before the classifier sees them. Surviving items go to **Claude Haiku 4.5** (via the `claude` CLI or DeepSeek API fallback) for categorisation, relevance scoring, and entity tagging. At 06:30 GST, the engine composes the day's digest from items above a relevance threshold and broadcasts it to Telegram. In parallel, forecast indices and **prediction market deltas** are compared against baselines. When a large shift is detected, the pipeline automatically escalates to a **5-minute ingest loop** for 4 hours to capture breaking news. A separate 15-minute doctrine pass extracts UAE-position updates from leadership-flagged items. Daily, the static website regenerates from the SQLite DB.

```
   ┌─── ingestors ────────┐         ┌─── classifier (LLM) ────────┐
   │ RSS feeds,           │  ───►   │ category, uae_relevance,    │
   │ GDELT, ACLED,        │         │ severity, country_focus,    │
   │ IATI, OCHA FTS,      │         │ entities, doctrine_relation │
   │ sitemap scraping     │         └────────────┬────────────────┘
   └──────────────────────┘                      │
                                                 ▼
   ┌─── foresight engine ────┐  ┌─── editor (LLM) ────────────────────┐
   │ Market Deltas (24h/7d)  │─►│ daily digest 06:30 GST              │
   │ Forecast Indices (CAST) │  │ + /more deep-dives                  │
   │ Log-Odds Volatility     │  │ + Foresight section (🔴/🟢/🆕/🟡)   │
   └─────────────────────────┘  └─────────────────┬───────────────────┘
                                                  │
                                                  ▼
   ┌─── doctrine tracker ───┐         ┌─── outputs ──────────────────┐
   │ position_summary,      │         │ Telegram broadcast           │
   │ evolution_log,         │         │ +                            │
   │ confidence; every 15m  │         │ static website:              │
   │ on flagged items       │         │   /index   /archive          │
   └────────────────────────┘         │   /countries  /markets       │
                                      └──────────────────────────────┘
```

## Quickstart (local)

Prerequisites: **Python 3.11+**, **Claude Code** installed or a **DeepSeek API Key**, and a **Telegram bot token**.

```bash
git clone https://github.com/aryfcunha/dalila.git
cd dalila
python -m venv .venv
source .venv/bin/activate
pip install -e .

cp .env.example .env                 # then edit: DEEPSEEK_API_KEY=... etc.
dalila init                          # bootstrap DB, apply migrations
dalila check                         # verify env
dalila bot                           # start the bot + scheduler
```

## Production: free Google Cloud deployment

Running `dalila bot` in a terminal only works while the terminal is open. For a real 24/7 deployment, the recommended path is the **Google Cloud always-free e2-micro tier**. Walkthrough lives at **[`deploy/README.md`](deploy/README.md)**.

## Repository layout

- `sources.yaml`: Source registry (RSS, GDELT, ACLED, etc.)
- `entities.yaml`: Entity watchlist (UAE focus)
- `prompts/`: LLM instructions for classification and editorial work
- `src/dalila/`: Core engine logic (Ingestors, Classifier, Editor, HTML Renderer)
- `docs/`: Static website served by GitHub Pages

## Contributing

Dalila is now **public and open-source**. PRs are welcome. Please ensure `pytest` passes and any prompt changes are documented in `METHODOLOGY.md`.

## License

MIT.

## Acknowledgements

Built on the [Claude Code](https://docs.claude.com/en/docs/agents-and-tools/claude-code) CLI by Anthropic. News and structured data provided by GDELT, ACLED, IATI, OCHA, and our curated list of 30+ humanitarian and development sources. Named after [دليلة](https://en.wikipedia.org/wiki/Delilah_(name)) — Arabic for *guide*.
