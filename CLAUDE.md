# CLAUDE.md — guidance for Claude Code working in this repo

This file orients an LLM coding agent (you) to the Dalila codebase. Read it before making changes.

## Concept

The full concept lives in `../dalila-concept-note.md` and `../dalila-sources.md`. Read both before architectural changes. The leadership pitch at `../dalila-leadership-pitch.pptx` is for context.

## Critical invariants

- **LLM access is via the `claude` CLI, NOT the Anthropic API SDK.** Do not add `anthropic` to dependencies. Do not write `client.messages.create(...)`. All Claude calls go through `src/dalila/llm.py`. The user is on a Pro/Max plan; per-token API spend defeats the architecture.
- **Free sources only in MVP.** No paid X API, no NewsAPI subscription, no Devex paid tier. If you're tempted to add a paid source, ask first.
- **Models**: Haiku 4.5 (`claude-haiku-4-5`) for both the classifier AND the editor in MVP. Originally specced Sonnet for the editor; pinned to Haiku-only to share one model/cache/rate-limit pool while iterating. Constants live in `llm.HAIKU` / `llm.SONNET`. To upgrade the editor back to Sonnet, edit the single `model=` argument in `editor.py::compose_digest`. Opus is not used.
- **The classifier system prompt is the instructions + entity watchlist concatenated.** Sending it identically on every call lets Claude Code's internal cache kick in. Don't interpolate timestamps, request IDs, or other per-call variables into the system prompt — that defeats caching.
- **Prefilter before classifying.** The classifier is the most expensive thing we do. `pipeline._prefilter_match` drops ~80% of items based on keyword + entity match. Do not bypass it. UAE state/entity sources are auto-passed (their volume is low enough that filtering doesn't help).

## File ownership

| Concern | File |
|---|---|
| Add a new source | `sources.yaml` |
| Add a new entity to track | `entities.yaml` |
| Change classifier behaviour | `prompts/classifier.md` |
| Change digest format | `prompts/editor.md` |
| Add a new CLI subcommand | `src/dalila/cli.py` |
| Add a new Telegram command | `src/dalila/bot.py` |
| Add a new ingestor kind | new file in `src/dalila/ingestors/` + dispatch in `ingestors/base.py` |
| Schema change | new `migrations/00X_*.sql` + apply in `db.init_db()` |

## Style / conventions

- Python 3.12. Use the new union syntax (`str | None`, not `Optional[str]`).
- Dataclasses for value objects (`models.py`). No Pydantic.
- Logging via `logging.getLogger(__name__)`, not `print()` (except in CLI handlers).
- DB access is always through helpers in `db.py` — don't write inline SQL in pipeline/bot/etc.
- All env config flows through `config.get_config()`. Don't read `os.getenv` elsewhere.
- Prompts live in `prompts/*.md` as version-controlled markdown, not inline strings.

## Classifier batch size (tuned 2026-05-12)

Default `batch_size = 25` was set after empirical measurement on this
Windows + Claude Code 2.1.126 setup. Single-item calls cost ~33s per item
(dominated by CLI process spawn + auth overhead); size-25/30 batches drop
to ~2.5s per item — roughly 13× speedup.

Going beyond 30 hits diminishing returns because Haiku's output tokens
scale linearly with batch size (~300 tokens per classification) and start
to dominate wall time. Going below 10 leaves spawn overhead on the table.

Benchmark script lives at `benchmark_batch.py`. Re-run if you switch hosts
(macOS / Linux will likely have lower spawn overhead and a different sweet
spot) or upgrade Claude Code.

## Known source blockers (audited 2026-05-12)

Eight sources from the original spec are disabled with reasons in `sources.yaml`:

- **WAM, MoFA UAE, ERC, UAE Aid Agency** — JS-rendered pages; static HTML has no
  article cards. Need a headless browser (Playwright) to pull. Tracked for v0.2.
- **Reuters** — `feeds.reuters.com` retired ~2020; no free RSS exists.
- **Devex, The New Humanitarian** — Anti-bot protection (DataDome/Cloudflare) returns
  HTML to non-browser requesters even on documented RSS paths.
- **Erth Zayed Philanthropies** — No URL yet; awaiting Ary's input.

Working sources: The National, Gulf News, NYT (World/Africa/Middle East), WaPo,
BBC World, Al Jazeera, ReliefWeb, GDELT 2.0, IDMC, ACLED (if creds provided).
That's 9 active + ACLED.

When v0.2 adds Playwright, the fix is in `src/dalila/ingestors/scrape.py`: add a
`renderer: playwright` option to source config, branch on it in `fetch()`, and
have the new code path call Playwright's sync API to load the page, wait for
hydration, then run BeautifulSoup over the rendered DOM.

## What NOT to do

- Don't add interactive Claude features (web search, computer use, file ops) to the classifier or editor. Both are one-shot, no-tool-use calls. `--max-turns 1` is set in `llm._run_claude` for this reason.
- Don't store secrets in `entities.yaml` or `sources.yaml` — they're committed to git.
- Don't commit `.env` or `*.db`.
- Don't reach for the Anthropic SDK. If you find yourself needing fine-grained features (prompt caching introspection, structured outputs schema enforcement, parallel API calls), surface the tradeoff to the user before pivoting — the CLI-only architecture was a deliberate cost choice.

## Common operations

```bash
dalila init                       # bootstrap DB
dalila check                      # verify env
dalila ingest                     # one ingest pass
dalila classify --limit 50        # classify pending items
dalila digest                     # compose + print today's digest
dalila bot                        # start bot + scheduler (production)
pytest -v                         # smoke tests
```
